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
    return SglangGovernor(Governor(float(os.environ.get("PIG_TPS_REFERENCE", "35"))))


class SglangGovernor(SchedulerGovernor):
    def after_result(self, batch, now):
        for req in batch.reqs:
            state = getattr(req, "governor_progress", None)
            if state is None:
                state = req.governor_progress = (self, Progress())
            owner, progress = state
            if owner is not self:
                raise RuntimeError("Request belongs to a different governor epoch")
            if type(req.finished_reason).__name__ == "FINISH_ABORT":
                self.terminated(progress, now)
            else:
                self.committed(progress, now, len(req.output_ids_through_stop), terminal=req.finished())
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
        state[0].terminated(state[1], time.monotonic())
