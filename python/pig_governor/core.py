"""Small versioned C ABI binding. All time inputs use host monotonic seconds."""
from collections.abc import Mapping
import ctypes as C
import math
import os
import threading
import time
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


class Admission(C.Structure):
    _fields_ = [
        ("abi_version", C.c_uint32),
        ("allowed", C.c_uint32),
        ("reason", C.c_uint32),
        ("observed", C.c_uint32),
        ("reference", C.c_double),
        ("conservative_tps", C.c_double),
        ("projected_tps", C.c_double),
        ("projected_concurrency", C.c_uint32),
        ("pressure_class", C.c_uint32),
        ("evidence_concurrency", C.c_uint32),
        ("evidence_pressure_class", C.c_uint32),
        ("active_sequences", C.c_uint64),
    ]


class ProfileCellV1(C.Structure):
    _fields_ = [
        ("concurrency", C.c_uint32),
        ("pressure_class", C.c_uint32),
        ("long_tokens", C.c_uint64),
        ("long_seconds", C.c_double),
        ("short_tokens", C.c_uint64),
        ("short_seconds", C.c_double),
        ("approved_lower_tps", C.c_double),
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


def _concurrency(value):
    if type(value) is not int or not 1 <= value < 2**32:
        raise ValueError("Expected positive 32-bit concurrency")
    return value


def _max_running_requests(value):
    if type(value) is not int or not 1 <= value < 2**32:
        raise ValueError("Expected positive 32-bit native max running requests")
    return value


def _pressure_class(value):
    if type(value) is not int or not 0 <= value < 4:
        raise ValueError("Expected context-pressure class 0..3")
    return value


class Governor:
    """Own one controller handle, never an SGLang Req, tensor, or KV page."""
    def __init__(self, reference, *, max_running_requests, profile_cells=None,
                 profile_ttl_seconds=None, now=None, library=None):
        path = library or os.environ.get("PIG_GOVERNOR_LIBRARY")
        if not path or not os.path.isabs(path):
            raise ValueError("PIG_GOVERNOR_LIBRARY must name an absolute library path")
        self._lib = C.CDLL(path)
        self._lock = threading.RLock()
        self._handle = C.c_void_p()
        self._max_running_requests = _max_running_requests(max_running_requests)
        self._profile_cell_count = 0
        self._profile_expires_at = None
        self.epoch = uuid.uuid4().hex
        self._functions = {}
        try:
            abi_version = getattr(self._lib, "pig_governor_abi_version")
        except AttributeError:
            raise RuntimeError("Governor library does not expose an ABI version") from None
        abi_version.argtypes, abi_version.restype = [], C.c_uint32
        if abi_version() != 4:
            raise RuntimeError("Unsupported Governor ABI: expected version 4")
        definitions = {
            "new": ([C.c_double, C.c_uint32, C.POINTER(C.c_void_p)], C.c_int32),
            "new_with_profile": (
                [C.c_double, C.c_uint32, C.c_double, C.c_double,
                 C.POINTER(ProfileCellV1), C.c_uint32, C.POINTER(C.c_void_p)],
                C.c_int32,
            ),
            "free": ([C.c_void_p], C.c_int32),
            "observe": ([C.c_void_p, C.c_double, C.c_uint64, C.c_uint64], C.c_int32),
            "observe_surface": (
                [C.c_void_p, C.c_double, C.c_uint64, C.c_double,
                 C.c_uint32, C.c_uint32],
                C.c_int32,
            ),
            "observe_batch": (
                [C.c_void_p, C.c_double, C.c_uint64, C.c_double,
                 C.c_uint32, C.c_uint32, C.c_uint64],
                C.c_int32,
            ),
            "observe_replacement": (
                [C.c_void_p, C.c_double, C.c_uint64, C.c_double,
                 C.c_uint32, C.c_uint32, C.c_uint64],
                C.c_int32,
            ),
            "prefill": ([C.c_void_p, C.c_double, C.c_double], C.c_int32),
            "update_reference": ([C.c_void_p, C.c_uint64, C.c_double], C.c_int32),
            "start_surface_epoch": (
                [C.c_void_p, C.c_double, C.c_uint64], C.c_int32,
            ),
            "export_profile": (
                [C.c_void_p, C.c_double, C.POINTER(ProfileCellV1),
                 C.c_uint32, C.POINTER(C.c_uint32)],
                C.c_int32,
            ),
            "snapshot": ([C.c_void_p, C.c_double, C.POINTER(Snapshot)], C.c_int32),
            "admit": (
                [C.c_void_p, C.c_double, C.c_uint32, C.c_uint32,
                 C.POINTER(Admission)],
                C.c_int32,
            ),
            "choose": (
                [C.c_void_p, C.c_double, C.c_uint32, C.c_uint32, C.c_double,
                 C.POINTER(C.c_uint32)],
                C.c_int32,
            ),
        }
        for name, (arguments, result) in definitions.items():
            symbol = "pig_governor_" + name
            try:
                function = getattr(self._lib, symbol)
            except AttributeError:
                raise RuntimeError(
                    "Governor ABI v4 library is missing symbol: " + symbol
                ) from None
            function.argtypes, function.restype = arguments, result
            self._functions[name] = function
        reference = _finite(reference)
        self._reference = reference
        if (profile_cells is None) != (profile_ttl_seconds is None):
            raise ValueError("profile_cells and profile_ttl_seconds are required together")
        if profile_cells is None:
            self._check(self._functions["new"](
                reference, self._max_running_requests, C.byref(self._handle),
            ))
        else:
            if not isinstance(profile_cells, (list, tuple)):
                raise ValueError("profile_cells must be a list or tuple")
            if len(profile_cells) > self._max_running_requests * 4:
                raise ValueError("Too many profile cells")
            native_cells = (
                (ProfileCellV1 * len(profile_cells))(
                    *(self._profile_cell(cell) for cell in profile_cells)
                )
                if profile_cells else None
            )
            profile_now = time.monotonic() if now is None else _finite(now)
            ttl = _finite(profile_ttl_seconds)
            if ttl == 0:
                raise ValueError("profile_ttl_seconds must be positive")
            self._check(self._functions["new_with_profile"](
                reference, self._max_running_requests, profile_now, ttl,
                native_cells, len(profile_cells), C.byref(self._handle),
            ))
            self._profile_cell_count = len(profile_cells)
            self._profile_expires_at = profile_now + ttl

    def _profile_cell(self, cell):
        if not isinstance(cell, Mapping):
            raise ValueError("Profile cells must be mappings")
        try:
            lower = cell["evidence_lower_tps"]
            return ProfileCellV1(
                _concurrency(cell["concurrency"]),
                _pressure_class(cell["pressure_class"]),
                _integer(cell["long_tokens"]),
                _finite(cell["long_seconds"]),
                _integer(cell["short_tokens"]),
                _finite(cell["short_seconds"]),
                _finite(lower),
            )
        except KeyError as error:
            raise ValueError("Incomplete profile cell") from error

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
            self._check(self._functions[name](self._handle, *args))

    def close(self):
        with self._lock:
            if self._handle:
                handle, self._handle = self._handle, C.c_void_p()
                self._check(self._functions["free"](handle))

    def observe(self, now, committed_decode_delta, active_after):
        self._call("observe", _finite(now), _integer(committed_decode_delta), _integer(active_after))

    def observe_surface(self, now, committed_decode_delta, sequence_seconds,
                        concurrency, pressure_class):
        self._call(
            "observe_surface",
            _finite(now),
            _integer(committed_decode_delta),
            _finite(sequence_seconds),
            _concurrency(concurrency),
            _pressure_class(pressure_class),
        )

    def observe_batch(self, now, committed_decode_delta, sequence_seconds,
                      concurrency, pressure_class, active_after):
        self._call(
            "observe_batch",
            _finite(now),
            _integer(committed_decode_delta),
            _finite(sequence_seconds),
            _concurrency(concurrency),
            _pressure_class(pressure_class),
            _integer(active_after),
        )

    def observe_replacement(self, now, committed_decode_delta, sequence_seconds,
                            concurrency, pressure_class, active_after):
        """Atomically retire the whole prior active set and start a fresh run."""
        self._call(
            "observe_replacement", _finite(now), _integer(committed_decode_delta),
            _finite(sequence_seconds), _concurrency(concurrency),
            _pressure_class(pressure_class), _integer(active_after),
        )

    def prefill(self, now, wall):
        self._call("prefill", _finite(now), _finite(wall))

    def choose_decode(self, now, *, runnable_decode, pending_prefill, oldest_age_s=0):
        if type(runnable_decode) is not bool or type(pending_prefill) is not bool:
            raise ValueError("Native runnable flags must be booleans")
        selected = C.c_uint32()
        self._call("choose", _finite(now), int(runnable_decode), int(pending_prefill), _finite(oldest_age_s), C.byref(selected))
        return bool(selected.value)

    def update_reference(self, expected_epoch, expected_revision, reference):
        reference = _finite(reference)
        with self._lock:
            if expected_epoch != self.epoch:
                raise RevisionConflict("Scheduler epoch changed")
            self._call("update_reference", _integer(expected_revision), reference)
            self._reference = reference

    @property
    def reference(self):
        with self._lock:
            if not self._handle:
                raise RuntimeError("Controller closed")
            return self._reference

    def rotate_surface_epoch(self, now, active_after):
        now = _finite(now)
        active_after = _integer(active_after)
        with self._lock:
            new_epoch = uuid.uuid4().hex
            self._call("start_surface_epoch", now, active_after)
            self.epoch = new_epoch
            self._profile_cell_count = 0
            self._profile_expires_at = None

    def export_profile(self, now):
        now = _finite(now)
        capacity = self._max_running_requests * 4
        cells = (ProfileCellV1 * capacity)()
        count = C.c_uint32()
        self._call("export_profile", now, cells, capacity, C.byref(count))
        return [
            {
                "concurrency": cell.concurrency,
                "pressure_class": cell.pressure_class,
                "long_tokens": cell.long_tokens,
                "long_seconds": cell.long_seconds,
                "short_tokens": cell.short_tokens,
                "short_seconds": cell.short_seconds,
                "evidence_lower_tps": cell.approved_lower_tps,
            }
            for cell in cells[:count.value]
        ]

    def snapshot(self, now):
        now = _finite(now)
        result = Snapshot()
        with self._lock:
            self._call("snapshot", now, C.byref(result))
            if result.abi_version != 4:
                raise RuntimeError("Incompatible snapshot ABI")
            epoch = self.epoch
            profile_cell_count = (
                self._profile_cell_count
                if self._profile_expires_at is not None and now < self._profile_expires_at
                else 0
            )
            profile_expires_at = self._profile_expires_at
        seconds = result.sequence_seconds_60s
        return {
            "epoch": epoch, "revision": result.revision,
            "mutable": {"tps_reference": result.reference},
            "observed": bool(result.observed),
            "average_tps": result.tokens_60s / seconds if seconds > 0 else None,
            "average_tps_2s": (
                result.tokens_2s / result.sequence_seconds_2s
                if result.sequence_seconds_2s > 0
                else None
            ),
            "decode_tokens": result.tokens_60s, "decode_sequence_seconds": seconds,
            "active_decode_sequences": result.active_sequences,
            "profile": {
                "cell_count": profile_cell_count,
                "expires_at": profile_expires_at,
            },
            "reference_semantics": "soft_target", "individual_tps_binding": False,
        }

    def admit(self, now, projected_concurrency, pressure_class):
        result = Admission()
        with self._lock:
            if not self._handle:
                raise RuntimeError("Controller closed")
            self._check(self._functions["admit"](
                self._handle,
                _finite(now),
                _concurrency(projected_concurrency),
                _pressure_class(pressure_class),
                C.byref(result),
            ))
        if result.abi_version != 4:
            raise RuntimeError("Incompatible admission ABI")
        evidence_source = (
            "aggregate_live"
            if result.reason == 6
            else "response_surface"
            if result.evidence_concurrency != 0
            else "none"
        )
        return {
            "allowed": bool(result.allowed),
            "reason": result.reason,
            "observed": bool(result.observed),
            "reference": result.reference,
            "conservative_tps": result.conservative_tps,
            "projected_tps": result.projected_tps,
            "projected_concurrency": result.projected_concurrency,
            "pressure_class": result.pressure_class,
            "evidence_concurrency": result.evidence_concurrency,
            "evidence_pressure_class": result.evidence_pressure_class,
            "evidence_source": evidence_source,
            "active_decode_sequences": result.active_sequences,
        }
