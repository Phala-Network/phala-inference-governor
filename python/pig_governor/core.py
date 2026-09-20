"""Small versioned C ABI binding. All time inputs use host monotonic seconds."""
import ctypes as C
import math
import os
import threading
import uuid


class RevisionConflict(ValueError):
    pass


class Snapshot(C.Structure):
    _fields_ = [
        ("abi_version", C.c_uint32), ("observed", C.c_uint32),
        ("revision", C.c_uint64), ("reference", C.c_double),
        ("tokens_60s", C.c_uint64), ("sequence_seconds_60s", C.c_double),
        ("tokens_2s", C.c_uint64), ("sequence_seconds_2s", C.c_double),
        ("active_sequences", C.c_uint64), ("last_prefill_end", C.c_double),
        ("last_prefill_wall", C.c_double), ("preference_until", C.c_double),
    ]


def _finite(value):
    if type(value) not in (float, int):
        raise ValueError("Expected finite nonnegative number")
    try:
        result = float(value)
    except OverflowError as error:
        raise ValueError("Expected finite nonnegative number") from error
    if not math.isfinite(result) or value < 0:
        raise ValueError("Expected finite nonnegative number")
    return result


def _integer(value):
    if type(value) is not int or not 0 <= value < 2**64:
        raise ValueError("Expected unsigned 64-bit integer")
    return value


class Governor:
    """Own one controller handle, never an SGLang Req, tensor, or KV page."""
    def __init__(self, reference, *, library=None):
        path = library or os.environ.get("PIG_GOVERNOR_LIBRARY")
        if not path or not os.path.isabs(path):
            raise ValueError("PIG_GOVERNOR_LIBRARY must name an absolute library path")
        self._lib = C.CDLL(path)
        self._lock = threading.RLock()
        self._handle = C.c_void_p()
        self.epoch = uuid.uuid4().hex
        definitions = {
            "abi_version": ([], C.c_uint32),
            "new": ([C.c_double, C.POINTER(C.c_void_p)], C.c_int32),
            "free": ([C.c_void_p], C.c_int32),
            "observe": ([C.c_void_p, C.c_double, C.c_uint64, C.c_uint64], C.c_int32),
            "prefill": ([C.c_void_p, C.c_double, C.c_double], C.c_int32),
            "update_reference": ([C.c_void_p, C.c_uint64, C.c_double], C.c_int32),
            "snapshot": ([C.c_void_p, C.c_double, C.POINTER(Snapshot)], C.c_int32),
            "choose": ([C.c_void_p, C.c_double, C.c_uint32, C.c_uint32, C.c_double, C.POINTER(C.c_uint32)], C.c_int32),
        }
        for name, (arguments, result) in definitions.items():
            function = getattr(self._lib, "pig_governor_" + name)
            function.argtypes, function.restype = arguments, result
        if self._lib.pig_governor_abi_version() != 1:
            raise RuntimeError("Unsupported Governor ABI")
        self._check(self._lib.pig_governor_new(_finite(reference), C.byref(self._handle)))

    @staticmethod
    def _check(status):
        if status == 2:
            raise RevisionConflict("Policy revision changed")
        if status == 1:
            raise ValueError("Invalid controller input or clock")
        if status != 0:
            raise RuntimeError("Controller unavailable")

    def _call(self, name, *args):
        with self._lock:
            if not self._handle:
                raise RuntimeError("Controller closed")
            self._check(getattr(self._lib, "pig_governor_" + name)(self._handle, *args))

    def close(self):
        with self._lock:
            if self._handle:
                handle, self._handle = self._handle, C.c_void_p()
                self._check(self._lib.pig_governor_free(handle))

    def observe(self, now, committed_decode_delta, active_after):
        self._call("observe", _finite(now), _integer(committed_decode_delta), _integer(active_after))

    def prefill(self, now, wall):
        self._call("prefill", _finite(now), _finite(wall))

    def choose_decode(self, now, *, runnable_decode, pending_prefill, oldest_age_s=0):
        if type(runnable_decode) is not bool or type(pending_prefill) is not bool:
            raise ValueError("Native runnable flags must be booleans")
        selected = C.c_uint32()
        self._call("choose", _finite(now), int(runnable_decode), int(pending_prefill), _finite(oldest_age_s), C.byref(selected))
        return bool(selected.value)

    def update_reference(self, expected_epoch, expected_revision, reference):
        if expected_epoch != self.epoch:
            raise RevisionConflict("Scheduler epoch changed")
        self._call("update_reference", _integer(expected_revision), _finite(reference))

    def snapshot(self, now):
        result = Snapshot()
        self._call("snapshot", _finite(now), C.byref(result))
        if result.abi_version != 1:
            raise RuntimeError("Incompatible snapshot ABI")
        seconds = result.sequence_seconds_60s
        return {
            "epoch": self.epoch, "revision": result.revision,
            "mutable": {"tps_reference": result.reference},
            "average_tps": result.tokens_60s / seconds if seconds > 0 else None,
            "decode_tokens": result.tokens_60s, "decode_sequence_seconds": seconds,
            "active_decode_sequences": result.active_sequences,
            "reference_semantics": "soft_target", "individual_tps_binding": False,
        }
