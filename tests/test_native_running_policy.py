"""Real Scheduler selection/CAS; only GPU allocation and telemetry are faked."""
import time
import unittest
from contextlib import ExitStack
from types import SimpleNamespace as NS
from unittest.mock import Mock, PropertyMock, patch

from sglang.test.test_utils import maybe_stub_sgl_kernel

maybe_stub_sgl_kernel()
from sglang.srt.managers.scheduler import Scheduler, AddReqResult
from sglang.srt.managers.schedule_policy import PrefillAdder
from sglang.srt.managers.io_struct import SetInternalStateReq
from sglang.srt.disaggregation.utils import DisaggregationMode
from pig_governor import Governor
from pig_governor.sglang import SglangGovernor


class NativeRunningPolicyTests(unittest.TestCase):
    def setUp(self):
        self.core = Governor(0, max_running_requests=43)
        self.addCleanup(self.core.close)
        s = self.s = object.__new__(Scheduler)
        s.governor = SglangGovernor(self.core, max_running_requests=43)
        s.max_running_requests = 43
        s.running_batch = Mock(reqs=[], batch_is_full=False)
        s.req_to_token_pool = Mock()
        s.req_to_token_pool.available_size.return_value = 43
        s.req_to_token_pool.mamba_allocator = None
        s.beam_coordinator = Mock()
        s.beam_coordinator.pending_member_rows.return_value = 0
        s.grammar_manager = Mock()
        s.grammar_manager.has_waiting_grammars.return_value = False
        for field in ("enable_priority_preemption", "is_hybrid_swa",
                      "is_mixed_chunk", "enable_lora", "enable_hicache_storage",
                      "enable_hierarchical_cache", "enable_unified_cache_external_linker",
                      "enable_overlap", "enable_priority_scheduling"):
            setattr(s, field, False)
        for field in ("min_free_slots_delayer", "chunked_req", "dynamic_chunk_sizer",
                      "dllm_config", "truncation_align_size"):
            setattr(s, field, None)
        s.waiting_queue = []
        s.policy = Mock()
        s.processed_tokens_counter = 0
        s.chunked_prefill_size = 128
        s.page_size = 1
        s.tree_cache = Mock(buffer_pipeline=None, storage_prefetch_retries=None)
        s.token_to_kv_pool_allocator = Mock()
        s.new_token_ratio_tracker = NS(current=1)
        s.max_prefill_tokens = 4096
        s.max_prefill_bs = 43
        s.priority_scheduling_preemption_threshold = 0
        s.disaggregation_mode = DisaggregationMode.NULL
        s.model_config = Mock()
        s.spec_algorithm = Mock()
        s.tp_worker = NS(model_runner=NS(attn_backend=NS(), prefill_aware_swa=False))
        s.load_inquirer = Mock()
        self.adder = Mock(can_run_list=[], preempt_list=[], new_chunked_req=None)
        self.adder.add_one_req.side_effect = self._add
        self.adder.add_chunked_req.side_effect = self._chunk
        self.stack = self.enterContext(ExitStack())
        prefix = "sglang.srt.managers.scheduler."
        self.stack.enter_context(patch(prefix + "get_parallel",
                                      return_value=NS(pp_max_micro_batch_size=43)))
        self.stack.enter_context(patch(prefix + "get_schedule",
                                      return_value=NS(prefill_max_requests=None)))
        self.factory = self.stack.enter_context(patch(prefix + "PrefillAdder",
                                                       return_value=self.adder))
        self.stack.enter_context(patch(prefix + "ScheduleBatch.init_new",
                                      side_effect=lambda reqs, *a, **kw: Mock(reqs=list(reqs))))
        self.stack.enter_context(patch(prefix + "PrefillStats.from_adder"))
        self.stack.enter_context(patch(prefix + "set_time_batch"))
        self.stack.enter_context(patch(prefix + "TEST_RETRACT", False))

    def _add(self, req, **kwargs):
        self.adder.can_run_list.append(req)
        return AddReqResult.CONTINUE

    def _chunk(self, req):
        self.adder.can_run_list.append(req)
        return None

    def req(self, rid):
        return Mock(rid=rid, beam_group=None)

    def cas(self, cap, revision=None):
        return self.s.set_internal_state(SetInternalStateReq(server_args={
            "pig_governor": {"expected_epoch": self.core.epoch,
                             "expected_revision": revision or self.core.snapshot(time.monotonic())["revision"],
                             "max_running": cap}}, control_nonce="running-policy-test"))

    def select(self):
        self.adder.can_run_list.clear()
        return self.s._get_new_batch_prefill_raw(None, self.s.running_batch)[0]

    def test_cas_caps_real_slots_and_selected_batch(self):
        self.assertTrue(self.cas(1).updated)
        self.assertEqual([self.s.get_num_allocatable_reqs(n) for n in (0, 1, 2)], [1, 0, 0])
        self.s.waiting_queue = [self.req(str(i)) for i in range(3)]
        batch = self.select()
        self.assertEqual(len(batch.reqs), 1)
        self.assertEqual(len(self.s.waiting_queue), 2)
        self.assertEqual(self.factory.call_args.kwargs["max_running_requests"], 1)

    def test_hot_lower_drains_without_preempting_or_starting(self):
        existing = [self.req("existing-a"), self.req("existing-b")]
        self.s.running_batch.reqs = existing.copy()
        self.s.waiting_queue = [self.req("waiting")]
        self.assertTrue(self.cas(1).updated)
        self.s.enable_priority_preemption = True
        self.assertIsNone(self.select())
        self.assertEqual(self.s.running_batch.reqs, existing)
        self.adder.preempt_to_schedule.assert_not_called()
        self.s.running_batch.reqs.clear()
        self.assertEqual(len(self.select().reqs), 1)

    def test_hot_raise_clears_stale_full_and_failed_cas_does_not(self):
        self.assertTrue(self.cas(1).updated)
        self.s.running_batch.reqs = [self.req("running")]
        self.s.waiting_queue = [self.req("waiting")]
        self.assertIsNone(self.select())
        self.assertTrue(self.s.running_batch.batch_is_full)
        self.assertFalse(self.cas(2, revision=1).updated)
        self.assertTrue(self.s.running_batch.batch_is_full)
        self.assertTrue(self.cas(2).updated)
        self.assertFalse(self.s.running_batch.batch_is_full)
        self.assertEqual(len(self.select().reqs), 1)

    def test_existing_chunk_progresses_above_lower_cap_without_new_owner(self):
        self.s.running_batch.reqs = [self.req("decode")]
        chunk = self.req("chunk")
        self.s.chunked_req = chunk
        waiting = self.req("waiting")
        self.s.waiting_queue = [waiting]
        self.assertTrue(self.cas(1).updated)
        batch = self.select()
        self.assertEqual(batch.reqs, [chunk])
        self.assertEqual(self.s.waiting_queue, [waiting])
        chunk.init_next_round_input.assert_called_once_with()

    def test_retracted_owner_waits_then_reenters_with_same_reservation(self):
        from pig_governor.scheduler import Reservation
        req = self.req("retracted")
        req.governor_reservation = Reservation(self.s.governor, 0, {})
        reservation = req.governor_reservation
        self.s.waiting_queue = [req]
        self.s.running_batch.reqs = [self.req("running")]
        self.assertTrue(self.cas(1).updated)
        self.assertIsNone(self.select())
        self.s.running_batch.reqs.clear()
        self.s.running_batch.batch_is_full = False
        self.assertEqual(self.select().reqs, [req])
        self.assertIs(req.governor_reservation, reservation)

    def test_governor_off_preserves_native_slots_and_batch(self):
        self.s.governor = None
        self.assertEqual([self.s.get_num_allocatable_reqs(n) for n in (0, 1, 2)], [43, 42, 41])
        self.s.waiting_queue = [self.req(str(i)) for i in range(3)]
        self.assertEqual(len(self.select().reqs), 3)
        self.assertEqual(self.factory.call_args.kwargs["max_running_requests"], 43)

    def test_policy_change_preserves_physical_capacity_and_identity(self):
        identity = {"test": "immutable-runtime"}
        self.s.governor.runtime_identity = identity
        epoch = self.core.epoch
        self.assertTrue(self.cas(1).updated)
        self.assertEqual(self.s.max_running_requests, 43)
        self.assertEqual(self.s.governor.native_max_running_requests, 43)
        self.assertEqual(self.s.governor.max_running_requests, 43)
        self.assertIs(self.s.governor.runtime_identity, identity)
        self.assertEqual(self.core.epoch, epoch)

    def test_real_chunk_adder_ignores_delayer_denial_but_obeys_physical_kv(self):
        adder = object.__new__(PrefillAdder)
        adder.dllm_config = None
        adder.rem_chunk_tokens = 8
        adder.is_hybrid_swa = False
        adder.page_size = 1
        adder.prefill_delayer_single_pass = Mock()
        adder.prefill_delayer_single_pass.negotiate_should_allow_prefill.return_value = False
        adder.running_batch = Mock()
        adder.running_batch.batch_size.return_value = 2
        adder.max_prefill_bs = 43
        adder.max_running_requests = 1
        adder.waiting_queue_len = 1
        adder.can_run_list = []
        adder.exact_chunk_fill = False
        adder._update_prefill_budget = Mock()
        adder._mamba_gap_budget_for_req = Mock(return_value=0)
        req = self.req("real-chunk")
        req.full_untruncated_fill_ids = list(range(16))
        req.prefix_indices = []
        req.sampling_params.max_new_tokens = 1
        req.set_extend_range.side_effect = lambda start, end: setattr(
            req, "extend_range", NS(length=end - start))
        with patch.object(PrefillAdder, "rem_total_tokens", new_callable=PropertyMock,
                          return_value=0), patch.object(
                              PrefillAdder, "cur_rem_tokens",
                              new_callable=PropertyMock, return_value=20) as physical:
            self.assertIs(adder.add_chunked_req(req), req)
            self.assertEqual(adder.can_run_list, [req])
            self.assertEqual(req.extend_range.length, 8)
            self.assertEqual(adder.prefill_delayer_single_pass.negotiate_should_allow_prefill.call_args.kwargs["max_running_requests"], 1)
            adder.can_run_list.clear()
            physical.return_value = 1
            self.assertIs(adder.add_chunked_req(req), req)
            self.assertEqual(adder.can_run_list, [])

    def test_alternate_schedulers_fail_closed_when_governor_enabled(self):
        self.s.is_generation = True
        for hisparse, pdmux, dllm in ((True, False, None),
                                     (False, True, None),
                                     (False, False, "diffusion")):
            with self.subTest(hisparse=hisparse, pdmux=pdmux, dllm=dllm):
                self.s.enable_hisparse = hisparse
                self.s.enable_pdmux = pdmux
                with patch.dict("os.environ", {"PIG_GOVERNOR_ENABLE": "1"}), patch(
                    "sglang.srt.managers.scheduler.get_exec",
                    return_value=NS(dllm=NS(dllm_algorithm=dllm))
                ):
                    with self.assertRaisesRegex(ValueError, "ordinary prefill/decode"):
                        self.s.init_governor()
