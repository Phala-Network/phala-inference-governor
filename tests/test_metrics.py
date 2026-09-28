"""Prometheus telemetry using the real Governor and native scheduler shapes."""
import math
import os
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families

from pig_governor import Governor
from pig_governor.metrics import GovernorMetrics
from pig_governor.sglang import SglangGovernor


def request(name="private-request", tokens=16):
    return SimpleNamespace(
        rid=name, origin_input_ids=[1],
        sampling_params=SimpleNamespace(max_new_tokens=tokens),
    )


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.core = Governor(0, max_running_requests=4)
        self.addCleanup(self.core.close)
        self.governor = SglangGovernor(self.core, max_running_requests=4)
        self.registry = CollectorRegistry()
        self.metrics = GovernorMetrics(
            {"model_name": "fixed-model", "tp_rank": "0"},
            registry=self.registry,
        )

    def publish(self, now, waiting=None):
        self.metrics.publish(
            self.governor, now=now, waiting_count=waiting, wall_time=1700000000 + now,
        )
        return {
            (sample.name, tuple(sorted(sample.labels.items()))): sample.value
            for family in text_string_to_metric_families(
                generate_latest(self.registry).decode()
            )
            for sample in family.samples
        }

    def value(self, samples, suffix, **labels):
        names = {"model_name": "fixed-model", "tp_rank": "0", **labels}
        return samples[("pig_governor_" + suffix, tuple(sorted(names.items())))]

    def test_cold_and_disabled_do_not_fabricate_tps(self):
        samples = self.publish(0, waiting=0)
        self.assertEqual(self.value(samples, "available"), 1)
        self.assertEqual(self.value(samples, "enabled"), 1)
        self.assertEqual(self.value(samples, "state_valid"), 1)
        self.assertEqual(self.value(samples, "surface_ready"), 0)
        self.assertTrue(math.isnan(self.value(samples, "average_tps", window="60s")))
        self.assertEqual(self.value(samples, "average_tps_valid", window="60s"), 0)
        self.assertEqual(self.value(samples, "online_cells_qualified"), 0)
        self.assertEqual(self.value(samples, "profile_cells_valid"), 0)
        self.assertEqual(self.value(samples, "coverage_cells_total"), 16)
        self.metrics.publish(None, now=1, waiting_count=0, wall_time=1700000001)
        self.assertEqual(self.registry.get_sample_value(
            "pig_governor_enabled", {"model_name": "fixed-model", "tp_rank": "0"}), 0)

    def test_waiting_uses_current_native_sample_not_last_admission(self):
        req = request()
        self.assertTrue(self.governor.admit_request(req, 0, waiting_count=2)["allowed"])
        samples = self.publish(0.1)
        self.assertTrue(math.isnan(self.value(samples, "waiting_requests")))
        self.assertEqual(self.value(samples, "waiting_valid"), 0)
        samples = self.publish(0.2, waiting=1)
        self.assertEqual(self.value(samples, "waiting_requests"), 1)
        self.assertEqual(self.value(samples, "waiting_valid"), 1)
        self.assertEqual(self.value(samples, "outstanding_requests"), 1)
        self.governor.release_request(req)
        samples = self.publish(0.3)
        self.assertEqual(self.value(samples, "waiting_requests"), 0)
        self.assertEqual(self.governor.last_waiting_count, 2)

    def test_real_rejection_reuse_and_policy_updates_preserve_counters(self):
        req = request("private-user-id")
        self.assertTrue(self.governor.admit_request(req, 0, waiting_count=0)["allowed"])
        self.assertTrue(self.governor.admit_request(req, 0, waiting_count=0)["allowed"])
        denied = self.governor.admit_request(request("secret-second-id"), 0, waiting_count=3)
        self.assertFalse(denied["allowed"])
        self.assertEqual(denied["reason_name"], "waiting_limit")
        samples = self.publish(0.1, waiting=0)
        for suffix, expected in (("admission_attempts_total", 3),
                                 ("admission_rejects_total", 1),
                                 ("admission_reuses_total", 1)):
            self.assertEqual(self.value(samples, suffix), expected)
        self.assertEqual(self.value(samples, "admission_rejections_total", reason="waiting_limit"), 1)
        before = self.governor.policy_snapshot(0.1)
        self.governor.update_policy(0.1, expected_epoch=before["epoch"],
                                   expected_revision=before["revision"], tps_reference=50)
        samples = self.publish(0.2, waiting=0)
        self.assertEqual(self.value(samples, "admission_attempts_total"), 3)
        self.assertEqual(self.value(samples, "tps_reference"), 50)
        body = generate_latest(self.registry).decode()
        self.assertNotIn("private-user-id", body)
        self.assertNotIn("secret-second-id", body)
        for _, labels in samples:
            self.assertTrue(set(dict(labels)) <= {"model_name", "tp_rank", "window", "reason"})

    def test_online_coverage_is_qualified_fresh_and_independent_of_profile(self):
        self.core.observe_batch(0, 0, 0, 1, 0, 1)
        self.core.observe_batch(0.2, 20, 0.2, 1, 0, 0)
        samples = self.publish(0.2, waiting=0)
        self.assertEqual(self.value(samples, "online_cells_qualified"), 1)
        self.assertEqual(self.value(samples, "profile_cells_valid"), 0)
        self.assertEqual(self.value(samples, "surface_ready"), 1)
        self.assertEqual(self.value(samples, "average_tps", window="60s"), 100)
        self.assertEqual(self.value(samples, "decode_tokens_60s"), 20)
        samples = self.publish(62, waiting=0)
        self.assertEqual(self.value(samples, "online_cells_qualified"), 0)
        self.assertEqual(self.value(samples, "surface_ready"), 0)
        self.assertTrue(math.isnan(self.value(samples, "average_tps", window="60s")))
        self.assertEqual(self.value(samples, "decode_tokens_60s"), 0)

    def test_failed_snapshot_invalidates_gauges_without_resetting_counters(self):
        self.governor.admit_request(request(), 0, waiting_count=3)
        self.publish(0.1, waiting=0)
        with patch.object(self.governor, "telemetry_snapshot", side_effect=RuntimeError("private")):
            with self.assertLogs("pig_governor.metrics", level="WARNING") as log:
                samples = self.publish(0.2, waiting=0)
        self.assertNotIn("private", " ".join(log.output))
        self.assertEqual(self.value(samples, "state_valid"), 0)
        self.assertTrue(math.isnan(self.value(samples, "outstanding_requests")))
        self.assertEqual(self.value(samples, "average_tps_valid", window="60s"), 0)
        self.assertEqual(self.value(samples, "admission_rejects_total"), 1)
        self.assertEqual(self.value(samples, "sample_timestamp_seconds"), 1700000000.1)
        samples = self.publish(0.3, waiting=0)
        self.assertEqual(self.value(samples, "state_valid"), 1)
        self.assertEqual(self.value(samples, "admission_rejects_total"), 1)

    def test_zero_tps_is_valid_and_prior_coverage_is_not_online_coverage(self):
        self.core.observe_batch(0, 0, 0, 1, 0, 1)
        self.core.observe_batch(0.2, 0, 0.2, 1, 0, 0)
        samples = self.publish(0.2, waiting=0)
        self.assertEqual(self.value(samples, "average_tps", window="60s"), 0)
        self.assertEqual(self.value(samples, "average_tps_valid", window="60s"), 1)
        prior = Governor(50, max_running_requests=4, profile_cells=[{
            "concurrency": 1, "pressure_class": 0,
            "long_tokens": 100, "long_seconds": 1.0,
            "short_tokens": 100, "short_seconds": 1.0,
            "evidence_lower_tps": 100.0,
        }], profile_ttl_seconds=2, now=0)
        self.addCleanup(prior.close)
        self.governor = SglangGovernor(prior, max_running_requests=4)
        samples = self.publish(1, waiting=0)
        self.assertEqual(self.value(samples, "online_cells_qualified"), 0)
        self.assertEqual(self.value(samples, "profile_cells_valid"), 1)
        self.assertEqual(self.value(samples, "surface_ready"), 1)
        samples = self.publish(2, waiting=0)
        self.assertEqual(self.value(samples, "profile_cells_valid"), 0)
        self.assertEqual(self.value(samples, "surface_ready"), 0)

    def test_telemetry_does_not_refresh_identity_or_claim_transition_readiness(self):
        self.core.observe_surface(0.2, 20, 0.2, 1, 0)
        self.governor._identity_provider = lambda: self.fail("Telemetry refreshed identity")
        samples = self.publish(0.2, waiting=0)
        self.assertEqual(self.value(samples, "online_cells_qualified"), 1)
        self.governor._admission_paused = True
        samples = self.publish(0.3, waiting=0)
        self.assertEqual(self.value(samples, "identity_transition"), 1)
        self.assertEqual(self.value(samples, "surface_ready"), 0)
        self.assertEqual(self.value(samples, "online_cells_qualified"), 0)
        self.assertEqual(self.value(samples, "average_tps_valid", window="60s"), 0)

    def test_scheduler_process_exports_to_http_process_registry(self):
        from prometheus_client import multiprocess

        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": directory}
            subprocess.run([sys.executable, "-c", """
from types import SimpleNamespace
from pig_governor import Governor
from pig_governor.sglang import SglangGovernor
from pig_governor.metrics import GovernorMetrics
core = Governor(0, max_running_requests=4)
governor = SglangGovernor(core, max_running_requests=4)
req = SimpleNamespace(rid='not-a-label', origin_input_ids=[1],
                      sampling_params=SimpleNamespace(max_new_tokens=1))
assert not governor.admit_request(req, 0, waiting_count=3)['allowed']
GovernorMetrics({'model_name': 'fixed-model', 'tp_rank': '0'}).publish(
    governor, now=0.1, waiting_count=0, wall_time=1700000000.1)
core.close()
"""], env=environment, check=True, capture_output=True, text=True, timeout=60)
            registry = CollectorRegistry()
            multiprocess.MultiProcessCollector(registry, path=directory)
            labels = {"model_name": "fixed-model", "tp_rank": "0"}
            self.assertEqual(registry.get_sample_value("pig_governor_enabled", labels), 1)
            self.assertEqual(registry.get_sample_value("pig_governor_admission_rejects_total", labels), 1)
            self.assertEqual(registry.get_sample_value(
                "pig_governor_admission_rejections_total", {**labels, "reason": "waiting_limit"}), 1)
            body = generate_latest(registry).decode()
            self.assertNotIn('pid=', body)
            self.assertNotIn('not-a-label', body)


if __name__ == "__main__":
    unittest.main()
