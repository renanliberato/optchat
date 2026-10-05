import json
import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from optchat.adapters import Events, agent_command, hook, hook_config
from optchat.memory import Memory
from optchat.compactor import CodexConversation, OPTCHAT_GUIDANCE, summarizer_factory
from optchat.mcp import handle


class LocalClient:
    def __init__(self, memory):
        self.memory = memory
        self.calls = []

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "append":
            return vars(self.memory.append(**params))
        if method == "zoom":
            return self.memory.zoom(**params)
        if method == "date":
            return self.memory.date(**params)
        if method == "begin":
            view = self.memory.render(False)
            return {"view": view, "message_id": self.memory.append("user", params["text"], params.get("event_key")).i}
        return {}


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)
        self.memory = Memory(self.home)
        self.client = LocalClient(self.memory)

    def tearDown(self):
        self.memory.close()
        self.directory.cleanup()

    def test_codex_logs_call_result_replies_once_and_no_reasoning(self):
        events = Events(self.client, "codex", "turn", show=lambda _: None)
        events.consume({"type": "item.completed", "item": {"id": "r", "type": "reasoning", "text": "SECRET"}})
        tool = {"id": "c", "type": "command_execution", "command": "ls", "status": "in_progress"}
        events.consume({"type": "item.started", "item": tool})
        completed = {"type": "item.completed", "item": {**tool, "aggregated_output": "files", "exit_code": 0}}
        events.consume(completed)
        events.consume(completed)
        events.consume({"type": "item.completed", "item": {"id": "t", "type": "agent_message", "text": "done"}})
        self.assertEqual([m.kind for m in self.memory.messages], ["tool", "echo", "talk"])
        self.assertNotIn("SECRET", str(self.memory.messages))
        self.assertIn("files", self.memory.messages[1].text)

    def test_claude_completed_blocks_results_and_no_thinking(self):
        events = Events(self.client, "claude", "turn", show=lambda _: None)
        event = {"type": "assistant", "message": {"id": "m", "content": [
            {"type": "thinking", "thinking": "SECRET"}, {"type": "text", "text": "looking"},
            {"type": "tool_use", "id": "tool1", "name": "Read", "input": {"file_path": "x"}}]}}
        events.consume(event)
        events.consume(event)
        events.consume({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tool1", "content": "contents"}]}})
        events.consume({"type": "result", "result": "looking", "is_error": False})
        self.assertEqual([m.kind for m in self.memory.messages], ["talk", "tool", "echo"])
        self.assertNotIn("SECRET", str(self.memory.messages))

    def test_hook_deduplication_and_wrapper_bypass(self):
        payload = {"hook_event_name": "PostToolUse", "session_id": "s", "tool_use_id": "t",
                   "tool_name": "Bash", "tool_input": {"command": "pwd"}, "tool_response": "here"}
        hook(self.client, "claude", {**payload, "hook_event_name": "PreToolUse"})
        hook(self.client, "claude", payload)
        hook(self.client, "claude", payload)
        self.assertEqual([m.kind for m in self.memory.messages], ["tool", "echo"])
        with patch.dict(os.environ, {"OPTCHAT_WRAPPER": "1"}):
            self.assertEqual(hook(self.client, "codex", payload), {})
        self.assertEqual(len(self.memory.messages), 2)

    def test_hook_injects_old_view_and_preserves_repeated_prompts(self):
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": "again"}
        reply = hook(self.client, "claude", payload)
        self.assertNotIn("user: again", reply["hookSpecificOutput"]["additionalContext"])
        hook(self.client, "claude", payload)
        self.assertEqual(len(self.memory.messages), 2)

    def test_session_start_adds_guidance_without_logging_it(self):
        for agent in ("codex", "claude"):
            reply = hook(self.client, agent, {"hook_event_name": "SessionStart", "session_id": "s"})
            context = reply["hookSpecificOutput"]["additionalContext"]
            self.assertTrue(context.startswith(OPTCHAT_GUIDANCE))
            self.assertIn("zoom(id, n)", context)
            self.assertIn("saved-file path", context)
            self.assertLess(len(context), 10_000)
        self.assertEqual(len(self.memory.messages), 0)

    def test_prompt_guidance_precedes_view_and_codex_allows_large_context(self):
        reply = hook(self.client, "codex", {"hook_event_name": "UserPromptSubmit", "prompt": "task"})
        context = reply["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(context.startswith(OPTCHAT_GUIDANCE))
        self.assertLess(context.index("zoom(id, n)"), context.index("<chat>\n"))
        config = hook_config("codex", self.home)
        self.assertEqual(config["hooks"]["UserPromptSubmit"][0]["hooks"][0]["additionalContextLimit"], 100_000)
        claude = hook_config("claude", self.home)
        self.assertNotIn("additionalContextLimit", claude["hooks"]["UserPromptSubmit"][0]["hooks"][0])

    def test_commands_start_fresh_and_wire_mcp(self):
        for agent in ("codex", "claude"):
            command = agent_command(agent, self.home, "constant instructions", cwd=str(self.home))
            self.assertNotIn("--resume", command)
            self.assertNotIn("--continue", command)
            self.assertIn("optchat", " ".join(command))
        self.assertIn("--ephemeral", agent_command("codex", self.home, "s"))
        self.assertIn("--no-session-persistence", agent_command("claude", self.home, "s"))
        self.assertIn("UserPromptSubmit", hook_config("codex", self.home)["hooks"])

    def test_codex_toml_overrides_preserve_unicode_instructions(self):
        instructions = "Olá 👋\nUse exact Unicode"
        command = agent_command("codex", self.home, instructions)
        parsed = [tomllib.loads(command[i + 1]) for i, value in enumerate(command) if value == "-c"]
        self.assertEqual(parsed[0]["developer_instructions"], instructions)

    def fake_codex(self, events):
        path = self.home / "fake-codex"
        capture = self.home / "codex-calls.jsonl"
        script = (f"#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n"
                  f"Path({str(capture)!r}).open('a').write(json.dumps({{'argv': sys.argv[1:], 'prompt': sys.stdin.read()}}) + '\\n')\n"
                  f"for event in {events!r}:\n    print(json.dumps(event), flush=True)\n")
        path.write_text(script)
        path.chmod(0o700)
        return path, capture

    def test_codex_summarizer_sends_transcript_and_parses_events(self):
        reply = {"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "user: kept"}}
        binary, capture = self.fake_codex([
            {"type": "thread.started", "thread_id": "t"},
            {"type": "error", "message": "non-fatal warning"},
            reply, reply, {"type": "turn.completed", "usage": {}}])
        conversation = CodexConversation(model="gpt-6-luna", binary=str(binary))
        self.assertEqual(conversation.ask("compress this"), "user: kept")
        self.assertEqual(conversation.ask("that line is too long"), "user: kept")
        calls = [json.loads(line) for line in capture.read_text().splitlines()]
        self.assertEqual(calls[0]["prompt"], "compress this")
        self.assertIn("assistant: user: kept", calls[1]["prompt"])
        self.assertIn("user: that line is too long", calls[1]["prompt"])
        for flag in ("--ephemeral", "--ignore-user-config", "--json"):
            self.assertIn(flag, calls[1]["argv"])
        self.assertIn('model_reasoning_effort="high"', calls[1]["argv"])
        self.assertTrue(any(a.startswith("developer_instructions=") for a in calls[1]["argv"]))

    def test_codex_summarizer_reports_missing_reply(self):
        binary, _ = self.fake_codex([{"type": "error", "message": "quota exhausted"}])
        conversation = CodexConversation(binary=str(binary))
        with self.assertRaises(RuntimeError) as caught:
            conversation.ask("compress this")
        self.assertIn("no summary", str(caught.exception))
        self.assertIn("quota exhausted", str(caught.exception))

    def test_summarizer_factory_selects_codex_defaults(self):
        factory = summarizer_factory({"summarizer": "codex", "codex_binary": "/bin/fake",
                                      "codex_reasoning_effort": "high"})
        conversation = factory()
        self.assertIsInstance(conversation, CodexConversation)
        self.assertEqual((conversation.model, conversation.binary, conversation.effort),
                         ("gpt-6-luna", "/bin/fake", "high"))

    def test_mcp_initialization_tools_and_errors(self):
        self.memory.append("user", "original")
        initialized = handle({"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}, self.client)
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")
        self.assertTrue(initialized["result"]["instructions"].startswith(OPTCHAT_GUIDANCE))
        self.assertIsNone(handle({"method": "notifications/initialized"}, self.client))
        tools = handle({"id": 2, "method": "tools/list"}, self.client)
        self.assertEqual([t["name"] for t in tools["result"]["tools"]], ["zoom", "date"])
        result = handle({"id": 3, "method": "tools/call", "params": {"name": "zoom", "arguments": {"id": 0, "n": 1}}}, self.client)
        self.assertEqual(result["result"]["content"][0]["text"], "0+0|user: original")
        error = handle({"id": 4, "method": "tools/call", "params": {"name": "zoom", "arguments": {"id": 1, "n": 1}}}, self.client)
        self.assertTrue(error["result"]["isError"])
