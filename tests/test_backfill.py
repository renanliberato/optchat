import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from optchat.backfill import prepare, rollout_records


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.home = self.root / "memory"
        (self.home / "main").mkdir(parents=True)
        self.database = self.root / "state.sqlite"
        with sqlite3.connect(self.database) as db:
            db.execute("create table threads (id text, rollout_path text, archived integer, created_at integer)")

    def transcript(self, id, payloads, archived=0):
        path = self.root / (id + ".jsonl")
        path.write_text("\n".join(json.dumps({"type": "response_item", "timestamp": "2020-01-01T00:00:00Z",
                                              "payload": p}) for p in payloads))
        with sqlite3.connect(self.database) as db:
            db.execute("insert into threads values (?,?,?,0)", (id, str(path), archived))
        return path

    def message(self, id, text, role="user"):
        return {"type": "message", "id": id, "role": role,
                "content": [{"type": "input_text", "text": text}]}

    def test_native_conversion_excludes_internal_context_and_reasoning(self):
        path = self.transcript("a", [self.message("s", "instructions", "system"),
            self.message("e", "<environment_context>internal</environment_context>"),
            {"type": "reasoning", "text": "private"}, self.message("u", "hello"),
            self.message("a", "answer", "assistant"),
            {"type": "custom_tool_call", "id": "c", "name": "exec", "call_id": "x", "input": "code"},
            {"type": "custom_tool_call_output", "id": "o", "call_id": "x",
             "output": [{"type": "input_text", "text": "result"}]}])
        records = list(rollout_records(path))
        self.assertEqual([r["kind"] for r in records], ["user", "talk", "tool", "echo"])
        self.assertEqual(records[-1]["text"], "result")
        self.assertEqual(records[0]["date"], "2020-01-01T00:00:00Z")

    def test_fork_dedup_hook_overlap_and_rerun(self):
        shared = self.message("shared", "same inherited prompt")
        self.transcript("a", [shared, self.message("a", "already captured", "assistant"),
            {"type": "function_call", "id": "call", "name": "run", "arguments": "{}", "call_id": "x"}])
        self.transcript("b", [shared, self.message("b", "new")])
        log = self.home / "main" / "log.jsonl"
        log.write_text(json.dumps({"kind": "talk", "text": "already captured",
                                  "event_key": "hook:codex:a:turn:talk:hash"}) + "\n" +
                       json.dumps({"kind": "tool", "text": "run: {}", "event_key": "hook:codex:a:tool:x"}) + "\n")
        manifest = [{"id": id, "title": id, "status": "idle"} for id in ("a", "b")]
        pending, report = prepare(manifest, self.database, self.home)
        self.assertEqual([r["text"] for r in pending if r["kind"] == "user"], ["same inherited prompt", "new"])
        self.assertEqual(sum(r["duplicate"] for r in report), 3)
        with log.open("a") as f:
            for record in pending:
                f.write(json.dumps(record) + "\n")
        self.assertEqual(prepare(manifest, self.database, self.home)[0], [])

    def test_running_refused_and_archived_skipped(self):
        self.transcript("a", [self.message("a", "archive")], archived=1)
        manifest = [{"id": "a", "title": "a", "status": "idle"}]
        records, report = prepare(manifest, self.database, self.home)
        self.assertEqual(records, [])
        self.assertEqual(report[0]["skipped"], "archived")
        manifest[0]["status"] = "active"
        with self.assertRaises(ValueError):
            prepare(manifest, self.database, self.home)


if __name__ == "__main__":
    unittest.main()
