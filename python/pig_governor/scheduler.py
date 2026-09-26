"""Explicit scheduler interface. The caller owns native request lifetimes.

Keep Progress on the existing request. Invoke terminal on real native removal,
including abort/retraction cleanup paths; retraction alone is not terminal.
No request registry, cache lookup, tensor readback, or admission acknowledgement.
"""
from dataclasses import dataclass
import threading


MAX_WAITING_LIMIT = 3
ADMISSION_REASON_WAITING_LIMIT = 5
ADMISSION_REASON_NAMES = {
    0: "fit",
    1: "reference_disabled",
    2: "tps_risk",
    3: "cold_prior",
    4: "unknown",
    ADMISSION_REASON_WAITING_LIMIT: "waiting_limit",
    6: "aggregate_tps_risk",
}


@dataclass(slots=True)
class Progress:
    output_tokens: int = 0
    decoding: bool = False
    terminal: bool = False
    pressure_class: int = 0
    last_time: float | None = None


@dataclass(slots=True)
class Reservation:
    """One Scheduler-owned admission, attached to the native request."""

    owner: object
    pressure_class: int
    decision: dict
    released: bool = False


class SchedulerGovernor:
    _CONTEXT_CLASS_UPPERS = (4_096, 16_384, 65_536)

    @staticmethod
    def _validate_limits(native_max_running, max_running, max_waiting):
        if (
            type(native_max_running) is not int
            or not 0 < native_max_running < 2**32
        ):
            raise ValueError("Invalid native max_running_requests")
        if type(max_running) is not int or not 0 < max_running <= native_max_running:
            raise ValueError("Invalid Governor max_running")
        if type(max_waiting) is not int or not 0 <= max_waiting <= MAX_WAITING_LIMIT:
            raise ValueError("Invalid Governor max_waiting")

    def __init__(self, core, *, max_running_requests=1, max_running=None,
                 max_waiting=MAX_WAITING_LIMIT):
        if max_running is None:
            max_running = max_running_requests
        self._validate_limits(max_running_requests, max_running, max_waiting)
        self.core = core
        self._policy_lock = threading.RLock()
        self.active = 0
        self.max_running_requests = max_running_requests
        self.max_running = max_running
        self.max_waiting = max_waiting
        self.last_waiting_count = None
        self.outstanding = 0
        self.admitted_pressure_counts = [0, 0, 0, 0]
        self.active_pressure_counts = [0, 0, 0, 0]
        self._pending_evidence = {}
        self._surface_time = None

    @property
    def native_max_running_requests(self):
        return self.max_running_requests

    @classmethod
    def _request_pressure_class(cls, req):
        try:
            input_tokens = len(req.origin_input_ids)
            output_tokens = req.sampling_params.max_new_tokens
        except (AttributeError, TypeError):
            raise ValueError("Request lacks a bounded token horizon") from None
        if (
            type(input_tokens) is not int
            or type(output_tokens) is not int
            or input_tokens < 0
            or output_tokens < 0
        ):
            raise ValueError("Invalid request token horizon")
        total = input_tokens + output_tokens
        if total >= 2**64:
            raise ValueError("Request token horizon overflow")
        for index, upper in enumerate(cls._CONTEXT_CLASS_UPPERS):
            if total <= upper:
                return index
        return len(cls._CONTEXT_CLASS_UPPERS)

    def _projected_pressure_class(self, candidate_class):
        for index in range(len(self.admitted_pressure_counts) - 1, candidate_class, -1):
            if self.admitted_pressure_counts[index]:
                return index
        return candidate_class

    def admit_request(self, req, now, *, is_retracted=False, waiting_count=None):
        """Atomically forecast and reserve one native request.

        The caller is the single admission owner.  It must call
        ``release_request`` if a later native enqueue step rolls back.
        """
        with self._policy_lock:
            reservation = getattr(req, "governor_reservation", None)
            if reservation is not None:
                if reservation.owner is not self:
                    raise RuntimeError("Request belongs to a different Governor owner")
                if reservation.released:
                    raise RuntimeError("Released Governor reservation cannot be reused")
                return reservation.decision
            if is_retracted:
                raise RuntimeError("Retracted request has no Governor reservation")

            outstanding_after = self.outstanding + 1
            logical_projected_waiting = max(
                0, outstanding_after - self.max_running
            )
            if waiting_count is None:
                # Unit-level and compatibility callers may not have access to
                # SGLang's separate ordinary/grammar queues. Keep their
                # historical logical-capacity behavior; the native scheduler
                # always supplies an exact waiting owner count.
                waiting_before = max(0, self.outstanding - self.max_running)
                projected_waiting = logical_projected_waiting
            else:
                if type(waiting_count) is not int or not 0 <= waiting_count < 2**32 - 1:
                    raise ValueError("Invalid waiting_count")
                waiting_before = waiting_count
                projected_waiting = max(
                    logical_projected_waiting, waiting_before + 1
                )

            pressure_class = self._request_pressure_class(req)
            # The TPS surface describes the physical SGLang runnable range. A
            # lower mutable max_running must never select a lighter TPS cell.
            projected_concurrency = min(
                outstanding_after, self.native_max_running_requests
            )
            projected_pressure = self._projected_pressure_class(pressure_class)
            decision = dict(
                self.core.admit(now, projected_concurrency, projected_pressure)
            )
            reason = decision.get("reason")
            if reason not in ADMISSION_REASON_NAMES:
                raise RuntimeError("Governor returned an unknown admission reason")
            decision.update({
                "reason_name": ADMISSION_REASON_NAMES[reason],
                "projected_waiting": projected_waiting,
                "max_waiting": self.max_waiting,
                "max_running": self.max_running,
                "native_max_running_requests": self.native_max_running_requests,
            })
            self.last_waiting_count = waiting_before
            # Keep TPS-first provenance. The waiting limit is evaluated only
            # after the physical projected cell is fit.
            if decision["allowed"] and projected_waiting > self.max_waiting:
                decision["allowed"] = False
                decision["reason"] = ADMISSION_REASON_WAITING_LIMIT
                decision["reason_name"] = ADMISSION_REASON_NAMES[
                    ADMISSION_REASON_WAITING_LIMIT
                ]
            if not decision["allowed"]:
                return decision

            req.governor_reservation = Reservation(self, pressure_class, decision)
            self.outstanding = outstanding_after
            self.admitted_pressure_counts[pressure_class] += 1
            return decision

    def release_request(self, req):
        """Release a request-attached reservation exactly once."""
        with self._policy_lock:
            reservation = getattr(req, "governor_reservation", None)
            if reservation is None:
                return False
            if reservation.owner is not self:
                raise RuntimeError("Request belongs to a different Governor owner")
            if reservation.released:
                return False
            if self.outstanding <= 0 or self.admitted_pressure_counts[reservation.pressure_class] <= 0:
                raise RuntimeError("Governor reservation accounting underflow")
            self.outstanding -= 1
            self.admitted_pressure_counts[reservation.pressure_class] -= 1
            reservation.released = True
            return True

    def update_policy(self, now, *, expected_epoch, expected_revision, **changes):
        """Atomically update mutable policy under the Scheduler owner lock."""
        allowed = {"tps_reference", "max_waiting", "max_running"}
        if not changes or set(changes) - allowed:
            raise ValueError("Invalid Governor policy fields")
        with self._policy_lock:
            reference = changes.get(
                "tps_reference", self.core.reference
            )
            max_waiting = changes.get("max_waiting", self.max_waiting)
            max_running = changes.get("max_running", self.max_running)
            self._validate_limits(
                self.native_max_running_requests, max_running, max_waiting
            )
            # The core owns the epoch/revision CAS. Validate every Python field
            # before this sole mutating call; the assignments below cannot fail.
            self.core.update_reference(
                expected_epoch, expected_revision, reference
            )
            self.max_running = max_running
            self.max_waiting = max_waiting

    def policy_snapshot(self, now):
        with self._policy_lock:
            snapshot = self.core.snapshot(now)
            snapshot["mutable"] = {
                "tps_reference": snapshot["mutable"]["tps_reference"],
                "max_waiting": self.max_waiting,
                "max_running": self.max_running,
            }
            snapshot["native_max_running_requests"] = (
                self.native_max_running_requests
            )
            return snapshot

    def commit_batch(self, updates, now):
        """Commit one native forward batch before publishing state changes."""
        with self._policy_lock:
            self._commit_batch_locked(updates, now)

    def _commit_batch_locked(self, updates, now):
        """Apply a batch while the Scheduler policy lock is held."""
        if not isinstance(updates, (list, tuple)):
            raise ValueError("Expected a batch update sequence")
        active_before = self.active
        pressure_before = list(self.active_pressure_counts)
        active_pressure_before = max(
            (index for index, count in enumerate(pressure_before) if count),
            default=0,
        )
        entering = [0, 0, 0, 0]
        leaving = [0, 0, 0, 0]
        total_delta = 0
        entering_delta = 0
        proposed = []
        seen_progress_ids = set()

        for progress, output_tokens, terminal, pressure_class in updates:
            if not isinstance(progress, Progress):
                raise ValueError("Expected Governor Progress")
            if id(progress) in seen_progress_ids:
                raise ValueError("Duplicate Governor Progress in batch")
            seen_progress_ids.add(id(progress))
            if type(output_tokens) is not int or output_tokens < 0:
                raise ValueError("Invalid committed output length")
            if type(terminal) is not bool:
                raise ValueError("Terminal flag must be boolean")
            if type(pressure_class) is not int or not 0 <= pressure_class < 4:
                raise ValueError("Invalid context-pressure class")
            if pressure_class != progress.pressure_class:
                raise ValueError("Governor Progress pressure class changed")
            if progress.terminal:
                continue
            if output_tokens < progress.output_tokens:
                raise ValueError("Committed output cannot go backwards; use terminated for abort")

            delta = 0
            entering_request = False
            if progress.decoding:
                delta = output_tokens - progress.output_tokens
                if progress.last_time is None:
                    raise RuntimeError("Decoding progress lacks an observation clock")
                if now < progress.last_time:
                    raise ValueError("Observation clock went backwards")
            else:
                delta = max(0, output_tokens - 1)
                entering_request = output_tokens > 0 and not terminal
                if entering_request:
                    entering[pressure_class] += 1
                    entering_delta += delta
                if terminal:
                    delta = 0
            if progress.decoding and terminal:
                leaving[pressure_class] += 1

            total_delta += delta
            new_decoding = (progress.decoding or entering_request) and not terminal
            new_last_time = now if new_decoding else None
            proposed.append((
                progress, output_tokens, new_decoding, terminal, new_last_time
            ))

        active_after = active_before + sum(entering) - sum(leaving)
        if active_after < 0:
            raise RuntimeError("Active Decode accounting underflow")
        pressure_after = list(pressure_before)
        for index in range(4):
            pressure_after[index] += entering[index] - leaving[index]
            if pressure_after[index] < 0:
                raise RuntimeError("Active pressure accounting underflow")

        if self._surface_time is None:
            elapsed = 0.0
        else:
            elapsed = now - self._surface_time
            if elapsed < 0:
                raise ValueError("Observation clock went backwards")
        total_sequence_seconds = elapsed * active_before

        pending_evidence = {
            key: value for key, value in self._pending_evidence.items()
            if now - value[1] <= 60.0
        }
        # A run ends when all old requests retire, even if entrants keep the
        # active count positive. New entrants have no elapsed exposure yet.
        full_replacement = active_before > 0 and sum(leaving) == active_before
        if active_before == 0 or full_replacement:
            prior_pending = pending_evidence
            pending_evidence = {}
        else:
            prior_pending = pending_evidence
        if full_replacement:
            if entering_delta >= 2**64:
                raise ValueError("Pending Decode token count overflow")
            if entering_delta:
                active_pressure_after = max(
                    (index for index, count in enumerate(pressure_after) if count),
                    default=0,
                )
                pending_evidence[(active_after, active_pressure_after)] = (entering_delta, now)
            retired_delta = 0
            if total_sequence_seconds > 0:
                pending_tokens, _ = prior_pending.get(
                    (active_before, active_pressure_before), (0, now)
                )
                retired_delta = total_delta - entering_delta + pending_tokens
            self.core.observe_replacement(
                now, retired_delta, total_sequence_seconds,
                active_before, active_pressure_before, active_after,
            )
        elif active_before > 0 and total_sequence_seconds > 0:
            cell = (active_before, active_pressure_before)
            pending_tokens, _ = pending_evidence.pop(cell, (0, now))
            self.core.observe_batch(
                now, total_delta + pending_tokens, total_sequence_seconds,
                active_before, active_pressure_before, active_after
            )
        else:
            if total_delta > 0:
                if active_before > 0:
                    cell = (active_before, active_pressure_before)
                elif active_after > 0:
                    active_pressure_after = max(
                        (index for index, count in enumerate(pressure_after) if count),
                        default=0,
                    )
                    cell = (active_after, active_pressure_after)
                else:
                    cell = None
                if cell is not None:
                    prior_tokens, _ = pending_evidence.get(cell, (0, now))
                    pending_tokens = prior_tokens + total_delta
                    if pending_tokens >= 2**64:
                        raise ValueError("Pending Decode token count overflow")
                    pending_evidence[cell] = (pending_tokens, now)
            self.core.observe(now, 0, active_after)
        if active_after == 0:
            pending_evidence.clear()
        for progress, output_tokens, new_decoding, terminal, new_last_time in proposed:
            progress.output_tokens = output_tokens
            progress.decoding = new_decoding
            progress.terminal = terminal
            progress.last_time = new_last_time
        self.active = active_after
        self.active_pressure_counts = pressure_after
        self._pending_evidence = pending_evidence
        self._surface_time = now

    def committed(self, progress, now, output_tokens, *, terminal=False,
                  pressure_class=0):
        """Compatibility single-request wrapper around batch commit."""
        self.commit_batch(
            [(progress, output_tokens, terminal, pressure_class)], now
        )

    def terminated(self, progress, now, *, pressure_class=0):
        # Abort can clear native output buffers; use already-accounted length.
        self.committed(
            progress, now, progress.output_tokens, terminal=True,
            pressure_class=pressure_class,
        )

    def prefill_completed(self, launch_time, completion_time):
        self.core.prefill(completion_time, completion_time - launch_time)

    def prefer_decode(self, now, *, runnable_decode, pending_prefill, oldest_ready_age):
        return self.core.choose_decode(now, runnable_decode=runnable_decode,
                                       pending_prefill=pending_prefill,
                                       oldest_age_s=oldest_ready_age)
