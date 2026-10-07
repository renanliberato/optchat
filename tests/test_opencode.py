"""OpenCode protocol checks with offline CLI fixtures."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from optchat import opencode
from optchat.adapters import Events, agent_command, run_turn
from optchat.cli import execute, parser
from optchat.compactor import COMPACT, OpenCodeConversation, summarizer_factory
from test_adapters import LocalClient
from optchat.memory import Memory


def text_event(text="summary", identity="reply"):
    return {"type": "text", "part": {"id": identity, "text": text, "time": {"start": 1, "end": 2}}}


class OpenCodeTests(unittest.TestCase):
    def test_cli_defaults_and_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            args = parser().parse_args(["--home", directory, "init", "--summarizer", "opencode"])
            with patch("builtins.print"):
                execute(args)
            config = json.loads((Path(directory) / "config.json").read_text())
            self.assertEqual(config["summary_model"], opencode.DEFAULT_MODEL)
        args = parser().parse_args(["run", "--agent", "opencode", "--model", "provider/other", "hi"])
        self.assertEqual(args.model, "provider/other")
        conversation = summarizer_factory({"summarizer": "opencode"})()
        self.assertIsInstance(conversation, OpenCodeConversation)
        self.assertEqual(conversation.model, opencode.DEFAULT_MODEL)
        custom = summarizer_factory({"summarizer": "opencode", "summary_model": "p/m",
                                     "opencode_binary": "/bin/custom", "summary_timeout": 12})()
        self.assertEqual((custom.model, custom.binary, custom.timeout), ("p/m", "/bin/custom", 12))
        command = agent_command("opencode", Path("/tmp/memory"), "system")
        self.assertEqual(command, opencode.command())
        self.assertNotIn("--continue", command)
        with self.assertRaises(ValueError):
            agent_command("opencode", Path("/tmp/memory"), "system", sandbox="read-only")

    def test_inline_config_preserves_provider_overrides_and_denies_summary_tools(self):
        previous = {"provider": {"custom": {"name": "custom"}}, "mcp": {"other": {"enabled": True}}}
        with patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": json.dumps(previous)}):
            env = opencode.environment("Olá\n👋", ["python", "mcp"])
            config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
            self.assertEqual(config["provider"], previous["provider"])
            self.assertEqual(config["agent"]["optchat"]["prompt"], "Olá\n👋")
            self.assertEqual(config["mcp"]["optchat"]["command"], ["python", "mcp"])
            self.assertEqual(config["share"], "disabled")
            summary = json.loads(opencode.environment(COMPACT, internal=True)["OPENCODE_CONFIG_CONTENT"])
            self.assertEqual(summary["agent"]["optchat"]["permission"], {"*": "deny"})
            self.assertFalse(summary["mcp"]["other"]["enabled"])
            self.assertEqual(json.loads(os.environ["OPENCODE_CONFIG_CONTENT"]), previous)

    def test_wrapper_captures_completed_events_once_and_excludes_reasoning(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            memory = Memory(home)
            client = LocalClient(memory)
            client.home = home
            binary = home / "fake-opencode"
            capture = home / "capture.json"
            tool = {"type": "tool_use", "part": {"id": "part", "callID": "call", "tool": "bash",
                    "state": {"status": "completed", "input": {"command": "pwd"}, "output": "here"}}}
            events = [{"type": "reasoning", "part": {"text": "SECRET"}},
                      {"type": "text", "part": {"id": "reply", "text": "partial", "time": {"start": 1}}},
                      tool, tool, text_event("OpenCode finished"), text_event("OpenCode finished")]
            binary.write_text("#!/usr/bin/env python3\nimport json, os, sys\nfrom pathlib import Path\n"
                f"Path({str(capture)!r}).write_text(json.dumps({{'argv': sys.argv, 'prompt': sys.stdin.read(), 'config': json.loads(os.environ['OPENCODE_CONFIG_CONTENT'])}}))\n"
                f"for event in {events!r}: print(json.dumps(event), flush=True)\n")
            binary.chmod(0o700)
            try:
                shown = []
                memory.append("note", "prior decision")
                run_turn(client, "opencode", "new request", binary=str(binary), cwd=directory, show=shown.append)
                self.assertEqual(shown, ["OpenCode finished"])
                self.assertEqual([m.kind for m in memory.messages], ["note", "user", "tool", "echo", "talk"])
                self.assertNotIn("SECRET", " ".join(m.text for m in memory.messages))
                captured = json.loads(capture.read_text())
                self.assertIn("0+1|(not summarized yet: zoom it)", captured["prompt"])
                self.assertTrue(captured["prompt"].endswith("new request"))
                self.assertEqual(captured["config"]["mcp"]["optchat"]["command"][-1], "mcp")
                recorder = Events(client, "opencode", "error")
                recorder.consume({"type": "error", "error": {"data": {"message": "quota"}}})
                self.assertIn("quota", recorder.error)
            finally:
                memory.close()

    def test_summary_transcript_usage_dedupe_and_errors(self):
        step = {"type": "step_finish", "part": {"id": "step", "tokens": {
            "input": 10, "output": 3, "cache": {"read": 20, "write": 4}}}}
        events = [text_event("kept", "a"), text_event("kept", "a"), text_event("facts", "b"), step, step]
        result = subprocess.CompletedProcess([], 0, "\n".join(map(json.dumps, events)), "")
        with patch("optchat.compactor.subprocess.run", return_value=result) as run:
            conversation = OpenCodeConversation()
            self.assertEqual(conversation.ask("compress"), "kept\nfacts")
            self.assertEqual(conversation.ask("shorter"), "kept\nfacts")
            call = run.call_args
            self.assertIn("assistant: kept\nfacts", call.kwargs["input"])
            self.assertIn("user: shorter", call.kwargs["input"])
            self.assertEqual(conversation.last_usage, {"input": 10, "output": 3, "cache_read": 20, "cache_write": 4})
            self.assertEqual(call.kwargs["env"]["OPTCHAT_INTERNAL"], "1")
            self.assertFalse(Path(call.kwargs["cwd"]).exists())
        for returncode, events in [(0, []), (0, [{"type": "error", "error": "quota"}]), (1, [])]:
            result = subprocess.CompletedProcess([], returncode, "\n".join(map(json.dumps, events)), "failure")
            with patch("optchat.compactor.subprocess.run", return_value=result):
                conversation = OpenCodeConversation()
                with self.assertRaises(RuntimeError):
                    conversation.ask("compress")
                self.assertEqual(conversation.messages, [])
