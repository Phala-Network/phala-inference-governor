"""Actual SGLang methods and msgspec wire types with a real Rust controller."""
from array import array
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
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
        sched.processed_tokens_counter = 0
        sched._set_or_validate_priority = lambda req: True
        sched._abort_on_queued_limit = lambda req: False
        sched._prefetch_kvcache = lambda req: None
        return sched

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
            'sglang.srt.managers.scheduler.compute_weight_version_spans',
            return_value=[],
        ):
            _make_abort_req(req)
        self.assertEqual(governor.outstanding, 0)
        self.assertIsNone(getattr(req, 'governor_progress', None))

    def test_governor_admission_reject_returns_429_before_queue_insertion(self):
        sched = self._scheduler()
        sent = []
        sched.ipc_channels = SimpleNamespace(
            send_to_tokenizer=SimpleNamespace(send_output=lambda abort, req: sent.append((abort, req)))
        )
        sched.governor = SimpleNamespace(
            admit_request=lambda req, now: {
                'allowed': False,
                'projected_tps': 10.0,
                'reference': 50.0,
                'projected_concurrency': 4,
                'pressure_class': 0,
                'active_decode_sequences': 0,
            }
        )
        req = SimpleNamespace(
            rid='risk',
            output_ids=[],
            finished=lambda: False,
            time_stats=SimpleNamespace(trace_ctx=SimpleNamespace(abort=lambda abort_info: None)),
        )
        with patch('sglang.srt.managers.scheduler.time.monotonic', return_value=1.0):
            self.assertTrue(sched._abort_on_governor_admission(req))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0].finished_reason['status_code'], 429)
        self.assertEqual(sched.waiting_queue, [])


if __name__ == '__main__': unittest.main()
