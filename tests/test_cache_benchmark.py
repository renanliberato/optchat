"""The benchmark counts distinct-node first attempts, not corrections as hits."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from optchat.compactor import RECENT_CHAT
from optchat.memory import Memory, Part

spec = importlib.util.spec_from_file_location("cache_benchmark", Path(__file__).resolve().parents[1]
                                            / "scripts/benchmark-summary-cache.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


class BenchmarkTests(unittest.TestCase):
    def test_streaming_copy_isolated_and_corrections_counted_separately(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            with Memory(home, 64, 256) as mem:
                for i in range(4):
                    mem.append("user", f"decision {i} " * 40)
                    mem.save_node(Part(0, i), "user: archived decision")
                mem.save_node(Part(1, 0), "user: initial merged decisions")
                mem.save_node(Part(1, 1), "user: new merged decisions")
            original = {p: p.read_bytes() for d in ("main", "tree") for p in (home / d).glob("*")}
            class Fake:
                def __init__(self, **kwargs):
                    self.count = 0
                    self.last_provider = "test"
                    self.last_cost_details = {"upstream_inference_prompt_cost": .0005}
                def ask(self, prompt):
                    self.count += 1
                    self.last_usage = {"input": 10, "output": 5,
                                       "cache_read": 50 if RECENT_CHAT in prompt else 0}
                    self.last_cost = .001
                    return "x" * 80 if self.count == 1 else "user: kept decision"
            with patch.object(bench, "OpenRouterConversation", Fake):
                report = bench.benchmark(home, 2, 2, .1,
                                         {"openrouter_api_key": "never-export-key", "node_bytes": 64,
                                          "view_bytes": 256})
            for arm in report["arms"].values():
                self.assertEqual(arm["first_attempts"]["calls"], 2)
                self.assertEqual(arm["corrections"]["calls"], 2)
                self.assertTrue(all(r["within_limit"] for r in arm["results"]))
                self.assertEqual([r["id"] for r in arm["results"]], [2, 3])
            self.assertNotIn("never-export-key", json.dumps(report))
            self.assertEqual(original, {p: p.read_bytes() for p in original})

    def test_budget_failure_preserves_paid_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            with Memory(home, 64, 256) as mem:
                for i in range(2):
                    mem.append("user", "source " * 40)
            class Fake:
                def __init__(self, **kwargs):
                    self.last_usage = {"input": 10, "output": 5, "cache_read": 0}
                    self.last_cost, self.last_provider, self.last_cost_details = .01, "test", {}
                def ask(self, prompt):
                    return "user: kept"
            with patch.object(bench, "OpenRouterConversation", Fake):
                report = bench.benchmark(home, 0, 2, .005,
                                         {"openrouter_api_key": "fake", "node_bytes": 64, "view_bytes": 256})
            self.assertEqual(report["spent_usd"], .01)
            self.assertEqual(report["arms"]["baseline"]["all_calls"]["calls"], 1)
            self.assertIn("ceiling", report["arms"]["baseline"]["results"][-1]["error"])
