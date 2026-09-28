"""Explicit adapter behavior with native-shaped CPU fixtures."""
import hashlib
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from pig_governor import Governor
from pig_governor.core import RevisionConflict
from pig_governor.identity import (
    RESOLVED_RUNTIME_FIELDS, build_online_runtime_identity, build_runtime_identity,
)
from pig_governor.profile import build_profile_document, canonical_profile_bytes
from pig_governor.scheduler import Progress, SchedulerGovernor
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
        'speculative_attention_mode': 'prefill',
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

    def test_health_result_releases_without_learning_or_spending_exploration(self):
        core = Governor(50, max_running_requests=1)
        self.addCleanup(core.close)
        adapter = SglangGovernor(core, max_running_requests=1)
        health = SimpleNamespace(
            rid='health', origin_input_ids=[0],
            sampling_params=SimpleNamespace(max_new_tokens=1),
            output_ids_through_stop=[1], finished_reason=None,
            finished=lambda: True,
        )
        decision = adapter.admit_request(
            health, 100, waiting_count=0, is_health_check=True,
        )
        self.assertEqual(decision['reason_name'], 'health_check')
        batch = SimpleNamespace(
            reqs=[health], launch_ts=100,
            forward_mode=SimpleNamespace(is_extend_without_speculative=lambda: True),
        )
        adapter.after_result(batch, 100.1)
        adapter.after_result(batch, 100.2)
        self.assertTrue(health.governor_reservation.released)
        self.assertIsNone(getattr(health, 'governor_progress', None))
        self.assertEqual(adapter.outstanding, 0)
        self.assertEqual(adapter.active, 0)
        policy = core.snapshot(100.2)
        self.assertEqual(policy['decode_tokens'], 0)
        self.assertEqual(policy['decode_sequence_seconds'], 0)
        self.assertEqual(adapter._last_exploration_at, None)
        ordinary = SimpleNamespace(
            rid='ordinary', origin_input_ids=[1],
            sampling_params=SimpleNamespace(max_new_tokens=1),
        )
        self.assertEqual(adapter.admit_request(
            ordinary, 101, waiting_count=0,
        )['reason_name'], 'online_exploration')

    def test_health_abort_release_is_idempotent_and_identity_drains(self):
        identity = {'value': {'sha256': 'a' * 64}}
        adapter = SglangGovernor(
            self.core, max_running_requests=4,
            runtime_identity=identity['value'],
            identity_provider=lambda: identity['value'],
        )
        health = SimpleNamespace(
            rid='health-abort', origin_input_ids=[0],
            sampling_params=SimpleNamespace(max_new_tokens=1),
        )
        self.assertTrue(adapter.admit_request(
            health, 1, is_health_check=True,
        )['allowed'])
        old_epoch = self.core.epoch
        identity['value'] = {'sha256': 'b' * 64}
        self.assertFalse(adapter.refresh_identity(1.5))
        self.assertTrue(adapter._admission_paused)
        self.assertEqual(adapter.outstanding, 1)
        with patch('pig_governor.sglang.time.monotonic', return_value=2):
            on_abort_emitted(health)
            on_abort_emitted(health)
        self.assertEqual(adapter.outstanding, 0)
        self.assertEqual(adapter._health_outstanding, 0)
        self.assertTrue(adapter._health_observation_dirty)
        cold = self.core.snapshot(2)
        self.assertEqual(cold['decode_tokens'], 0)
        self.assertEqual(cold['decode_sequence_seconds'], 0)
        self.assertTrue(adapter.refresh_identity(2.1))
        self.assertFalse(adapter._admission_paused)
        self.assertFalse(adapter._health_observation_dirty)
        self.assertNotEqual(self.core.epoch, old_epoch)
        self.assertEqual(adapter.runtime_identity, identity['value'])

    def test_health_waiting_does_not_bias_decode_preference(self):
        health = SimpleNamespace(
            rid='health-waiting', origin_input_ids=[0],
            sampling_params=SimpleNamespace(max_new_tokens=1),
            time_stats=SimpleNamespace(wait_queue_entry_time=1),
        )
        self.adapter.admit_request(health, 1, is_health_check=True)
        business = SimpleNamespace(
            time_stats=SimpleNamespace(wait_queue_entry_time=9)
        )
        with patch.object(self.adapter, 'prefer_decode', return_value=True) as prefer:
            self.adapter.before_prefill(None, [health, business], health, 10)
        prefer.assert_called_once_with(
            10, runnable_decode=False, pending_prefill=True, oldest_ready_age=1,
        )

    def test_mixed_health_result_quarantines_native_learning_until_clean_batch(self):
        business = self._request('business')
        business.output_ids_through_stop = [1]
        decode_mode = SimpleNamespace(is_extend_without_speculative=lambda: False)
        prefill_mode = SimpleNamespace(is_extend_without_speculative=lambda: True)
        self.adapter.after_result(SimpleNamespace(
            reqs=[business], launch_ts=0, forward_mode=decode_mode,
        ), 1)

        health = SimpleNamespace(
            rid='health-mixed', origin_input_ids=[0],
            sampling_params=SimpleNamespace(max_new_tokens=1),
            output_ids_through_stop=[], finished_reason=None, done=False,
        )
        health.finished = lambda: health.done
        self.adapter.admit_request(health, 2, is_health_check=True)
        with patch.object(self.adapter, 'prefill_completed') as prefill:
            self.adapter.after_result(SimpleNamespace(
                reqs=[health], launch_ts=2, forward_mode=prefill_mode,
            ), 2.5)
            business.output_ids_through_stop = [1, 2, 3, 4, 5]
            health.output_ids_through_stop = [1]
            health.done = True
            self.adapter.after_result(SimpleNamespace(
                reqs=[business, health], launch_ts=2.5,
                forward_mode=prefill_mode,
            ), 3)
            prefill.assert_not_called()

        self.assertEqual(self.core.snapshot(3)['decode_tokens'], 0)
        self.assertEqual(self.core.snapshot(3)['decode_sequence_seconds'], 0)
        self.assertEqual(self.adapter.active, 1)
        self.assertEqual(self.adapter.outstanding, 1)
        self.assertTrue(health.governor_reservation.released)
        self.assertEqual(self.adapter._pending_evidence, {})

        business.output_ids_through_stop = [1, 2, 3, 4, 5, 6]
        self.adapter.after_result(SimpleNamespace(
            reqs=[business], launch_ts=3, forward_mode=decode_mode,
        ), 4)
        self.assertEqual(self.core.snapshot(4)['decode_sequence_seconds'], 0)
        business.output_ids_through_stop = [1, 2, 3, 4, 5, 6, 7, 8]
        self.adapter.after_result(SimpleNamespace(
            reqs=[business], launch_ts=4, forward_mode=decode_mode,
        ), 5)
        recovered = self.core.snapshot(5)
        self.assertEqual(recovered['decode_tokens'], 2)
        self.assertEqual(recovered['decode_sequence_seconds'], 1)

    def test_native_waiting_rejection_records_reason5_without_reservation(self):
        req = SimpleNamespace(
            rid='native-waiting-limit',
            origin_input_ids=[1],
            sampling_params=SimpleNamespace(max_new_tokens=1),
        )
        decision = self.adapter.admit_request(req, 0, waiting_count=3)
        snapshot = self.adapter.admission_snapshot()

        self.assertFalse(decision['allowed'])
        self.assertEqual(decision['reason_name'], 'waiting_limit')
        self.assertEqual(decision['projected_waiting'], 4)
        self.assertFalse(hasattr(req, 'governor_reservation'))
        self.assertEqual(snapshot['attempts'], 1)
        self.assertEqual(snapshot['rejects'], 1)
        self.assertEqual(snapshot['reject_reasons'], {'waiting_limit': 1})
        self.assertEqual(snapshot['waiting_count'], 0)
        self.assertEqual(snapshot['last_waiting_count'], 3)
        self.assertEqual(snapshot['projected_waiting'], 4)
        self.assertEqual(snapshot['outstanding'], 0)

    def test_waiting_sample_is_historical_after_reservations_drain(self):
        req = SimpleNamespace(
            rid='queued-then-finished',
            origin_input_ids=[1],
            sampling_params=SimpleNamespace(max_new_tokens=1),
        )
        self.assertTrue(self.adapter.admit_request(req, 0, waiting_count=1)['allowed'])
        self.assertTrue(self.adapter.release_request(req))

        snapshot = self.adapter.admission_snapshot()
        self.assertEqual(snapshot['outstanding'], 0)
        self.assertEqual(snapshot['active'], 0)
        self.assertEqual(snapshot['waiting_count'], 0)
        self.assertEqual(snapshot['last_waiting_count'], 1)

    def test_waiting_snapshot_requires_live_native_count_while_outstanding(self):
        req = SimpleNamespace(
            rid='still-owned',
            origin_input_ids=[1],
            sampling_params=SimpleNamespace(max_new_tokens=1),
        )
        self.assertTrue(self.adapter.admit_request(req, 0, waiting_count=1)['allowed'])
        self.assertIsNone(self.adapter.admission_snapshot()['waiting_count'])
        self.assertEqual(self.adapter.admission_snapshot(waiting_count=1)['waiting_count'], 1)
        self.assertEqual(self.adapter.admission_snapshot(waiting_count=0)['waiting_count'], 0)
        with self.assertRaises(ValueError):
            self.adapter.admission_snapshot(waiting_count=-1)
        with self.assertRaises(ValueError):
            self.adapter.admission_snapshot(waiting_count=True)
        self.assertTrue(self.adapter.release_request(req))
        self.assertEqual(self.adapter.admission_snapshot()['waiting_count'], 0)

    def test_aggregate_live_rejection_is_counted_without_reservation(self):
        core = Governor(
            50,
            max_running_requests=4,
            profile_cells=[{
                "concurrency": 2,
                "pressure_class": 0,
                "long_tokens": 56,
                "long_seconds": 1.0,
                "short_tokens": 56,
                "short_seconds": 1.0,
                "evidence_lower_tps": 56.0,
            }],
            profile_ttl_seconds=120,
            now=0,
        )
        self.addCleanup(core.close)
        adapter = SglangGovernor(core, max_running_requests=4)
        first = SimpleNamespace(
            rid='aggregate-owner', origin_input_ids=[1],
            sampling_params=SimpleNamespace(max_new_tokens=16),
        )
        self.assertTrue(adapter.admit_request(first, 0)['allowed'])
        core.observe(0, 0, 1)
        core.observe_batch(6.84046809701249, 25, 6.84046809701249, 1, 0, 1)

        second = SimpleNamespace(
            rid='aggregate-rejected', origin_input_ids=[1],
            sampling_params=SimpleNamespace(max_new_tokens=16),
        )
        decision = adapter.admit_request(second, 6.84046809701249)
        snapshot = adapter.admission_snapshot()
        self.assertFalse(decision['allowed'])
        self.assertEqual(decision['reason_name'], 'aggregate_tps_risk')
        self.assertEqual(decision['evidence_source'], 'aggregate_live')
        self.assertFalse(hasattr(second, 'governor_reservation'))
        self.assertEqual(snapshot['reject_reasons'], {'aggregate_tps_risk': 1})
        self.assertEqual(snapshot['outstanding'], 1)

    def test_pending_same_timestamp_tokens_do_not_cross_active_runs(self):
        seed = Governor(0, max_running_requests=43)
        try:
            seed.observe_surface(0, 56, 1, 2, 0)
            cells = seed.export_profile(0)
        finally:
            seed.close()
        core = Governor(
            50,
            max_running_requests=43,
            profile_cells=cells,
            profile_ttl_seconds=120,
            now=0,
        )
        self.addCleanup(core.close)
        adapter = SchedulerGovernor(core, max_running_requests=43)
        old = Progress()
        new = Progress()

        adapter.committed(old, 1, 1)
        adapter.committed(old, 1, 101)
        adapter.terminated(old, 1)
        self.assertEqual(adapter._pending_evidence, {})
        adapter.committed(new, 2, 1)
        adapter.committed(new, 3, 2)

        decision = core.admit(3, 2, 0)
        self.assertFalse(decision['allowed'])
        self.assertEqual(decision['reason'], 6)
        self.assertEqual(decision['evidence_source'], 'aggregate_live')
        self.assertEqual(decision['conservative_tps'], 1.0)

    def test_full_replacement_isolates_new_set_from_fast_retired_set(self):
        for replacement_time, initial_tokens in ((1, 1), (2, 1), (1, 3), (2, 3)):
            with self.subTest(replacement_time=replacement_time, initial_tokens=initial_tokens):
                core = Governor(50, max_running_requests=4, profile_cells=[{
                    "concurrency": 2, "pressure_class": 0,
                    "long_tokens": 56, "long_seconds": 1.0,
                    "short_tokens": 56, "short_seconds": 1.0,
                    "evidence_lower_tps": 56.0,
                }], profile_ttl_seconds=120, now=0)
                self.addCleanup(core.close)
                adapter = SchedulerGovernor(core, max_running_requests=4)
                old, new = Progress(), Progress()
                adapter.committed(old, 0, 1)
                adapter.committed(old, 1, 1001)
                adapter.commit_batch([(old, 1002, True, 0),
                                      (new, initial_tokens, False, 0)], replacement_time)
                adapter.committed(new, replacement_time + 1, initial_tokens + 1)
                decision = core.admit(replacement_time + 1, 2, 0)
                self.assertFalse(decision['allowed'])
                self.assertEqual(decision['reason'], 6)
                self.assertEqual(decision['conservative_tps'], float(initial_tokens))

    def test_partial_replacement_keeps_surviving_active_run_evidence(self):
        core = Governor(50, max_running_requests=4, profile_cells=[{
            "concurrency": 3, "pressure_class": 0,
            "long_tokens": 56, "long_seconds": 1.0,
            "short_tokens": 56, "short_seconds": 1.0,
            "evidence_lower_tps": 56.0,
        }], profile_ttl_seconds=120, now=0)
        self.addCleanup(core.close)
        adapter = SchedulerGovernor(core, max_running_requests=4)
        old, survivor, new = Progress(), Progress(), Progress()
        adapter.commit_batch([(old, 1, False, 0), (survivor, 1, False, 0)], 0)
        adapter.commit_batch([(old, 1001, False, 0), (survivor, 1001, False, 0)], 1)
        adapter.commit_batch([(old, 1001, True, 0), (new, 1, False, 0)], 1)
        adapter.committed(new, 2, 2)
        self.assertTrue(core.admit(2, 3, 0)['allowed'])

    def test_replacement_late_native_rejection_rolls_back_python_and_native(self):
        core = Governor(50, max_running_requests=4)
        self.addCleanup(core.close)
        adapter = SchedulerGovernor(core, max_running_requests=4)
        old, new = Progress(), Progress()
        adapter.committed(old, 0, 1)
        adapter.committed(old, 0, 101)
        native_before = core.snapshot(0)
        before = (tuple(getattr(old, key) for key in old.__slots__), tuple(getattr(new, key) for key in new.__slots__), adapter.active,
                  list(adapter.active_pressure_counts), dict(adapter._pending_evidence),
                  adapter._surface_time)
        original = core.observe_replacement
        def invalid_active(now, delta, duration, concurrency, pressure, active):
            return original(now, delta, duration, concurrency, pressure, 5)
        with patch.object(core, "observe_replacement", side_effect=invalid_active):
            with self.assertRaises(ValueError):
                adapter.commit_batch([(old, 102, True, 0), (new, 3, False, 0)], 1)
        self.assertEqual(core.snapshot(0), native_before)
        self.assertEqual((tuple(getattr(old, key) for key in old.__slots__), tuple(getattr(new, key) for key in new.__slots__), adapter.active,
                          list(adapter.active_pressure_counts), dict(adapter._pending_evidence),
                          adapter._surface_time), before)

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

    def test_before_prefill_uses_oldest_waiting_timestamp(self):
        waiting = [
            SimpleNamespace(time_stats=SimpleNamespace(wait_queue_entry_time=4)),
            SimpleNamespace(time_stats=SimpleNamespace(wait_queue_entry_time=7)),
        ]
        chunked = SimpleNamespace(
            time_stats=SimpleNamespace(wait_queue_entry_time=2)
        )
        with patch.object(self.adapter, "prefer_decode", return_value=True) as prefer:
            self.assertTrue(self.adapter.before_prefill(None, waiting, chunked, 10))
        prefer.assert_called_once_with(
            10,
            runnable_decode=False,
            pending_prefill=True,
            oldest_ready_age=8,
        )

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

    def test_create_with_positive_reference_learns_without_a_frozen_profile(self):
        parallel = SimpleNamespace(tp_size=1, pp_size=1, dp_size=1)
        schedule = SimpleNamespace(disable_overlap_schedule=True, max_running_requests=43)
        disagg = SimpleNamespace(disaggregation_mode='null')
        context = SimpleNamespace(
            resolved_server_args_dict=lambda: resolved_runtime()
        )
        environment = {
            'PIG_GOVERNOR_ENABLE': '1',
            'PIG_GOVERNOR_LIBRARY': self.core._lib._name,
            'PIG_TPS_REFERENCE': '50',
        }
        with patch.dict('pig_governor.sglang.os.environ', environment, clear=True), \
             patch('pig_governor.sglang.get_parallel', return_value=parallel), \
             patch('pig_governor.sglang.get_schedule', return_value=schedule), \
             patch('pig_governor.sglang.get_disagg', return_value=disagg), \
             patch('pig_governor.sglang.get_context', return_value=context):
            adapter = create(
                SimpleNamespace(model_path='/models/example'), is_generation=True
            )
            self.addCleanup(adapter.core.close)
            self.assertIsNone(adapter.loaded_profile)
            self.assertEqual(adapter.runtime_identity['schema'],
                             'phala.pig.online-runtime-identity.v1')
            self.assertEqual(adapter.runtime_identity['runtime']['max_running_requests'], 43)
            req = SimpleNamespace(
                rid='first', origin_input_ids=[1],
                sampling_params=SimpleNamespace(max_new_tokens=1),
            )
            decision = adapter.admit_request(req, time.monotonic(), waiting_count=0)
            self.assertTrue(decision['allowed'])
            self.assertEqual(decision['reason_name'], 'online_exploration')
            self.assertTrue(adapter.release_request(req))
            exported = adapter.profile_snapshot(time.monotonic())
            self.assertIsNone(exported['profile'])
            self.assertEqual(exported['availability'], 'online_identity')

    def test_empty_profile_environment_is_online_mode(self):
        parallel = SimpleNamespace(tp_size=1, pp_size=1, dp_size=1)
        schedule = SimpleNamespace(disable_overlap_schedule=True, max_running_requests=2)
        disagg = SimpleNamespace(disaggregation_mode='null')
        context = SimpleNamespace(
            resolved_server_args_dict=lambda: resolved_runtime(max_running_requests=2)
        )
        environment = {
            'PIG_GOVERNOR_ENABLE': '1',
            'PIG_GOVERNOR_LIBRARY': self.core._lib._name,
            'PIG_TPS_REFERENCE': '50',
            'PIG_TPS_PROFILE_PATH': '',
            'PIG_TPS_PROFILE_SHA256': '',
        }
        with patch.dict('pig_governor.sglang.os.environ', environment, clear=True), \
             patch('pig_governor.sglang.get_parallel', return_value=parallel), \
             patch('pig_governor.sglang.get_schedule', return_value=schedule), \
             patch('pig_governor.sglang.get_disagg', return_value=disagg), \
             patch('pig_governor.sglang.get_context', return_value=context):
            adapter = create(SimpleNamespace(model_path='model'), is_generation=True)
        self.addCleanup(adapter.core.close)
        self.assertIsNone(adapter.loaded_profile)
        self.assertEqual(adapter.runtime_identity['schema'],
                         'phala.pig.online-runtime-identity.v1')

    def test_online_exploration_is_bounded_and_learned_risk_rejects(self):
        core = Governor(50, max_running_requests=3)
        self.addCleanup(core.close)
        adapter = SglangGovernor(core, max_running_requests=3)
        def req(rid):
            return SimpleNamespace(rid=rid, origin_input_ids=[1],
                                   sampling_params=SimpleNamespace(max_new_tokens=1))
        first = req('first')
        self.assertEqual(adapter.admit_request(first, 0, waiting_count=0)['reason'], 7)
        blocked = req('blocked')
        self.assertEqual(adapter.admit_request(blocked, 0.1, waiting_count=0)['reason'], 4)
        self.assertFalse(hasattr(blocked, 'governor_reservation'))
        self.assertTrue(adapter.release_request(first))
        self.assertEqual(adapter.outstanding, 0)
        self.assertEqual(adapter.admit_request(req('cooldown'), 0.2, waiting_count=0)['reason'], 4)
        core.observe_surface(0.3, 8, 0.2, 1, 0)
        self.assertEqual(adapter.admit_request(req('slow'), 0.3, waiting_count=0)['reason'], 2)
        self.assertEqual(adapter.outstanding, 0)

    def test_online_exploration_expands_one_cell_at_a_time(self):
        core = Governor(50, max_running_requests=3)
        self.addCleanup(core.close)
        adapter = SglangGovernor(core, max_running_requests=3)
        def req(rid):
            return SimpleNamespace(rid=rid, origin_input_ids=[1],
                                   sampling_params=SimpleNamespace(max_new_tokens=1))
        first = req('first')
        decision = adapter.admit_request(first, 0, waiting_count=0)
        self.assertEqual(decision['reason'], 7)
        decision['reason'] = 4
        self.assertTrue(adapter.release_request(first))
        self.assertFalse(adapter.release_request(first))
        core.observe_surface(0.2, 20, 0.2, 1, 0)
        fit = req('fit')
        self.assertEqual(adapter.admit_request(fit, 2.1, waiting_count=0)['reason'], 0)
        probe = req('probe')
        self.assertEqual(adapter.admit_request(probe, 2.1, waiting_count=0)['reason'], 7)
        self.assertEqual(adapter.admit_request(req('third'), 2.1, waiting_count=0)['reason'], 4)
        self.assertTrue(adapter.release_request(probe))
        self.assertTrue(adapter.release_request(fit))
        self.assertEqual(adapter.outstanding, 0)
        self.assertEqual(adapter.admitted_pressure_counts, [0, 0, 0, 0])

    def test_prior_only_lower_cell_does_not_authorize_higher_exploration(self):
        core = Governor(
            50, max_running_requests=2,
            profile_cells=[{
                'concurrency': 1, 'pressure_class': 0,
                'long_tokens': 100, 'long_seconds': 1.0,
                'short_tokens': 100, 'short_seconds': 1.0,
                'evidence_lower_tps': 100.0,
            }], profile_ttl_seconds=120, now=0,
        )
        self.addCleanup(core.close)
        adapter = SglangGovernor(core, max_running_requests=2)
        def req(rid):
            return SimpleNamespace(rid=rid, origin_input_ids=[1],
                                   sampling_params=SimpleNamespace(max_new_tokens=1))
        first = req('prior')
        self.assertEqual(adapter.admit_request(first, 1, waiting_count=0)['reason'], 3)
        second = req('higher')
        decision = adapter.admit_request(second, 1, waiting_count=0)
        self.assertFalse(decision['allowed'])
        self.assertEqual(decision['reason'], 4)
        self.assertFalse(hasattr(second, 'governor_reservation'))
        self.assertTrue(adapter.release_request(first))

    def test_online_exploration_retries_after_cooldown_without_learning(self):
        core = Governor(50, max_running_requests=1)
        self.addCleanup(core.close)
        adapter = SglangGovernor(core, max_running_requests=1)
        def req(rid):
            return SimpleNamespace(rid=rid, origin_input_ids=[1],
                                   sampling_params=SimpleNamespace(max_new_tokens=1))
        first = req('first')
        self.assertEqual(adapter.admit_request(first, 0, waiting_count=0)['reason'], 7)
        self.assertTrue(adapter.release_request(first))
        self.assertEqual(adapter.admit_request(req('early'), 1.9, waiting_count=0)['reason'], 4)
        self.assertEqual(adapter.admit_request(req('queued'), 2, waiting_count=1)['reason'], 4)
        retry = req('retry')
        self.assertEqual(adapter.admit_request(retry, 2, waiting_count=0)['reason'], 7)
        self.assertTrue(adapter.release_request(retry))

    def test_create_accepts_only_non_decreasing_profile_capacity(self):
        parallel = SimpleNamespace(tp_size=1, pp_size=1, dp_size=1)
        schedule = SimpleNamespace(disable_overlap_schedule=True, max_running_requests=43)
        disagg = SimpleNamespace(disaggregation_mode='null')
        profile_identity = build_runtime_identity(
            resolved_runtime(max_total_tokens=954291), environ=IDENTITY_ENV
        )
        cells = [
            {
                'concurrency': concurrency,
                'pressure_class': pressure,
                'long_tokens': 100,
                'long_seconds': 1.0,
                'short_tokens': 100,
                'short_seconds': 1.0,
                'evidence_lower_tps': 100.0,
            }
            for concurrency in range(1, 44)
            for pressure in range(4)
        ]
        document = build_profile_document(profile_identity, 43, cells)
        data = canonical_profile_bytes(document)
        digest = hashlib.sha256(data).hexdigest()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, 'profile.json')
            path.write_bytes(data)
            environment = {
                **IDENTITY_ENV,
                'PIG_GOVERNOR_ENABLE': '1',
                'PIG_GOVERNOR_LIBRARY': self.core._lib._name,
                'PIG_TPS_REFERENCE': '50',
                'PIG_TPS_PROFILE_PATH': str(path),
                'PIG_TPS_PROFILE_SHA256': digest,
                'PIG_MAX_RUNNING': '43',
                'PIG_MAX_WAITING': '3',
            }

            for current_tokens, accepted in ((954454, True), (954290, False)):
                context = SimpleNamespace(
                    resolved_server_args_dict=lambda value=current_tokens: resolved_runtime(
                        max_total_tokens=value
                    )
                )
                with self.subTest(current_tokens=current_tokens), patch.dict(
                    'pig_governor.sglang.os.environ', environment, clear=True
                ), patch(
                    'pig_governor.sglang.get_parallel', return_value=parallel
                ), patch(
                    'pig_governor.sglang.get_schedule', return_value=schedule
                ), patch(
                    'pig_governor.sglang.get_disagg', return_value=disagg
                ), patch(
                    'pig_governor.sglang.get_context', return_value=context
                ):
                    if not accepted:
                        with self.assertRaisesRegex(
                            ValueError, 'profile requires at least 954291'
                        ):
                            create(SimpleNamespace(), is_generation=True)
                        continue
                    adapter = create(SimpleNamespace(), is_generation=True)
                    self.addCleanup(adapter.core.close)
                    self.assertEqual(
                        adapter.loaded_profile.metadata['profile_max_total_tokens'],
                        954291,
                    )
                    self.assertEqual(
                        adapter.loaded_profile.metadata['current_max_total_tokens'],
                        954454,
                    )
                    policy = adapter.policy_snapshot(time.monotonic())
                    self.assertEqual(
                        policy['profile_bootstrap']['profile_max_total_tokens'],
                        954291,
                    )
                    self.assertEqual(
                        policy['profile_bootstrap']['current_max_total_tokens'],
                        954454,
                    )

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

    def test_inflight_old_decode_cannot_populate_new_identity_surface(self):
        state = resolved_runtime(weight_version='v0', max_running_requests=2)
        def provider():
            return build_online_runtime_identity(state, {}, model_locator='model')
        core = Governor(50, max_running_requests=2)
        self.addCleanup(core.close)
        adapter = SglangGovernor(
            core, max_running_requests=2,
            runtime_identity=provider(), identity_provider=provider,
        )
        req = SimpleNamespace(
            rid='old', origin_input_ids=[1],
            sampling_params=SimpleNamespace(max_new_tokens=64),
            output_ids_through_stop=[], finished_reason=None,
            finished=lambda: False, to_finish=None,
        )
        self.assertEqual(adapter.admit_request(req, 0, waiting_count=0)['reason'], 7)
        batch = SimpleNamespace(
            reqs=[req], launch_ts=0,
            forward_mode=SimpleNamespace(is_extend_without_speculative=lambda: False),
        )
        req.output_ids_through_stop = [1]
        adapter.after_result(batch, 1)
        old_epoch = core.epoch
        old_identity = adapter.runtime_identity

        state['weight_version'] = 'v1'
        req.output_ids_through_stop = list(range(21))
        adapter.after_result(batch, 2)
        self.assertEqual(core.epoch, old_epoch)
        self.assertEqual(adapter.runtime_identity, old_identity)
        policy = adapter.policy_snapshot(2)
        self.assertTrue(policy['identity_transition']['pending'])
        with self.assertRaises(RevisionConflict):
            adapter.update_policy(
                2, expected_epoch=old_epoch,
                expected_revision=policy['revision'], tps_reference=40,
            )
        self.assertIsNone(adapter.profile_snapshot(2))
        newcomer = SimpleNamespace(
            rid='new', origin_input_ids=[1],
            sampling_params=SimpleNamespace(max_new_tokens=1),
        )
        blocked = adapter.admit_request(newcomer, 2, waiting_count=0)
        self.assertEqual(blocked['reason_name'], 'identity_transition')
        self.assertFalse(blocked['allowed'])
        self.assertFalse(hasattr(newcomer, 'governor_reservation'))

        req.finished = lambda: True
        adapter.after_result(batch, 3)
        self.assertEqual(adapter.outstanding, 0)
        self.assertEqual(adapter.active, 0)
        self.assertTrue(adapter.refresh_identity(3.1))
        self.assertNotEqual(core.epoch, old_epoch)
        self.assertEqual(adapter.runtime_identity['runtime']['weight_version'], 'v1')
        self.assertEqual(core.snapshot(3.1)['decode_tokens'], 0)
        self.assertEqual(core.export_profile(3.1), [])
        self.assertIsNone(adapter.policy_snapshot(3.1)['identity_transition'])

    def test_identity_transition_drains_aborted_request(self):
        state = resolved_runtime(weight_version='v0', max_running_requests=1)
        def provider():
            return build_online_runtime_identity(state, {})
        core = Governor(0, max_running_requests=1)
        self.addCleanup(core.close)
        adapter = SglangGovernor(
            core, max_running_requests=1,
            runtime_identity=provider(), identity_provider=provider,
        )
        req = SimpleNamespace(
            rid='old', origin_input_ids=[1],
            sampling_params=SimpleNamespace(max_new_tokens=64),
            output_ids_through_stop=[1], finished_reason=None,
            finished=lambda: False, to_finish=None,
        )
        self.assertTrue(adapter.admit_request(req, 0, waiting_count=0)['allowed'])
        batch = SimpleNamespace(
            reqs=[req], launch_ts=0,
            forward_mode=SimpleNamespace(is_extend_without_speculative=lambda: False),
        )
        adapter.after_result(batch, 1)
        old_epoch = core.epoch
        state['weight_version'] = 'v1'
        self.assertFalse(adapter.refresh_identity(2))
        with patch('pig_governor.sglang.time.monotonic', return_value=3):
            on_abort_emitted(req)
            on_abort_emitted(req)
        self.assertEqual((adapter.active, adapter.outstanding), (0, 0))
        self.assertTrue(adapter.refresh_identity(3.1))
        self.assertNotEqual(core.epoch, old_epoch)
        self.assertEqual(core.snapshot(3.1)['decode_tokens'], 0)

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
