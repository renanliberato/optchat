"""Real processes and sockets; fake vendor CLIs keep tests offline and free."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from optchat.adapters import run_turn
from optchat.daemon import Client, socket_path

ROOT = Path(__file__).resolve().parent.parent


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="optchat-test-")
        self.root = Path(self.folder.name)
        self.home = self.root / "memory"
        self.home.mkdir()
        self.provider = self.root / "summarize.py"
        self.provider.write_text("import json, sys\npayload=json.load(sys.stdin)\n"
                                 "assert payload['system']\nassert payload['messages']\n"
                                 "print('user: decisions retained; talk: work completed')\n")
        (self.home / "config.json").write_text(json.dumps({"summarizer": "command",
             "summary_command": [sys.executable, str(self.provider)], "node_bytes": 64,
             "view_bytes": 512, "retry_seconds": 0.05}))
        self.client = Client(self.home)

    def tearDown(self):
        try:
            Client(self.home, autostart=False).call("shutdown")
            deadline = time.monotonic() + 5
            while socket_path(self.home).exists() and time.monotonic() < deadline:
                time.sleep(0.02)
        except RuntimeError:
            pass
        self.folder.cleanup()

    def cli(self, *args, input=None):
        result = subprocess.run([sys.executable, "-m", "optchat", "--home", str(self.home), *args],
                                input=input, text=True, capture_output=True, cwd=ROOT, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_concurrent_clients_single_writer_restart_and_mcp(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(self.client.call, "append", kind="user", text=f"message {i}") for i in range(12)]
            ids = [future.result()["i"] for future in futures]
        self.assertEqual(sorted(ids), list(range(12)))
        status = self.client.call("compact", timeout=5)
        self.assertTrue(status["settled"])
        self.assertEqual(status["nodes"], 22)  # 12 + 6 + 3 + 1
        view = self.client.call("context", timeout=5)
        self.assertNotIn("not summarized", view)
        self.assertTrue(self.client.call("zoom", id=0, n=1).startswith("0+0|user: message"))
        self.client.call("shutdown")
        deadline = time.monotonic() + 5
        while socket_path(self.home).exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.client.call("status")["messages"], 12)
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "zoom", "arguments": {"id": 0, "n": 1}}}]
        replies = self.cli("mcp", input="".join(json.dumps(r) + "\n" for r in requests))
        self.assertEqual(len(replies.splitlines()), 2)
        self.assertIn("0+0|user:", json.loads(replies.splitlines()[1])["result"]["content"][0]["text"])

    def fake_agent(self, agent):
        path = self.root / f"fake-{agent}"
        capture = self.root / f"{agent}-input.json"
        if agent == "codex":
            events = [{"type": "item.started", "item": {"id": "c", "type": "command_execution", "command": "pwd"}},
                      {"type": "item.completed", "item": {"id": "c", "type": "command_execution", "command": "pwd", "aggregated_output": "here", "exit_code": 0}},
                      {"type": "item.completed", "item": {"id": "r", "type": "reasoning", "text": "SECRET"}},
                      {"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "Codex finished"}}]
        else:
            events = [{"type": "assistant", "message": {"id": "a", "content": [{"type": "text", "text": "Claude finished"}]}},
                      {"type": "result", "result": "Claude finished", "is_error": False}]
        script = (f"#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n"
                  f"Path({str(capture)!r}).write_text(json.dumps({{'argv':sys.argv, 'prompt':sys.stdin.read()}}))\n"
                  f"for event in {events!r}:\n    print(json.dumps(event), flush=True)\n")
        path.write_text(script)
        path.chmod(0o700)
        return path, capture

    def test_fresh_wrappers_share_history_across_vendors(self):
        codex, codex_input = self.fake_agent("codex")
        claude, claude_input = self.fake_agent("claude")
        shown = []
        run_turn(self.client, "codex", "first request", binary=str(codex), cwd=str(self.root), show=shown.append)
        self.assertEqual(json.loads(codex_input.read_text())["prompt"], "<chat>\n\n</chat>\n\nfirst request")
        run_turn(self.client, "claude", "second request", binary=str(claude), cwd=str(self.root), show=shown.append)
        prompt = json.loads(claude_input.read_text())["prompt"]
        self.assertIn("user: first request", prompt)
        self.assertIn("talk: Codex finished", prompt)
        self.assertNotIn("user: second request", prompt)
        self.assertTrue(prompt.endswith("\n\nsecond request"))
        self.assertEqual(shown, ["Codex finished", "Claude finished"])
        self.assertEqual(self.client.call("status")["messages"], 6)
        self.assertNotIn("SECRET", self.client.call("export"))

    def test_import_is_idempotent_and_export_is_browsable(self):
        imported = self.root / "old.jsonl"
        imported.write_text('{"text":"a note","date":"2020-01-01T12:00:00+00:00"}\n')
        self.cli("import", str(imported))
        self.cli("import", str(imported))
        self.assertEqual(self.client.call("status")["messages"], 1)
        self.assertIn("2020", self.client.call("date", id=0))
        page = self.root / "memory.html"
        self.cli("export", str(page))
        self.assertIn("a note", page.read_text())

    def test_failed_launch_keeps_unanswered_user_message(self):
        with self.assertRaises(FileNotFoundError):
            run_turn(self.client, "codex", "must survive", binary="/missing/optchat-test-executable")
        self.assertEqual(self.client.call("status")["messages"], 1)
        self.assertIn("must survive", self.client.call("zoom", id=0, n=1))


if __name__ == "__main__":
    unittest.main()
