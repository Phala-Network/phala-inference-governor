"""Native lifecycle adapter contracts; no SGLang import or GPU dependency."""
import unittest
from pig_governor.scheduler import Progress, SchedulerGovernor


class Core:
    def __init__(self):
        self.rows = []
    def observe(self, now, delta, active):
        if self.rows and now < self.rows[-1][0]:
            raise ValueError("Clock went backwards")
        self.rows.append((now, delta, active))


class LifecycleTests(unittest.TestCase):
    def test_prefill_decode_terminal_exactly_once(self):
        core = Core()
        adapter = SchedulerGovernor(core)
        progress = Progress()
        adapter.committed(progress, 1, 3)
        adapter.committed(progress, 2, 7)
        adapter.committed(progress, 3, 9, terminal=True)
        adapter.terminated(progress, 4)
        self.assertEqual(core.rows, [(1, 2, 1), (2, 4, 1), (3, 2, 0)])
        self.assertEqual(adapter.active, 0)

    def test_abort_uses_committed_count_even_if_native_buffer_cleared(self):
        adapter = SchedulerGovernor(Core())
        progress = Progress()
        adapter.committed(progress, 1, 4)
        adapter.terminated(progress, 2)
        self.assertEqual(adapter.core.rows[-1], (2, 0, 0))
        self.assertEqual(progress.output_tokens, 4)

    def test_first_batch_terminal_does_not_invent_decode_time(self):
        adapter = SchedulerGovernor(Core())
        adapter.committed(Progress(), 1, 3, terminal=True)
        self.assertEqual(adapter.core.rows, [(1, 0, 0)])

    def test_invalid_clock_cannot_partially_advance_request(self):
        adapter = SchedulerGovernor(Core())
        progress = Progress()
        adapter.committed(progress, 3, 1)
        with self.assertRaises(ValueError):
            adapter.committed(progress, 2, 8, terminal=True)
        self.assertEqual((progress.output_tokens, progress.decoding, progress.terminal, adapter.active), (1, True, False, 1))


if __name__ == "__main__":
    unittest.main()
