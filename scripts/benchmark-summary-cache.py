"""Paid, bounded streaming A/B benchmark. Never writes to the live chat.

Uses distinct sources, real byte-limit corrections, and historical merge
summaries to drive view coarsening. Separate first attempts from corrections;
do not replay identical prompts or count archived merges as generated results.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from optchat import compactor
from optchat.compactor import Compactor, OpenRouterConversation
from optchat.memory import Memory, Part, byte_size


def records(home, folder):
    result = []
    for path in sorted((home / folder).glob("*.jsonl")):
        for line in path.read_bytes().splitlines():
            try:
                result.append(json.loads(line))
            except (ValueError, UnicodeError):
                continue
    return result


def totals(calls):
    usage = {key: sum(c["usage"].get(key, 0) for c in calls)
             for key in ("input", "output", "cache_read", "cache_write")}
    prompt = usage["input"] + usage["cache_read"]
    return {**usage, "cache_hit_fraction": usage["cache_read"] / prompt if prompt else 0,
            "cost_usd": sum(c["cost_usd"] or 0 for c in calls),
            "input_cost_usd": sum((c.get("cost_details") or {}).get(
                "upstream_inference_prompt_cost", 0) or 0 for c in calls),
            "median_seconds": statistics.median(c["seconds"] for c in calls) if calls else 0,
            "calls": len(calls)}


def benchmark(home, start, count, budget, config):
    messages = sorted(records(home, "main"), key=lambda m: m["i"])
    nodes = records(home, "tree")
    archived = {Part(n["l"], n["i"]): n for n in nodes}
    if start < 0 or count < 1 or start + count > len(messages):
        raise ValueError("Requested source range is outside the durable log")
    key = config.get("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise ValueError("Set openrouter_api_key in config or OPENROUTER_API_KEY")
    run = uuid.uuid4().hex
    report = {"run": run, "start": start, "count": count,
              "method": "distinct streaming leaves; archived merges; isolated arm namespaces",
              "model": config.get("summary_model", compactor.OPENROUTER_DEFAULT_MODEL),
              "budget_usd": budget, "arms": {}}
    spent = 0
    for arm, window in (("baseline", 0), ("shared_prefix", 32)):
        results, calls = [], []
        session = "optchat-bench-" + run + "-" + arm
        with tempfile.TemporaryDirectory(prefix="optchat-cache-bench-") as folder:
            root = Path(folder)
            for name, data in (("main", [m for m in messages if m["i"] < start]),
                               ("tree", [n for n in nodes if Part(n["l"], n["i"]).end <= start])):
                (root / name).mkdir()
                (root / name / "snapshot.jsonl").write_text(
                    "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in data))
            with Memory(root, config.get("node_bytes", 512), config.get("view_bytes", 128000)) as mem:
                class Measured:
                    def __init__(self):
                        self.inner = OpenRouterConversation(
                            model=report["model"], api_key=key,
                            effort=config.get("openrouter_reasoning_effort", "low"),
                            timeout=config.get("summary_timeout", 300),
                            base_url=config.get("openrouter_base_url", compactor.OPENROUTER_BASE_URL),
                            provider=config.get("openrouter_provider"), session_id=session,
                            explicit_cache=(False if not window else config.get("openrouter_explicit_cache")))
                        self.attempt = 0

                    def ask(self, prompt):
                        nonlocal spent
                        if spent >= budget:
                            raise RuntimeError("Benchmark cost ceiling reached")
                        t = time.monotonic()
                        reply = self.inner.ask(prompt)
                        self.attempt += 1
                        spent += self.inner.last_cost or 0
                        calls.append({"attempt": self.attempt, "seconds": time.monotonic() - t,
                                      "usage": self.inner.last_usage, "cost_usd": self.inner.last_cost,
                                      "cost_details": self.inner.last_cost_details,
                                      "provider": self.inner.last_provider, "bytes": byte_size(reply)})
                        return reply

                worker = Compactor(mem, Measured, batch=1, cache_window=window)
                # Cold namespace prevents the running daemon or a repeated test
                # from warming either arm. No cache-hit claims from exact replay.
                with patch.object(compactor, "COMPACT", "Benchmark namespace: " + session + "\n" + compactor.COMPACT):
                    try:
                        for message in messages[start:start + count]:
                            mem.append(message["kind"], message["text"], date=message["date"])
                            part = Part(0, message["i"])
                            before = len(calls)
                            try:
                                worker.build(part)
                            except RuntimeError as exc:
                                # Preserve already-paid evidence on a budget,
                                # quota or network failure rather than losing it.
                                results.append({"id": message["i"], "error": str(exc)})
                                break
                            reference = archived.get(part, {}).get("text")
                            results.append({"id": message["i"], "kind": message["kind"],
                                            "source": message["kind"] + ": " + message["text"],
                                            "summary": mem.nodes[part].text,
                                            "reference_summary": reference,
                                            "calls": len(calls) - before,
                                            "within_limit": mem.nodes[part].size <= mem.node_bytes})
                            # Drive the same streaming tree/coarsening scheduler
                            # without buying unrelated merge completions.
                            for level in range(1, len(mem.messages).bit_length()):
                                parent = Part(level, (len(mem.messages) - 1) // (1 << level))
                                if parent.end <= len(mem.messages) and parent in archived and mem.ready(parent):
                                    mem.save_node(parent, archived[parent]["text"])
                            latest = calls[before:]
                            if latest:
                                c = latest[0]
                                print(f"{arm} {message['i']}: first={c['usage']} "
                                      f"cost=${sum(x['cost_usd'] or 0 for x in latest):.6f} "
                                      f"attempts={len(latest)} bytes={mem.nodes[part].size}", flush=True)
                            else:
                                print(f"{arm} {message['i']}: copied free", flush=True)
                    finally:
                        worker.close()
        report["arms"][arm] = {"first_attempts": totals([c for c in calls if c["attempt"] == 1]),
                                "corrections": totals([c for c in calls if c["attempt"] > 1]),
                                "all_calls": totals(calls), "calls": calls, "results": results}
        if spent >= budget:
            break
    report["spent_usd"] = spent
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home() / ".optchat")
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument("--budget", type=float, default=.10)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads((args.home / "config.json").read_text())
    report = benchmark(args.home, args.start, args.count, args.budget, config)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Reports contain private sources for fidelity review. Credentials are never
    # copied; do not publish these reports or commit them by default.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
    print(json.dumps({arm: data["first_attempts"] for arm, data in report["arms"].items()}, indent=2))
    print("Fidelity review:", args.output)


if __name__ == "__main__":
    main()
