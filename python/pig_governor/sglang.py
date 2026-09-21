"""Thin explicit SGLang adapter; loaded only by the small integration patch.

No class replacement, decorator patching, source rewriting, or cache hooks.
This initial integration is restricted to one non-overlap scheduler replica;
multi-rank decision broadcast needs its own verified adapter before enabling.
"""
import os
import time
from sglang.srt.runtime_context import get_disagg, get_parallel, get_schedule
from .core import Governor
from .scheduler import Progress, SchedulerGovernor


def create(server_args):
    if os.environ.get("PIG_GOVERNOR_ENABLE") != "1":
        return None
    parallel = get_parallel()
    schedule = get_schedule()
    disagg = get_disagg()
    if (parallel.tp_size != 1 or parallel.pp_size != 1 or parallel.dp_size != 1
            or not schedule.disable_overlap_schedule
            or disagg.disaggregation_mode != "null"):
        raise ValueError("Governor adapter requires TP1/PP1/non-overlap/no disaggregation")
    max_running_requests = schedule.max_running_requests
    if max_running_requests is None:
        raise ValueError("Governor requires resolved native max_running_requests")
    return SglangGovernor(
        Governor(
            float(os.environ.get("PIG_TPS_REFERENCE", "50")),
            max_running_requests=max_running_requests,
        ),
        max_running_requests=max_running_requests,
    )


class SglangGovernor(SchedulerGovernor):
    def __init__(self, core, *, max_running_requests=1):
        super().__init__(core, max_running_requests=max_running_requests)
        self.admission_attempts = 0
        self.admission_rejects = 0
        self.admission_reuses = 0
        self.last_admission = None

    def admit_request(self, req, now, *, is_retracted=False):
        reused = getattr(req, "governor_reservation", None) is not None
        result = super().admit_request(req, now, is_retracted=is_retracted)
        self.admission_attempts += 1
        if reused:
            self.admission_reuses += 1
        if not result["allowed"]:
            self.admission_rejects += 1
        self.last_admission = result
        return result

    def admission_snapshot(self):
        return {
            "attempts": self.admission_attempts,
            "rejects": self.admission_rejects,
            "reuses": self.admission_reuses,
            "outstanding": self.outstanding,
            "active": self.active,
            "admitted_pressure_counts": list(self.admitted_pressure_counts),
            "active_pressure_counts": list(self.active_pressure_counts),
            "last": self.last_admission,
        }

    def _progress_for(self, req):
        state = getattr(req, "governor_progress", None)
        if state is None:
            reservation = getattr(req, "governor_reservation", None)
            if reservation is None:
                raise RuntimeError("Request has no Governor reservation")
            progress = Progress(pressure_class=reservation.pressure_class)
            state = req.governor_progress = (self, progress)
        owner, progress = state
        if owner is not self:
            raise RuntimeError("Request belongs to a different governor epoch")
        return progress

    def after_result(self, batch, now):
        updates = []
        terminal_requests = []
        for req in batch.reqs:
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
