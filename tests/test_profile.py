import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from pig_governor.identity import build_runtime_identity
from pig_governor.profile import (
    MAX_PROFILE_BYTES,
    PROFILE_PATH_ENV,
    PROFILE_SCHEMA,
    PROFILE_SHA256_ENV,
    bootstrap_profile,
    build_profile_document,
    canonical_profile_bytes,
    load_profile,
    predictor_contract,
    validate_profile,
)


ENVIRONMENT = {
    "PIG_ENGINE_COMMIT": "1" * 40,
    "PIG_GOVERNOR_COMMIT": "2" * 40,
    "PIG_MODEL_ARTIFACT_ID": "sha256:" + "3" * 64,
    "PIG_RUNTIME_HARDWARE_ID": "h100-sxm-tp1-v1",
}
NOW = datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc)


def resolved_runtime():
    return {
        "attention_backend": "flashinfer",
        "decode_attention_backend": None,
        "prefill_attention_backend": None,
        "chunked_prefill_size": 8192,
        "context_length": 32768,
        "cuda_graph_backend_decode": None,
        "cuda_graph_backend_prefill": None,
        "disable_cuda_graph": False,
        "disable_overlap_schedule": True,
        "disable_radix_cache": False,
        "disaggregation_mode": "null",
        "dp_size": 1,
        "dtype": "bfloat16",
        "enable_dp_attention": False,
        "enable_torch_compile": False,
        "kv_cache_dtype": "auto",
        "load_format": "auto",
        "max_prefill_tokens": 16384,
        "max_running_requests": 2,
        "max_total_tokens": 65536,
        "mem_fraction_static": 0.88,
        "model_config_parser": "default",
        "model_impl": "auto",
        "page_size": 1,
        "pp_max_micro_batch_size": None,
        "pp_size": 1,
        "quantization": None,
        "sampling_backend": "flashinfer",
        "schedule_policy": "lpm",
        "speculative_accept_threshold_acc": None,
        "speculative_accept_threshold_single": None,
        "speculative_algorithm": None,
        "speculative_draft_attention_backend": None,
        "speculative_draft_kv_cache_dtype": None,
        "speculative_eagle_topk": None,
        "speculative_num_draft_tokens": None,
        "speculative_num_steps": None,
        "torch_compile_max_bs": None,
        "tp_size": 1,
        "weight_version": None,
    }


def flat_cell(concurrency=2, pressure=3, lower=100.0):
    return {
        "concurrency": concurrency,
        "pressure_class": pressure,
        "long_tokens": 100,
        "long_seconds": 1.0,
        "short_tokens": 20,
        "short_seconds": 0.2,
        "evidence_lower_tps": lower,
    }


def cell(concurrency=2, pressure=3, *, approved=90.0):
    return {
        "concurrency": concurrency,
        "pressure_class": pressure,
        "evidence": {
            "long": {"tokens": 100, "seconds": 1.0},
            "short": {"tokens": 20, "seconds": 0.2},
        },
        "evidence_lower_tps": 100.0,
        "approved_tps": approved,
    }


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.identity = build_runtime_identity(resolved_runtime(), ENVIRONMENT)
        self.document = {
            "schema": PROFILE_SCHEMA,
            "created_at": "2026-09-20T00:00:00Z",
            "valid_until": "2026-09-22T00:00:00Z",
            "runtime_identity": self.identity,
            "predictor": predictor_contract(),
            "max_running_requests": 2,
            "cells": [cell()],
        }

    def validate(self, document=None, **kwargs):
        return validate_profile(
            self.document if document is None else document,
            max_running_requests=kwargs.pop("max_running_requests", 2),
            current_identity=kwargs.pop("current_identity", self.identity),
            now=kwargs.pop("now", NOW),
            **kwargs,
        )

    def write_and_load(self, content, *, digest=None, current_identity=None):
        if isinstance(content, dict):
            content = canonical_profile_bytes(content)
        elif isinstance(content, str):
            content = content.encode()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "profile.json")
            path.write_bytes(content)
            expected = digest or hashlib.sha256(content).hexdigest()
            return load_profile(
                path,
                expected,
                self.identity if current_identity is None else current_identity,
                2,
                NOW,
            )

    def test_valid_profile_returns_core_cells_and_full_coverage(self):
        loaded = self.write_and_load(self.document)
        self.assertEqual(dict(loaded.cells[0]), {
            "concurrency": 2,
            "pressure_class": 3,
            "long_tokens": 100,
            "long_seconds": 1.0,
            "short_tokens": 20,
            "short_seconds": 0.2,
            "evidence_lower_tps": 90.0,
        })
        self.assertEqual(loaded.coverage_count, 8)
        self.assertEqual(loaded.missing, ())
        self.assertEqual(loaded.ttl_seconds, 86400.0)
        self.assertRegex(loaded.sha256, r"^[0-9a-f]{64}$")

    def test_profile_capacity_is_a_fail_closed_minimum(self):
        profile_runtime = resolved_runtime()
        profile_runtime["max_total_tokens"] = 954291
        profile_identity = build_runtime_identity(profile_runtime, ENVIRONMENT)
        document = copy.deepcopy(self.document)
        document["runtime_identity"] = profile_identity
        larger_runtime = resolved_runtime()
        larger_runtime["max_total_tokens"] = 954454
        larger = build_runtime_identity(larger_runtime, ENVIRONMENT)
        loaded = self.write_and_load(document, current_identity=larger)
        self.assertNotEqual(profile_identity["sha256"], larger["sha256"])
        self.assertEqual(
            loaded.metadata["runtime_identity_sha256"], profile_identity["sha256"]
        )
        self.assertEqual(loaded.metadata["current_runtime_identity_sha256"], larger["sha256"])
        self.assertEqual(loaded.metadata["profile_max_total_tokens"], 954291)
        self.assertEqual(loaded.metadata["current_max_total_tokens"], 954454)

        for current_tokens in (954290, 900000):
            current_runtime = resolved_runtime()
            current_runtime["max_total_tokens"] = current_tokens
            current = build_runtime_identity(current_runtime, ENVIRONMENT)
            with self.subTest(current_tokens=current_tokens), self.assertRaisesRegex(
                ValueError, "profile requires at least 954291"
            ):
                self.write_and_load(document, current_identity=current)

    def test_document_is_json_ready_independent_deep_copy(self):
        loaded = self.validate()
        first = loaded.document
        json.dumps(first)
        first["cells"][0]["evidence"]["long"]["tokens"] = 0
        second = loaded.document
        self.assertEqual(second["cells"][0]["evidence"]["long"]["tokens"], 100)
        self.document["cells"][0]["evidence"]["long"]["tokens"] = 0
        self.assertEqual(loaded.document, second)

    def test_canonical_profile_bytes_is_deterministic(self):
        reverse = dict(reversed(list(self.document.items())))
        self.assertEqual(
            canonical_profile_bytes(self.document), canonical_profile_bytes(reverse)
        )
        with self.assertRaises(ValueError):
            canonical_profile_bytes({"value": math.nan})

    def test_export_bytes_hash_load_roundtrip(self):
        document = build_profile_document(
            self.identity, 2, [flat_cell()], created_at="2026-09-20T00:00:00Z"
        )
        data = canonical_profile_bytes(document)
        digest = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "profile.json")
            path.write_bytes(data)
            loaded = load_profile(path, digest, self.identity, 2, NOW)
        self.assertEqual(loaded.document, document)
        self.assertEqual(loaded.sha256, digest)
        self.assertEqual(loaded.missing, ())

    def test_builder_sorts_flat_cells_and_validates_duration(self):
        created = datetime.now(timezone.utc)
        document = build_profile_document(
            self.identity,
            2,
            [flat_cell(2, 3), flat_cell(1, 0)],
            created_at=created,
            valid_for_seconds=60,
        )
        self.assertEqual(
            [(item["concurrency"], item["pressure_class"]) for item in document["cells"]],
            [(1, 0), (2, 3)],
        )
        for invalid in (True, 59, 2592001, math.inf):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                build_profile_document(
                    self.identity, 2, [flat_cell()],
                    created_at=created,
                    valid_for_seconds=invalid,
                )

    def test_bootstrap_reference_zero_and_positive_policy(self):
        self.assertIsNone(bootstrap_profile(0, self.identity, 2, {}, NOW))
        for environment in (
            {PROFILE_PATH_ENV: "profile.json"},
            {PROFILE_SHA256_ENV: "0" * 64},
        ):
            with self.subTest(environment=environment), self.assertRaisesRegex(ValueError, "together"):
                bootstrap_profile(0, self.identity, 2, environment, NOW)
        with self.assertRaisesRegex(ValueError, "non-empty"):
            bootstrap_profile(
                0,
                self.identity,
                2,
                {PROFILE_PATH_ENV: "", PROFILE_SHA256_ENV: ""},
                NOW,
            )
        with self.assertRaisesRegex(ValueError, "PIG_TPS_PROFILE"):
            bootstrap_profile(50, self.identity, 2, {}, NOW)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "profile.json")
            low_tps = copy.deepcopy(self.document)
            low_tps["cells"][0]["approved_tps"] = 10.0
            data = canonical_profile_bytes(low_tps)
            path.write_bytes(data)
            environment = {
                PROFILE_PATH_ENV: str(path),
                PROFILE_SHA256_ENV: hashlib.sha256(data).hexdigest(),
            }
            production = bootstrap_profile(50, self.identity, 2, environment, NOW)
            self.assertEqual(production.metadata["bootstrap_mode"], "production")
            self.assertTrue(production.metadata["coverage_required"])
            self.assertEqual(production.cells[0]["evidence_lower_tps"], 10.0)

    def test_bootstrap_allows_partial_only_for_offline_sampling(self):
        partial = copy.deepcopy(self.document)
        partial["cells"] = [cell(2, 2)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "profile.json")
            data = canonical_profile_bytes(partial)
            path.write_bytes(data)
            environment = {
                PROFILE_PATH_ENV: str(path),
                PROFILE_SHA256_ENV: hashlib.sha256(data).hexdigest(),
            }
            sampled = bootstrap_profile(0, self.identity, 2, environment, NOW)
            self.assertEqual(sampled.metadata["bootstrap_mode"], "offline_sampling")
            self.assertFalse(sampled.metadata["coverage_complete"])
            self.assertEqual(sampled.missing, ((1, 3), (2, 3)))
            with self.assertRaisesRegex(ValueError, "cover"):
                bootstrap_profile(50, self.identity, 2, environment, NOW)

    def test_future_created_at_is_rejected(self):
        document = copy.deepcopy(self.document)
        document["created_at"] = "2026-09-21T00:00:01Z"
        with self.assertRaisesRegex(ValueError, "current wall time"):
            self.validate(document)

    def test_duplicate_unknown_nonfinite_and_boolean_are_rejected(self):
        raw = '{"schema":"a","schema":"b"}'
        with self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
            self.write_and_load(raw)
        document = copy.deepcopy(self.document)
        document["unknown"] = 1
        with self.assertRaisesRegex(ValueError, "unknown"):
            self.validate(document)
        raw = canonical_profile_bytes(self.document).decode().replace("90.0", "NaN")
        with self.assertRaisesRegex(ValueError, "Non-finite"):
            self.write_and_load(raw)
        document = copy.deepcopy(self.document)
        document["cells"][0]["concurrency"] = True
        with self.assertRaises(ValueError):
            self.validate(document)

    def test_file_size_symlink_and_hash_are_enforced(self):
        with self.assertRaisesRegex(ValueError, "64 KiB"):
            self.write_and_load(b" " * (MAX_PROFILE_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "sha256"):
            self.write_and_load(self.document, digest="0" * 64)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "profile.json")
            data = canonical_profile_bytes(self.document)
            path.write_bytes(data)
            symlink_stat = os.stat_result((stat.S_IFLNK,) + (0,) * 9)
            digest = hashlib.sha256(data).hexdigest()
            with mock.patch.object(Path, "lstat", return_value=symlink_stat):
                with self.assertRaisesRegex(ValueError, "non-symlink"):
                    load_profile(path, digest, self.identity, 2, NOW)

    def test_dates_evidence_ranges_and_order_are_strict(self):
        cases = (
            (lambda d: d.update(created_at="2026-09-20T00:00:00+00:00"), "RFC3339"),
            (lambda d: d.update(valid_until="2026-09-21T00:00:00Z"), "expired"),
            (lambda d: d["cells"][0]["evidence"]["long"].update(tokens=2**64), "integer"),
            (lambda d: d["cells"][0]["evidence"]["short"].update(tokens=1, seconds=0), "zero seconds"),
            (lambda d: d["cells"][0].update(evidence_lower_tps=99), "min rule"),
            (lambda d: d["cells"][0].update(approved_tps=101), "exceeds"),
        )
        for mutate, message in cases:
            document = copy.deepcopy(self.document)
            mutate(document)
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.validate(document)
        document = copy.deepcopy(self.document)
        document["cells"] = [cell(2, 0), cell(1, 3)]
        with self.assertRaisesRegex(ValueError, "canonical sorted order"):
            self.validate(document)

    def test_partial_coverage_and_identity_mismatch(self):
        document = copy.deepcopy(self.document)
        document["cells"] = [cell(2, 2)]
        loaded = self.validate(document)
        self.assertEqual(loaded.coverage_count, 6)
        self.assertEqual(loaded.missing, ((1, 3), (2, 3)))
        changed = resolved_runtime()
        changed["dtype"] = "float16"
        changed_identity = build_runtime_identity(changed, ENVIRONMENT)
        with self.assertRaisesRegex(ValueError, "runtime.dtype"):
            self.validate(current_identity=changed_identity)


if __name__ == "__main__":
    unittest.main()
