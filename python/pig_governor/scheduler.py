"""Explicit scheduler interface. The caller owns native request lifetimes.

Keep Progress on the existing request. Invoke terminal on real native removal,
including abort/retraction cleanup paths; retraction alone is not terminal.
No request registry, cache lookup, tensor readback, or admission acknowledgement.
"""
from dataclasses import dataclass


@dataclass(slots=True)
class Progress:
    output_tokens: int = 0
    decoding: bool = False
    terminal: bool = False


class SchedulerGovernor:
    def __init__(self, core):
        self.core = core
        self.active = 0

    def committed(self, progress, now, output_tokens, *, terminal=False):
        if type(output_tokens) is not int or output_tokens < 0:
            raise ValueError("Invalid committed output length")
        if progress.terminal:
            return
        if output_tokens < progress.output_tokens:
            raise ValueError("Committed output cannot go backwards; use terminated for abort")
        delta = output_tokens - progress.output_tokens if progress.decoding else max(0, output_tokens - 1)
        entering = not progress.decoding and output_tokens > 0 and not terminal
        # A request completing in its first result has no measured Decode
        # interval. Do not manufacture an infinite-rate observation.
        if not progress.decoding and terminal:
            delta = 0
        leaving = progress.decoding and terminal
        active = self.active + int(entering) - int(leaving)
        self.core.observe(now, delta, active)
        self.active = active
        progress.output_tokens = output_tokens
        progress.decoding = (progress.decoding or entering) and not terminal
        progress.terminal = terminal

    def terminated(self, progress, now):
        # Abort can clear native output buffers; use already-accounted length.
        self.committed(progress, now, progress.output_tokens, terminal=True)

    def prefill_completed(self, launch_time, completion_time):
        self.core.prefill(completion_time, completion_time - launch_time)

    def prefer_decode(self, now, *, runnable_decode, pending_prefill, oldest_ready_age):
        return self.core.choose_decode(now, runnable_decode=runnable_decode,
                                       pending_prefill=pending_prefill,
                                       oldest_age_s=oldest_ready_age)
