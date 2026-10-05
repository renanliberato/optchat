"""Observability stays read-only and does not interfere with memory correctness."""
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from optchat.monitor import snapshot
from optchat.telemetry import Telemetry
from optchat.compactor import ClaudeConversation, CodexConversation


class MonitorTests(unittest.TestCase):
    def test_offline_probe_does_not_create_home(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / 'missing'
            result = snapshot(home)
            self.assertFalse(result['running'])
            self.assertIsNone(result['metrics'])
            self.assertFalse(home.exists())

    def test_storage_ignores_symlinks_and_offline_gauges(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / 'home'
            home.mkdir()
            (home / 'main.jsonl').write_bytes(b'abc')
            (home / 'outside').symlink_to(Path(folder), target_is_directory=True)
            telemetry = Telemetry(home)
            telemetry.record('zoom', .02)
            result = snapshot(home)
            self.assertFalse(result['running'])
            self.assertIsNone(result['metrics']['active_summarizers'])
            self.assertEqual(result['disk_bytes'], 3 + telemetry.path.stat().st_size)

    def test_aggregates_survive_restart_and_trim_hourly_history(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            telemetry = Telemetry(home)
            telemetry.data['hourly']['0'] = {'nodes': 99}
            telemetry.record('zoom', .02)
            telemetry.record('zoom', .04, False)
            telemetry.usage({'input': 12, 'output': 7})
            telemetry.usage(None)
            restored = Telemetry(home).snapshot()
            self.assertEqual(restored['operations']['zoom']['count'], 2)
            self.assertEqual(restored['operations']['zoom']['errors'], 1)
            self.assertAlmostEqual(restored['operations']['zoom']['total_ms'], 60)
            self.assertEqual(restored['tokens'], {'input': 12, 'output': 7})
            self.assertEqual(restored['unreported_calls'], 1)
            self.assertNotIn('0', restored['hourly'])
            self.assertEqual(restored['active_summarizers'], 0)
            self.assertEqual(telemetry.path.stat().st_mode & 0o777, 0o600)

    def test_concurrent_summarizer_gauge_and_failure_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            telemetry = Telemetry(Path(folder))
            entered = threading.Barrier(3)
            finish = threading.Event()
            class Provider:
                def ask(self, text):
                    entered.wait(timeout=2)
                    finish.wait(timeout=2)
                    raise ValueError('expected failure')
            factory = telemetry.conversation(Provider)
            def run():
                with self.assertRaises(ValueError):
                    factory().ask('not recorded')
            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads: thread.start()
            entered.wait(timeout=2)
            self.assertEqual(telemetry.snapshot()['active_summarizers'], 2)
            finish.set()
            for thread in threads: thread.join(timeout=2)
            self.assertEqual(telemetry.snapshot()['active_summarizers'], 0)
            self.assertEqual(telemetry.snapshot()['operations']['summarize']['errors'], 2)
            self.assertNotIn('not recorded', telemetry.path.read_text())

    def test_metrics_write_failure_does_not_fail_operation(self):
        with tempfile.TemporaryDirectory() as folder:
            telemetry = Telemetry(Path(folder))
            with patch('optchat.telemetry.os.replace', side_effect=OSError('disk full')):
                telemetry.record('zoom', .01)
            self.assertEqual(telemetry.snapshot()['operations']['zoom']['count'], 1)

    def test_malformed_metrics_are_disposable(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            for value in [[], {"operations": {"zoom": {"count": "invalid"}}, "hourly": {"bad": []}}]:
                (home / "metrics.json").write_text(json.dumps(value))
                telemetry = Telemetry(home)
                telemetry.record("zoom", .01)
                self.assertEqual(telemetry.snapshot()["operations"]["zoom"]["count"], 1)

    def test_vendor_usage_is_normalized_without_double_counting_cache(self):
        from subprocess import CompletedProcess
        claude_reply = {'result': 'summary', 'usage': {'input_tokens': 10,
            'output_tokens': 4, 'cache_read_input_tokens': 20, 'cache_creation_input_tokens': 5}}
        codex_events = [{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'summary'}},
                        {'type': 'turn.completed', 'usage': {'input_tokens': 30,
                         'output_tokens': 4, 'cached_input_tokens': 20}}]
        with patch('optchat.compactor.subprocess.run', return_value=CompletedProcess([], 0, json.dumps(claude_reply), '')):
            claude = ClaudeConversation()
            self.assertEqual(claude.ask('prompt'), 'summary')
            self.assertEqual(claude.last_usage, {'input': 10, 'output': 4, 'cache_read': 20, 'cache_write': 5})
        with patch('optchat.compactor.subprocess.run', return_value=CompletedProcess([], 0, '\n'.join(map(json.dumps, codex_events)), '')):
            codex = CodexConversation()
            self.assertEqual(codex.ask('prompt'), 'summary')
            self.assertEqual(codex.last_usage, {'input': 10, 'output': 4, 'cache_read': 20})
