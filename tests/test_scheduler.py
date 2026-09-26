"""Native lifecycle adapter contracts; no SGLang import or GPU dependency."""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from types import SimpleNamespace
from pig_governor.scheduler import MAX_WAITING_LIMIT, Progress, SchedulerGovernor


class Core:
    def __init__(self):
        self.rows = []
        self.allowed = True
        self.reason = 2
        self.last_time = None
        self.epoch = "1" * 32
        self.revision = 1
        self.reference = 50.0

    def observe(self, now, delta, active_after):
        if self.last_time is not None and now < self.last_time:
            raise ValueError("Clock went backwards")
        self.last_time = now
        self.rows.append(("observe", now, delta, active_after))

    def observe_surface(self, now, delta, duration, concurrency, pressure_class):
        self.rows.append(
            ("surface", now, delta, duration, concurrency, pressure_class)
        )
    def observe_batch(self, now, delta, duration, concurrency,
                     pressure_class, active_after):
        self.observe_surface(now, delta, duration, concurrency, pressure_class)
        self.observe(now, delta, active_after)

    def observe_replacement(self, now, delta, duration, concurrency,
                            pressure_class, active_after):
        if duration > 0:
            self.observe_batch(now, delta, duration, concurrency,
                               pressure_class, active_after)
        else:
            self.observe(now, 0, active_after)

    def admit(self, now, projected_concurrency, pressure_class):
        self.rows.append(
            ("admit", now, projected_concurrency, pressure_class)
        )
        return {
            "allowed": self.allowed,
            "projected_tps": 100 if self.allowed else 10,
            "reason": self.reason if not self.allowed else 0,
        }

    def snapshot(self, now):
        return {
            "epoch": self.epoch,
            "revision": self.revision,
            "mutable": {"tps_reference": self.reference},
        }

    def update_reference(self, epoch, revision, reference):
        if epoch != self.epoch or revision != self.revision:
            raise ValueError("conflict")
        self.reference = float(reference)
        self.revision += 1

    def prefill(self, now, wall):
        self.rows.append(("prefill", now, wall))

    def choose_decode(self, now, **kwargs):
        self.rows.append(("choose", now, kwargs))
        return False


class BlockingObserveCore(Core):
    def __init__(self):
        super().__init__()
        self.observe_entered = threading.Event()
        self.allow_observe = threading.Event()

    def observe(self, now, delta, active_after):
        self.observe_entered.set()
        if not self.allow_observe.wait(timeout=2):
            raise AssertionError("test observe gate timed out")
        super().observe(now, delta, active_after)


def request(rid, input_tokens=16, max_new_tokens=16):
    return SimpleNamespace(
        rid=rid,
        origin_input_ids=list(range(input_tokens)),
        sampling_params=SimpleNamespace(max_new_tokens=max_new_tokens),
    )


class LifecycleTests(unittest.TestCase):
    def test_duplicate_progress_in_batch_does_not_change_accounting(self):
        core = Core()
        adapter = SchedulerGovernor(core, max_running_requests=4)
        progress = Progress()

        with self.assertRaisesRegex(ValueError, "Duplicate Governor Progress"):
            adapter.commit_batch(
                [(progress, 1, False, 0), (progress, 1, False, 0)], 1
            )

        self.assertEqual(core.rows, [])
        self.assertEqual(adapter.active, 0)
        self.assertEqual(adapter.active_pressure_counts, [0, 0, 0, 0])
        self.assertEqual(progress.output_tokens, 0)

    def test_pressure_class_mismatch_does_not_change_accounting(self):
        core = Core()
        adapter = SchedulerGovernor(core, max_running_requests=4)
        progress = Progress(pressure_class=1)

        with self.assertRaisesRegex(ValueError, "pressure class changed"):
            adapter.commit_batch([(progress, 1, False, 0)], 1)

        self.assertEqual(core.rows, [])
        self.assertEqual(adapter.active, 0)
        self.assertEqual(adapter.active_pressure_counts, [0, 0, 0, 0])
        self.assertEqual(progress.output_tokens, 0)

    def test_batch_commit_serializes_policy_snapshot(self):
        core = BlockingObserveCore()
        adapter = SchedulerGovernor(core)
        progress = Progress()
        with ThreadPoolExecutor(max_workers=2) as pool:
            commit = pool.submit(adapter.committed, progress, 1, 1)
            self.assertTrue(core.observe_entered.wait(timeout=1))
            snapshot = pool.submit(adapter.policy_snapshot, 1)
            with self.assertRaises(TimeoutError):
                snapshot.result(timeout=0.05)
            core.allow_observe.set()
            commit.result(timeout=1)
            self.assertEqual(snapshot.result(timeout=1)["revision"], 1)
        self.assertEqual(adapter.active, 1)

    def test_max_running_requests_is_bounded_to_32_bits(self):
        with self.assertRaises(ValueError):
            SchedulerGovernor(Core(), max_running_requests=2**32)

    def test_policy_update_rejects_none_without_mutation(self):
        core = Core()
        adapter = SchedulerGovernor(core, max_running_requests=4, max_running=3)
        before = (core.reference, core.revision, adapter.max_running)
        with self.assertRaisesRegex(ValueError, "Invalid Governor max_running"):
            adapter.update_policy(
                1, expected_epoch=core.epoch, expected_revision=core.revision,
                max_running=None,
            )
        self.assertEqual(
            (core.reference, core.revision, adapter.max_running), before
        )

    def test_first_decode_token_is_excluded_and_entering_exposure_starts(self):
        core = Core()
        adapter = SchedulerGovernor(core)
        progress = Progress()
        adapter.committed(progress, 1, 1, pressure_class=0)
        self.assertEqual(core.rows, [("observe", 1, 0, 1)])
        self.assertEqual((progress.output_tokens, progress.decoding, progress.terminal), (1, True, False))
        self.assertEqual(progress.last_time, 1)

    def test_decode_interval_records_tokens_and_real_seconds(self):
        core = Core()
        adapter = SchedulerGovernor(core)
        progress = Progress()
        adapter.committed(progress, 1, 1, pressure_class=0)
        adapter.committed(progress, 3, 4, pressure_class=0)
        self.assertIn(("surface", 3, 3, 2.0, 1, 0), core.rows)
        self.assertEqual((progress.output_tokens, progress.decoding, progress.terminal, adapter.active), (4, True, False, 1))

    def test_active_state_transition_splits_surface_exposure_at_transition(self):
        core = Core()
        adapter = SchedulerGovernor(core, max_running_requests=4)
        first = Progress(pressure_class=0)
        second = Progress(pressure_class=0)

        adapter.committed(first, 0, 1, pressure_class=0)
        # A Prefill-only result can introduce the second request without
        # carrying the already-running request in the native batch.
        adapter.committed(second, 10, 1, pressure_class=0)
        adapter.commit_batch(
            [(first, 2, False, 0), (second, 2, False, 0)],
            11,
        )

        surfaces = [row for row in core.rows if row[0] == "surface"]
        self.assertEqual(
            surfaces,
            [
                ("surface", 10, 0, 10.0, 1, 0),
                ("surface", 11, 2, 2.0, 2, 0),
            ],
        )

    def test_zero_time_tokens_are_buffered_until_positive_exposure(self):
        core = Core()
        adapter = SchedulerGovernor(core)
        progress = Progress()
        adapter.committed(progress, 1, 3, pressure_class=0)
        self.assertEqual(adapter._pending_evidence[(1, 0)][0], 2)
        adapter.committed(progress, 1, 5, pressure_class=0)
        self.assertEqual(adapter._pending_evidence[(1, 0)][0], 4)
        self.assertEqual(
            [row for row in core.rows if row[0] == "surface"], []
        )
        adapter.committed(progress, 2, 6, pressure_class=0)
        self.assertEqual(adapter._pending_evidence, {})
        self.assertIn(("surface", 2, 5, 1.0, 1, 0), core.rows)

    def test_zero_time_tokens_never_cross_response_surface_cells(self):
        core = Core()
        adapter = SchedulerGovernor(core, max_running_requests=4)
        first = Progress()
        second = Progress()
        adapter.commit_batch(
            [(first, 1, False, 0), (second, 1, False, 0)], 1
        )
        adapter.commit_batch(
            [(first, 2, False, 0), (second, 2, True, 0)], 1
        )
        self.assertEqual(adapter._pending_evidence[(2, 0)][0], 2)
        adapter.committed(first, 2, 3, pressure_class=0)
        self.assertIn(("surface", 2, 1, 1.0, 1, 0), core.rows)
        self.assertEqual(adapter._pending_evidence[(2, 0)][0], 2)

    def test_zero_time_tokens_are_cleared_when_active_run_ends(self):
        core = Core()
        adapter = SchedulerGovernor(core)
        old = Progress()
        new = Progress()

        adapter.committed(old, 1, 1, pressure_class=0)
        adapter.committed(old, 1, 101, pressure_class=0)
        self.assertEqual(adapter._pending_evidence[(1, 0)][0], 100)
        adapter.terminated(old, 1, pressure_class=0)
        self.assertEqual(adapter._pending_evidence, {})

        adapter.committed(new, 2, 1, pressure_class=0)
        adapter.committed(new, 3, 2, pressure_class=0)
        self.assertIn(("surface", 3, 1, 1.0, 1, 0), core.rows)

    def test_zero_time_full_replacement_keeps_only_new_run_pending_tokens(self):
        core = Core()
        adapter = SchedulerGovernor(core)
        old = Progress()
        new = Progress()

        adapter.committed(old, 1, 1, pressure_class=0)
        adapter.committed(old, 1, 101, pressure_class=0)
        adapter.commit_batch(
            [(old, 102, True, 0), (new, 3, False, 0)],
            1,
        )
        self.assertEqual(adapter.active, 1)
        self.assertEqual(adapter._pending_evidence[(1, 0)][0], 2)

        adapter.committed(new, 2, 4, pressure_class=0)
        self.assertIn(("surface", 2, 3, 1.0, 1, 0), core.rows)

    def test_positive_time_replacement_defers_only_entrant_decode_tokens(self):
        core = Core()
        adapter = SchedulerGovernor(core)
        old, new = Progress(), Progress()
        adapter.committed(old, 0, 1)
        adapter.committed(old, 0, 101)
        adapter.commit_batch([(old, 102, True, 0), (new, 3, False, 0)], 1)
        self.assertIn(("surface", 1, 101, 1.0, 1, 0), core.rows)
        self.assertEqual(adapter._pending_evidence, {(1, 0): (2, 1)})
        adapter.committed(new, 2, 4)
        self.assertIn(("surface", 2, 3, 1.0, 1, 0), core.rows)

    def test_replacement_native_failure_does_not_publish_python_state(self):
        class FailingCore(Core):
            def observe_replacement(self, *args):
                raise ValueError("native rejected")
        core = FailingCore()
        adapter = SchedulerGovernor(core)
        old, new = Progress(), Progress()
        adapter.committed(old, 0, 1)
        adapter.committed(old, 0, 101)
        before = (tuple(getattr(old, key) for key in old.__slots__), tuple(getattr(new, key) for key in new.__slots__), adapter.active,
                  list(adapter.active_pressure_counts),
                  dict(adapter._pending_evidence), adapter._surface_time,
                  list(core.rows))
        with self.assertRaisesRegex(ValueError, "native rejected"):
            adapter.commit_batch([(old, 102, True, 0), (new, 3, False, 0)], 1)
        after = (tuple(getattr(old, key) for key in old.__slots__), tuple(getattr(new, key) for key in new.__slots__), adapter.active,
                 list(adapter.active_pressure_counts),
                 dict(adapter._pending_evidence), adapter._surface_time,
                 list(core.rows))
        self.assertEqual(after, before)

    def test_terminal_decode_records_interval_and_leaves_active(self):
        core = Core()
        adapter = SchedulerGovernor(core)
        progress = Progress()
        adapter.committed(progress, 1, 1, pressure_class=0)
        adapter.committed(progress, 3, 9, terminal=True, pressure_class=0)
        self.assertIn(("surface", 3, 8, 2.0, 1, 0), core.rows)
        self.assertEqual((progress.output_tokens, progress.decoding, progress.terminal, adapter.active), (9, False, True, 0))

    def test_abort_without_decode_does_not_enter_surface(self):
        core = Core()
        adapter = SchedulerGovernor(core)
        adapter.committed(Progress(), 1, 0, terminal=True, pressure_class=0)
        self.assertEqual(core.rows, [("observe", 1, 0, 0)])
        self.assertEqual(adapter.active, 0)

    def test_output_regression_is_rejected_without_state_mutation(self):
        core = Core()
        adapter = SchedulerGovernor(core)
        progress = Progress()
        adapter.committed(progress, 3, 5, pressure_class=0)
        with self.assertRaises(ValueError):
            adapter.committed(progress, 2, 8, terminal=True, pressure_class=0)
        self.assertEqual((progress.output_tokens, progress.decoding, progress.terminal, adapter.active), (5, True, False, 1))

    def test_admission_reserves_projected_state_atomically(self):
        core = Core()
        adapter = SchedulerGovernor(core, max_running_requests=4)
        first = request("first")
        second = request("second")
        self.assertTrue(adapter.admit_request(first, 5)["allowed"])
        self.assertTrue(adapter.admit_request(second, 5)["allowed"])
        self.assertEqual(
            core.rows[-2:],
            [("admit", 5, 1, 0), ("admit", 5, 2, 0)],
        )
        self.assertEqual(adapter.outstanding, 2)

    def test_rejection_commits_no_reservation(self):
        core = Core()
        core.allowed = False
        adapter = SchedulerGovernor(core, max_running_requests=4)
        req = request("risk")
        self.assertFalse(adapter.admit_request(req, 1)["allowed"])
        self.assertEqual(adapter.outstanding, 0)
        self.assertFalse(hasattr(req, "governor_reservation"))

    def test_waiting_limit_allows_three_and_rejects_the_fourth(self):
        core = Core()
        adapter = SchedulerGovernor(
            core, max_running_requests=4, max_running=4, max_waiting=3
        )
        reqs = [request(str(index)) for index in range(8)]
        decisions = [
            adapter.admit_request(req, index) for index, req in enumerate(reqs)
        ]
        self.assertTrue(all(decision["allowed"] for decision in decisions[:7]))
        self.assertFalse(decisions[7]["allowed"])
        self.assertEqual(decisions[7]["reason_name"], "waiting_limit")
        self.assertEqual(decisions[7]["reason"], 5)
        self.assertEqual(decisions[7]["projected_waiting"], 4)
        self.assertFalse(hasattr(reqs[7], "governor_reservation"))
        projected = [row[2] for row in core.rows if row[0] == "admit"]
        self.assertEqual(projected[:5], [1, 2, 3, 4, 4])
        self.assertEqual(projected[-1], 4)
        self.assertEqual(adapter.outstanding, 7)

    def test_waiting_two_allows_only_one_of_two_same_owner_candidates(self):
        core = Core()
        adapter = SchedulerGovernor(
            core, max_running_requests=4, max_running=4, max_waiting=3
        )
        for index in range(6):
            self.assertTrue(adapter.admit_request(request(str(index)), 1)["allowed"])
        first = adapter.admit_request(request("first-boundary"), 2)
        second_req = request("second-boundary")
        second = adapter.admit_request(second_req, 2)
        self.assertTrue(first["allowed"])
        self.assertEqual(first["projected_waiting"], 3)
        self.assertFalse(second["allowed"])
        self.assertEqual(second["reason_name"], "waiting_limit")
        self.assertEqual(adapter.outstanding, 7)
        self.assertFalse(hasattr(second_req, "governor_reservation"))

    def test_concurrent_boundary_candidates_reserve_only_one_slot(self):
        core = Core()
        adapter = SchedulerGovernor(
            core, max_running_requests=4, max_running=4, max_waiting=3
        )
        for index in range(6):
            self.assertTrue(adapter.admit_request(request(str(index)), 1)["allowed"])
        candidates = [request("boundary-a"), request("boundary-b")]
        with ThreadPoolExecutor(max_workers=2) as pool:
            decisions = list(
                pool.map(lambda req: adapter.admit_request(req, 2), candidates)
            )
        self.assertEqual(sum(decision["allowed"] for decision in decisions), 1)
        rejected = next(decision for decision in decisions if not decision["allowed"])
        self.assertEqual(rejected["reason_name"], "waiting_limit")
        self.assertEqual(adapter.outstanding, 7)

    def test_zero_waiting_limit_pauses_new_native_admission_until_cas_restore(self):
        core = Core()
        adapter = SchedulerGovernor(
            core, max_running_requests=4, max_running=2, max_waiting=0
        )
        paused_req = request("paused")
        paused = adapter.admit_request(paused_req, 1, waiting_count=0)
        self.assertFalse(paused["allowed"])
        self.assertEqual(paused["projected_waiting"], 1)
        self.assertEqual(paused["reason_name"], "waiting_limit")
        self.assertFalse(hasattr(paused_req, "governor_reservation"))

        adapter.update_policy(
            2,
            expected_epoch=core.epoch,
            expected_revision=core.revision,
            max_waiting=3,
        )
        resumed = adapter.admit_request(request("resumed"), 3, waiting_count=0)
        self.assertTrue(resumed["allowed"])
        self.assertEqual(resumed["projected_waiting"], 1)

    def test_compatibility_call_without_native_count_uses_logical_capacity(self):
        core = Core()
        adapter = SchedulerGovernor(
            core, max_running_requests=4, max_running=2, max_waiting=0
        )
        first = adapter.admit_request(request("first"), 1)
        second = adapter.admit_request(request("second"), 1)
        third = adapter.admit_request(request("third"), 1)
        self.assertTrue(first["allowed"])
        self.assertTrue(second["allowed"])
        self.assertFalse(third["allowed"])
        self.assertEqual(third["projected_waiting"], 1)
        self.assertEqual(third["reason_name"], "waiting_limit")

    def test_native_waiting_limit_allows_three_and_rejects_the_fourth(self):
        core = Core()
        adapter = SchedulerGovernor(core, max_running_requests=43)
        decisions = [
            adapter.admit_request(request(str(index)), 1, waiting_count=index)
            for index in range(4)
        ]
        self.assertTrue(all(decision["allowed"] for decision in decisions[:3]))
        self.assertFalse(decisions[3]["allowed"])
        self.assertEqual(decisions[3]["projected_waiting"], 4)
        self.assertEqual(decisions[3]["reason_name"], "waiting_limit")
        self.assertEqual(adapter.last_waiting_count, 3)
        self.assertEqual(adapter.outstanding, 3)

    def test_native_waiting_hard_limit_has_no_running_slot_exception(self):
        core = Core()
        adapter = SchedulerGovernor(core, max_running_requests=43)
        req = request("candidate")
        decision = adapter.admit_request(req, 1, waiting_count=3)
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["projected_waiting"], 4)
        self.assertEqual(decision["reason_name"], "waiting_limit")
        self.assertEqual(adapter.outstanding, 0)
        self.assertFalse(hasattr(req, "governor_reservation"))

    def test_reference_zero_still_enforces_logical_capacity(self):
        core = Core()
        core.reference = 0.0
        adapter = SchedulerGovernor(
            core, max_running_requests=4, max_running=2, max_waiting=1
        )
        decisions = [
            adapter.admit_request(request(str(index)), 1, waiting_count=0)
            for index in range(4)
        ]
        self.assertTrue(decisions[0]["allowed"])
        self.assertTrue(decisions[1]["allowed"])
        self.assertTrue(decisions[2]["allowed"])
        self.assertEqual(decisions[2]["projected_waiting"], 1)
        self.assertFalse(decisions[3]["allowed"])
        self.assertEqual(decisions[3]["reason_name"], "waiting_limit")
        self.assertEqual(decisions[3]["projected_waiting"], 2)
        self.assertEqual(adapter.outstanding, 3)

    def test_projected_waiting_is_maximum_of_logical_and_native_occupancy(self):
        core = Core()
        adapter = SchedulerGovernor(
            core, max_running_requests=8, max_running=2, max_waiting=3
        )
        first = adapter.admit_request(request("first"), 1, waiting_count=0)
        second = adapter.admit_request(request("second"), 1, waiting_count=2)
        third = adapter.admit_request(request("third"), 1, waiting_count=0)
        self.assertTrue(first["allowed"])
        self.assertTrue(second["allowed"])
        self.assertTrue(third["allowed"])
        self.assertEqual(first["projected_waiting"], 1)
        self.assertEqual(second["projected_waiting"], 3)
        self.assertEqual(third["projected_waiting"], 1)

    def test_waiting_policy_cannot_raise_the_hard_limit(self):
        core = Core()
        with self.assertRaises(ValueError):
            SchedulerGovernor(core, max_running_requests=4, max_waiting=MAX_WAITING_LIMIT + 1)
        adapter = SchedulerGovernor(core, max_running_requests=4)
        with self.assertRaises(ValueError):
            adapter.update_policy(
                1,
                expected_epoch=core.epoch,
                expected_revision=core.revision,
                max_waiting=MAX_WAITING_LIMIT + 1,
            )

    def test_tps_rejection_keeps_precedence_when_waiting_is_also_over_limit(self):
        core = Core()
        adapter = SchedulerGovernor(
            core, max_running_requests=43, max_running=43, max_waiting=3
        )
        core.allowed = False
        decision = adapter.admit_request(
            request("both-red"), 2, waiting_count=3
        )
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], 2)
        self.assertEqual(decision["reason_name"], "tps_risk")
        self.assertEqual(decision["projected_waiting"], 4)

    def test_aggregate_tps_reason_is_preserved_by_scheduler(self):
        core = Core()
        adapter = SchedulerGovernor(core, max_running_requests=4)
        core.allowed = False
        core.reason = 6
        decision = adapter.admit_request(request("aggregate-low"), 2)
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], 6)
        self.assertEqual(decision["reason_name"], "aggregate_tps_risk")

    def test_policy_update_is_atomic_and_uses_native_tps_projection(self):
        core = Core()
        adapter = SchedulerGovernor(
            core, max_running_requests=4, max_running=4, max_waiting=3
        )
        adapter.update_policy(
            1,
            expected_epoch=core.epoch,
            expected_revision=core.revision,
            tps_reference=55,
            max_running=2,
            max_waiting=1,
        )
        self.assertEqual((core.reference, adapter.max_running, adapter.max_waiting), (55.0, 2, 1))
        for index in range(3):
            adapter.admit_request(request(str(index)), 2)
        projected = [row[2] for row in core.rows if row[0] == "admit"]
        self.assertEqual(projected, [1, 2, 3])
        self.assertEqual(adapter.outstanding, 3)
        rejected = adapter.admit_request(request("over"), 2)
        self.assertFalse(rejected["allowed"])
        self.assertEqual(rejected["reason_name"], "waiting_limit")

    def test_lowered_limits_do_not_evict_but_block_until_natural_drain(self):
        core = Core()
        adapter = SchedulerGovernor(
            core, max_running_requests=4, max_running=4, max_waiting=3
        )
        reqs = [request(str(index)) for index in range(4)]
        for req in reqs:
            self.assertTrue(adapter.admit_request(req, 1)["allowed"])
        adapter.update_policy(
            2,
            expected_epoch=core.epoch,
            expected_revision=core.revision,
            max_running=1,
            max_waiting=1,
        )
        self.assertEqual(adapter.outstanding, 4)
        self.assertTrue(all(not req.governor_reservation.released for req in reqs))
        blocked = adapter.admit_request(
            request("blocked"), 2, waiting_count=0
        )
        self.assertFalse(blocked["allowed"])
        self.assertEqual(blocked["reason_name"], "waiting_limit")
        for req in reqs[:3]:
            self.assertTrue(adapter.release_request(req))
        allowed = adapter.admit_request(
            request("after-drain"), 3, waiting_count=0
        )
        self.assertTrue(allowed["allowed"])
        self.assertEqual(allowed["projected_waiting"], 1)

    def test_invalid_policy_update_does_not_mutate_any_field(self):
        core = Core()
        adapter = SchedulerGovernor(
            core, max_running_requests=4, max_running=4, max_waiting=3
        )
        before = (core.reference, core.revision, adapter.max_running, adapter.max_waiting)
        with self.assertRaises(ValueError):
            adapter.update_policy(
                1,
                expected_epoch=core.epoch,
                expected_revision=core.revision,
                max_running=5,
            )
        self.assertEqual(
            (core.reference, core.revision, adapter.max_running, adapter.max_waiting),
            before,
        )

    def test_retraction_reuses_reservation_and_release_is_exactly_once(self):
        core = Core()
        adapter = SchedulerGovernor(core, max_running_requests=4)
        req = request("retracted")
        first = adapter.admit_request(req, 1, waiting_count=0)
        repeated = adapter.admit_request(
            req, 2, is_retracted=True, waiting_count=3
        )
        self.assertTrue(first["allowed"])
        self.assertTrue(repeated["allowed"])
        self.assertEqual(len([row for row in core.rows if row[0] == "admit"]), 1)
        self.assertEqual(adapter.outstanding, 1)
        self.assertTrue(adapter.release_request(req))
        self.assertFalse(adapter.release_request(req))
        self.assertEqual(adapter.outstanding, 0)

    def test_foreign_reservation_cannot_bypass_owner_admission(self):
        first = SchedulerGovernor(Core(), max_running_requests=4)
        second_core = Core()
        second = SchedulerGovernor(second_core, max_running_requests=4)
        req = request("foreign")
        self.assertTrue(first.admit_request(req, 1)["allowed"])

        with self.assertRaisesRegex(RuntimeError, "different Governor owner"):
            second.admit_request(req, 2)

        self.assertEqual(first.outstanding, 1)
        self.assertEqual(second.outstanding, 0)
        self.assertEqual(second_core.rows, [])
        self.assertTrue(first.release_request(req))

    def test_candidate_context_changes_pressure_class_from_admitted_ledger(self):
        core = Core()
        adapter = SchedulerGovernor(core, max_running_requests=4)
        adapter.admit_request(request("small", 100, 100), 1)
        adapter.admit_request(request("large", 70_000, 1), 2)
        calls = [row for row in core.rows if row[0] == "admit"]
        self.assertEqual(calls[0][3], 0)
        self.assertEqual(calls[1][3], 3)

    def test_mixed_batch_attributes_tokens_to_aggregate_batch_start_state(self):
        core = Core()
        adapter = SchedulerGovernor(core, max_running_requests=4)
        first = Progress(pressure_class=0)
        second = Progress(pressure_class=1)
        adapter.commit_batch(
            [(first, 1, False, 0), (second, 1, False, 1)],
            2,
        )
        adapter.commit_batch(
            [(first, 2, False, 0), (second, 3, False, 1)],
            3,
        )
        surfaces = [row for row in core.rows if row[0] == "surface"]
        self.assertEqual(surfaces, [("surface", 3, 3, 2.0, 2, 1)])
        self.assertEqual(adapter.active, 2)
        self.assertEqual(adapter.active_pressure_counts, [1, 1, 0, 0])

    def test_pressure_transition_splits_exposure_before_cell_change(self):
        core = Core()
        adapter = SchedulerGovernor(core, max_running_requests=4)
        low = Progress(pressure_class=0)
        high = Progress(pressure_class=3)
        replacement = Progress(pressure_class=0)

        adapter.commit_batch(
            [(low, 1, False, 0), (high, 1, False, 3)],
            0,
        )
        adapter.commit_batch(
            [(high, 1, True, 3), (replacement, 1, False, 0)],
            10,
        )
        adapter.commit_batch(
            [(low, 2, False, 0), (replacement, 2, False, 0)],
            11,
        )

        surfaces = [row for row in core.rows if row[0] == "surface"]
        self.assertEqual(
            surfaces,
            [
                ("surface", 10, 0, 20.0, 2, 3),
                ("surface", 11, 2, 2.0, 2, 0),
            ],
        )

    def test_partial_abort_splits_exposure_before_concurrency_change(self):
        core = Core()
        adapter = SchedulerGovernor(core, max_running_requests=4)
        first = Progress(pressure_class=0)
        second = Progress(pressure_class=0)

        adapter.commit_batch(
            [(first, 1, False, 0), (second, 1, False, 0)],
            0,
        )
        adapter.terminated(first, 10, pressure_class=0)
        adapter.committed(second, 11, 2, pressure_class=0)

        surfaces = [row for row in core.rows if row[0] == "surface"]
        self.assertEqual(
            surfaces,
            [
                ("surface", 10, 0, 20.0, 2, 0),
                ("surface", 11, 1, 1.0, 1, 0),
            ],
        )

    def test_invalid_later_update_does_not_mutate_batch_progress(self):
        core = Core()
        adapter = SchedulerGovernor(core, max_running_requests=4)
        first = Progress(pressure_class=0)
        second = Progress(pressure_class=1)
        adapter.commit_batch(
            [(first, 1, False, 0), (second, 1, False, 1)],
            1,
        )
        with self.assertRaises(ValueError):
            adapter.commit_batch(
                [(first, 2, False, 0), (second, 0, False, 1)],
                2,
            )
        self.assertEqual((first.output_tokens, second.output_tokens), (1, 1))
        self.assertEqual(adapter.active, 2)
        self.assertEqual(adapter.active_pressure_counts, [1, 1, 0, 0])

    def test_waiting_heavy_request_does_not_pollute_active_pressure_observation(self):
        core = Core()
        adapter = SchedulerGovernor(core, max_running_requests=4)
        light = request("light", 100, 100)
        heavy = request("heavy", 70_000, 1)
        adapter.admit_request(light, 1)
        adapter.admit_request(heavy, 2)
        light_progress = Progress(pressure_class=0)
        adapter.commit_batch([(light_progress, 1, False, 0)], 1)
        adapter.commit_batch([(light_progress, 3, False, 0)], 2)
        surfaces = [row for row in core.rows if row[0] == "surface"]
        self.assertEqual(surfaces[-1][5], 0)
        self.assertEqual(adapter.active_pressure_counts[3], 0)
        self.assertEqual(adapter.admitted_pressure_counts[3], 1)

    def test_reference_zero_admission_still_reserves_for_offline_sampling(self):
        core = Core()
        core.reference = 0.0
        adapter = SchedulerGovernor(core, max_running_requests=4)
        reqs = [request(str(index)) for index in range(8)]
        decisions = [adapter.admit_request(req, 1) for req in reqs]
        self.assertTrue(all(decision["allowed"] for decision in decisions[:7]))
        self.assertFalse(decisions[7]["allowed"])
        self.assertEqual(decisions[7]["reason_name"], "waiting_limit")
        self.assertEqual(adapter.outstanding, 7)


if __name__ == "__main__":
    unittest.main()
