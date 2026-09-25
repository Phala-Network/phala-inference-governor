"""Tests against the real Rust library; run after cargo build --release."""
import ctypes as C
import os
import unittest
from unittest import mock
from pig_governor import Governor, RevisionConflict
from pig_governor.core import ProfileCellV1
import pig_governor.core as core_module


class FakeFunction:
    def __init__(self, result):
        self.result = result
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.result


class BindingCompatibilityTests(unittest.TestCase):
    def test_profile_cell_v1_layout_is_fixed(self):
        self.assertEqual(C.sizeof(ProfileCellV1), 48)
        self.assertEqual({
            name: getattr(ProfileCellV1, name).offset
            for name, _ in ProfileCellV1._fields_
        }, {
            "concurrency": 0,
            "pressure_class": 4,
            "long_tokens": 8,
            "long_seconds": 16,
            "short_tokens": 24,
            "short_seconds": 32,
            "approved_lower_tps": 40,
        })

    def test_old_abi_is_rejected_before_v4_symbol_binding(self):
        library = type("OldLibrary", (), {
            "pig_governor_abi_version": FakeFunction(3),
        })()
        with mock.patch.object(core_module.C, "CDLL", return_value=library):
            with self.assertRaisesRegex(RuntimeError, "expected version 4"):
                Governor(50, max_running_requests=4, library=os.path.abspath("old.dll"))

    def test_missing_abi_and_v4_symbol_have_stable_errors(self):
        with mock.patch.object(core_module.C, "CDLL", return_value=object()):
            with self.assertRaisesRegex(RuntimeError, "does not expose an ABI version"):
                Governor(50, max_running_requests=4, library=os.path.abspath("missing.dll"))

        library = type("IncompleteLibrary", (), {
            "pig_governor_abi_version": FakeFunction(4),
        })()
        with mock.patch.object(core_module.C, "CDLL", return_value=library):
            with self.assertRaisesRegex(RuntimeError, "missing symbol: pig_governor_new"):
                Governor(50, max_running_requests=4, library=os.path.abspath("v4.dll"))

    def test_v4_library_without_replacement_extension_is_rejected(self):
        real = C.CDLL(os.environ["PIG_GOVERNOR_LIBRARY"])
        class OldV4:
            def __getattr__(self, name):
                if name == "pig_governor_observe_replacement":
                    raise AttributeError(name)
                return getattr(real, name)
        with mock.patch.object(core_module.C, "CDLL", return_value=OldV4()):
            with self.assertRaisesRegex(RuntimeError, "missing symbol: pig_governor_observe_replacement"):
                Governor(50, max_running_requests=4, library=os.path.abspath("old-v4.dll"))


class NativeAbiTests(unittest.TestCase):
    def setUp(self):
        self.core = Governor(35, max_running_requests=4)
        self.addCleanup(self.core.close)

    def test_real_ffi_window_and_reference_cas_contract(self):
        self.core.observe(0, 0, 2)
        self.core.observe(1, 10, 1)
        self.core.observe(3, 4, 0)
        before = self.core.snapshot(3)
        self.assertEqual(before["decode_tokens"], 14)
        self.assertEqual(before["decode_sequence_seconds"], 4)
        self.core.update_reference(before["epoch"], before["revision"], 50)
        after = self.core.snapshot(3)
        self.assertEqual(after["revision"], 2)
        self.assertEqual(after["decode_tokens"], before["decode_tokens"])
        self.assertEqual(after["decode_sequence_seconds"], before["decode_sequence_seconds"])
        with self.assertRaises(RevisionConflict):
            self.core.update_reference(before["epoch"], 1, 20)
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

    @staticmethod
    def profile_cell(concurrency=1, pressure_class=0, lower=80.0):
        return {
            "concurrency": concurrency,
            "pressure_class": pressure_class,
            "long_tokens": 100,
            "long_seconds": 1.0,
            "short_tokens": 100,
            "short_seconds": 1.0,
            "evidence_lower_tps": lower,
        }

    def test_profile_exact_heavier_live_lower_and_expiry(self):
        core = Governor(
            50,
            max_running_requests=4,
            profile_cells=[
                self.profile_cell(1, 0, 80),
                self.profile_cell(4, 1, 30),
            ],
            profile_ttl_seconds=2,
            now=0,
        )
        self.addCleanup(core.close)
        exact = core.admit(0, 1, 0)
        self.assertTrue(exact["allowed"])
        self.assertEqual(exact["reason"], 3)
        self.assertFalse(exact["observed"])
        self.assertEqual(exact["projected_tps"], 80)
        self.assertEqual(exact["evidence_concurrency"], 1)

        fallback = core.admit(0, 1, 1)
        self.assertFalse(fallback["allowed"])
        self.assertEqual(fallback["projected_tps"], 30)
        self.assertEqual(fallback["evidence_concurrency"], 4)
        self.assertEqual(fallback["evidence_pressure_class"], 1)

        core.observe_surface(0.05, 10, 0.01, 2, 0)
        unrelated = core.admit(0.05, 1, 0)
        self.assertEqual(unrelated["reason"], 3)
        self.assertFalse(unrelated["observed"])
        self.assertEqual(unrelated["projected_tps"], 80)

        core.observe_surface(0.1, 0, 0.01, 1, 0)
        lowered = core.admit(0.1, 1, 0)
        self.assertFalse(lowered["allowed"])
        self.assertEqual(lowered["reason"], 2)
        self.assertTrue(lowered["observed"])
        self.assertEqual(lowered["projected_tps"], 0)

        capped = Governor(
            50,
            max_running_requests=4,
            profile_cells=[self.profile_cell()],
            profile_ttl_seconds=1,
            now=0,
        )
        self.addCleanup(capped.close)
        capped.observe_surface(0.01, 50, 0.05, 1, 0)
        unqualified = capped.admit(0.01, 1, 0)
        self.assertEqual(unqualified["reason"], 3)
        self.assertFalse(unqualified["observed"])
        self.assertEqual(unqualified["projected_tps"], 80)
        capped.observe_surface(0.1, 50, 0.05, 1, 0)
        qualified = capped.admit(0.1, 1, 0)
        self.assertEqual(qualified["reason"], 0)
        self.assertTrue(qualified["observed"])
        self.assertEqual(qualified["projected_tps"], 80)
        self.assertEqual(capped.admit(0.999, 1, 0)["projected_tps"], 80)
        expired_live = capped.admit(1, 1, 0)
        self.assertEqual(expired_live["reason"], 0)
        self.assertTrue(expired_live["observed"])
        self.assertEqual(expired_live["projected_tps"], 1000)

        expiry = Governor(
            50,
            max_running_requests=4,
            profile_cells=[self.profile_cell()],
            profile_ttl_seconds=1,
            now=0,
        )
        self.addCleanup(expiry.close)
        self.assertEqual(expiry.admit(0.999, 1, 0)["reason"], 3)
        self.assertEqual(expiry.admit(1, 1, 0)["reason"], 4)
        self.assertEqual(expiry.snapshot(1)["profile"]["cell_count"], 0)

    def test_stale_low_cell_refill_reaches_v4_admission_boundary(self):
        core = Governor(
            50, max_running_requests=43,
            profile_cells=[self.profile_cell(39, 2, 58)],
            profile_ttl_seconds=100, now=1,
        )
        self.addCleanup(core.close)
        core.observe_batch(2.0, 0, 0.5, 39, 2, 38)
        self.assertEqual(core.admit(2.1, 39, 2)["reason"], 2)
        core.observe(2.5, 1200, 38)
        core.observe(3.0, 1200, 38)
        self.assertEqual(core.admit(3.1, 39, 2)["reason"], 2)
        core.observe(4.1, 2200, 38)
        recovered = core.admit(4.1, 39, 2)
        self.assertTrue(recovered["allowed"])
        self.assertEqual(recovered["reason"], 3)
        self.assertEqual(recovered["projected_tps"], 58)
        self.assertEqual(recovered["evidence_source"], "response_surface")

    def test_aggregate_live_cross_cell_veto_has_explicit_source(self):
        core = Governor(
            50,
            max_running_requests=4,
            profile_cells=[self.profile_cell(2, 0, 56)],
            profile_ttl_seconds=120,
            now=0,
        )
        self.addCleanup(core.close)
        core.observe(0, 0, 1)
        core.observe_batch(6.84046809701249, 25, 6.84046809701249, 1, 0, 1)

        decision = core.admit(6.84046809701249, 2, 0)
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], 6)
        self.assertEqual(decision["evidence_source"], "aggregate_live")
        self.assertEqual(decision["evidence_concurrency"], 0)
        self.assertAlmostEqual(decision["projected_tps"], 10.681623916135177)

    def test_aggregate_recovery_uses_healthy_short_window(self):
        self.check_aggregate_recovery((5, 5, 55, 55), allowed=True, reason=3)

    def test_aggregate_recovery_keeps_sustained_slow_veto(self):
        self.check_aggregate_recovery((5, 5, 5, 5), allowed=False, reason=6)

    def test_aggregate_requires_minimum_real_exposure(self):
        core = Governor(
            50, max_running_requests=43,
            profile_cells=[self.profile_cell(2, 0, 56)],
            profile_ttl_seconds=120, now=0,
        )
        self.addCleanup(core.close)
        core.observe(0, 0, 1)
        core.observe_batch(.05, 1, .05, 1, 0, 1)
        self.assertEqual(core.admit(.05, 2, 0)["reason"], 3)
        qualified = core.admit(.1, 2, 0)
        self.assertFalse(qualified["allowed"])
        self.assertEqual(qualified["reason"], 6)
        self.assertEqual(qualified["projected_tps"], 10.0)

    def check_aggregate_recovery(self, deltas, *, allowed, reason):
        core = Governor(
            50, max_running_requests=43,
            profile_cells=[self.profile_cell(2, 0, 56)],
            profile_ttl_seconds=120, now=0,
        )
        self.addCleanup(core.close)
        core.observe(0, 0, 1)
        for now, delta in zip((1.0, 1.5, 2.0, 2.5), deltas):
            core.observe_batch(now, delta, .5, 1, 0, 1)
        decision = core.admit(4.0, 2, 0)
        self.assertEqual(decision["allowed"], allowed)
        self.assertEqual(decision["reason"], reason)
        self.assertEqual(decision["projected_tps"], 56.0 if allowed else 5.0)

    def test_profile_constructor_requires_complete_valid_input(self):
        with self.assertRaises(ValueError):
            Governor(50, max_running_requests=4, profile_cells=[self.profile_cell()])
        with self.assertRaises(ValueError):
            Governor(50, max_running_requests=4, profile_ttl_seconds=10)
        empty = Governor(
            50, max_running_requests=4, profile_cells=[],
            profile_ttl_seconds=10, now=0,
        )
        self.addCleanup(empty.close)
        self.assertEqual(empty.snapshot(0)["profile"]["cell_count"], 0)
        for cell in (
            {**self.profile_cell(), "short_tokens": 101},
            {**self.profile_cell(), "long_seconds": 0.09},
            {**self.profile_cell(), "evidence_lower_tps": 101},
        ):
            with self.subTest(cell=cell):
                with self.assertRaises(ValueError):
                    Governor(
                        50, max_running_requests=4, profile_cells=[cell],
                        profile_ttl_seconds=10, now=0,
                    )
        with self.assertRaises(ValueError):
            Governor(
                50, max_running_requests=4,
                profile_cells=[self.profile_cell(), self.profile_cell()],
                profile_ttl_seconds=10, now=0,
            )

    def test_export_profile_roundtrip_and_atomic_failures(self):
        core = Governor(
            50, max_running_requests=4,
            profile_cells=[self.profile_cell(2, 0, 20)],
            profile_ttl_seconds=10, now=0,
        )
        self.addCleanup(core.close)
        core.observe_surface(1, 40, 1.0, 1, 0)
        exported = core.export_profile(1)
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0], {
            "concurrency": 1,
            "pressure_class": 0,
            "long_tokens": 40,
            "long_seconds": 1.0,
            "short_tokens": 40,
            "short_seconds": 1.0,
            "evidence_lower_tps": 40.0,
        })
        target = Governor(
            50, max_running_requests=4, profile_cells=exported,
            profile_ttl_seconds=10, now=1,
        )
        self.addCleanup(target.close)
        self.assertEqual(target.admit(1, 1, 0)["projected_tps"], 40)

        buffer = (ProfileCellV1 * 1)()
        buffer[0].concurrency = 99
        count = C.c_uint32(77)
        self.assertEqual(
            core._lib.pig_governor_export_profile(
                core._handle, 2.0, buffer, 0, C.byref(count),
            ),
            1,
        )
        self.assertEqual(buffer[0].concurrency, 99)
        self.assertEqual(count.value, 77)
        core.observe_surface(1.5, 4, 0.1, 1, 0)

        self.assertEqual(
            core._lib.pig_governor_export_profile(
                core._handle, 1.5, None, 1, C.byref(count),
            ),
            1,
        )
        self.assertEqual(count.value, 77)
        self.assertEqual(
            core._lib.pig_governor_export_profile(
                core._handle, 1.5, buffer, 1, None,
            ),
            1,
        )
        self.assertEqual(buffer[0].concurrency, 99)

    def test_rotate_surface_epoch_preserves_policy_and_resets_clock(self):
        core = Governor(
            50, max_running_requests=4,
            profile_cells=[self.profile_cell()],
            profile_ttl_seconds=10, now=10,
        )
        self.addCleanup(core.close)
        core.observe_batch(11, 100, 1.0, 1, 0, 1)
        core.prefill(11, 0.5)
        before = core.snapshot(11)
        core.update_reference(core.epoch, before["revision"], 60)
        old_epoch = core.epoch

        stable = core.snapshot(11)
        with mock.patch.object(core_module.uuid, "uuid4", side_effect=RuntimeError("uuid")):
            with self.assertRaisesRegex(RuntimeError, "uuid"):
                core.rotate_surface_epoch(2, 3)
        self.assertEqual(core.epoch, old_epoch)
        self.assertEqual(core.snapshot(11), stable)

        with self.assertRaises(ValueError):
            core.rotate_surface_epoch(2, 5)
        self.assertEqual(core.epoch, old_epoch)
        self.assertEqual(core.snapshot(11), stable)

        core.rotate_surface_epoch(2, 3)
        after = core.snapshot(2)
        self.assertNotEqual(core.epoch, old_epoch)
        self.assertEqual(after["revision"], 2)
        self.assertEqual(after["mutable"]["tps_reference"], 60)
        self.assertEqual(after["decode_tokens"], 0)
        self.assertEqual(after["active_decode_sequences"], 3)
        self.assertEqual(after["profile"]["cell_count"], 0)
        self.assertEqual(core.admit(2, 1, 0)["reason"], 4)
        core.observe_surface(2.1, 10, 0.1, 1, 0)

    def test_reference_zero_samples_and_cas_preserves_surface(self):
        core = Governor(0, max_running_requests=4)
        self.addCleanup(core.close)
        admission = core.admit(1, 4, 0)
        self.assertTrue(admission["allowed"])
        self.assertEqual(admission["reason"], 1)
        core.observe_surface(1, 100, 1.0, 4, 0)
        core.update_reference(core.epoch, 1, 50)
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
                self.assertEqual(self.core.snapshot(1), before)


if __name__ == "__main__":
    unittest.main()
