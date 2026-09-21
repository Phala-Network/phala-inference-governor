import copy
import math
import unittest

from pig_governor.identity import (
    JSON_SAFE_INTEGER,
    RESOLVED_RUNTIME_FIELDS,
    build_runtime_identity,
    canonical_json,
    compare_identity,
    first_identity_difference,
    validate_identity,
)


ENVIRONMENT = {
    "PIG_ENGINE_COMMIT": "1" * 40,
    "PIG_GOVERNOR_COMMIT": "2" * 40,
    "PIG_MODEL_ARTIFACT_ID": "sha256:" + "3" * 64,
    "PIG_RUNTIME_HARDWARE_ID": "h100-sxm-tp1-v1",
}


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


class RuntimeIdentityTests(unittest.TestCase):
    def test_required_field_set_is_exact_and_canonical_order_is_stable(self):
        runtime = resolved_runtime()
        self.assertEqual(set(runtime), RESOLVED_RUNTIME_FIELDS)
        first = build_runtime_identity(runtime, ENVIRONMENT)
        second = build_runtime_identity(
            dict(reversed(list(runtime.items()))),
            dict(reversed(list(ENVIRONMENT.items()))),
        )
        self.assertEqual(first, second)
        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertEqual(validate_identity(first), first)

    def test_every_runtime_field_is_required(self):
        for field in sorted(RESOLVED_RUNTIME_FIELDS):
            runtime = resolved_runtime()
            del runtime[field]
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                build_runtime_identity(runtime, ENVIRONMENT)

    def test_every_runtime_field_has_strict_scalar_type(self):
        for field in sorted(RESOLVED_RUNTIME_FIELDS):
            runtime = resolved_runtime()
            value = runtime[field]
            if type(value) is bool:
                runtime[field] = 1
            elif type(value) is str:
                runtime[field] = False
            elif type(value) is int:
                runtime[field] = True
            elif type(value) is float:
                runtime[field] = True
            else:
                runtime[field] = []
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                build_runtime_identity(runtime, ENVIRONMENT)

    def test_numeric_ranges_and_json_safe_integer_limit(self):
        runtime = resolved_runtime()
        runtime["max_total_tokens"] = JSON_SAFE_INTEGER + 1
        with self.assertRaisesRegex(ValueError, "max_total_tokens"):
            build_runtime_identity(runtime, ENVIRONMENT)
        runtime = resolved_runtime()
        runtime["mem_fraction_static"] = math.inf
        with self.assertRaisesRegex(ValueError, "mem_fraction_static"):
            build_runtime_identity(runtime, ENVIRONMENT)
        runtime = resolved_runtime()
        runtime["mem_fraction_static"] = 1.1
        with self.assertRaisesRegex(ValueError, "mem_fraction_static"):
            build_runtime_identity(runtime, ENVIRONMENT)

    def test_each_locked_environment_field_changes_identity(self):
        original = build_runtime_identity(resolved_runtime(), ENVIRONMENT)
        for field in ENVIRONMENT:
            changed_environment = dict(ENVIRONMENT)
            if field.endswith("COMMIT"):
                changed_environment[field] = "4" * 40
            elif field == "PIG_MODEL_ARTIFACT_ID":
                changed_environment[field] = "sha256:" + "4" * 64
            else:
                changed_environment[field] += "-changed"
            changed = build_runtime_identity(resolved_runtime(), changed_environment)
            with self.subTest(field=field):
                difference = first_identity_difference(original, changed)
                self.assertEqual(difference.field, f"environment.{field}")
                with self.assertRaisesRegex(ValueError, f"environment.{field}"):
                    compare_identity(original, changed)

    def test_runtime_change_reports_first_canonical_path(self):
        expected = build_runtime_identity(resolved_runtime(), ENVIRONMENT)
        changed_runtime = resolved_runtime()
        changed_runtime["dtype"] = "float16"
        actual = build_runtime_identity(changed_runtime, ENVIRONMENT)
        with self.assertRaisesRegex(ValueError, "runtime.dtype"):
            compare_identity(expected, actual)

    def test_unrelated_runtime_and_identity_fields_are_rejected(self):
        runtime = resolved_runtime()
        runtime["node_uuid"] = "host-1"
        with self.assertRaisesRegex(ValueError, "node_uuid"):
            build_runtime_identity(runtime, ENVIRONMENT)
        identity = build_runtime_identity(resolved_runtime(), ENVIRONMENT)
        identity["node_uuid"] = "host-1"
        with self.assertRaisesRegex(ValueError, "node_uuid"):
            validate_identity(identity)

    def test_environment_formats_are_strict(self):
        cases = {
            "PIG_ENGINE_COMMIT": "A" * 40,
            "PIG_MODEL_ARTIFACT_ID": "sha256:model",
            "PIG_RUNTIME_HARDWARE_ID": "Node UUID",
        }
        for field, value in cases.items():
            environment = dict(ENVIRONMENT)
            environment[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                build_runtime_identity(resolved_runtime(), environment)

    def test_tampered_identity_digest_is_rejected(self):
        identity = build_runtime_identity(resolved_runtime(), ENVIRONMENT)
        tampered = copy.deepcopy(identity)
        tampered["runtime"]["dtype"] = "float16"
        with self.assertRaisesRegex(ValueError, "canonical contents"):
            validate_identity(tampered)


if __name__ == "__main__":
    unittest.main()
