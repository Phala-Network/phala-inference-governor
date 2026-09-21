"""Explicit adapter behavior with native-shaped CPU fixtures."""
import unittest
from types import SimpleNamespace
from pig_governor import Governor
from pig_governor.sglang import SglangGovernor, create, on_abort_emitted
from unittest.mock import patch


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.core = Governor(0, max_running_requests=4)
        self.addCleanup(self.core.close)
        self.adapter = SglangGovernor(self.core, max_running_requests=4)

    def _request(self, rid='request', tokens=16, max_new_tokens=16):
        request = SimpleNamespace(
            rid=rid,
            origin_input_ids=list(range(tokens)),
            sampling_params=SimpleNamespace(max_new_tokens=max_new_tokens),
            output_ids_through_stop=[],
            finished_reason=None,
            finished=lambda: False,
            kv=object(),
        )
        self.adapter.admit_request(request, 0)
        return request

    def test_result_then_actual_abort_never_changes_native_resource_objects(self):
        req = self._request()
        req.output_ids_through_stop = [1, 2]
        batch = SimpleNamespace(
            reqs=[req], launch_ts=0,
            forward_mode=SimpleNamespace(is_extend_without_speculative=lambda: True),
        )
        self.adapter.after_result(batch, 1)
        resource = req.kv
        with patch('pig_governor.sglang.time.monotonic', return_value=2):
            on_abort_emitted(req)
            on_abort_emitted(req)
        self.assertIs(req.kv, resource)
        self.assertEqual(self.core.snapshot(2)['active_decode_sequences'], 0)
        self.assertEqual(self.core.snapshot(2)['decode_tokens'], 1)
        self.assertEqual(self.adapter.outstanding, 0)

    def test_retracted_request_stays_exposed_until_native_abort(self):
        req = self._request()
        req.output_ids_through_stop = [1]
        batch = SimpleNamespace(
            reqs=[req], launch_ts=0,
            forward_mode=SimpleNamespace(is_extend_without_speculative=lambda: False),
        )
        self.adapter.after_result(batch, 1)
        self.assertFalse(self.adapter.before_prefill(None, [], None, 2))
        self.assertEqual(self.core.snapshot(2)['active_decode_sequences'], 1)
        with patch('pig_governor.sglang.time.monotonic', return_value=3):
            on_abort_emitted(req)
        self.assertEqual(self.core.snapshot(3)['decode_sequence_seconds'], 2)
        self.assertEqual(self.adapter.outstanding, 0)

    def test_native_pre_admission_abort_skips_governor_progress(self):
        req = SimpleNamespace(
            finished=lambda: False,
            to_finish=object(),
        )
        batch = SimpleNamespace(
            reqs=[req], launch_ts=0,
            forward_mode=SimpleNamespace(is_extend_without_speculative=lambda: False),
        )
        self.adapter.after_result(batch, 1)
        self.assertIsNone(getattr(req, 'governor_progress', None))
        self.assertEqual(self.adapter.outstanding, 0)

    def test_unmanaged_nonterminal_result_still_fails_closed(self):
        req = SimpleNamespace(finished=lambda: False, to_finish=None)
        batch = SimpleNamespace(
            reqs=[req], launch_ts=0,
            forward_mode=SimpleNamespace(is_extend_without_speculative=lambda: False),
        )
        with self.assertRaisesRegex(RuntimeError, 'no Governor reservation'):
            self.adapter.after_result(batch, 1)

    def test_create_reads_resolved_namespaces_not_raw_server_args(self):
        raw = SimpleNamespace(tp_size=8, pp_size=8, dp_size=8,
                              disable_overlap_schedule=False,
                              disaggregation_mode='decode')
        parallel = SimpleNamespace(tp_size=1, pp_size=1, dp_size=1)
        schedule = SimpleNamespace(disable_overlap_schedule=True, max_running_requests=43)
        disagg = SimpleNamespace(disaggregation_mode='null')
        with patch.dict('pig_governor.sglang.os.environ', {'PIG_GOVERNOR_ENABLE': '1'}), \
             patch('pig_governor.sglang.get_parallel', return_value=parallel), \
             patch('pig_governor.sglang.get_schedule', return_value=schedule), \
             patch('pig_governor.sglang.get_disagg', return_value=disagg):
            adapter = create(raw)
        self.addCleanup(adapter.core.close)
        self.assertIsInstance(adapter, SglangGovernor)
        self.assertEqual(adapter.max_running_requests, 43)

    def test_create_rejects_resolved_unsupported_topology(self):
        raw = SimpleNamespace(tp_size=1, pp_size=1, dp_size=1,
                              disable_overlap_schedule=True,
                              disaggregation_mode='null')
        parallel = SimpleNamespace(tp_size=2, pp_size=1, dp_size=1)
        schedule = SimpleNamespace(disable_overlap_schedule=True, max_running_requests=43)
        disagg = SimpleNamespace(disaggregation_mode='null')
        with patch.dict('pig_governor.sglang.os.environ', {'PIG_GOVERNOR_ENABLE': '1'}), \
             patch('pig_governor.sglang.get_parallel', return_value=parallel), \
             patch('pig_governor.sglang.get_schedule', return_value=schedule), \
             patch('pig_governor.sglang.get_disagg', return_value=disagg):
            with self.assertRaisesRegex(ValueError, 'TP1/PP1/non-overlap/no disaggregation'):
                create(raw)


if __name__ == '__main__':
    unittest.main()
