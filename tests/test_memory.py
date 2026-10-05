import json
import random
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from optchat.compactor import Compactor, scale
from optchat.memory import Memory, Part, byte_size, cap_result, cut_bytes


class Reply:
    def ask(self, text):
        return "user: preserve decisions; talk: completed work"


def finish(memory):
    worker = Compactor(memory, Reply)
    while True:
        candidates = list(memory.candidates())
        if not candidates:
            break
        for part in candidates:
            worker.build(part)
    worker.close()


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)
        self.memory = Memory(self.home, node_bytes=64, view_bytes=256)

    def tearDown(self):
        self.memory.close()
        self.directory.cleanup()

    def test_verbatim_utf8_restart_and_fsync(self):
        text = "Olá 👋\nexact newline and spaces  "
        with patch("optchat.memory.os.fsync", wraps=__import__("os").fsync) as fsync:
            message = self.memory.append("user", text, "unique")
            self.assertGreaterEqual(fsync.call_count, 1)
        self.assertEqual(message.size, byte_size("user: " + text))
        self.assertEqual(self.memory.zoom(0, 1), "0+0|user: " + text)
        self.memory.append("user", "a retry", "unique")
        self.assertEqual(len(self.memory.messages), 1)
        self.memory.close()
        self.memory = Memory(self.home, 64, 256)
        self.assertEqual(self.memory.messages[0].text, text)
        self.memory.append("user", "dedupe after restart", "unique")
        self.assertEqual(len(self.memory.messages), 1)

    def test_torn_tail_is_reported_skipped_and_separated(self):
        self.memory.append("note", "durable")
        self.memory.close()
        path = next((self.home / "main").glob("*.jsonl"))
        with path.open("ab") as stream:
            stream.write(b'{"i":1,"text":"torn')
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.memory = Memory(self.home, 64, 256)
        self.assertEqual(len(caught), 1)
        self.assertTrue(path.read_bytes().endswith(b"\n"))
        self.memory.append("user", "after crash")
        self.assertEqual(json.loads(path.read_bytes().splitlines()[-1])["i"], 1)

    def test_one_writer(self):
        with self.assertRaisesRegex(RuntimeError, "Another writer"):
            Memory(self.home)

    def test_free_nodes_and_zoom(self):
        self.memory.append("user", "keep this")
        self.memory.append("talk", "done")
        factory = unittest.mock.Mock(side_effect=AssertionError("No model call for free nodes"))
        worker = Compactor(self.memory, factory)
        worker.build(Part(0, 0))
        worker.build(Part(0, 1))
        worker.build(Part(1, 0))
        worker.close()
        self.assertEqual(self.memory.nodes[Part(1, 0)].text, "user: keep this\ntalk: done")
        self.assertEqual(self.memory.zoom(0, 2), "0+1|user: keep this\n1+1|talk: done")
        self.assertTrue(self.memory.date(0))
        for id, n in [(-1, 1), (1, 2), (0, 3), (0, 4), (True, 1), (0, True)]:
            with self.assertRaises(ValueError):
                self.memory.zoom(id, n)

    def test_free_leaf_is_verbatim_including_trailing_whitespace(self):
        self.memory.append("user", "exact text  \n")
        finish(self.memory)
        self.assertEqual(self.memory.nodes[Part(0, 0)].text, "user: exact text  \n")

    def test_no_partial_raw_messages_in_view(self):
        text = "large raw text " * 100
        self.memory.append("user", text)
        with self.assertRaisesRegex(RuntimeError, "not summarized"):
            self.memory.render()
        self.assertNotIn("large raw", self.memory.render(False))
        finish(self.memory)
        self.assertNotIn(text, self.memory.render())
        self.assertIn(text, self.memory.zoom(0, 1))

    def test_ordered_leaf_compression(self):
        for _ in range(4):
            self.memory.append("user", "long " * 100)
        self.assertEqual(list(self.memory.candidates()), [Part(0, 0)])
        worker = Compactor(self.memory, Reply)
        worker.build(Part(0, 0))
        self.assertEqual(list(self.memory.candidates()), [Part(0, 1)])
        worker.build(Part(0, 1))
        self.assertEqual(list(self.memory.candidates()), [Part(0, 2), Part(1, 0)])
        worker.close()

    def test_fit_chooses_most_due_built_pair(self):
        self.memory.view_bytes = 150
        for _ in range(4):
            self.memory.append("user", "a" * 34)
        for i in range(4):
            self.memory.save_node(Part(0, i), "user: " + "a" * 34)
        # Pass over the unbuilt oldest parent rather than block the newer pair.
        self.memory.save_node(Part(1, 1), "newer summary")
        self.assertEqual(self.memory.view, [Part(0, 0), Part(0, 1), Part(1, 1)])
        self.memory.view_bytes = 64
        self.memory.save_node(Part(1, 0), "old summary")
        self.assertEqual(self.memory.view, [Part(1, 0), Part(1, 1)])

    def test_randomized_tiling_budget_and_no_splits(self):
        rng = random.Random(17)
        self.memory.view_bytes = 512
        for i in range(100):
            old = list(self.memory.view)
            self.memory.append("user", "x" * rng.randint(10, 200))
            finish(self.memory)
            self.assertEqual(self.memory.view[0].start, 0)
            self.assertEqual(self.memory.view[-1].end, i + 1)
            for a, b in zip(self.memory.view, self.memory.view[1:]):
                self.assertEqual(a.end, b.start)
            for part in old:
                self.assertTrue(any(p.start <= part.start and p.end >= part.end for p in self.memory.view))
            self.assertLessEqual(self.memory.view_size(), self.memory.view_bytes)
        self.memory.close()
        self.memory = Memory(self.home, 64, 512)
        self.assertEqual(self.memory.first(), 100)
        self.assertLessEqual(self.memory.view_size(), 512)

    def test_cap_head_tail_and_utf8_cut(self):
        text = "HEAD" + "a" * 40_000 + "TAIL"
        result = cap_result(text)
        self.assertEqual(len(result), 30_000)
        self.assertTrue(result.startswith("HEAD"))
        self.assertTrue(result.endswith("TAIL"))
        self.assertIn("characters omitted", result)
        self.assertEqual(cut_bytes("a🙂b", 4), "a")
        self.assertEqual(byte_size(scale(512)), 512)
        self.assertEqual(self.memory.append("echo", text).text, result)
        self.assertEqual(self.memory.append("user", text).text, text)

    def test_export_escapes_html(self):
        self.memory.append("user", "<script>alert(1)</script>")
        page = self.memory.export_html()
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>", page)


class CompactorTests(unittest.TestCase):
    def test_settle_cancellation_never_exposes_raw_text(self):
        with tempfile.TemporaryDirectory() as folder, Memory(Path(folder), 64, 256) as mem:
            mem.append("user", "pending" * 100)
            worker = Compactor(mem, Reply)
            try:
                with self.assertRaises(InterruptedError):
                    worker.settle(cancelled=lambda: True)
                with self.assertRaises(RuntimeError):
                    mem.render()
                self.assertEqual(len(mem.messages), 1)
            finally:
                worker.close()

    def test_context_no_ids_whole_source_and_same_conversation_retries(self):
        with tempfile.TemporaryDirectory() as folder, Memory(Path(folder), 64, 256) as mem:
            mem.append("user", "context first")
            mem.append("user", "full source " * 100)
            calls = []

            class Oversize:
                def ask(self, prompt):
                    calls.append(prompt)
                    return "é" * 40 if len(calls) == 1 else "user: condensed"

            factory = unittest.mock.Mock(return_value=Oversize())
            worker = Compactor(mem, factory)
            worker.build(Part(0, 0))
            worker.build(Part(0, 1))
            worker.close()
            self.assertEqual(factory.call_count, 1)
            self.assertIn("<chat>\nuser: context first\n</chat>", calls[0])
            self.assertNotIn("0+1|", calls[0])
            self.assertIn("full source " * 100, calls[0])
            self.assertIn("That line is 80 bytes", calls[1])
            self.assertEqual(mem.nodes[Part(0, 1)].text, "user: condensed")

    def test_shortest_attempt_after_five_tries(self):
        with tempfile.TemporaryDirectory() as folder, Memory(Path(folder), 64, 256) as mem:
            mem.append("user", "source" * 100)

            class Oversize:
                index = 0

                def ask(self, prompt):
                    size = [90, 80, 85, 81, 88][self.index]
                    self.index += 1
                    return "s" * size

            worker = Compactor(mem, Oversize)
            worker.build(Part(0, 0))
            worker.close()
            self.assertEqual(mem.nodes[Part(0, 0)].size, 80)

    def test_batch_leaves_compress_in_one_call_and_save_each(self):
        with tempfile.TemporaryDirectory() as folder, Memory(Path(folder), 64, 256) as mem:
            for i in range(3):
                mem.append("user", f"message {i} " + "source " * 20)
            calls = []

            class Batch:
                def ask(self, prompt):
                    calls.append(prompt)
                    return "\n".join(f"user: batch {i}" for i in range(3))

            factory = unittest.mock.Mock(return_value=Batch())
            worker = Compactor(mem, factory, batch=8)
            worker.build_batch([Part(0, 0), Part(0, 1), Part(0, 2)])
            worker.close()
            self.assertEqual(factory.call_count, 1)
            self.assertIn("MESSAGE 1:", calls[0])
            self.assertIn("MESSAGE 3:", calls[0])
            self.assertEqual([mem.nodes[Part(0, i)].text for i in range(3)],
                             ["user: batch 0", "user: batch 1", "user: batch 2"])

    def test_batch_direct_saves_short_messages_without_model(self):
        with tempfile.TemporaryDirectory() as folder, Memory(Path(folder), 64, 256) as mem:
            mem.append("user", "long one " * 20)
            mem.append("echo", "ok")
            mem.append("user", "long two " * 20)

            class Batch:
                def ask(self, prompt):
                    return "user: first\nuser: second"

            factory = unittest.mock.Mock(return_value=Batch())
            worker = Compactor(mem, factory, batch=8)
            worker.build_batch([Part(0, 0), Part(0, 1), Part(0, 2)])
            worker.close()
            self.assertEqual(factory.call_count, 1)
            self.assertEqual(mem.nodes[Part(0, 1)].text, "echo: ok")
            self.assertEqual(mem.nodes[Part(0, 0)].text, "user: first")
            self.assertEqual(mem.nodes[Part(0, 2)].text, "user: second")

    def test_batch_parses_numbered_lines(self):
        with tempfile.TemporaryDirectory() as folder, Memory(Path(folder), 64, 256) as mem:
            for i in range(2):
                mem.append("user", f"message {i} " + "source " * 20)

            class Numbered:
                def ask(self, prompt):
                    return "1. user: first\n2. user: second"

            factory = unittest.mock.Mock(return_value=Numbered())
            worker = Compactor(mem, factory, batch=8)
            worker.build_batch([Part(0, 0), Part(0, 1)])
            worker.close()
            self.assertEqual(factory.call_count, 1)
            self.assertEqual(mem.nodes[Part(0, 0)].text, "user: first")
            self.assertEqual(mem.nodes[Part(0, 1)].text, "user: second")

    def test_batch_rejects_misnumbered_lines(self):
        with tempfile.TemporaryDirectory() as folder, Memory(Path(folder), 64, 256) as mem:
            for i in range(2):
                mem.append("user", f"message {i} " + "source " * 20)
            calls = []

            class Wrong:
                def ask(self, prompt):
                    calls.append(prompt)
                    if len(calls) == 1:
                        return "2. user: first\n1. user: second"
                    return "user: single"

            factory = unittest.mock.Mock(return_value=Wrong())
            worker = Compactor(mem, factory, batch=8)
            worker.build_batch([Part(0, 0), Part(0, 1)])
            worker.close()
            self.assertEqual(factory.call_count, 3)
            self.assertTrue(all(node.text == "user: single" for node in mem.nodes.values()))

    def test_batch_falls_back_to_single_calls_on_wrong_count(self):
        with tempfile.TemporaryDirectory() as folder, Memory(Path(folder), 64, 256) as mem:
            for i in range(3):
                mem.append("user", f"message {i} " + "source " * 20)
            calls = []

            class Short:
                def ask(self, prompt):
                    calls.append(prompt)
                    return "user: one line only"

            factory = unittest.mock.Mock(return_value=Short())
            worker = Compactor(mem, factory, batch=8)
            worker.build_batch([Part(0, 0), Part(0, 1), Part(0, 2)])
            worker.close()
            self.assertEqual(factory.call_count, 4)
            self.assertEqual(len(mem.nodes), 3)
            self.assertTrue(all(node.text == "user: one line only" for node in mem.nodes.values()))

    def test_batch_retries_oversize_lines_individually(self):
        with tempfile.TemporaryDirectory() as folder, Memory(Path(folder), 64, 256) as mem:
            for i in range(2):
                mem.append("user", f"message {i} " + "source " * 20)
            calls = []

            class Mixed:
                def ask(self, prompt):
                    calls.append(prompt)
                    if len(calls) == 1:
                        return "user: good\n" + "x" * 100
                    return "user: fixed"

            factory = unittest.mock.Mock(return_value=Mixed())
            worker = Compactor(mem, factory, batch=8)
            worker.build_batch([Part(0, 0), Part(0, 1)])
            worker.close()
            self.assertEqual(mem.nodes[Part(0, 0)].text, "user: good")
            self.assertEqual(mem.nodes[Part(0, 1)].text, "user: fixed")

    def test_background_batch_and_single_paths_both_settle(self):
        for batch in (1, 8):
            with tempfile.TemporaryDirectory() as folder, Memory(Path(folder), 64, 256) as mem:
                for i in range(3):
                    mem.append("user", f"message {i} " + "source " * 20)
                calls = []

                class Sized:
                    def ask(self, prompt):
                        calls.append(prompt)
                        count = prompt.count("MESSAGE ")
                        if count:
                            return "\n".join(f"user: batch {i}" for i in range(count))
                        return "user: single"

                worker = Compactor(mem, Sized, batch=batch)
                worker.start()
                try:
                    worker.settle(timeout=2)
                finally:
                    worker.close()
                expected = 1 if batch > 1 else 3
                self.assertEqual(len(calls), expected, f"batch={batch}")
                self.assertTrue(all(Part(0, i) in mem.nodes for i in range(3)))

    def test_background_failure_retries_and_settle_waits(self):
        with tempfile.TemporaryDirectory() as folder, Memory(Path(folder), 64, 256) as mem:
            mem.append("user", "source" * 100)
            state = {"tries": 0}
            reports = []

            class Flaky:
                def ask(self, prompt):
                    state["tries"] += 1
                    if state["tries"] < 3:
                        raise RuntimeError("transient")
                    return "user: remembered"

            worker = Compactor(mem, Flaky, retry=0.02, report=reports.append)
            worker.start()
            try:
                worker.settle(timeout=2)
                self.assertEqual(len(reports), 1)
                self.assertEqual(state["tries"], 3)
                self.assertFalse(worker.failed)
            finally:
                worker.close()


if __name__ == "__main__":
    unittest.main()
