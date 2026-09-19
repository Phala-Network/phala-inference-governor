"""Explicit adapter behavior with native-shaped CPU fixtures."""
import unittest
from types import SimpleNamespace
from pig_governor import Governor
from pig_governor.sglang import SglangGovernor, create, on_abort_emitted
from unittest.mock import patch


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.core = Governor(35)
        self.addCleanup(self.core.close)
        self.adapter = SglangGovernor(self.core)

    def test_result_then_actual_abort_never_changes_native_resource_objects(self):
        resource = object()
        req = SimpleNamespace(output_ids_through_stop=[1, 2], finished_reason=None,
                              finished=lambda: False, kv=resource)
        batch = SimpleNamespace(reqs=[req], launch_ts=0,
                    forward_mode=SimpleNamespace(is_extend_without_speculative=lambda: True))
        self.adapter.after_result(batch, 1)
        self.assertIs(req.kv, resource)
        with patch('pig_governor.sglang.time.monotonic', return_value=2):
            on_abort_emitted(req)
            on_abort_emitted(req)
        self.assertIs(req.kv, resource)
        self.assertEqual(self.core.snapshot(2)['active_decode_sequences'], 0)
        self.assertEqual(self.core.snapshot(2)['decode_tokens'], 1)

    def test_retracted_request_stays_exposed_until_native_abort(self):
        req = SimpleNamespace(output_ids_through_stop=[1], finished_reason=None,
                              finished=lambda: False)
        batch = SimpleNamespace(reqs=[req], launch_ts=0,
                    forward_mode=SimpleNamespace(is_extend_without_speculative=lambda: False))
        self.adapter.after_result(batch, 1)
        self.assertFalse(self.adapter.before_prefill(None, [], None, 2))
        self.assertEqual(self.core.snapshot(2)['active_decode_sequences'], 1)
        with patch('pig_governor.sglang.time.monotonic', return_value=3):
            on_abort_emitted(req)
        self.assertEqual(self.core.snapshot(3)['decode_sequence_seconds'], 2)

    def test_create_reads_resolved_namespaces_not_raw_server_args(self):
        raw = SimpleNamespace(
            tp_size=8,
            pp_size=8,
            dp_size=8,
            disable_overlap_schedule=False,
            disaggregation_mode='decode',
        )
        parallel = SimpleNamespace(tp_size=1, pp_size=1, dp_size=1)
        schedule = SimpleNamespace(disable_overlap_schedule=True)
        disagg = SimpleNamespace(disaggregation_mode='null')
        with patch.dict('pig_governor.sglang.os.environ', {'PIG_GOVERNOR_ENABLE': '1'}), \
             patch('pig_governor.sglang.get_parallel', return_value=parallel), \
             patch('pig_governor.sglang.get_schedule', return_value=schedule), \
             patch('pig_governor.sglang.get_disagg', return_value=disagg):
            adapter = create(raw)
        self.addCleanup(adapter.core.close)
        self.assertIsInstance(adapter, SglangGovernor)

    def test_create_rejects_resolved_unsupported_topology(self):
        raw = SimpleNamespace(
            tp_size=1,
            pp_size=1,
            dp_size=1,
            disable_overlap_schedule=True,
            disaggregation_mode='null',
        )
        parallel = SimpleNamespace(tp_size=2, pp_size=1, dp_size=1)
        schedule = SimpleNamespace(disable_overlap_schedule=True)
        disagg = SimpleNamespace(disaggregation_mode='null')
        with patch.dict('pig_governor.sglang.os.environ', {'PIG_GOVERNOR_ENABLE': '1'}), \
             patch('pig_governor.sglang.get_parallel', return_value=parallel), \
             patch('pig_governor.sglang.get_schedule', return_value=schedule), \
             patch('pig_governor.sglang.get_disagg', return_value=disagg):
            with self.assertRaisesRegex(ValueError, 'TP1/PP1/non-overlap/no disaggregation'):
                create(raw)


if __name__ == '__main__': unittest.main()
