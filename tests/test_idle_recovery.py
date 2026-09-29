"""Idle recovery causality against the real Rust ABI (no GPU or fake core)."""
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from pig_governor import Governor
from pig_governor.scheduler import Progress, SchedulerGovernor


def request():
    return SimpleNamespace(origin_input_ids=[1],
                           sampling_params=SimpleNamespace(max_new_tokens=32))


class IdleRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.core = Governor(50, max_running_requests=4)
        self.addCleanup(self.core.close)
        self.adapter = SchedulerGovernor(
            self.core, max_running_requests=4, online_exploration=True)

    def slow_run(self):
        req = request()
        self.assertTrue(self.adapter.admit_request(req, 100, waiting_count=0)['allowed'])
        progress = Progress()
        self.adapter.committed(progress, 100, 1)
        self.adapter.committed(progress, 101, 11, terminal=True)
        self.adapter.release_request(req)

    def test_retired_slow_cell_can_collect_real_recovery_evidence(self):
        self.slow_run()
        # Prove the real core still forecasts risk, rather than expiring the cell.
        before = self.core.admit(103, 1, 0)
        self.assertFalse(before['allowed'])
        self.assertEqual(before['reason'], 2)
        self.assertAlmostEqual(before['projected_tps'], 10)
        probe = request()
        decision = self.adapter.admit_request(probe, 103, waiting_count=0)
        self.assertTrue(decision['allowed'])
        self.assertTrue(decision['recovery_probe'])
        self.assertEqual(decision['original_reason'], 2)
        self.assertEqual(decision['reason_name'], 'online_exploration')
        self.assertAlmostEqual(decision['projected_tps'], 10)
        progress = Progress()
        self.adapter.committed(progress, 103, 1)
        self.adapter.committed(progress, 104, 101, terminal=True)
        self.adapter.release_request(probe)
        recovered = self.adapter.admit_request(request(), 104, waiting_count=0)
        self.assertTrue(recovered['allowed'])
        self.assertEqual(recovered['reason_name'], 'fit')

    def test_recent_retirement_waiting_and_dirty_health_block_recovery(self):
        self.slow_run()
        self.assertFalse(self.adapter.admit_request(request(), 102, waiting_count=0)['allowed'])
        self.assertFalse(self.adapter.admit_request(request(), 103, waiting_count=1)['allowed'])
        self.adapter._health_observation_dirty = True
        self.assertFalse(self.adapter.admit_request(request(), 103, waiting_count=0)['allowed'])

    def test_one_probe_and_slow_probe_does_not_become_fit(self):
        self.slow_run()
        probe = request()
        self.assertTrue(self.adapter.admit_request(probe, 103, waiting_count=0)['allowed'])
        self.assertFalse(self.adapter.admit_request(request(), 103, waiting_count=0)['allowed'])
        progress = Progress()
        self.adapter.committed(progress, 103, 1)
        self.adapter.committed(progress, 104, 11, terminal=True)
        self.assertTrue(self.adapter.release_request(probe))
        self.assertFalse(self.adapter.release_request(probe))
        self.assertFalse(self.adapter.admit_request(request(), 105, waiting_count=0)['allowed'])
        again = self.adapter.admit_request(request(), 106, waiting_count=0)
        self.assertTrue(again['allowed'])
        self.assertTrue(again['recovery_probe'])
        self.assertLess(again['projected_tps'], 50)

    def test_disabled_exploration_keeps_risk_refusal(self):
        self.slow_run()
        self.adapter.online_exploration = False
        decision = self.adapter.admit_request(request(), 103, waiting_count=0)
        self.assertFalse(decision['allowed'])
        self.assertEqual(decision['reason_name'], 'tps_risk')

    def test_native_rollback_releases_probe_but_preserves_cooldown(self):
        self.slow_run()
        probe = request()
        self.assertTrue(self.adapter.admit_request(probe, 103, waiting_count=0)['allowed'])
        self.assertTrue(self.adapter.release_request(probe))
        self.assertEqual(self.adapter.outstanding, 0)
        blocked = self.adapter.admit_request(request(), 104, waiting_count=0)
        self.assertFalse(blocked['allowed'])
        self.assertEqual(blocked['exploration_blocker'], 'cooldown')
        self.assertTrue(self.adapter.admit_request(request(), 105, waiting_count=0)['allowed'])

    def test_busy_slow_cell_is_not_overridden(self):
        self.slow_run()
        probe = request()
        self.adapter.admit_request(probe, 103, waiting_count=0)
        # Real measured higher-concurrency evidence, not a fake admission reply.
        self.core.observe_surface(103, 2, 1, 2, 0)
        refused = self.adapter.admit_request(request(), 103, waiting_count=0)
        self.assertFalse(refused['allowed'])
        self.assertEqual(refused['reason_name'], 'tps_risk')
        self.assertEqual(refused['exploration_blocker'], 'busy')
        self.assertEqual(self.adapter.outstanding, 1)

    def test_concurrent_idle_arrivals_reserve_only_one_probe(self):
        self.slow_run()
        candidates = [request() for _ in range(16)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(
                lambda req: self.adapter.admit_request(req, 103, waiting_count=0),
                candidates))
        self.assertEqual(sum(d['allowed'] for d in results), 1)
        self.assertEqual(self.adapter.outstanding, 1)
        self.assertTrue(self.adapter._exploration_active)
        for req in candidates:
            self.adapter.release_request(req)
        self.assertEqual(self.adapter.outstanding, 0)
        self.assertFalse(self.adapter._exploration_active)
