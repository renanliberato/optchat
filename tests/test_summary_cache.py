"""Cache invariants under streaming writes, coarsening and out-of-order jobs."""
import tempfile
import threading
import time
import unittest
from pathlib import Path

from optchat.compactor import Compactor, PrefixWarmup, RECENT_CHAT
from optchat.memory import Memory, Part, byte_size


class SummaryCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.mem = Memory(Path(self.temp.name), 64, 256)
        self.worker = Compactor(self.mem, lambda: None, cache_window=4)

    def tearDown(self):
        self.worker.close()
        self.mem.close()
        self.temp.cleanup()

    def seed(self, count):
        for i in range(count):
            self.mem.append("user", f"decision-{i} " + "a" * 40)
            self.mem.save_node(Part(0, i), f"user: decision-{i} " + "a" * 40)

    def context(self, part):
        with self.mem.cv:
            return self.worker.context(part)

    def test_prefix_survives_coarsening_recent_decision_stays_fresh(self):
        self.seed(4)
        self.mem.append("user", "NEW-DECISION " * 20)
        first = self.context(Part(0, 4))
        self.mem.save_node(Part(1, 0), "user: older decisions")
        self.mem.save_node(Part(1, 1), "user: more older decisions")
        self.mem.save_node(Part(0, 4), "user: NEW-DECISION")
        self.mem.append("user", "next source " * 20)
        second = self.context(Part(0, 5))
        self.assertEqual(first.split(RECENT_CHAT)[0], second.split(RECENT_CHAT)[0])
        self.assertNotIn("NEW-DECISION", first)
        self.assertIn("NEW-DECISION", second.split(RECENT_CHAT)[1])
        self.assertNotIn("next source", second)

    def test_crossing_view_node_is_split_without_future_leak(self):
        self.seed(4)
        self.mem.save_node(Part(1, 0), "user: combined 0 and 1")
        self.mem.save_node(Part(1, 1), "user: combined 2 and FUTURE-3")
        self.mem.save_node(Part(2, 0), "user: FUTURE-3")
        self.mem.view = [Part(2, 0)]
        with self.mem.cv:
            context = self.mem.compact_context(Part(0, 3))
        self.assertIn("decision-2", context)
        self.assertNotIn("FUTURE-3", context)
        self.assertNotIn("decision-3", context)

    def test_out_of_order_jobs_do_not_freeze_future_context(self):
        self.seed(8)
        later = self.context(Part(0, 7))
        earlier = self.context(Part(0, 4))
        self.assertEqual(later.split(RECENT_CHAT)[0], earlier.split(RECENT_CHAT)[0])
        self.assertNotIn("decision-4", earlier)
        self.assertIn("decision-6", later)
        self.assertNotIn("decision-7", later)

    def test_pending_predecessor_is_available_in_tail(self):
        self.mem.append("user", "Keep private.")
        self.mem.append("user", "Now do it " * 20)
        self.assertIn("Keep private.", self.context(Part(0, 1)))

    def test_utf8_context_budget_and_bounded_snapshots(self):
        for i in range(270):
            self.mem.append("user", f"{i} " + "é" * 22)
            self.mem.save_node(Part(0, i), f"user: {i} " + "é" * 22)
            context = self.context(Part(0, i))
            prefix, tail = context.split(RECENT_CHAT)
            payload = prefix.removeprefix("<chat>\n").removesuffix("\n</chat>")
            payload += tail.removesuffix("\n</recent_chat>")
            self.assertLessEqual(byte_size(payload), self.mem.view_bytes)
        self.assertLessEqual(len(self.worker.contexts), 64)

    def test_merges_reuse_prefix_and_include_current_children(self):
        self.seed(8)
        first = self.context(Part(1, 2))
        second = self.context(Part(1, 3))
        self.assertEqual(first.split(RECENT_CHAT)[0], second.split(RECENT_CHAT)[0])
        self.assertIn("decision-5", first)
        self.assertNotIn("decision-6", first)
        self.assertIn("decision-7", second)


class PrefixWarmupTests(unittest.TestCase):
    def test_cold_request_finishes_before_waiters_then_warm_calls_are_parallel(self):
        warmup = PrefixWarmup()
        entered, release = threading.Event(), threading.Event()
        parallel = threading.Barrier(3)
        errors = []
        def cold():
            with warmup.use("prefix"):
                entered.set()
                release.wait(2)
        def follower():
            try:
                with warmup.use("prefix"):
                    self.assertTrue(release.is_set())
                    parallel.wait(2)
            except BaseException as exc:
                errors.append(exc)
        first = threading.Thread(target=cold)
        first.start()
        self.assertTrue(entered.wait(2))
        followers = [threading.Thread(target=follower) for _ in range(2)]
        for thread in followers:
            thread.start()
        release.set()
        parallel.wait(2)
        for thread in [first, *followers]:
            thread.join(2)
        self.assertFalse(errors)

    def test_failure_does_not_warm_and_expiry_rewarms(self):
        warmup = PrefixWarmup()
        with self.assertRaises(ValueError):
            with warmup.use("prefix"):
                raise ValueError("failed")
        entry = next(iter(warmup.entries.values()))
        self.assertEqual(entry[1], 0)
        with warmup.use("prefix"):
            pass
        self.assertGreater(entry[1], 0)
        entry[1] = time.monotonic() - 300
        with warmup.use("prefix"):
            pass
        self.assertLess(time.monotonic() - entry[1], 1)


if __name__ == "__main__":
    unittest.main()
