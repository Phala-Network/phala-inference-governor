"""Strict loading and export for versioned response-surface profiles."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Mapping

from .identity import compare_profile_compatibility, validate_identity


PROFILE_SCHEMA = "phala.pig.response-surface-profile.v1"
PROFILE_PATH_ENV = "PIG_TPS_PROFILE_PATH"
PROFILE_SHA256_ENV = "PIG_TPS_PROFILE_SHA256"
MAX_PROFILE_BYTES = 64 * 1024
PREDICTOR = {
    "abi_version": 4,
    "surface_schema_version": 1,
    "algorithm": "min-short-2s-long-60s-prior-reprobe-v2",
    "bucket_seconds": 0.5,
    "short_window_seconds": 2,
    "long_window_seconds": 60,
    "min_exposure_seconds": 0.1,
    "pressure_boundaries": [4096, 16384, 65536],
}
_TOP_FIELDS = frozenset({
    "schema", "created_at", "valid_until", "runtime_identity", "predictor",
    "max_running_requests", "cells",
})
_CELL_FIELDS = frozenset({
    "concurrency", "pressure_class", "evidence", "evidence_lower_tps",
    "approved_tps",
})
_FLAT_CELL_FIELDS = frozenset({
    "concurrency", "pressure_class", "long_tokens", "long_seconds",
    "short_tokens", "short_seconds", "evidence_lower_tps",
})
_EVIDENCE_FIELDS = frozenset({"long", "short"})
_SAMPLE_FIELDS = frozenset({"tokens", "seconds"})
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z")


@dataclass(frozen=True)
class LoadedProfile:
    cells: tuple[Mapping[str, object], ...]
    metadata: Mapping[str, object]
    missing: tuple[tuple[int, int], ...]
    coverage_count: int
    _document_bytes: bytes

    @property
    def document(self) -> dict[str, object]:
        """Return a fresh JSON-compatible deep copy on every access."""
        return json.loads(self._document_bytes.decode("ascii"))

    @property
    def ttl_seconds(self) -> float:
        return self.metadata["ttl_seconds"]

    @property
    def sha256(self) -> str | None:
        return self.metadata["sha256"]


Profile = LoadedProfile


def predictor_contract() -> dict[str, object]:
    """Return a fresh JSON-ready copy of the exact predictor contract."""
    return {**PREDICTOR, "pressure_boundaries": list(PREDICTOR["pressure_boundaries"])}


def canonical_profile_bytes(document: object) -> bytes:
    """Serialize a profile deterministically for hashing and transport."""
    if type(document) is not dict:
        raise ValueError("Profile document must be an object")
    try:
        return json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError) as error:
        raise ValueError("Profile document is not canonical JSON data") from error


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value):
    raise ValueError(f"Non-finite JSON number is forbidden: {value}")


def _fields(value: object, expected: frozenset[str], name: str) -> dict:
    if type(value) is not dict:
        raise ValueError(f"{name} must be an object")
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown or missing:
        field = sorted(unknown or missing)[0]
        raise ValueError(f"Invalid {name} field: {field}")
    return value


def _uint(value: object, name: str, *, minimum: int = 0, maximum: int = 2**64 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def _number(value: object, name: str, *, minimum: float = 0.0) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except OverflowError as error:
        raise ValueError(f"{name} must be a finite number") from error
    if not math.isfinite(result) or result < minimum:
        raise ValueError(f"{name} must be a finite number >= {minimum}")
    return result


def _timestamp(value: object, name: str) -> datetime:
    if type(value) is not str or not _UTC_RE.fullmatch(value):
        raise ValueError(f"{name} must be strict UTC RFC3339 ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{name} is not a valid timestamp") from error
    if parsed.tzinfo != timezone.utc:
        raise ValueError(f"{name} must use UTC")
    return parsed


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _same_json_types(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return set(left) == set(right) and all(
            _same_json_types(left[key], right[key]) for key in left
        )
    if type(left) is list:
        return len(left) == len(right) and all(
            _same_json_types(a, b) for a, b in zip(left, right)
        )
    return left == right


def _now(value: datetime | float | int | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if type(value) in (int, float):
        seconds = _number(value, "now")
        return datetime.fromtimestamp(seconds, timezone.utc)
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    raise ValueError("now must be an aware datetime or nonnegative timestamp")


def coverage(
    cells: tuple[Mapping[str, object], ...],
    max_running_requests: int,
) -> tuple[tuple[tuple[int, int], ...], int]:
    """Report reachable cells lacking exact or jointly-heavier evidence."""
    keys = {(cell["concurrency"], cell["pressure_class"]) for cell in cells}
    missing = tuple(
        (concurrency, pressure)
        for concurrency in range(1, max_running_requests + 1)
        for pressure in range(4)
        if not any(c >= concurrency and p >= pressure for c, p in keys)
    )
    return missing, max_running_requests * 4 - len(missing)


def validate_profile(
    document: object,
    *,
    max_running_requests: int,
    current_identity: object,
    now: datetime | float | int | None = None,
    source_sha256: str | None = None,
) -> LoadedProfile:
    """Validate a decoded profile and return immutable preload material."""
    profile = _fields(document, _TOP_FIELDS, "profile")
    if profile["schema"] != PROFILE_SCHEMA:
        raise ValueError("Unsupported response-surface profile schema")
    runtime_max = _uint(
        max_running_requests, "runtime max_running_requests", minimum=1,
        maximum=2**32 - 1,
    )
    profile_max = _uint(
        profile["max_running_requests"], "max_running_requests", minimum=1,
        maximum=2**32 - 1,
    )
    if profile_max != runtime_max:
        raise ValueError("Profile max_running_requests does not match the runtime")
    wall = _now(now)
    created = _timestamp(profile["created_at"], "created_at")
    valid_until = _timestamp(profile["valid_until"], "valid_until")
    if created > wall:
        raise ValueError("created_at cannot be later than the current wall time")
    if valid_until <= created:
        raise ValueError("valid_until must be later than created_at")
    if valid_until <= wall:
        raise ValueError("Profile has expired")
    if not _same_json_types(profile["predictor"], PREDICTOR):
        raise ValueError("Profile predictor does not match the runtime algorithm")
    identity = validate_identity(profile["runtime_identity"])
    current = validate_identity(current_identity)
    compare_profile_compatibility(identity, current)
    if identity["runtime"]["max_running_requests"] != runtime_max:
        raise ValueError("Runtime identity max_running_requests does not match the profile")
    raw_cells = profile["cells"]
    if type(raw_cells) is not list:
        raise ValueError("cells must be an array")
    if len(raw_cells) > profile_max * 4:
        raise ValueError("cells exceeds max_running_requests * 4")
    core_cells = []
    document_cells = []
    previous = None
    for index, raw_cell in enumerate(raw_cells):
        cell = _fields(raw_cell, _CELL_FIELDS, f"cells[{index}]")
        concurrency = _uint(
            cell["concurrency"], f"cells[{index}].concurrency", minimum=1,
            maximum=profile_max,
        )
        pressure = _uint(
            cell["pressure_class"], f"cells[{index}].pressure_class", maximum=3
        )
        key = (concurrency, pressure)
        if previous is not None and key <= previous:
            raise ValueError("cells must have unique keys in canonical sorted order")
        previous = key
        evidence = _fields(cell["evidence"], _EVIDENCE_FIELDS, f"cells[{index}].evidence")
        samples = {}
        for window in ("long", "short"):
            sample = _fields(
                evidence[window], _SAMPLE_FIELDS, f"cells[{index}].evidence.{window}"
            )
            tokens = _uint(sample["tokens"], f"cells[{index}].evidence.{window}.tokens")
            seconds = _number(sample["seconds"], f"cells[{index}].evidence.{window}.seconds")
            if seconds == 0 and tokens != 0:
                raise ValueError("Evidence with zero seconds cannot contain tokens")
            samples[window] = (tokens, seconds)
        long_tokens, long_seconds = samples["long"]
        short_tokens, short_seconds = samples["short"]
        if long_seconds < PREDICTOR["min_exposure_seconds"]:
            raise ValueError("Long evidence is below minimum exposure")
        if short_seconds > long_seconds:
            raise ValueError("Short evidence seconds cannot exceed long evidence seconds")
        if short_tokens > long_tokens:
            raise ValueError("Short evidence tokens cannot exceed long evidence tokens")
        long_tps = long_tokens / long_seconds
        lower = min(long_tps, short_tokens / short_seconds) if short_seconds else long_tps
        stated_lower = _number(
            cell["evidence_lower_tps"], f"cells[{index}].evidence_lower_tps"
        )
        if stated_lower != lower:
            raise ValueError("evidence_lower_tps does not match the predictor min rule")
        approved = _number(cell["approved_tps"], f"cells[{index}].approved_tps")
        if approved > lower:
            raise ValueError("approved_tps exceeds evidence_lower_tps")
        core_cells.append(MappingProxyType({
            "concurrency": concurrency,
            "pressure_class": pressure,
            "long_tokens": long_tokens,
            "long_seconds": long_seconds,
            "short_tokens": short_tokens,
            "short_seconds": short_seconds,
            "evidence_lower_tps": approved,
        }))
        document_cells.append({
            "concurrency": concurrency,
            "pressure_class": pressure,
            "evidence": {
                "long": {"tokens": long_tokens, "seconds": long_seconds},
                "short": {"tokens": short_tokens, "seconds": short_seconds},
            },
            "evidence_lower_tps": stated_lower,
            "approved_tps": approved,
        })
    cell_tuple = tuple(core_cells)
    missing, coverage_count = coverage(cell_tuple, profile_max)
    normalized_document = {
        "schema": PROFILE_SCHEMA,
        "created_at": profile["created_at"],
        "valid_until": profile["valid_until"],
        "runtime_identity": identity,
        "predictor": predictor_contract(),
        "max_running_requests": profile_max,
        "cells": document_cells,
    }
    document_bytes = canonical_profile_bytes(normalized_document)
    metadata = MappingProxyType({
        "schema": PROFILE_SCHEMA,
        "created_at": profile["created_at"],
        "valid_until": profile["valid_until"],
        "ttl_seconds": (valid_until - wall).total_seconds(),
        "sha256": source_sha256,
        "runtime_identity_sha256": identity["sha256"],
        "current_runtime_identity_sha256": current["sha256"],
        "profile_max_total_tokens": identity["runtime"]["max_total_tokens"],
        "current_max_total_tokens": current["runtime"]["max_total_tokens"],
        "max_running_requests": profile_max,
        "predictor": MappingProxyType({
            **PREDICTOR,
            "pressure_boundaries": tuple(PREDICTOR["pressure_boundaries"]),
        }),
        "coverage_count": coverage_count,
        "coverage_total": profile_max * 4,
        "coverage_complete": not missing,
        "missing": missing,
    })
    return LoadedProfile(cell_tuple, metadata, missing, coverage_count, document_bytes)


def load_profile(
    path: str | os.PathLike[str],
    expected_sha256: str | None,
    runtime_identity: object,
    max_running_requests: int,
    now_utc: datetime | float | int | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> LoadedProfile:
    """Read, authenticate, decode, and validate a profile file."""
    expected = expected_sha256
    if expected is None:
        source_environment = os.environ if environment is None else environment
        expected = source_environment.get(PROFILE_SHA256_ENV)
    if type(expected) is not str or not _DIGEST_RE.fullmatch(expected):
        raise ValueError(f"{PROFILE_SHA256_ENV} must be 64 lowercase hexadecimal characters")
    profile_path = Path(path)
    try:
        before = profile_path.lstat()
    except OSError as error:
        raise ValueError("Profile path is not a readable regular file") from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError("Profile path must be a regular non-symlink file")
    try:
        with profile_path.open("rb") as source:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError("Profile path must be a regular file")
            if not os.path.samestat(before, opened):
                raise ValueError("Profile path changed while it was being opened")
            after = profile_path.lstat()
            if stat.S_ISLNK(after.st_mode) or not os.path.samestat(after, opened):
                raise ValueError("Profile path changed while it was being opened")
            data = source.read(MAX_PROFILE_BYTES + 1)
    except OSError as error:
        raise ValueError("Profile path is not a readable regular file") from error
    if len(data) > MAX_PROFILE_BYTES:
        raise ValueError("Profile file exceeds 64 KiB")
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected:
        raise ValueError("Profile sha256 does not match the expected digest")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Profile must be UTF-8 JSON") from error
    try:
        document = json.loads(text, object_pairs_hook=_object, parse_constant=_constant)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"Invalid profile JSON: {error}") from error
    return validate_profile(
        document,
        max_running_requests=max_running_requests,
        current_identity=runtime_identity,
        now=now_utc,
        source_sha256=digest,
    )


def _bootstrap_metadata(profile: LoadedProfile, reference: float) -> LoadedProfile:
    metadata = MappingProxyType({
        **profile.metadata,
        "bootstrap_reference": reference,
        "bootstrap_mode": "production" if reference > 0 else "offline_sampling",
        "coverage_required": reference > 0,
        "coverage_complete": not profile.missing,
    })
    return LoadedProfile(
        profile.cells, metadata, profile.missing, profile.coverage_count,
        profile._document_bytes,
    )


def bootstrap_profile(
    reference: object,
    runtime_identity: object,
    max_running_requests: int,
    environ: Mapping[str, str] = os.environ,
    now_utc: datetime | float | int | None = None,
) -> LoadedProfile | None:
    """Load configured startup evidence, failing closed for production policy."""
    reference_value = _number(reference, "reference")
    path = environ.get(PROFILE_PATH_ENV)
    digest = environ.get(PROFILE_SHA256_ENV)
    if (path is None) != (digest is None):
        raise ValueError(f"{PROFILE_PATH_ENV} and {PROFILE_SHA256_ENV} must be configured together")
    if path is None:
        if reference_value == 0:
            return None
        raise ValueError(
            f"{PROFILE_PATH_ENV} and {PROFILE_SHA256_ENV} are required when reference is positive"
        )
    if type(path) is not str or not path or type(digest) is not str or not digest:
        raise ValueError("TPS profile path and sha256 must be non-empty strings")
    profile = load_profile(
        path, digest, runtime_identity, max_running_requests, now_utc
    )
    if reference_value > 0 and profile.missing:
        raise ValueError("TPS profile does not cover every reachable response-surface cell")
    return _bootstrap_metadata(profile, reference_value)


def build_profile_document(
    runtime_identity: object,
    max_running_requests: int,
    flat_cells: object,
    created_at: datetime | str | None = None,
    valid_for_seconds: object = 604800,
) -> dict[str, object]:
    """Convert native flat export cells into a validated profile document."""
    duration = _number(valid_for_seconds, "valid_for_seconds")
    if not 60 <= duration <= 2592000:
        raise ValueError("valid_for_seconds must be in 60..2592000")
    wall = _now(None)
    if created_at is None:
        created = wall
    elif type(created_at) is str:
        created = _timestamp(created_at, "created_at")
    elif isinstance(created_at, datetime) and created_at.tzinfo is not None:
        created = created_at.astimezone(timezone.utc)
    else:
        raise ValueError("created_at must be an aware datetime or UTC RFC3339 Z string")
    if created > wall:
        raise ValueError("created_at cannot be later than the current wall time")
    identity = validate_identity(runtime_identity)
    maximum = _uint(
        max_running_requests, "max_running_requests", minimum=1, maximum=2**32 - 1
    )
    if identity["runtime"]["max_running_requests"] != maximum:
        raise ValueError("Runtime identity max_running_requests does not match the profile")
    if type(flat_cells) not in (list, tuple):
        raise ValueError("flat_cells must be a list or tuple")
    nested = []
    for index, raw in enumerate(flat_cells):
        flat = _fields(raw, _FLAT_CELL_FIELDS, f"flat_cells[{index}]")
        nested.append({
            "concurrency": flat["concurrency"],
            "pressure_class": flat["pressure_class"],
            "evidence": {
                "long": {"tokens": flat["long_tokens"], "seconds": flat["long_seconds"]},
                "short": {"tokens": flat["short_tokens"], "seconds": flat["short_seconds"]},
            },
            "evidence_lower_tps": flat["evidence_lower_tps"],
            "approved_tps": flat["evidence_lower_tps"],
        })
    nested.sort(key=lambda item: (item["concurrency"], item["pressure_class"]))
    document = {
        "schema": PROFILE_SCHEMA,
        "created_at": _format_timestamp(created),
        "valid_until": _format_timestamp(created + timedelta(seconds=duration)),
        "runtime_identity": identity,
        "predictor": predictor_contract(),
        "max_running_requests": maximum,
        "cells": nested,
    }
    return validate_profile(
        document,
        max_running_requests=maximum,
        current_identity=identity,
        now=wall,
    ).document
