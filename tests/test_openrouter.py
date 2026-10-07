"""OpenRouter protocol checks with an offline HTTP endpoint."""

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from optchat.cli import execute, parser
from optchat.compactor import (COMPACT, RECENT_CHAT, OPENROUTER_DEFAULT_MODEL, OpenRouterConversation,
                               summarizer_factory)


class FakeOpenRouter(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.server.requests.append({"path": self.path, "headers": dict(self.headers),
                                     "body": json.loads(self.rfile.read(length))})
        status, payload = self.server.responses.pop(0)
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class OpenRouterTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenRouter)
        self.server.requests = []
        self.server.responses = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/api/v1"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def reply(self, text="user: kept", status=200):
        self.server.responses.append((status, {
            "choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 7, "cost": 0.00001,
                      "prompt_tokens_details": {"cached_tokens": 40}}}))

    def test_request_shape_usage_and_transcript(self):
        self.reply("line one")
        self.reply("line two")
        conversation = OpenRouterConversation(api_key="secret", base_url=self.base, timeout=5, effort="low")
        self.assertEqual(conversation.ask("first"), "line one")
        self.assertEqual(conversation.ask("second"), "line two")
        first, second = self.server.requests
        self.assertEqual(first["path"], "/api/v1/chat/completions")
        self.assertEqual(first["body"]["model"], OPENROUTER_DEFAULT_MODEL)
        self.assertEqual(first["body"]["messages"], [{"role": "system", "content": COMPACT},
                                                     {"role": "user", "content": "first"}])
        self.assertEqual(first["body"]["reasoning"], {"effort": "low"})
        self.assertTrue(first["body"]["usage"]["include"])
        self.assertEqual(first["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(second["body"]["messages"][-3:], [
            {"role": "user", "content": "first"}, {"role": "assistant", "content": "line one"},
            {"role": "user", "content": "second"}])
        self.assertEqual(conversation.last_usage, {"input": 60, "output": 7, "cache_read": 40,
                                                   "cache_write": 0})
        self.assertEqual(conversation.last_cost, 0.00001)

    def test_effort_can_be_omitted(self):
        self.reply()
        OpenRouterConversation(api_key="secret", base_url=self.base, timeout=5, effort=None).ask("x")
        self.assertNotIn("reasoning", self.server.requests[0]["body"])

    def test_shared_prefix_only_is_cached_and_survives_correction(self):
        self.reply("oversize")
        self.reply("short")
        c = OpenRouterConversation(api_key="secret", base_url=self.base)
        c.ask("frozen history" + RECENT_CHAT + "fresh tail\n</recent_chat>\nsource")
        c.ask("shorten it")
        first, correction = [r["body"] for r in self.server.requests]
        self.assertEqual(first["prompt_cache_options"], {"mode": "explicit"})
        blocks = first["messages"][1]["content"]
        self.assertEqual(blocks[0]["text"], "frozen history")
        self.assertIn("prompt_cache_breakpoint", blocks[0])
        self.assertNotIn("prompt_cache_breakpoint", blocks[1])
        self.assertEqual(correction["messages"][1]["content"], blocks)
        self.assertEqual(first["session_id"], correction["session_id"])

    def test_factory_shares_routing_across_independent_nodes(self):
        factory = summarizer_factory({"summarizer": "openrouter"})
        a, b = factory(), factory()
        self.assertEqual(a.session_id, b.session_id)
        self.assertIsNot(a.messages, b.messages)
        self.assertNotEqual(a.session_id, summarizer_factory({"summarizer": "openrouter"})().session_id)

    def test_unsupported_model_preserves_automatic_cache(self):
        self.reply()
        c = OpenRouterConversation(model="xiaomi/mimo-v2.6-flash", api_key="secret", base_url=self.base)
        c.ask("history" + RECENT_CHAT + "tail")
        body = self.server.requests[0]["body"]
        self.assertNotIn("prompt_cache_options", body)
        self.assertIsInstance(body["messages"][1]["content"], str)

    def test_provider_preferences_are_forwarded(self):
        self.reply()
        provider = {"zdr": True, "data_collection": "deny"}
        OpenRouterConversation(api_key="secret", base_url=self.base, timeout=5, provider=provider).ask("x")
        self.assertEqual(self.server.requests[0]["body"]["provider"], provider)

    def test_zero_data_retention_is_always_requested(self):
        self.reply()
        OpenRouterConversation(api_key="secret", base_url=self.base, timeout=5).ask("x")
        self.reply()
        OpenRouterConversation(api_key="secret", base_url=self.base, timeout=5,
                               provider={"zdr": False, "data_collection": "allow", "sort": "price"}).ask("x")
        self.assertEqual(self.server.requests[0]["body"]["provider"], {"zdr": True, "data_collection": "deny"})
        self.assertEqual(self.server.requests[1]["body"]["provider"],
                         {"sort": "price", "zdr": True, "data_collection": "deny"})

    def test_errors_leave_history_untouched(self):
        self.server.responses.append((500, {"error": {"message": "no credit"}}))
        conversation = OpenRouterConversation(api_key="secret", base_url=self.base, timeout=5)
        with self.assertRaisesRegex(RuntimeError, "no credit"):
            conversation.ask("x")
        self.server.responses.append((200, {"error": {"message": "bad model"}}))
        with self.assertRaisesRegex(RuntimeError, "bad model"):
            conversation.ask("x")
        self.server.responses.append((200, {"choices": [{"message": {"content": "   "}}]}))
        with self.assertRaisesRegex(RuntimeError, "no summary"):
            conversation.ask("x")
        self.assertEqual(conversation.messages, [])
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "OPENROUTER_API_KEY"):
                OpenRouterConversation(base_url=self.base, timeout=5).ask("x")

    def test_factory_and_cli_defaults(self):
        conversation = summarizer_factory({"summarizer": "openrouter", "summary_model": "openai/other",
                                           "summary_timeout": 9, "openrouter_reasoning_effort": "high",
                                           "openrouter_base_url": self.base,
                                           "openrouter_provider": {"zdr": True},
                                           "openrouter_api_key": "cfg-key"})()
        self.assertIsInstance(conversation, OpenRouterConversation)
        self.assertEqual((conversation.model, conversation.timeout, conversation.effort, conversation.base_url,
                          conversation.provider, conversation.api_key),
                         ("openai/other", 9, "high", self.base, {"zdr": True, "data_collection": "deny"},
                          "cfg-key"))
        default = summarizer_factory({"summarizer": "openrouter"})()
        self.assertEqual((default.model, default.effort), (OPENROUTER_DEFAULT_MODEL, "low"))
        self.assertEqual(default.base_url, "https://openrouter.ai/api/v1")
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "env-key"}):
            self.assertEqual(summarizer_factory({"summarizer": "openrouter"})().api_key, "env-key")
            self.assertEqual(summarizer_factory({"summarizer": "openrouter",
                                                 "openrouter_api_key": "cfg-key"})().api_key, "cfg-key")
        with tempfile.TemporaryDirectory() as directory:
            args = parser().parse_args(["--home", directory, "init", "--summarizer", "openrouter"])
            with patch("builtins.print"):
                execute(args)
            config = json.loads((Path(directory) / "config.json").read_text())
            self.assertEqual(config["summarizer"], "openrouter")
            self.assertEqual(config["summary_model"], OPENROUTER_DEFAULT_MODEL)
