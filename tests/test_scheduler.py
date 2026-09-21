"""Native lifecycle adapter contracts; no SGLang import or GPU dependency."""
import unittest
from types import SimpleNamespace
from pig_governor.scheduler import Progress, SchedulerGovernor


class Core:
    def __init__(self):
        self.rows = []
        self.allowed = True
        self.last_time = None

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

    def admit(self, now, projected_concurrency, pressure_class):
        self.rows.append(
            ("admit", now, projected_concurrency, pressure_class)
        )
        return {
            "allowed": self.allowed,
            "projected_tps": 100 if self.allowed else 10,
            "reason": "fit" if self.allowed else "tps_risk",
        }

    def prefill(self, now, wall):
        self.rows.append(("prefill", now, wall))

    def choose_decode(self, now, **kwargs):
        self.rows.append(("choose", now, kwargs))
        return False


def request(rid, input_tokens=16, max_new_tokens=16):
    return SimpleNamespace(
        rid=rid,
        origin_input_ids=list(range(input_tokens)),
        sampling_params=SimpleNamespace(max_new_tokens=max_new_tokens),
    )


class LifecycleTests(unittest.TestCase):
    def test_max_running_requests_is_bounded_to_32_bits(self):
        with self.assertRaises(ValueError):
            SchedulerGovernor(Core(), max_running_requests=2**32)

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

    def test_waiting_population_has_no_fixed_governor_cap(self):
        core = Core()
        adapter = SchedulerGovernor(core, max_running_requests=4)
        reqs = [request(str(index)) for index in range(100)]
        self.assertTrue(all(adapter.admit_request(req, index)["allowed"] for index, req in enumerate(reqs)))
        projected = [row[2] for row in core.rows if row[0] == "admit"]
        self.assertEqual(projected[:5], [1, 2, 3, 4, 4])
        self.assertEqual(projected[-1], 4)
        self.assertEqual(adapter.outstanding, 100)

    def test_retraction_reuses_reservation_and_release_is_exactly_once(self):
        core = Core()
        adapter = SchedulerGovernor(core, max_running_requests=4)
        req = request("retracted")
        first = adapter.admit_request(req, 1)
        repeated = adapter.admit_request(req, 2, is_retracted=True)
        self.assertTrue(first["allowed"])
        self.assertTrue(repeated["allowed"])
        self.assertEqual(len([row for row in core.rows if row[0] == "admit"]), 1)
        self.assertEqual(adapter.outstanding, 1)
        self.assertTrue(adapter.release_request(req))
        self.assertFalse(adapter.release_request(req))
        self.assertEqual(adapter.outstanding, 0)

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
        adapter = SchedulerGovernor(core, max_running_requests=4)
        req = request("sample")
        self.assertTrue(adapter.admit_request(req, 1)["allowed"])
        self.assertEqual(adapter.outstanding, 1)


if __name__ == "__main__":
    unittest.main()
