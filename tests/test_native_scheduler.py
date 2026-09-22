"""Actual SGLang methods and msgspec wire types with a real Rust controller."""
from array import array
import copy
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
import msgspec
from sglang.test.test_utils import maybe_stub_sgl_kernel

maybe_stub_sgl_kernel()
from sglang.srt.managers.scheduler import Scheduler, _make_abort_req
from sglang.srt.managers.io_struct import AbortReq, SetInternalStateReq
from sglang.srt.disaggregation.utils import DisaggregationMode
from sglang.srt.mem_cache.base_prefix_cache import (
    CacheRequestHandle,
    CacheRequestOutcome,
)
from sglang.srt.managers.schedule_batch import FINISH_ABORT, Req
from sglang.srt.sampling.sampling_params import SamplingParams
from pig_governor import Governor
from pig_governor.sglang import SglangGovernor


class NativeSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.core = Governor(35, max_running_requests=43)
        self.addCleanup(self.core.close)
        self.sched = object.__new__(Scheduler)
        self.sched.governor = SglangGovernor(self.core, max_running_requests=43)

    def test_real_msgspec_cas_dispatch(self):
        before = self.core.snapshot(time.monotonic())
        request = SetInternalStateReq(server_args={'pig_governor': {
            'expected_epoch': self.core.epoch, 'expected_revision': 1, 'tps_reference': 50}}, control_nonce='test-control-nonce')
        decoded = msgspec.msgpack.decode(msgspec.msgpack.encode(request), type=SetInternalStateReq)
        result = self.sched.set_internal_state(decoded)
        self.assertTrue(result.updated)
        self.assertEqual(result.control_nonce, request.control_nonce)
        self.assertFalse(self.sched.set_internal_state(decoded).updated)
        state = self.core.snapshot(time.monotonic())
        self.assertEqual(state['revision'], 2)
        self.assertEqual(state['decode_tokens'], before['decode_tokens'])

    def test_real_defer_hook_keeps_native_physical_fallback(self):
        self.sched.waiting_queue = []
        self.sched.chunked_req = None
        self.sched._prefill_decode_interval_remaining = 0
        self.assertFalse(self.sched._should_defer_prefill(None))
        self.assertEqual(self.core.snapshot(time.monotonic())['active_decode_sequences'], 0)

    def test_queued_abort_releases_mm_without_plugin_resource_ownership(self):
        # The original native abort routine owns feature cleanup. A queued
        # request has not entered Decode and therefore has no Governor state.
        handle = CacheRequestHandle(rid='queued', attempt_id=1)
        req = SimpleNamespace(rid='queued', multimodal_inputs=Mock(), session=None,
            kv=SimpleNamespace(holds_mamba=False), weight_version_events=[], output_ids=[],
            cache_request_handle=handle)
        mm = req.multimodal_inputs
        sched = self.sched
        sched.chunked_req = None
        sched.mm_receiver = None
        sched.waiting_queue = [req]
        sched.beam_coordinator = Mock()
        sched.enable_hicache_storage = False
        sched.tree_cache = Mock()
        sched.ipc_channels = SimpleNamespace(send_to_tokenizer=Mock())
        sched.disaggregation_mode = DisaggregationMode.NULL
        sched.dllm_config = None
        sched.grammar_manager = Mock()
        sched.collect_inflight_reqs = lambda: []
        with patch('sglang.srt.managers.scheduler.get_serving', return_value=SimpleNamespace(weight_version='v0')):
            sched.abort_request(AbortReq(
                rid='queued',
                finished_reason={
                    'type': 'abort',
                    'status_code': 499,
                    'message': 'client disconnected',
                },
            ))
        mm.release_features.assert_called_once()
        sched.tree_cache.finish.assert_called_once_with(handle, CacheRequestOutcome.ABORT)
        self.assertIsNone(req.multimodal_inputs)
        self.assertEqual(sched.waiting_queue, [])

    def _chunked_req(self, *, session=None):
        sampling_params = SamplingParams(max_new_tokens=1)
        sampling_params.normalize(None)
        req = Req(
            rid='chunked',
            origin_input_text='',
            origin_input_ids=array('q', [1]),
            sampling_params=sampling_params,
            session=session,
        )
        req.multimodal_inputs = Mock()
        return req

    def test_pending_chunked_abort_releases_mm_only_at_nonoverlap_non_session_boundary(self):
        cases = (
            ('nonoverlap', False, None, True),
            ('overlap-outstanding-result', True, None, False),
            ('session-owned', False, object(), False),
        )
        for name, enable_overlap, session, release_mm in cases:
            with self.subTest(name=name):
                req = self._chunked_req(session=session)
                mm = req.multimodal_inputs
                sched = self.sched
                sched.chunked_req = req
                sched._pending_chunked_abort_req = req
                sched.enable_overlap = enable_overlap
                sched.disaggregation_mode = DisaggregationMode.NULL
                sched.tree_cache = Mock()
                sched.tree_cache.supports_mamba.return_value = True
                sched.ipc_channels = SimpleNamespace(send_to_tokenizer=Mock())
                with patch(
                    'sglang.srt.managers.scheduler.get_serving',
                    return_value=SimpleNamespace(weight_version='v0'),
                ):
                    sched.process_pending_chunked_abort()

                self.assertIsInstance(req.finished_reason, FINISH_ABORT)
                sched.tree_cache.finish.assert_called_once_with(
                    req.cache_request_handle, CacheRequestOutcome.ABORT
                )
                # This call originates in the real release_kv_cache path; the
                # no-KV fixture uses the Mamba-capable early-release branch.
                sched.tree_cache.supports_mamba.assert_called_once_with()
                self.assertIsNone(sched.chunked_req)
                self.assertIsNone(sched._pending_chunked_abort_req)
                if release_mm:
                    mm.release_features.assert_called_once_with()
                    self.assertIsNone(req.multimodal_inputs)
                else:
                    mm.release_features.assert_not_called()
                    self.assertIs(req.multimodal_inputs, mm)


class GovernorHookTests(unittest.TestCase):
    def _scheduler(self):
        sched = object.__new__(Scheduler)
        sched.governor = object()
        sched.disaggregation_mode = DisaggregationMode.NULL
        sched.waiting_queue = []
        sched.chunked_req = None
        sched.grammar_manager = MagicMock()
        sched.grammar_manager.__len__.return_value = 0
        sched.processed_tokens_counter = 0
        sched._set_or_validate_priority = lambda req: True
        sched._abort_on_queued_limit = lambda req: False
        sched._prefetch_kvcache = lambda req: None
        return sched

    def _native_request_scheduler(self):
        sched = self._scheduler()
        sched.model_config = SimpleNamespace(hf_eos_token_id=2, vocab_size=32000)
        sched.metrics_reporter = SimpleNamespace(enable_metrics=False)
        sched.tokenizer = object()
        sched.enable_session_radix_cache = False
        sched.dllm_config = None
        sched.output_streamer = SimpleNamespace(stream_output=Mock())
        sched.beam_coordinator = SimpleNamespace(validate_and_init=Mock())
        return sched

    def _generate_input(self):
        return SimpleNamespace(
            rid='request', session_params=None, session_id=None, bootstrap_port=1,
            input_embeds=None, input_text='', input_ids=array('q', [1]),
            sampling_params=SimpleNamespace(), return_logprob=False,
            top_logprobs_num=0, token_ids_logprob=None,
            return_sampling_mask=False, return_flat_raw_top_logprobs=False,
            stream=False, lora_id=None, positional_embed_overrides=None,
            token_type_ids=None, custom_logit_processor=None,
            require_reasoning=False, return_hidden_states=False,
            return_routed_experts=False, routed_experts_start_len=0,
            return_indexer_topk=None,
            bootstrap_host=None, bootstrap_room=None, routed_dp_rank=None,
            disagg_prefill_dp_rank=None, priority=None, routing_key=None,
            extra_key=None, cache_salt=None, http_worker_ipc=None,
            time_stats=None, multi_item_delimiter_indices=None,
            mm_inputs=None, logprob_start_len=-1,
        )

    def _embedding_input(self):
        return SimpleNamespace(
            rid='embedding', input_text='', input_ids=array('q', [1]),
            sampling_params=SimpleNamespace(), positional_embed_overrides=None,
            token_type_ids=None, routed_dp_rank=None, priority=None,
            dimensions=None, lora_id=None, http_worker_ipc=None,
            time_stats=None, return_pooled_hidden_states=False,
            multi_item_delimiter_indices=None,
        )

    def test_governor_rejects_beam_before_coordinator_or_admission(self):
        sched = self._native_request_scheduler()
        sched.governor = Mock()
        req = SimpleNamespace(tokenizer=None, return_logprob=False, beam_group=None)
        recv_req = self._generate_input()

        with patch(
            'sglang.srt.managers.scheduler.Req', return_value=req
        ), patch(
            'sglang.srt.managers.scheduler.BeamCoordinator.request_beam_width',
            return_value=2,
        ), patch('sglang.srt.managers.scheduler.prepare_abort') as prepare_abort:
            sched.handle_generate_request(recv_req)

        prepare_abort.assert_called_once()
        self.assertEqual(prepare_abort.call_args.kwargs['status_code'], 400)
        sched.beam_coordinator.validate_and_init.assert_not_called()
        sched.governor.admit_request.assert_not_called()
        self.assertIsNone(req.beam_group)
        self.assertIsNone(getattr(req, 'governor_reservation', None))
        self.assertEqual(sched.waiting_queue, [])

    def test_generation_scheduler_rejects_embedding_without_queueing(self):
        sched = self._native_request_scheduler()
        sched.governor = Mock()
        trace = SimpleNamespace(abort=Mock())
        req = SimpleNamespace(
            tokenizer=None,
            return_logprob=False,
            time_stats=SimpleNamespace(trace_ctx=trace),
        )

        with patch(
            'sglang.srt.managers.scheduler.Req', return_value=req
        ), patch('sglang.srt.managers.scheduler.prepare_abort') as prepare_abort:
            sched.handle_embedding_request(self._embedding_input())

        prepare_abort.assert_called_once()
        self.assertEqual(prepare_abort.call_args.kwargs['status_code'], 400)
        sched.governor.admit_request.assert_not_called()
        self.assertEqual(sched.waiting_queue, [])

    def _admission_handoff(self):
        sched = self._native_request_scheduler()
        core = Governor(0, max_running_requests=43)
        self.addCleanup(core.close)
        sched.governor = SglangGovernor(core, max_running_requests=43)
        sched.session_controller = {}
        sched.spec_algorithm = SimpleNamespace(
            is_dflash_family=lambda: False,
            is_uno=lambda: False,
            is_none=lambda: True,
        )
        sched._maybe_namespace_elastic_radix_cache = lambda req: None
        sched.init_req_max_new_tokens = lambda req: None
        sched.enable_priority_scheduling = False
        sched.abort_on_priority_when_disabled = False
        sched.max_queued_requests = None
        sched.max_req_input_len = 1024
        sched.enable_hicache_storage = False
        sched.processed_tokens_counter = 0
        req = SimpleNamespace(
            rid='admitted', origin_input_ids=array('q', [1]),
            sampling_params=SimpleNamespace(max_new_tokens=1, top_k=1),
            return_sampling_mask=False, return_logprob=False,
            logprob_start_len=-1, is_prefill_only=False,
            finished_reason=None, to_finish=None, finished=lambda: False,
            time_stats=SimpleNamespace(set_wait_queue_entry_time=Mock()),
            output_ids=[], weight_version_events=[], tokenizer=None,
        )
        return sched, req, self._generate_input()

    def _run_admission_handoff(self, sched, req, recv_req):
        with patch(
            'sglang.srt.managers.scheduler.Req', return_value=req
        ), patch(
            'sglang.srt.managers.scheduler.BeamCoordinator.request_beam_width',
            return_value=1,
        ), patch(
            'sglang.srt.managers.scheduler.validate_input_length', return_value=None
        ), patch(
            'sglang.srt.managers.scheduler.get_serving',
            return_value=SimpleNamespace(
                allow_auto_truncate=False, weight_version='v0'
            ),
        ), patch(
            'sglang.srt.managers.scheduler.get_device',
            return_value=SimpleNamespace(mlx_enable_sampling=False),
        ):
            sched.handle_generate_request(recv_req)

    def test_cold_native_burst_rejects_fourth_before_queue_handoffs(self):
        sched, template_req, template_recv = self._admission_handoff()
        sched.grammar_manager.process_req_with_grammar.return_value = False
        sched._prefetch_kvcache = Mock()
        output = Mock()
        sched.ipc_channels = SimpleNamespace(
            send_to_tokenizer=SimpleNamespace(send_output=output)
        )
        accepted = []
        rejected = None

        for index in range(4):
            req = copy.copy(template_req)
            recv_req = copy.copy(template_recv)
            req.rid = recv_req.rid = f'cold-burst-{index}'
            req.session = None
            req.multimodal_inputs = None
            req.http_worker_ipc = None
            req.time_stats = SimpleNamespace(
                set_wait_queue_entry_time=Mock(),
                trace_ctx=SimpleNamespace(abort=Mock()),
            )
            with patch(
                'sglang.srt.managers.scheduler.time.monotonic', return_value=1.0
            ):
                self._run_admission_handoff(sched, req, recv_req)
            if index < 3:
                accepted.append(req)
            else:
                rejected = req

        self.assertEqual(sched.waiting_queue, accepted)
        self.assertEqual(sched.governor.outstanding, 3)
        self.assertEqual(sched.governor.last_admission['reason'], 5)
        self.assertEqual(
            sched.governor.last_admission['reason_name'], 'waiting_limit'
        )
        self.assertEqual(sched.governor.last_admission['projected_waiting'], 4)
        output.assert_called_once()
        abort, emitted_req = output.call_args.args
        self.assertIs(emitted_req, rejected)
        self.assertEqual(abort.finished_reason['status_code'], 429)
        self.assertIsNone(getattr(rejected, 'governor_reservation', None))
        rejected.time_stats.set_wait_queue_entry_time.assert_not_called()
        self.assertEqual(
            sched.grammar_manager.process_req_with_grammar.call_count, 3
        )
        self.assertEqual(sched._prefetch_kvcache.call_count, 3)

    def test_admitted_grammar_handoff_exception_releases_exactly_once(self):
        sched, req, recv_req = self._admission_handoff()
        sched.grammar_manager.process_req_with_grammar.side_effect = RuntimeError('grammar')
        sched.governor.release_request = Mock(wraps=sched.governor.release_request)

        with self.assertRaisesRegex(RuntimeError, 'grammar'):
            self._run_admission_handoff(sched, req, recv_req)

        sched.governor.release_request.assert_called_once_with(req)
        self.assertEqual(sched.governor.outstanding, 0)

    def test_admitted_prefetch_exception_releases_exactly_once(self):
        sched, req, recv_req = self._admission_handoff()
        sched.grammar_manager.process_req_with_grammar.return_value = False
        sched._prefetch_kvcache = Mock(side_effect=RuntimeError('prefetch'))
        sched.governor.release_request = Mock(wraps=sched.governor.release_request)

        with self.assertRaisesRegex(RuntimeError, 'prefetch'):
            self._run_admission_handoff(sched, req, recv_req)

        sched.governor.release_request.assert_called_once_with(req)
        self.assertEqual(sched.governor.outstanding, 0)
        self.assertEqual(sched.waiting_queue, [])

    def test_admitted_native_queue_rejections_release_without_leak(self):
        for rejection in ('priority', 'queued_limit'):
            with self.subTest(rejection=rejection):
                sched, req, recv_req = self._admission_handoff()
                sched.grammar_manager.process_req_with_grammar.return_value = False
                if rejection == 'priority':
                    sched._set_or_validate_priority = Mock(return_value=False)
                else:
                    sched._abort_on_queued_limit = Mock(return_value=True)
                sched.governor.release_request = Mock(
                    wraps=sched.governor.release_request
                )

                self._run_admission_handoff(sched, req, recv_req)

                sched.governor.release_request.assert_called_once_with(req)
                self.assertEqual(sched.governor.outstanding, 0)
                self.assertEqual(sched.waiting_queue, [])

    def test_add_request_to_queue_requires_governor_reservation(self):
        sched = self._scheduler()
        req = SimpleNamespace(
            rid='admitted',
            finished=lambda: False,
            time_stats=SimpleNamespace(set_wait_queue_entry_time=lambda: None),
            governor_reservation=object(),
        )
        sched._add_request_to_queue(req)
        self.assertIn(req, sched.waiting_queue)

        unadmitted = SimpleNamespace(
            rid='unadmitted',
            finished=lambda: False,
            time_stats=SimpleNamespace(set_wait_queue_entry_time=lambda: None),
        )
        with self.assertRaisesRegex(RuntimeError, 'without Governor reservation'):
            sched._add_request_to_queue(unadmitted)

    def test_native_validation_abort_bypasses_governor_reservation(self):
        sched = self._scheduler()
        sampling_params = SamplingParams(max_new_tokens=1)
        sampling_params.normalize(None)
        req = Req(
            rid='invalid-before-admission',
            origin_input_text='',
            origin_input_ids=array('q', [1]),
            sampling_params=sampling_params,
        )
        with patch(
            'sglang.srt.managers.schedule_batch.get_parallel',
            return_value=SimpleNamespace(tp_rank=1),
        ):
            req.set_finish_with_abort('prompt is too long')

        self.assertFalse(req.finished())
        self.assertIsInstance(req.to_finish, FINISH_ABORT)
        sched._add_request_to_queue(req)
        self.assertIn(req, sched.waiting_queue)
        self.assertIsNone(getattr(req, 'governor_reservation', None))

    def test_embedding_prefill_only_request_bypasses_generation_reservation(self):
        sched = self._scheduler()
        req = SimpleNamespace(
            rid='embedding',
            is_prefill_only=True,
            finished=lambda: False,
            time_stats=SimpleNamespace(set_wait_queue_entry_time=lambda: None),
        )
        sched._add_request_to_queue(req)
        self.assertIn(req, sched.waiting_queue)
        self.assertIsNone(getattr(req, 'governor_reservation', None))

    def test_abort_releases_queued_reservation_without_progress(self):
        core = Governor(0, max_running_requests=43)
        self.addCleanup(core.close)
        governor = SglangGovernor(core, max_running_requests=43)
        req = SimpleNamespace(
            rid='queued',
            origin_input_ids=[1],
            sampling_params=SimpleNamespace(max_new_tokens=1),
            finished=lambda: False,
            output_ids=[],
            weight_version_events=[],
        )
        governor.admit_request(req, 1)
        self.assertEqual(governor.outstanding, 1)
        with patch(
            'sglang.srt.managers.scheduler.get_serving',
            return_value=SimpleNamespace(weight_version='v0'),
        ):
            _make_abort_req(req)
        self.assertEqual(governor.outstanding, 0)
        self.assertIsNone(getattr(req, 'governor_progress', None))

    def test_governor_admission_reject_returns_429_before_queue_insertion(self):
        cases = (
            ('tps_risk', 2, 'The request is rejected by Governor TPS admission.'),
            ('waiting_limit', 5, 'The request is rejected by the Governor waiting limit.'),
        )
        for reason_name, reason, message in cases:
            with self.subTest(reason=reason_name):
                sched = self._scheduler()
                sent = []
                sched._prefetch_kvcache = Mock()
                sched.grammar_manager = MagicMock()
                sched.grammar_manager.__len__.return_value = 0
                sched.tp_worker = Mock()
                sched.ipc_channels = SimpleNamespace(
                    send_to_tokenizer=SimpleNamespace(
                        send_output=lambda abort, req: sent.append((abort, req))
                    )
                )
                sched.governor = SimpleNamespace(
                    admit_request=lambda req, now, waiting_count: {
                        'allowed': False,
                        'reason': reason,
                        'reason_name': reason_name,
                        'projected_tps': 10.0,
                        'reference': 50.0,
                        'projected_concurrency': 4,
                        'projected_waiting': 4,
                        'max_running': 4,
                        'max_waiting': 3,
                        'pressure_class': 0,
                        'active_decode_sequences': 0,
                    }
                )
                sampling_params = SamplingParams(max_new_tokens=1)
                sampling_params.normalize(None)
                req = Req(
                    rid=reason_name,
                    origin_input_text='',
                    origin_input_ids=array('q', [1]),
                    sampling_params=sampling_params,
                )
                req.multimodal_inputs = Mock()
                features = req.multimodal_inputs
                req.time_stats.trace_ctx = SimpleNamespace(abort=Mock())
                with patch(
                    'sglang.srt.managers.scheduler.get_serving',
                    return_value=SimpleNamespace(weight_version='v0'),
                ), patch(
                    'sglang.srt.managers.scheduler.time.monotonic',
                    return_value=1.0,
                ):
                    self.assertTrue(sched._abort_on_governor_admission(req))
                self.assertEqual(len(sent), 1)
                self.assertEqual(sent[0][0].finished_reason['status_code'], 429)
                self.assertEqual(sent[0][0].finished_reason['message'], message)
                features.release_features.assert_called_once_with()
                self.assertIsNone(req.multimodal_inputs)
                sched._prefetch_kvcache.assert_not_called()
                sched.grammar_manager.process_req_with_grammar.assert_not_called()
                sched.tp_worker.assert_not_called()
                self.assertEqual(sched.waiting_queue, [])

    def test_governor_admission_counts_every_native_waiting_owner(self):
        sched = self._scheduler()
        sched.waiting_queue = [object(), object()]
        sched.grammar_manager.__len__.return_value = 1
        sched.chunked_req = object()
        captured = {}

        def admit_request(req, now, *, waiting_count):
            captured["waiting_count"] = waiting_count
            return {"allowed": True}

        sched.governor = SimpleNamespace(admit_request=admit_request)
        self.assertFalse(
            sched._abort_on_governor_admission(SimpleNamespace(rid="candidate"))
        )
        self.assertEqual(captured["waiting_count"], 4)


if __name__ == '__main__': unittest.main()
