"""Import a reviewed manifest of idle Codex chats through the OptChat daemon.

The app supplies the manifest; this module never guesses live thread status from
rollouts, edits Codex's database, or imports reasoning/compaction snapshots.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3

from .daemon import Client


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def content_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return encode(content)
    parts = []
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
        else:
            # Mark non-text content without copying inline image/audio data.
            parts.append("[Non-text content: " + str(block.get("type", "unknown")) + "]"
                         if isinstance(block, dict) else encode(block))
    return "\n".join(parts)


def rollout_records(path):
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        record = json.loads(line)
        if record.get("type") != "response_item":
            continue
        item = record["payload"]
        kind = item.get("type")
        call_id = item.get("call_id")
        if kind == "message" and item.get("role") in {"user", "assistant"}:
            text = content_text(item.get("content", []))
            if item["role"] == "user" and text.lstrip().startswith((
                "<environment_context>", "<external_codex_apps_open_page>")):
                continue
            output_kind = "user" if item["role"] == "user" else "talk"
        elif kind in {"function_call", "custom_tool_call"}:
            output_kind = "tool"
            text = item.get("name", "tool") + ": " + str(item.get("arguments", item.get("input", "")))
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            output_kind = "echo"
            text = content_text(item.get("output", ""))
        elif kind == "agent_message":
            output_kind = "note"
            text = "Historical agent report: " + content_text(item.get("content", ""))
        else:
            continue
        if not text.strip():
            continue
        date = record["timestamp"]
        datetime.fromisoformat(date)
        # Forks retain item IDs. A global key prevents copying their shared prefix.
        identity = item.get("id") or hashlib.sha256(encode([date, output_kind, text]).encode()).hexdigest()
        yield {"kind": output_kind, "text": text, "date": date,
               "event_key": f"backfill:codex:item:{output_kind}:{identity}",
               "call_id": call_id,
               "turn_id": item.get("internal_chat_message_metadata_passthrough", {}).get("turn_id"),
               "line": number}


def existing_records(home):
    for path in sorted((Path(home) / "main").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            yield json.loads(line)


def prepare(manifest, database, home):
    existing = list(existing_records(home))
    keys = {r["event_key"] for r in existing if r.get("event_key")}
    captured = Counter()
    for r in existing:
        key = r.get("event_key") or ""
        if key.startswith("hook:codex:") and r["kind"] in {"user", "talk"}:
            captured[(key.split(":")[2], r["kind"], r["text"])] += 1
    pending, report = [], []
    with sqlite3.connect(f"file:{Path(database).resolve()}?mode=ro", uri=True) as db:
        for thread in manifest:
            if thread["status"] not in {"idle", "notLoaded"}:
                raise ValueError(f"Refusing non-idle thread {thread['id']}")
            row = db.execute("select rollout_path, archived, created_at from threads where id=?",
                             (thread["id"],)).fetchone()
            if row is None or row[1]:
                report.append({"id": thread["id"], "title": thread["title"], "import": 0,
                               "duplicate": 0, "skipped": "missing" if row is None else "archived"})
                continue
            stats = {"id": thread["id"], "title": thread["title"], "import": 0, "duplicate": 0}
            records = list(rollout_records(row[0]))  # Validate whole input before append.
            for r in records:
                prefix = f"hook:codex:{thread['id']}"
                hook_key = (f"{prefix}:tool:{r['call_id']}" if r["kind"] == "tool" else
                            f"{prefix}:echo:{r['call_id']}") if r.get("call_id") else None
                content_key = (thread["id"], r["kind"], r["text"])
                if r["event_key"] in keys or hook_key in keys:
                    stats["duplicate"] += 1
                    keys.add(r["event_key"])
                    continue
                if captured[content_key]:
                    captured[content_key] -= 1
                    stats["duplicate"] += 1
                    keys.add(r["event_key"])
                    continue
                keys.add(r["event_key"])
                stats["import"] += 1
                pending.append({k: r[k] for k in ("kind", "text", "date", "event_key")})
            if stats["import"]:
                # Keep the source chat identifiable when reading the appended block.
                start = len(pending) - stats["import"]
                pending.insert(start, {"kind": "note",
                    "text": f"Historical Codex chat: {thread['title']} (session {thread['id']}). "
                            "The following block is imported history, not a new user request.",
                    "date": records[0]["date"],
                    "event_key": f"backfill:codex:session:{thread['id']}"})
            report.append(stats)
    return pending, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="JSON array of app-reviewed id/title/status objects")
    parser.add_argument("--database", type=Path, default=Path.home() / ".codex/state_5.sqlite")
    parser.add_argument("--home", type=Path, default=Path.home() / ".optchat")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Append; default is a read-only dry run")
    args = parser.parse_args()
    pending, sessions = prepare(json.loads(args.manifest.read_text()), args.database, args.home)
    report = {"sessions": sessions, "messages": len(pending),
              "duplicates": sum(s["duplicate"] for s in sessions),
              "bytes": sum(len(r["text"].encode()) for r in pending), "applied": 0}
    args.report.write_text(encode(report) + "\n")
    print(encode({k: v for k, v in report.items() if k != "sessions"}), flush=True)
    if args.apply:
        client = Client(args.home)
        client.call("status")
        for i, r in enumerate(pending, 1):
            client.call("append", **r)
            report["applied"] = i
            if i % 100 == 0:
                args.report.write_text(encode(report) + "\n")
                print(f"Appended {i}/{len(pending)}", flush=True)
        args.report.write_text(encode(report) + "\n")
        print(encode({"applied": report["applied"]}), flush=True)


if __name__ == "__main__":
    main()
