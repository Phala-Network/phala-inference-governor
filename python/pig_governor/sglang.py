"""Thin explicit SGLang adapter; loaded only by the small integration patch.

No class replacement, decorator patching, source rewriting, or cache hooks.
This initial integration is restricted to one non-overlap scheduler replica;
multi-rank decision broadcast needs its own verified adapter before enabling.
"""
import os
import time
from sglang.srt.runtime_context import (
    get_context,
    get_disagg,
    get_parallel,
    get_schedule,
)
from .core import Governor
from .identity import RESOLVED_RUNTIME_FIELDS, build_runtime_identity
from .profile import bootstrap_profile, build_profile_document, coverage
from .scheduler import MAX_WAITING_LIMIT, Progress, SchedulerGovernor


def _runtime_identity(runtime_overrides):
    resolved = get_context().resolved_server_args_dict()
    resolved.update(runtime_overrides)
    try:
        selected = {name: resolved[name] for name in RESOLVED_RUNTIME_FIELDS}
    except KeyError as error:
        raise ValueError(
            f"Resolved runtime identity is missing {error.args[0]}"
        ) from None
    return build_runtime_identity(selected)


def _policy_integer(name, default, *, minimum, maximum=2**32 - 1):
    raw = os.environ.get(name, str(default))
    if type(raw) is not str or not raw.isascii() or not raw.isdecimal():
        raise ValueError(f"{name} must be an integer")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} is outside the supported range")
    return value


def create(server_args, *, runtime_overrides=None, is_generation=None):
    if os.environ.get("PIG_GOVERNOR_ENABLE") != "1":
        return None
    if is_generation is not True:
        raise ValueError("Governor adapter requires a generation model")
    parallel = get_parallel()
    schedule = get_schedule()
    disagg = get_disagg()
    if (parallel.tp_size != 1 or parallel.pp_size != 1 or parallel.dp_size != 1
            or not schedule.disable_overlap_schedule
            or disagg.disaggregation_mode != "null"):
        raise ValueError("Governor adapter requires TP1/PP1/non-overlap/no disaggregation")
    overrides = dict(runtime_overrides or ())
    max_running_requests = overrides.get(
        "max_running_requests", schedule.max_running_requests
    )
    if max_running_requests is None:
        raise ValueError("Governor requires resolved native max_running_requests")
    overrides["max_running_requests"] = max_running_requests

    def identity_provider():
        return _runtime_identity(overrides)

    identity = identity_provider()
    reference = float(os.environ.get("PIG_TPS_REFERENCE", "50"))
    max_running = _policy_integer(
        "PIG_MAX_RUNNING", max_running_requests, minimum=1
    )
    if max_running > max_running_requests:
        raise ValueError("PIG_MAX_RUNNING exceeds native max_running_requests")
    max_waiting = _policy_integer(
        "PIG_MAX_WAITING", MAX_WAITING_LIMIT, minimum=0, maximum=MAX_WAITING_LIMIT
    )
    loaded_profile = bootstrap_profile(
        reference,
        identity,
        max_running_requests,
    )
    now = time.monotonic()
    core_options = {
        "max_running_requests": max_running_requests,
    }
    if loaded_profile is not None:
        core_options.update(
            profile_cells=loaded_profile.cells,
            profile_ttl_seconds=loaded_profile.ttl_seconds,
            now=now,
        )
    return SglangGovernor(
        Governor(reference, **core_options),
        max_running_requests=max_running_requests,
        max_running=max_running,
        max_waiting=max_waiting,
        runtime_identity=identity,
        identity_provider=identity_provider,
        loaded_profile=loaded_profile,
    )


class SglangGovernor(SchedulerGovernor):
    def __init__(self, core, *, max_running_requests=1, runtime_identity=None,
                 identity_provider=None, loaded_profile=None, max_running=None,
                 max_waiting=MAX_WAITING_LIMIT):
        super().__init__(
            core,
            max_running_requests=max_running_requests,
            max_running=max_running,
            max_waiting=max_waiting,
        )
        self.runtime_identity = runtime_identity
        self._identity_provider = identity_provider
        self.loaded_profile = loaded_profile
        self.surface_epoch_rotations = 0
        self.last_identity_change = None
        self.admission_attempts = 0
        self.admission_rejects = 0
        self.admission_reuses = 0
        self.admission_reject_reasons = {}
        self.last_admission = None

    def admit_request(self, req, now, *, is_retracted=False, waiting_count=None):
        with self._policy_lock:
            self.refresh_identity(now)
            reused = getattr(req, "governor_reservation", None) is not None
            result = super().admit_request(
                req,
                now,
                is_retracted=is_retracted,
                waiting_count=waiting_count,
            )
            self.admission_attempts += 1
            if reused:
                self.admission_reuses += 1
            if not result["allowed"]:
                self.admission_rejects += 1
                reason = result["reason_name"]
                self.admission_reject_reasons[reason] = (
                    self.admission_reject_reasons.get(reason, 0) + 1
                )
                policy = self.core.snapshot(now)
                result.update(
                    policy_epoch=policy["epoch"],
                    policy_revision=policy["revision"],
                    runtime_identity_sha256=(
                        None
                        if self.runtime_identity is None
                        else self.runtime_identity["sha256"]
                    ),
                )
            self.last_admission = result
            return result

    def admission_snapshot(self):
        with self._policy_lock:
            return {
                "attempts": self.admission_attempts,
                "rejects": self.admission_rejects,
                "reuses": self.admission_reuses,
                "reject_reasons": dict(self.admission_reject_reasons),
                "outstanding": self.outstanding,
                "active": self.active,
                "waiting_count": self.last_waiting_count,
                "projected_waiting": (
                    None
                    if self.last_admission is None
                    else self.last_admission["projected_waiting"]
                ),
                "max_waiting": self.max_waiting,
                "max_running": self.max_running,
                "native_max_running_requests": self.native_max_running_requests,
                "admitted_pressure_counts": list(self.admitted_pressure_counts),
                "active_pressure_counts": list(self.active_pressure_counts),
                "last": self.last_admission,
                "surface_epoch_rotations": self.surface_epoch_rotations,
                "runtime_identity_sha256": (
                    None if self.runtime_identity is None
                    else self.runtime_identity["sha256"]
                ),
                "last_identity_change": self.last_identity_change,
            }

    def refresh_identity(self, now):
        with self._policy_lock:
            if self._identity_provider is None:
                return False
            current = self._identity_provider()
            if current == self.runtime_identity:
                return False
            previous = self.runtime_identity
            self.core.rotate_surface_epoch(now, self.active)
            self.runtime_identity = current
            self.loaded_profile = None
            self._pending_evidence = {}
            self._surface_time = now
            self.last_admission = None
            self.surface_epoch_rotations += 1
            self.last_identity_change = {
                "at_monotonic": now,
                "previous_sha256": None if previous is None else previous["sha256"],
                "current_sha256": current["sha256"],
            }
            return True

    def policy_snapshot(self, now):
        with self._policy_lock:
            self.refresh_identity(now)
            snapshot = super().policy_snapshot(now)
            snapshot["runtime_identity"] = self.runtime_identity
            snapshot["profile_bootstrap"] = {
                "loaded": self.loaded_profile is not None,
                "sha256": (
                    None if self.loaded_profile is None else self.loaded_profile.sha256
                ),
                "runtime_identity_sha256": (
                    None
                    if self.loaded_profile is None
                    else self.loaded_profile.metadata["runtime_identity_sha256"]
                ),
                "coverage_count": (
                    0 if self.loaded_profile is None
                    else self.loaded_profile.coverage_count
                ),
                "coverage_total": self.native_max_running_requests * 4,
                "profile_max_total_tokens": (
                    None
                    if self.loaded_profile is None
                    else self.loaded_profile.metadata["profile_max_total_tokens"]
                ),
                "current_max_total_tokens": (
                    None
                    if self.runtime_identity is None
                    else self.runtime_identity["runtime"]["max_total_tokens"]
                ),
            }
            return snapshot

    def update_policy(self, now, *, expected_epoch, expected_revision, **changes):
        with self._policy_lock:
            self.refresh_identity(now)
            return super().update_policy(
                now,
                expected_epoch=expected_epoch,
                expected_revision=expected_revision,
                **changes,
            )

    def profile_snapshot(self, now):
        with self._policy_lock:
            self.refresh_identity(now)
            if self.runtime_identity is None:
                raise RuntimeError("Governor runtime identity is unavailable")
            cells = self.core.export_profile(now)
            document = build_profile_document(
                self.runtime_identity,
                self.native_max_running_requests,
                cells,
            )
            missing, covered = coverage(
                tuple(cells), self.native_max_running_requests
            )
            return {
                "epoch": self.core.epoch,
                "runtime_identity_sha256": self.runtime_identity["sha256"],
                "coverage": {
                    "count": covered,
                    "total": self.native_max_running_requests * 4,
                    "missing": [list(key) for key in missing],
                },
                "profile": document,
            }

    def _progress_for(self, req):
        state = getattr(req, "governor_progress", None)
        if state is None:
            reservation = getattr(req, "governor_reservation", None)
            if reservation is None:
                raise RuntimeError("Request has no Governor reservation")
            if reservation.owner is not self:
                raise RuntimeError("Request belongs to a different Governor owner")
            progress = Progress(pressure_class=reservation.pressure_class)
            state = req.governor_progress = (self, progress)
        owner, progress = state
        if owner is not self:
            raise RuntimeError("Request belongs to a different governor epoch")
        return progress

    def after_result(self, batch, now):
        self.refresh_identity(now)
        updates = []
        terminal_requests = []
        for req in batch.reqs:
            if (
                getattr(req, "governor_progress", None) is None
                and getattr(req, "governor_reservation", None) is None
            ):
                if (
                    getattr(req, "is_prefill_only", False)
                    or req.finished()
                    or getattr(req, "to_finish", None) is not None
                ):
                    continue
                raise RuntimeError("Request has no Governor reservation")
            progress = self._progress_for(req)
            if progress.terminal:
                continue
            output_tokens = len(req.output_ids_through_stop)
            terminal = bool(req.finished() or type(req.finished_reason).__name__ == "FINISH_ABORT")
            updates.append((progress, output_tokens, terminal, progress.pressure_class))
            if terminal:
                terminal_requests.append(req)
        if updates:
            self.commit_batch(updates, now)
        for req in terminal_requests:
            self.release_request(req)
        if batch.forward_mode.is_extend_without_speculative():
            self.prefill_completed(batch.launch_ts, now)

    def before_prefill(self, running, waiting, chunked, now):
        ready = [req.time_stats.wait_queue_entry_time for req in waiting]
        if chunked is not None:
            ready.append(chunked.time_stats.wait_queue_entry_time)
        age = max(0, now - min(ready)) if ready else 0
        runnable = bool(running and not running.is_empty() and not running.is_prefill_only)
        return self.prefer_decode(now, runnable_decode=runnable,
                                  pending_prefill=bool(waiting or chunked is not None),
                                  oldest_ready_age=age)


def on_abort_emitted(req):
    state = getattr(req, "governor_progress", None)
    if state is not None:
        owner, progress = state
        owner.terminated(progress, time.monotonic(), pressure_class=progress.pressure_class)
    reservation = getattr(req, "governor_reservation", None)
    if reservation is not None:
        if state is not None and reservation.owner is not state[0]:
            raise RuntimeError("Request has mismatched Governor owners")
        reservation.owner.release_request(req)
