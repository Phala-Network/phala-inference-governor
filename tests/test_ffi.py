"""Tests against the real Rust library; run after cargo build --release."""
import unittest
from pig_governor import Governor, RevisionConflict
from pig_governor.admin import execute


class NativeAbiTests(unittest.TestCase):
    def setUp(self):
        self.core = Governor(35)
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

    def test_invalid_inputs_and_closed_handle(self):
        self.core.observe(5, 0, 1)
        with self.assertRaises(ValueError): self.core.observe(4, 8, 0)
        with self.assertRaises(ValueError): self.core.observe(6, -1, 0)
        with self.assertRaises(ValueError): self.core.observe(6, True, 0)
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


if __name__ == "__main__":
    unittest.main()
