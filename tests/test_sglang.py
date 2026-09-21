"""Explicit adapter behavior with native-shaped CPU fixtures."""
import unittest
from types import SimpleNamespace
from pig_governor import Governor
from pig_governor.identity import RESOLVED_RUNTIME_FIELDS, build_runtime_identity
from pig_governor.sglang import SglangGovernor, create, on_abort_emitted
from unittest.mock import patch


IDENTITY_ENV = {
    'PIG_ENGINE_COMMIT': '1' * 40,
    'PIG_GOVERNOR_COMMIT': '2' * 40,
    'PIG_MODEL_ARTIFACT_ID': 'sha256:' + '3' * 64,
    'PIG_RUNTIME_HARDWARE_ID': 'h100-sxm-tp1-v1',
}


def resolved_runtime(**changes):
    values = {
        'attention_backend': 'fa3',
        'decode_attention_backend': None,
        'prefill_attention_backend': None,
        'chunked_prefill_size': 4096,
        'context_length': 262144,
        'cuda_graph_backend_decode': 'auto',
        'cuda_graph_backend_prefill': 'auto',
        'disable_cuda_graph': False,
        'disable_overlap_schedule': True,
        'disable_radix_cache': False,
        'disaggregation_mode': 'null',
        'dp_size': 1,
        'dtype': 'bfloat16',
        'enable_dp_attention': False,
        'enable_torch_compile': False,
        'kv_cache_dtype': 'bfloat16',
        'load_format': 'auto',
        'max_prefill_tokens': 16384,
        'max_running_requests': 43,
        'max_total_tokens': 262144,
        'mem_fraction_static': 0.8,
        'model_config_parser': 'auto',
        'model_impl': 'sglang',
        'page_size': 1,
        'pp_max_micro_batch_size': 43,
        'pp_size': 1,
        'quantization': None,
        'sampling_backend': 'pytorch',
        'schedule_policy': 'fcfs',
        'speculative_accept_threshold_acc': 0.9,
        'speculative_accept_threshold_single': 1.0,
        'speculative_algorithm': 'EAGLE',
        'speculative_draft_attention_backend': None,
        'speculative_draft_kv_cache_dtype': 'bfloat16',
        'speculative_eagle_topk': 1,
        'speculative_num_draft_tokens': 5,
        'speculative_num_steps': 3,
        'torch_compile_max_bs': 32,
        'tp_size': 1,
        'weight_version': 'default',
    }
    values.update(changes)
    if set(values) != set(RESOLVED_RUNTIME_FIELDS):
        raise AssertionError('Test runtime identity fields are out of sync')
    return values


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

    def test_unmanaged_embedding_result_is_outside_decode_governor(self):
        req = SimpleNamespace(
            is_prefill_only=True,
            finished=lambda: False,
            to_finish=None,
        )
        batch = SimpleNamespace(
            reqs=[req], launch_ts=0,
            forward_mode=SimpleNamespace(is_extend_without_speculative=lambda: False),
        )
        self.adapter.after_result(batch, 1)
        self.assertEqual(self.adapter.outstanding, 0)
        self.assertEqual(self.adapter.active, 0)

    def test_owner_mismatch_fails_closed_for_release_and_progress(self):
        other_core = Governor(0, max_running_requests=4)
        self.addCleanup(other_core.close)
        other = SglangGovernor(other_core, max_running_requests=4)
        req = self._request()

        with self.assertRaisesRegex(RuntimeError, 'different Governor owner'):
            other.release_request(req)
        with self.assertRaisesRegex(RuntimeError, 'different Governor owner'):
            other._progress_for(req)

        self.assertEqual(self.adapter.outstanding, 1)
        self.assertEqual(other.outstanding, 0)

    def test_progress_owner_mismatch_fails_closed(self):
        other_core = Governor(0, max_running_requests=4)
        self.addCleanup(other_core.close)
        other = SglangGovernor(other_core, max_running_requests=4)
        req = self._request()
        self.adapter._progress_for(req)

        with self.assertRaisesRegex(RuntimeError, 'different governor epoch'):
            other._progress_for(req)

    def test_create_reads_resolved_namespaces_not_raw_server_args(self):
        raw = SimpleNamespace(tp_size=8, pp_size=8, dp_size=8,
                              disable_overlap_schedule=False,
                              disaggregation_mode='decode')
        parallel = SimpleNamespace(tp_size=1, pp_size=1, dp_size=1)
        schedule = SimpleNamespace(disable_overlap_schedule=True, max_running_requests=43)
        disagg = SimpleNamespace(disaggregation_mode='null')
        context = SimpleNamespace(
            resolved_server_args_dict=lambda: resolved_runtime()
        )
        environment = {
            **IDENTITY_ENV,
            'PIG_GOVERNOR_ENABLE': '1',
            'PIG_GOVERNOR_LIBRARY': self.core._lib._name,
            'PIG_TPS_REFERENCE': '0',
            'PIG_MAX_RUNNING': '41',
            'PIG_MAX_WAITING': '3',
        }
        with patch.dict('pig_governor.sglang.os.environ', environment, clear=True), \
             patch('pig_governor.sglang.get_parallel', return_value=parallel), \
             patch('pig_governor.sglang.get_schedule', return_value=schedule), \
             patch('pig_governor.sglang.get_disagg', return_value=disagg), \
             patch('pig_governor.sglang.get_context', return_value=context):
            adapter = create(raw, is_generation=True)
        self.addCleanup(adapter.core.close)
        self.assertIsInstance(adapter, SglangGovernor)
        self.assertEqual(adapter.max_running_requests, 43)
        self.assertEqual(adapter.native_max_running_requests, 43)
        self.assertEqual(adapter.max_running, 41)
        self.assertEqual(adapter.max_waiting, 3)

    def test_create_rejects_non_generation_model_when_enabled(self):
        with patch.dict(
            'pig_governor.sglang.os.environ',
            {'PIG_GOVERNOR_ENABLE': '1'},
            clear=True,
        ):
            for is_generation in (False, None):
                with self.subTest(is_generation=is_generation), self.assertRaisesRegex(
                    ValueError, 'requires a generation model'
                ):
                    create(SimpleNamespace(), is_generation=is_generation)

    def test_create_prefers_effective_max_running_override(self):
        parallel = SimpleNamespace(tp_size=1, pp_size=1, dp_size=1)
        disagg = SimpleNamespace(disaggregation_mode='null')
        context = SimpleNamespace(
            resolved_server_args_dict=lambda: resolved_runtime()
        )
        environment = {
            **IDENTITY_ENV,
            'PIG_GOVERNOR_ENABLE': '1',
            'PIG_GOVERNOR_LIBRARY': self.core._lib._name,
            'PIG_TPS_REFERENCE': '0',
        }
        overrides = resolved_runtime(max_running_requests=17)
        for configured in (None, 43):
            with self.subTest(configured=configured):
                schedule = SimpleNamespace(
                    disable_overlap_schedule=True,
                    max_running_requests=configured,
                )
                with patch.dict('pig_governor.sglang.os.environ', environment, clear=True), \
                     patch('pig_governor.sglang.get_parallel', return_value=parallel), \
                     patch('pig_governor.sglang.get_schedule', return_value=schedule), \
                     patch('pig_governor.sglang.get_disagg', return_value=disagg), \
                     patch('pig_governor.sglang.get_context', return_value=context):
                    adapter = create(
                        SimpleNamespace(),
                        runtime_overrides=overrides,
                        is_generation=True,
                    )
                self.addCleanup(adapter.core.close)
                self.assertEqual(adapter.max_running_requests, 17)
                self.assertEqual(adapter.max_running, 17)
                self.assertEqual(adapter.max_waiting, 3)
                self.assertEqual(
                    adapter.runtime_identity['runtime']['max_running_requests'], 17
                )

    def test_create_with_positive_reference_requires_a_frozen_profile(self):
        parallel = SimpleNamespace(tp_size=1, pp_size=1, dp_size=1)
        schedule = SimpleNamespace(disable_overlap_schedule=True, max_running_requests=43)
        disagg = SimpleNamespace(disaggregation_mode='null')
        context = SimpleNamespace(
            resolved_server_args_dict=lambda: resolved_runtime()
        )
        environment = {
            **IDENTITY_ENV,
            'PIG_GOVERNOR_ENABLE': '1',
            'PIG_GOVERNOR_LIBRARY': self.core._lib._name,
            'PIG_TPS_REFERENCE': '50',
        }
        with patch.dict('pig_governor.sglang.os.environ', environment, clear=True), \
             patch('pig_governor.sglang.get_parallel', return_value=parallel), \
             patch('pig_governor.sglang.get_schedule', return_value=schedule), \
             patch('pig_governor.sglang.get_disagg', return_value=disagg), \
             patch('pig_governor.sglang.get_context', return_value=context):
            with self.assertRaisesRegex(ValueError, 'PIG_TPS_PROFILE'):
                create(SimpleNamespace(), is_generation=True)

    def test_create_rejects_invalid_mutable_limits(self):
        parallel = SimpleNamespace(tp_size=1, pp_size=1, dp_size=1)
        schedule = SimpleNamespace(
            disable_overlap_schedule=True, max_running_requests=43
        )
        disagg = SimpleNamespace(disaggregation_mode='null')
        context = SimpleNamespace(
            resolved_server_args_dict=lambda: resolved_runtime()
        )
        base = {
            **IDENTITY_ENV,
            'PIG_GOVERNOR_ENABLE': '1',
            'PIG_GOVERNOR_LIBRARY': self.core._lib._name,
            'PIG_TPS_REFERENCE': '0',
        }
        for name, value in (
            ('PIG_MAX_RUNNING', '44'),
            ('PIG_MAX_RUNNING', '0'),
            ('PIG_MAX_WAITING', '-1'),
            ('PIG_MAX_WAITING', '3.0'),
            ('PIG_MAX_WAITING', '4'),
        ):
            with self.subTest(name=name, value=value), patch.dict(
                'pig_governor.sglang.os.environ', {**base, name: value}, clear=True
            ), patch(
                'pig_governor.sglang.get_parallel', return_value=parallel
            ), patch(
                'pig_governor.sglang.get_schedule', return_value=schedule
            ), patch(
                'pig_governor.sglang.get_disagg', return_value=disagg
            ), patch(
                'pig_governor.sglang.get_context', return_value=context
            ):
                with self.assertRaises(ValueError):
                    create(SimpleNamespace(), is_generation=True)

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
                create(raw, is_generation=True)

    def test_runtime_identity_change_rotates_epoch_and_clears_surface(self):
        state = resolved_runtime(weight_version='v0')

        def provider():
            return build_runtime_identity(state, environ=IDENTITY_ENV)

        core = Governor(0, max_running_requests=43)
        self.addCleanup(core.close)
        adapter = SglangGovernor(
            core,
            max_running_requests=43,
            max_running=40,
            max_waiting=3,
            runtime_identity=provider(),
            identity_provider=provider,
        )
        core.observe_batch(1, 10, 1.0, 1, 0, 1)
        old_epoch = core.epoch
        state['weight_version'] = 'v1'
        self.assertTrue(adapter.refresh_identity(2))
        snapshot = core.snapshot(2)
        self.assertNotEqual(core.epoch, old_epoch)
        self.assertEqual(snapshot['decode_tokens'], 0)
        self.assertEqual(snapshot['active_decode_sequences'], 0)
        self.assertEqual(adapter.surface_epoch_rotations, 1)
        policy = adapter.policy_snapshot(2)
        self.assertEqual(policy['mutable']['max_running'], 40)
        self.assertEqual(policy['mutable']['max_waiting'], 3)

    def test_profile_snapshot_is_an_epoch_guarded_loadable_envelope(self):
        identity = build_runtime_identity(resolved_runtime(), environ=IDENTITY_ENV)
        core = Governor(0, max_running_requests=43)
        self.addCleanup(core.close)
        adapter = SglangGovernor(
            core,
            max_running_requests=43,
            runtime_identity=identity,
            identity_provider=lambda: identity,
        )
        core.observe_surface(1, 8, 0.1, 1, 0)
        exported = adapter.profile_snapshot(1)
        self.assertEqual(exported['epoch'], core.epoch)
        self.assertEqual(exported['runtime_identity_sha256'], identity['sha256'])
        self.assertEqual(exported['profile']['runtime_identity'], identity)
        self.assertEqual(exported['profile']['cells'][0]['approved_tps'], 80)
        self.assertEqual(exported['coverage']['count'], 1)


if __name__ == '__main__':
    unittest.main()
