"""Canonical, versioned runtime identities for offline profile binding."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
import re
from typing import Mapping


IDENTITY_SCHEMA = "phala.pig.runtime-identity.v1"
COMMIT_ENV_FIELDS = ("PIG_ENGINE_COMMIT", "PIG_GOVERNOR_COMMIT")
STRING_ENV_FIELDS = ("PIG_MODEL_ARTIFACT_ID", "PIG_RUNTIME_HARDWARE_ID")
JSON_SAFE_INTEGER = 2**53 - 1

_BOOL_FIELDS = frozenset({
    "disable_cuda_graph", "disable_overlap_schedule", "disable_radix_cache",
    "enable_dp_attention", "enable_torch_compile",
})
_STRING_FIELDS = frozenset({
    "attention_backend", "disaggregation_mode", "dtype", "kv_cache_dtype",
    "load_format", "model_config_parser", "model_impl", "sampling_backend",
    "schedule_policy",
})
_OPTIONAL_STRING_FIELDS = frozenset({
    "cuda_graph_backend_decode", "cuda_graph_backend_prefill",
    "decode_attention_backend", "prefill_attention_backend", "quantization",
    "speculative_algorithm", "speculative_draft_attention_backend",
    "speculative_draft_kv_cache_dtype", "weight_version",
})
_POSITIVE_INTEGER_FIELDS = frozenset({
    "context_length", "dp_size", "max_prefill_tokens", "max_running_requests",
    "max_total_tokens", "page_size", "pp_size", "tp_size",
})
_NONNEGATIVE_INTEGER_FIELDS = frozenset({
    "chunked_prefill_size", "pp_max_micro_batch_size", "speculative_eagle_topk",
    "speculative_num_draft_tokens", "speculative_num_steps", "torch_compile_max_bs",
})
_OPTIONAL_INTEGER_FIELDS = frozenset({
    "pp_max_micro_batch_size", "speculative_eagle_topk",
    "speculative_num_draft_tokens", "speculative_num_steps", "torch_compile_max_bs",
})
_FRACTION_FIELDS = frozenset({
    "mem_fraction_static", "speculative_accept_threshold_acc",
    "speculative_accept_threshold_single",
})
_OPTIONAL_FRACTION_FIELDS = frozenset({
    "speculative_accept_threshold_acc", "speculative_accept_threshold_single",
})
RESOLVED_RUNTIME_FIELDS = frozenset().union(
    _BOOL_FIELDS, _STRING_FIELDS, _OPTIONAL_STRING_FIELDS,
    _POSITIVE_INTEGER_FIELDS, _NONNEGATIVE_INTEGER_FIELDS, _FRACTION_FIELDS,
)

_IDENTITY_FIELDS = frozenset({"schema", "environment", "runtime", "sha256"})
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_MODEL_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_HARDWARE_RE = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class IdentityDifference:
    field: str
    profile: object
    current: object


def canonical_json(value: object) -> bytes:
    """Return the single UTF-8 representation used by identity digests."""
    return json.dumps(
        value, allow_nan=False, ensure_ascii=True, separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _required_environment(environment: Mapping[str, str]) -> dict[str, str]:
    result = {}
    for name in COMMIT_ENV_FIELDS + STRING_ENV_FIELDS:
        value = environment.get(name)
        if type(value) is not str or not value:
            raise ValueError(f"{name} must be set to a non-empty string")
        if name in COMMIT_ENV_FIELDS and not _COMMIT_RE.fullmatch(value):
            raise ValueError(f"{name} must be 40 lowercase hexadecimal characters")
        if name == "PIG_MODEL_ARTIFACT_ID" and not _MODEL_RE.fullmatch(value):
            raise ValueError(f"{name} must be sha256: followed by 64 lowercase hex characters")
        if name == "PIG_RUNTIME_HARDWARE_ID" and not _HARDWARE_RE.fullmatch(value):
            raise ValueError(f"{name} must be a non-empty lowercase slug")
        result[name] = value
    return result


def _runtime_value(name: str, value: object) -> object:
    field = f"runtime.{name}"
    if name in _BOOL_FIELDS:
        if type(value) is not bool:
            raise ValueError(f"{field} must be a boolean")
        return value
    if name in _STRING_FIELDS or name in _OPTIONAL_STRING_FIELDS:
        if value is None and name in _OPTIONAL_STRING_FIELDS:
            return None
        if type(value) is not str or not value:
            raise ValueError(f"{field} must be a non-empty string")
        return value
    if name in _POSITIVE_INTEGER_FIELDS or name in _NONNEGATIVE_INTEGER_FIELDS:
        if value is None and name in _OPTIONAL_INTEGER_FIELDS:
            return None
        minimum = 1 if name in _POSITIVE_INTEGER_FIELDS else 0
        if type(value) is not int or not minimum <= value <= JSON_SAFE_INTEGER:
            raise ValueError(f"{field} must be an integer in {minimum}..{JSON_SAFE_INTEGER}")
        return value
    if name in _FRACTION_FIELDS:
        if value is None and name in _OPTIONAL_FRACTION_FIELDS:
            return None
        if type(value) not in (int, float):
            interval = "(0,1]" if name == "mem_fraction_static" else "0..1"
            raise ValueError(f"{field} must be a finite number in {interval}")
        result = float(value)
        valid = 0 <= result <= 1
        if name == "mem_fraction_static":
            valid = 0 < result <= 1
        if not math.isfinite(result) or not valid:
            interval = "(0,1]" if name == "mem_fraction_static" else "0..1"
            raise ValueError(f"{field} must be a finite number in {interval}")
        return result
    raise ValueError(f"Unknown resolved runtime field: {name}")


def _runtime(runtime: object) -> dict[str, object]:
    if not isinstance(runtime, Mapping):
        raise ValueError("resolved_runtime must be a mapping")
    keys = set(runtime)
    unknown = keys - RESOLVED_RUNTIME_FIELDS
    missing = RESOLVED_RUNTIME_FIELDS - keys
    if unknown or missing:
        name = sorted(unknown or missing)[0]
        kind = "Unknown" if unknown else "Missing"
        raise ValueError(f"{kind} resolved runtime field: {name}")
    return {name: _runtime_value(name, runtime[name]) for name in sorted(keys)}


def build_runtime_identity(
    resolved_runtime: Mapping[str, object],
    environ: Mapping[str, str] = os.environ,
) -> dict[str, object]:
    """Build and hash the complete allowlisted runtime identity."""
    unsigned = {
        "schema": IDENTITY_SCHEMA,
        "environment": _required_environment(environ),
        "runtime": _runtime(resolved_runtime),
    }
    return {
        **unsigned,
        "sha256": hashlib.sha256(canonical_json(unsigned)).hexdigest(),
    }


def build_identity(
    resolved_runtime: Mapping[str, object],
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Backward-compatible descriptive alias for build_runtime_identity."""
    return build_runtime_identity(
        resolved_runtime, environ=os.environ if environment is None else environment
    )


def validate_identity(identity: object) -> dict[str, object]:
    """Validate an identity received from an untrusted profile."""
    if type(identity) is not dict:
        raise ValueError("runtime_identity must be an object")
    unknown = set(identity) - _IDENTITY_FIELDS
    missing = _IDENTITY_FIELDS - set(identity)
    if unknown or missing:
        name = sorted(unknown or missing)[0]
        raise ValueError(f"Invalid runtime identity field: {name}")
    if identity["schema"] != IDENTITY_SCHEMA:
        raise ValueError("Unsupported runtime identity schema")
    environment = identity["environment"]
    if type(environment) is not dict or set(environment) != set(
        COMMIT_ENV_FIELDS + STRING_ENV_FIELDS
    ):
        raise ValueError("Runtime identity environment fields do not match the allowlist")
    unsigned = {
        "schema": IDENTITY_SCHEMA,
        "environment": _required_environment(environment),
        "runtime": _runtime(identity["runtime"]),
    }
    digest = identity["sha256"]
    if type(digest) is not str or not _DIGEST_RE.fullmatch(digest):
        raise ValueError("Runtime identity sha256 must be 64 lowercase hexadecimal characters")
    expected = hashlib.sha256(canonical_json(unsigned)).hexdigest()
    if digest != expected:
        raise ValueError("Runtime identity sha256 does not match its canonical contents")
    return {**unsigned, "sha256": digest}


def first_identity_difference(
    profile_identity: object, current_identity: object
) -> IdentityDifference | None:
    """Return the first canonical field difference, or None when identical."""
    profile = validate_identity(profile_identity)
    current = validate_identity(current_identity)

    def first(profile_value: object, current_value: object, path: str):
        if type(profile_value) is dict and type(current_value) is dict:
            for key in sorted(set(profile_value) | set(current_value)):
                child = f"{path}.{key}" if path else key
                if key not in profile_value:
                    return IdentityDifference(child, None, current_value[key])
                if key not in current_value:
                    return IdentityDifference(child, profile_value[key], None)
                difference = first(profile_value[key], current_value[key], child)
                if difference is not None:
                    return difference
            return None
        if profile_value != current_value:
            return IdentityDifference(path, profile_value, current_value)
        return None

    return first(profile, current, "")


def compare_identity(expected: object, actual: object) -> None:
    """Raise with the first canonical field path when identities differ."""
    difference = first_identity_difference(expected, actual)
    if difference is not None:
        raise ValueError(
            f"Runtime identity mismatch at {difference.field}: "
            f"expected {difference.profile!r}, got {difference.current!r}"
        )
