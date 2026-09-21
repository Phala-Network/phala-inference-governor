"""Tests against the real Rust library; run after cargo build --release."""
import unittest
from pig_governor import Governor, RevisionConflict
from pig_governor.admin import execute


class NativeAbiTests(unittest.TestCase):
    def setUp(self):
        self.core = Governor(35, max_running_requests=4)
        self.addCleanup(self.core.close)

    def test_real_ffi_window_and_external_policy_contract(self):
        self.core.observe(0, 0, 2)
        self.core.observe(1, 10, 1)
        self.core.observe(3, 4, 0)
        before = execute(self.core, "get", 3)
        self.assertEqual(before["decode_tokens"], 14)
        self.assertEqual(before["decode_sequence_seconds"], 4)
        after = execute(self.core, "patch", 3, {
            "expected_epoch": before["epoch"], "expected_revision": before["revision"],
            "tps_reference": 50,
        })
        self.assertEqual(after["revision"], 2)
        self.assertEqual(after["decode_tokens"], before["decode_tokens"])
        self.assertEqual(after["decode_sequence_seconds"], before["decode_sequence_seconds"])
        with self.assertRaises(RevisionConflict):
            execute(self.core, "patch", 3, {"expected_epoch": before["epoch"], "expected_revision": 1, "tps_reference": 20})
        with self.assertRaises(RevisionConflict):
            self.core.update_reference("0" * 32, 2, 20)

    def test_surface_exact_cell_fit_and_risk(self):
        core = Governor(50, max_running_requests=4)
        self.addCleanup(core.close)
        self.assertEqual(core.admit(1, 1, 0)["reason"], 4)
        core.observe_surface(1, 100, 1.0, 1, 0)
        fit = core.admit(1, 1, 0)
        self.assertTrue(fit["allowed"])
        self.assertEqual(fit["reason"], 0)
        self.assertEqual(fit["evidence_concurrency"], 1)
        self.assertGreaterEqual(fit["projected_tps"], 50)

        core.observe_surface(2, 10, 1.0, 2, 0)
        risk = core.admit(2, 2, 0)
        self.assertFalse(risk["allowed"])
        self.assertEqual(risk["reason"], 2)

    def test_atomic_batch_observation_updates_surface_and_window(self):
        core = Governor(50, max_running_requests=4)
        self.addCleanup(core.close)
        # Duration is total sequence-seconds, not elapsed wall-time. At four
        # concurrent sequences this must retain all four seconds even at t=1.
        core.observe(0, 0, 4)
        core.observe_batch(1, 160, 4.0, 4, 0, 4)
        state = core.snapshot(1)
        self.assertEqual(state["decode_tokens"], 160)
        self.assertEqual(state["decode_sequence_seconds"], 4.0)
        admission = core.admit(1, 4, 0)
        self.assertFalse(admission["allowed"])
        self.assertEqual(admission["reason"], 2)
        self.assertEqual(admission["projected_tps"], 40.0)
        self.assertEqual(admission["evidence_concurrency"], 4)

    def test_zero_sequence_tokens_are_rejected_without_mutation(self):
        core = Governor(50, max_running_requests=4)
        self.addCleanup(core.close)
        core.observe_batch(1, 4, 0.1, 1, 0, 1)
        before = core.snapshot(1)
        with self.assertRaises(ValueError):
            core.observe_surface(1.1, 1000, 0.0, 1, 0)
        with self.assertRaises(ValueError):
            core.observe_batch(1.1, 1000, 0.0, 1, 0, 4)
        self.assertEqual(core.snapshot(1), before)
        admission = core.admit(1.1, 1, 0)
        self.assertFalse(admission["allowed"])
        self.assertEqual(admission["reason"], 2)
        self.assertEqual(admission["projected_tps"], 40.0)
        self.assertEqual(admission["active_decode_sequences"], 1)

    def test_surface_heavier_cell_is_safe_but_lighter_cell_is_not(self):
        core = Governor(50, max_running_requests=4)
        self.addCleanup(core.close)
        core.observe_surface(1, 100, 1.0, 4, 1)
        heavier = core.admit(1, 1, 0)
        self.assertTrue(heavier["allowed"])
        self.assertEqual(heavier["evidence_concurrency"], 4)
        self.assertEqual(heavier["evidence_pressure_class"], 1)

        lighter_core = Governor(50, max_running_requests=4)
        self.addCleanup(lighter_core.close)
        lighter_core.observe_surface(1, 100, 1.0, 1, 0)
        lighter = lighter_core.admit(1, 2, 0)
        self.assertFalse(lighter["allowed"])
        self.assertEqual(lighter["reason"], 4)

    def test_reference_zero_samples_and_cas_preserves_surface(self):
        core = Governor(0, max_running_requests=4)
        self.addCleanup(core.close)
        admission = core.admit(1, 4, 0)
        self.assertTrue(admission["allowed"])
        self.assertEqual(admission["reason"], 1)
        core.observe_surface(1, 100, 1.0, 4, 0)
        execute(core, "patch", 1, {
            "expected_epoch": core.epoch,
            "expected_revision": 1,
            "tps_reference": 50,
        })
        admission = core.admit(1, 4, 0)
        self.assertTrue(admission["allowed"])
        self.assertEqual(admission["reason"], 0)
        self.assertEqual(admission["reference"], 50)

    def test_invalid_inputs_and_closed_handle(self):
        with self.assertRaises(ValueError):
            Governor(35, max_running_requests=0)
        with self.assertRaises(ValueError):
            Governor(35, max_running_requests=2**32)

        self.core.observe(5, 0, 1)
        with self.assertRaises(ValueError): self.core.observe(4, 8, 0)
        with self.assertRaises(ValueError): self.core.observe(6, -1, 0)
        with self.assertRaises(ValueError): self.core.observe(6, True, 0)
        with self.assertRaises(ValueError): self.core.observe_surface(6, 1, 1.0, 0, 0)
        with self.assertRaises(ValueError): self.core.observe_surface(6, 1, 1.0, 1, 4)
        with self.assertRaises(ValueError): self.core.admit(6, 0, 0)
        with self.assertRaises(ValueError): self.core.observe_surface(6, 1, 1.0, 5, 0)
        with self.assertRaises(ValueError): self.core.admit(6, 5, 0)
        before = self.core.snapshot(5)
        self.assertEqual(before["active_decode_sequences"], 1)
        self.core.close()
        self.core.close()
        with self.assertRaises(RuntimeError): self.core.snapshot(6)

    def test_soft_advice_does_not_create_a_request_deadline(self):
        self.core.observe(0, 0, 1)
        self.core.observe(10, 1, 1)
        self.core.prefill(10, .5)
        self.assertTrue(self.core.choose_decode(10, runnable_decode=True, pending_prefill=True))
        self.assertFalse(self.core.choose_decode(10.3, runnable_decode=True, pending_prefill=True))
        state = self.core.snapshot(10.3)
        self.assertEqual(state["active_decode_sequences"], 1)
        self.assertEqual(state["decode_tokens"], 1)
        self.assertFalse(state["individual_tps_binding"])

    def test_unrepresentable_reference_rejected_before_ffi_or_policy_update(self):
        self.core.observe(0, 0, 1)
        self.core.observe(1, 7, 0)
        before = self.core.snapshot(1)
        for value in (10**1000, -(10**1000)):
            with self.subTest(value_sign=value > 0):
                with self.assertRaises(ValueError):
                    self.core.update_reference(self.core.epoch, before["revision"], value)
                with self.assertRaises(ValueError):
                    execute(self.core, "patch", 1, {
                        "expected_epoch": self.core.epoch,
                        "expected_revision": before["revision"],
                        "tps_reference": value,
                    })
                self.assertEqual(self.core.snapshot(1), before)


if __name__ == "__main__":
    unittest.main()
