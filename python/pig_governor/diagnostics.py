"""Opt-in bounded admission receipts; caller holds the scheduler policy lock.

Only fixed scalar fields are copied. Never serialize request objects or arbitrary
decision metadata. The per-process HMAC key is not exported or persisted.
"""
from collections import deque
import hashlib
import hmac
import math
import os


_NUMBERS = (
    "reason", "original_reason", "reference", "conservative_tps",
    "projected_tps", "projected_concurrency", "pressure_class",
    "evidence_concurrency", "evidence_pressure_class", "active_decode_sequences",
    "projected_waiting", "max_waiting", "max_running",
    "native_max_running_requests",
)
_SOURCES = frozenset(("none", "aggregate_live", "response_surface",
                      "online_exploration", "online_recovery", "internal_health"))
_BLOCKERS = frozenset(("disabled", "active", "capacity", "waiting", "cooldown",
                       "outstanding", "projection", "lower_unobserved",
                       "lower_risk", "lower_headroom", "busy", "health",
                       "unobserved", "recent_evidence", "not_applicable"))


def _integer(env, suffix, default, maximum):
    name = "PIG_ADMISSION_DIAGNOSTICS_" + suffix
    value = env.get(name, str(default))
    if (type(value) is not str or len(value) > 8 or not value.isascii()
            or not value.isdecimal() or not 1 <= int(value) <= maximum):
        raise ValueError(name + " is outside the supported integer range")
    return int(value)


class AdmissionDiagnostics:
    @classmethod
    def from_environment(cls, env=None):
        env = os.environ if env is None else env
        enabled = env.get("PIG_ADMISSION_DIAGNOSTICS", "0")
        if enabled not in ("0", "1"):
            raise ValueError("PIG_ADMISSION_DIAGNOSTICS must be 0 or 1")
        if enabled == "0":
            return None
        return cls(
            capacity=_integer(env, "CAPACITY", 128, 512),
            max_events=_integer(env, "MAX_EVENTS", 4096, 65536),
            duration_seconds=_integer(env, "SECONDS", 1200, 3600),
        )

    def __init__(self, *, capacity=128, max_events=4096, duration_seconds=1200):
        for value, maximum in ((capacity, 512), (max_events, 65536),
                               (duration_seconds, 3600)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("Invalid admission diagnostics bound")
        self.capacity = capacity
        self.max_events = max_events
        self.duration_seconds = duration_seconds
        self._events = deque(maxlen=capacity)
        self._key = os.urandom(32)
        self._start = None
        self._sequence = 0
        self._overwritten = 0
        self._stopped = None

    def record(self, req, decision, now, *, epoch, outstanding, active,
               waiting_count, reused, health_check):
        if self._stopped is not None:
            return
        if self._start is None:
            self._start = now
        if now - self._start >= self.duration_seconds:
            self._stopped = "duration"
            return
        if self._sequence >= self.max_events:
            self._stopped = "event_limit"
            return
        self._sequence += 1
        rid = getattr(req, "rid", None)
        # A long/unusual RID is uncorrelatable, not a reason to copy its value.
        request_key = None
        if type(rid) is str and len(rid) <= 256:
            request_key = hmac.new(self._key, rid.encode("utf-8", "replace"),
                                   hashlib.sha256).hexdigest()
        event = {
            "sequence": self._sequence, "at_monotonic": now,
            "request_key": request_key,
            "epoch": epoch if (type(epoch) is str and len(epoch) == 32
                                 and all(c in "0123456789abcdef" for c in epoch)) else None,
            "outstanding_after": outstanding, "active": active,
            "waiting_count": waiting_count, "reused": reused,
            "health_check": health_check,
        }
        for name in _NUMBERS:
            value = decision.get(name)
            event[name] = (value if type(value) in (int, float)
                           and math.isfinite(value) and abs(value) <= 2**64 else None)
        for name in ("allowed", "observed", "recovery_probe"):
            value = decision.get(name)
            event[name] = value if type(value) is bool else None
        for name in ("evidence_source", "original_evidence_source"):
            value = decision.get(name)
            event[name] = value if type(value) is str and value in _SOURCES else None
        # Only policy enum members can cross the receipt boundary.
        value = decision.get("exploration_blocker")
        event["exploration_blocker"] = value if type(value) is str and value in _BLOCKERS else None
        if len(self._events) == self.capacity:
            self._overwritten += 1
        self._events.append(event)
        if self._sequence == self.max_events:
            self._stopped = "event_limit"

    def snapshot(self):
        return {
            "enabled": True, "capacity": self.capacity,
            "max_events": self.max_events, "duration_seconds": self.duration_seconds,
            "recorded": self._sequence, "overwritten": self._overwritten,
            "stopped": self._stopped,
            "events": [dict(event) for event in self._events],
        }
