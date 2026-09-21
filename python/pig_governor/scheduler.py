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

    def __init__(self, core, *, max_running_requests=1):
        if (
            type(max_running_requests) is not int
            or max_running_requests <= 0
            or max_running_requests >= 2**32
        ):
            raise ValueError("Invalid native max_running_requests")
        self.core = core
        self.active = 0
        self.max_running_requests = max_running_requests
        self.outstanding = 0
        self.admitted_pressure_counts = [0, 0, 0, 0]
        self.active_pressure_counts = [0, 0, 0, 0]
        self._pending_evidence = {}
        self._surface_time = None

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

    def admit_request(self, req, now, *, is_retracted=False):
        """Atomically forecast and reserve one native request.

        The caller is the single admission owner.  It must call
        ``release_request`` if a later native enqueue step rolls back.
        """
        reservation = getattr(req, "governor_reservation", None)
        if reservation is not None:
            if reservation.released:
                raise RuntimeError("Released Governor reservation cannot be reused")
            return reservation.decision
        if is_retracted:
            raise RuntimeError("Retracted request has no Governor reservation")

        pressure_class = self._request_pressure_class(req)
        projected_concurrency = min(
            self.outstanding + 1, self.max_running_requests
        )
        projected_pressure = self._projected_pressure_class(pressure_class)
        decision = self.core.admit(
            now, projected_concurrency, projected_pressure
        )
        if not decision["allowed"]:
            return decision

        req.governor_reservation = Reservation(self, pressure_class, decision)
        self.outstanding += 1
        self.admitted_pressure_counts[pressure_class] += 1
        return decision

    def release_request(self, req):
        """Release a request-attached reservation exactly once."""
        reservation = getattr(req, "governor_reservation", None)
        if reservation is None or reservation.released:
            return False
        if self.outstanding <= 0 or self.admitted_pressure_counts[reservation.pressure_class] <= 0:
            raise RuntimeError("Governor reservation accounting underflow")
        self.outstanding -= 1
        self.admitted_pressure_counts[reservation.pressure_class] -= 1
        reservation.released = True
        return True

    def commit_batch(self, updates, now):
        """Commit one native forward batch before publishing state changes."""
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
        proposed = []

        for progress, output_tokens, terminal, pressure_class in updates:
            if not isinstance(progress, Progress):
                raise ValueError("Expected Governor Progress")
            if type(output_tokens) is not int or output_tokens < 0:
                raise ValueError("Invalid committed output length")
            if type(terminal) is not bool:
                raise ValueError("Terminal flag must be boolean")
            if type(pressure_class) is not int or not 0 <= pressure_class < 4:
                raise ValueError("Invalid context-pressure class")
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
        if active_before > 0 and total_sequence_seconds > 0:
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
