"""Receipt bounds/privacy and actual adapter decision parity (CPU only)."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pig_governor.diagnostics import AdmissionDiagnostics


def request(rid="private-client-rid"):
    return SimpleNamespace(rid=rid, origin_input_ids=[1],
                           sampling_params=SimpleNamespace(max_new_tokens=2))


class ReceiptTests(unittest.TestCase):
    def record(self, capture, now=0, **decision):
        capture.record(request(), decision, now, epoch="a" * 32,
                       outstanding=0, active=0, waiting_count=0,
                       reused=False, health_check=False)

    def test_disabled_ignores_unused_parameters_and_allocates_nothing(self):
        with patch("pig_governor.diagnostics.os.urandom") as random:
            self.assertIsNone(AdmissionDiagnostics.from_environment({
                "PIG_ADMISSION_DIAGNOSTICS_CAPACITY": "invalid"}))
            random.assert_not_called()

    def test_enabled_parameters_are_strictly_bounded(self):
        for name, value in (("", "true"), ("_CAPACITY", "513"),
                            ("_MAX_EVENTS", "65537"), ("_SECONDS", "3601"),
                            ("_CAPACITY", "0"), ("_CAPACITY", "1" * 10000)):
            env = {"PIG_ADMISSION_DIAGNOSTICS": "1",
                   "PIG_ADMISSION_DIAGNOSTICS" + name: value}
            with self.assertRaises(ValueError):
                AdmissionDiagnostics.from_environment(env)

    def test_ring_and_total_event_bound_with_loss_accounting(self):
        capture = AdmissionDiagnostics(capacity=2, max_events=3)
        for now in range(6):
            self.record(capture, now, allowed=False, original_reason=4,
                        exploration_blocker="active")
        snapshot = capture.snapshot()
        self.assertEqual(snapshot["recorded"], 3)
        self.assertEqual(snapshot["overwritten"], 1)
        self.assertEqual(snapshot["stopped"], "event_limit")
        self.assertEqual([event["sequence"] for event in snapshot["events"]], [2, 3])
        self.assertEqual(snapshot["events"][0]["exploration_blocker"], "active")

    def test_elapsed_duration_stops_at_boundary_and_does_not_restart(self):
        capture = AdmissionDiagnostics(duration_seconds=2)
        self.record(capture, 100)
        self.record(capture, 102)
        self.record(capture, 101)
        self.assertEqual(capture.snapshot()["recorded"], 1)
        self.assertEqual(capture.snapshot()["stopped"], "duration")

    def test_privacy_fixed_schema_keyed_request_and_snapshot_isolation(self):
        capture = AdmissionDiagnostics()
        decision = dict(prompt="SECRET", original_reason="SECRET",
                        evidence_source="SECRET", exploration_blocker="SECRET",
                        projected_tps=float("nan"), recovery_probe=True)
        self.record(capture, **decision)
        self.record(capture, 1, **decision)
        snapshot = capture.snapshot()
        self.assertNotIn("SECRET", json.dumps(snapshot, allow_nan=False))
        self.assertNotIn("private-client-rid", json.dumps(snapshot))
        self.assertEqual(snapshot["events"][0]["request_key"],
                         snapshot["events"][1]["request_key"])
        other = AdmissionDiagnostics()
        self.record(other)
        self.assertNotEqual(snapshot["events"][0]["request_key"],
                            other.snapshot()["events"][0]["request_key"])
        snapshot["events"][0]["recovery_probe"] = False
        self.assertTrue(capture.snapshot()["events"][0]["recovery_probe"])

    def test_oversized_rid_has_no_exported_correlation(self):
        capture = AdmissionDiagnostics()
        capture.record(request("x" * 257), {}, 0, epoch="a" * 32,
                       outstanding=0, active=0, waiting_count=0,
                       reused=False, health_check=False)
        self.assertIsNone(capture.snapshot()["events"][0]["request_key"])


class AdapterReceiptTests(unittest.TestCase):
    def test_capture_preserves_decisions_reservations_and_counters(self):
        from pig_governor import Governor
        from pig_governor.sglang import SglangGovernor

        outputs = []
        for enabled in ("0", "1"):
            with patch.dict("os.environ", {"PIG_ADMISSION_DIAGNOSTICS": enabled}):
                core = Governor(50, max_running_requests=4)
                self.addCleanup(core.close)
                adapter = SglangGovernor(core, max_running_requests=4)
            first, second = request("first"), request("second")
            first_decision = adapter.admit_request(first, 100, waiting_count=0)
            second_decision = adapter.admit_request(second, 100.1, waiting_count=0)
            adapter.release_request(first)
            third_decision = adapter.admit_request(request("third"), 100.2, waiting_count=0)
            normalized = [{k: v for k, v in decision.items() if k != "policy_epoch"}
                          for decision in (first_decision, second_decision, third_decision)]
            outputs.append((normalized, adapter.outstanding, adapter.admission_rejects))
            snapshot = adapter.admission_snapshot(waiting_count=0)
            if enabled == "0":
                self.assertIsNone(snapshot["diagnostics"])
            else:
                events = snapshot["diagnostics"]["events"]
                self.assertEqual(len(events), 3)
                self.assertEqual(events[0]["original_reason"], 4)
                self.assertEqual(events[1]["exploration_blocker"], "active")
                self.assertEqual(events[2]["exploration_blocker"], "cooldown")
                self.assertEqual(events[0]["epoch"], core.epoch)
                self.assertEqual(events, adapter.admission_snapshot()["diagnostics"]["events"])
        self.assertEqual(outputs[0], outputs[1])
