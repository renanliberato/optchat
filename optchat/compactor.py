"""Ordered background compaction, with concurrent merges and bounded retries."""

from __future__ import annotations

import json
import hashlib
from collections import OrderedDict
import os
import re
import shlex
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Protocol

from .memory import Memory, Part, byte_size, cut_bytes
from . import opencode

PROMPTS = Path(__file__).with_name("prompts")
COMPACT = (PROMPTS / "compact.txt").read_text()
MASTER = (PROMPTS / "master.txt").read_text()
VIEW_DOC = (PROMPTS / "view.txt").read_text()
OPTCHAT_GUIDANCE = (PROMPTS / "hooks.txt").read_text()
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_DEFAULT_MODEL = "openai/gpt-6-luna"
OPENROUTER_ZDR = {"zdr": True, "data_collection": "deny"}
RECENT_CHAT = "\n\n<recent_chat>\n"
CORRECTION_ECHO = re.compile(r"^\s*(user|talk):\s*(prior|previous)\b.*\b(bytes|limit|cutoff|enforce)\b",
                             re.IGNORECASE)


class Conversation(Protocol):
    def ask(self, text: str) -> str: ...


class CommandConversation:
    """A custom provider receives the complete node conversation as JSON stdin."""

    def __init__(self, command: list[str], timeout: float = 180):
        self.command, self.timeout = command, timeout
        self.messages: list[dict] = []

    def ask(self, text: str) -> str:
        self.messages.append({"role": "user", "content": text})
        payload = json.dumps({"system": COMPACT, "messages": self.messages}, ensure_ascii=False)
        result = subprocess.run(self.command, input=payload, capture_output=True, text=True,
                                timeout=self.timeout, env={**os.environ, "OPTCHAT_INTERNAL": "1"})
        if result.returncode:
            raise RuntimeError(f"Summary command exited {result.returncode}: {result.stderr[-2000:]}")
        reply = result.stdout.strip()
        self.messages.append({"role": "assistant", "content": reply})
        return reply


class ClaudeConversation:
    """Fresh session per node; oversize corrections resume only that node's session."""

    def __init__(self, model: str = "sonnet", binary: str = "claude", timeout: float = 180):
        self.model, self.binary, self.timeout = model, binary, timeout
        self.session = str(uuid.uuid4())
        self.started = False

    def ask(self, text: str) -> str:
        command = [self.binary, "-p", "--output-format", "json", "--model", self.model,
                   "--effort", "medium", "--tools", "", "--strict-mcp-config",
                   "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
                   "--system-prompt", COMPACT]
        command += ["--resume" if self.started else "--session-id", self.session]
        # No project instructions or files are needed for compaction, and a
        # stable cwd survives the daemon's directory being moved or deleted.
        with tempfile.TemporaryDirectory(prefix="optchat-summary-") as cwd:
            result = subprocess.run(command, input=text, capture_output=True, text=True,
                                    timeout=self.timeout, cwd=cwd,
                                    env={**os.environ, "OPTCHAT_INTERNAL": "1"})
        if result.returncode:
            raise RuntimeError(f"Claude compactor exited {result.returncode}: {result.stderr[-2000:]}")
        reply = json.loads(result.stdout)
        if reply.get("is_error"):
            raise RuntimeError(str(reply.get("result", "Claude compactor failed")))
        self.started = True
        usage = reply.get("usage")
        self.last_usage = ({"input": usage.get("input_tokens", 0),
                            "output": usage.get("output_tokens", 0),
                            "cache_read": usage.get("cache_read_input_tokens", 0),
                            "cache_write": usage.get("cache_creation_input_tokens", 0)}
                           if isinstance(usage, dict) else None)
        return reply.get("result", "")


class CodexConversation:
    """Fresh ephemeral codex exec per node; corrections resend the accumulated transcript."""

    def __init__(self, model: str = "gpt-6-luna", binary: str = "codex", timeout: float = 180,
                 effort: str = "high"):
        self.model, self.binary, self.timeout, self.effort = model, binary, timeout, effort
        self.messages: list[tuple[str, str]] = []

    def transcript(self, text: str) -> str:
        if not self.messages:
            return text
        lines = ["The conversation so far, oldest first; your previous replies are included:", ""]
        for request, reply in self.messages:
            lines += [f"user: {request}", f"assistant: {reply}", ""]
        lines.append(f"user: {text}")
        return "\n".join(lines)

    def ask(self, text: str) -> str:
        command = [self.binary, "exec", "--json", "--ephemeral", "--skip-git-repo-check",
                   "--sandbox", "read-only", "--ignore-user-config", "--model", self.model,
                   "-c", "model_reasoning_effort=" + json.dumps(self.effort),
                   "-c", "developer_instructions=" + json.dumps(COMPACT, ensure_ascii=False), "-"]
        # No project instructions or files are needed for compaction, and a
        # stable cwd survives the daemon's directory being moved or deleted.
        with tempfile.TemporaryDirectory(prefix="optchat-summary-") as cwd:
            result = subprocess.run(command, input=self.transcript(text), capture_output=True, text=True,
                                    timeout=self.timeout, cwd=cwd,
                                    env={**os.environ, "OPTCHAT_INTERNAL": "1"})
        if result.returncode:
            raise RuntimeError(f"Codex compactor exited {result.returncode}: {result.stderr[-2000:]}")
        reply, errors = "", []
        self.last_usage = None
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            type = event.get("type")
            if type == "turn.completed" and isinstance(event.get("usage"), dict):
                usage = event["usage"]
                self.last_usage = {"input": max(0, usage.get("input_tokens", 0) - usage.get("cached_input_tokens", 0)),
                                   "output": usage.get("output_tokens", 0),
                                   "cache_read": usage.get("cached_input_tokens", 0)}
            if type == "error":
                errors.append(str(event.get("message", event)))
            elif type == "turn.failed":
                errors.append(str(event.get("error", event)))
            elif type == "item.completed" and event.get("item", {}).get("type") == "agent_message":
                reply = event["item"].get("text", "")
        if not reply:
            raise RuntimeError("Codex compactor returned no summary: " + ("; ".join(errors) or result.stderr[-500:]))
        self.messages.append((text, reply))
        return reply


class OpenCodeConversation(CodexConversation):
    """Fresh OpenCode session per call; corrections resend the node transcript."""

    def __init__(self, model: str = opencode.DEFAULT_MODEL, binary: str = "opencode", timeout: float = 180):
        self.model, self.binary, self.timeout = model, binary, timeout
        self.messages: list[tuple[str, str]] = []

    def ask(self, text: str) -> str:
        # No project instructions or files are needed for compaction.
        with tempfile.TemporaryDirectory(prefix="optchat-summary-") as cwd:
            result = subprocess.run(opencode.command(self.binary, self.model),
                                    input=self.transcript(text), capture_output=True, text=True,
                                    timeout=self.timeout, cwd=cwd,
                                    env=opencode.environment(COMPACT, internal=True))
        if result.returncode:
            raise RuntimeError(f"OpenCode compactor exited {result.returncode}: {result.stderr[-2000:]}")
        replies, errors, seen = [], [], set()
        self.last_usage = None
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("type") == "error":
                errors.append(json.dumps(event.get("error", event), ensure_ascii=False))
            for kind, value, key in opencode.completed(event):
                if kind == "talk" and key not in seen:
                    replies.append(value)
                    seen.add(key)
            part = event.get("part", {})
            if event.get("type") == "step_finish" and isinstance(part.get("tokens"), dict):
                key = part.get("id")
                if key in seen:
                    continue
                if key:
                    seen.add(key)
                tokens = part["tokens"]
                usage = {"input": tokens.get("input", 0), "output": tokens.get("output", 0),
                         "cache_read": tokens.get("cache", {}).get("read", 0),
                         "cache_write": tokens.get("cache", {}).get("write", 0)}
                self.last_usage = {name: (self.last_usage or {}).get(name, 0) + value
                                   for name, value in usage.items()}
        reply = "\n".join(replies)
        if errors or not reply:
            raise RuntimeError("OpenCode compactor returned no valid summary: " +
                               ("; ".join(errors) or result.stderr[-500:]))
        self.messages.append((text, reply))
        return reply


class PrefixWarmup:
    """Let one real request warm a new prefix before parallel workers use it.

    No synthetic warm-up call. Locks apply only while a prefix is cold; warm
    requests run concurrently. Failed requests release the lock without warming.
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.entries = OrderedDict()

    @contextmanager
    def use(self, prefix):
        key = hashlib.sha256((COMPACT + prefix).encode()).digest()
        with self.lock:
            entry = self.entries.setdefault(key, [threading.Lock(), 0.0])
            self.entries.move_to_end(key)
            if len(self.entries) > 64:
                self.entries.popitem(last=False)
        if time.monotonic() - entry[1] < 240:
            yield
            return
        with entry[0]:
            # A preceding worker may have warmed this while we were waiting.
            cold = time.monotonic() - entry[1] >= 240
            if cold:
                yield
                entry[1] = time.monotonic()
                return
        yield


class OpenRouterConversation:
    """Direct OpenRouter chat completions; corrections resend the accumulated transcript."""

    def __init__(self, model: str = OPENROUTER_DEFAULT_MODEL, timeout: float = 180,
                 effort: str | None = "low", base_url: str = OPENROUTER_BASE_URL,
                 api_key: str | None = None, provider: dict | None = None,
                 session_id: str | None = None, explicit_cache: bool | None = None,
                 warmup: PrefixWarmup | None = None):
        self.model, self.timeout, self.effort = model, timeout, effort
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        # Zero data retention is mandatory for every request; extra routing
        # preferences may be added, but they cannot relax it.
        self.provider = {**(provider or {}), **OPENROUTER_ZDR}
        self.session_id = session_id or "optchat-summary-" + uuid.uuid4().hex
        self.warmup = warmup
        # Explicit breakpoints are supported by OpenAI GPT-5.6+; other models
        # keep automatic caching unless deliberately opted in.
        self.explicit_cache = (bool(re.match(r"^openai/gpt-(?:6(?:[.-]|$)|5\.[6-9](?:[.-]|$))", model))
                               if explicit_cache is None else explicit_cache)
        self.messages: list[dict] = []
        self.last_usage = None
        self.last_cost = None
        self.last_provider = None
        self.last_cost_details = None

    def ask(self, text: str) -> str:
        prefix, separator, _ = text.partition(RECENT_CHAT)
        if self.warmup is not None and separator:
            with self.warmup.use(prefix):
                return self._ask(text)
        return self._ask(text)

    def _ask(self, text: str) -> str:
        if not self.api_key:
            raise RuntimeError("OpenRouter compactor requires OPENROUTER_API_KEY")
        content = text
        if self.explicit_cache:
            prefix, separator, suffix = text.partition(RECENT_CHAT)
            if separator:
                content = [{"type": "text", "text": prefix,
                            "prompt_cache_breakpoint": {"mode": "explicit"}},
                           {"type": "text", "text": separator + suffix}]
        payload = {"model": self.model, "usage": {"include": True},
                   "session_id": self.session_id,
                   "messages": [{"role": "system", "content": COMPACT}, *self.messages,
                                {"role": "user", "content": content}]}
        if self.explicit_cache:
            # Only the shared prefix gets a write; unique sources do not.
            payload["prompt_cache_options"] = {"mode": "explicit"}
        if self.effort:
            payload["reasoning"] = {"effort": self.effort}
        if self.provider:
            payload["provider"] = self.provider
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                     "X-Title": "OptChat"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[-2000:]
            raise RuntimeError(f"OpenRouter compactor exited {exc.code}: {detail}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise RuntimeError(f"OpenRouter compactor failed: {exc}") from None
        if not isinstance(body, dict):
            raise RuntimeError("OpenRouter compactor returned an invalid response")
        if isinstance(body.get("error"), dict):
            raise RuntimeError("OpenRouter compactor failed: " + str(body["error"].get("message", body["error"])))
        choices = body.get("choices") or []
        reply_content = choices[0].get("message", {}).get("content") if choices else None
        reply = reply_content.strip() if isinstance(reply_content, str) else ""
        if not reply:
            raise RuntimeError("OpenRouter compactor returned no summary")
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        details = usage.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens", usage.get("cache_read_input_tokens", 0)) or 0
        prompt = usage.get("prompt_tokens", 0) or 0
        self.last_usage = {"input": max(0, prompt - cached), "output": usage.get("completion_tokens", 0) or 0,
                           "cache_read": cached,
                           "cache_write": details.get("cache_write_tokens", usage.get("cache_write_input_tokens", 0)) or 0}
        cost = usage.get("cost")
        self.last_cost = cost if isinstance(cost, (int, float)) else None
        self.last_provider = body.get("provider")
        self.last_cost_details = usage.get("cost_details")
        self.messages += [{"role": "user", "content": content}, {"role": "assistant", "content": reply}]
        return reply


def summarizer_factory(config: dict):
    provider = config.get("summarizer", "claude")
    if provider == "claude":
        return lambda: ClaudeConversation(config.get("summary_model", "sonnet"),
                                          config.get("claude_binary", "claude"),
                                          config.get("summary_timeout", 180))
    if provider == "codex":
        return lambda: CodexConversation(config.get("summary_model", "gpt-6-luna"),
                                         config.get("codex_binary", "codex"),
                                         config.get("summary_timeout", 180),
                                         config.get("codex_reasoning_effort", "high"))
    if provider == "opencode":
        return lambda: OpenCodeConversation(config.get("summary_model", opencode.DEFAULT_MODEL),
                                            config.get("opencode_binary", "opencode"),
                                            config.get("summary_timeout", 180))
    if provider == "openrouter":
        session = "optchat-summary-" + uuid.uuid4().hex
        warmup = PrefixWarmup()
        return lambda: OpenRouterConversation(config.get("summary_model", OPENROUTER_DEFAULT_MODEL),
                                              config.get("summary_timeout", 180),
                                              config.get("openrouter_reasoning_effort", "low"),
                                              config.get("openrouter_base_url", OPENROUTER_BASE_URL),
                                              api_key=config.get("openrouter_api_key"),
                                              provider=config.get("openrouter_provider"),
                                              session_id=session,
                                              explicit_cache=config.get("openrouter_explicit_cache"),
                                              warmup=warmup)
    if provider == "command":
        command = config.get("summary_command")
        if not command:
            raise ValueError("command summarizer requires summary_command (an argv array)")
        if isinstance(command, str):
            command = shlex.split(command)
        return lambda: CommandConversation(command, config.get("summary_timeout", 180))
    raise ValueError(f"Unknown summarizer: {provider}")


def scale(limit: int) -> str:
    example = ("user: Keep preferences verbatim; finish the requested implementation. "
               "talk: Built durable chat storage and fresh turns. echo: Tests passed; "
               "tool: Read the project configuration. user: Use the same memory when "
               "switching agents. talk: Pending question: choose the deployment host. ")
    return (example * (limit // len(example) + 1))[:limit]


class Compactor:
    def __init__(self, memory: Memory, factory, jobs: int = 8, tries: int = 5,
                 retry: float = 10, report=print, batch: int = 8, telemetry=None,
                 cache_window: int = 32):
        self.memory, self.factory = memory, factory
        self.jobs, self.tries, self.retry, self.report = jobs, tries, retry, report
        self.batch = max(1, batch)
        self.telemetry = telemetry
        self.cache_window = max(0, cache_window)
        self.contexts: OrderedDict[tuple[int, int], tuple[int, str]] = OrderedDict()
        self.busy: set[Part] = set()
        self.failed: dict[Part, str] = {}
        self.retry_at: dict[Part, float] = {}
        self.stopping = False
        self.pool = ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="optchat-node")
        self.thread = threading.Thread(target=self._pump, daemon=True, name="optchat-compactor")

    def start(self):
        self.thread.start()

    def save_node(self, part, text):
        with self.memory.cv:
            existed = part in self.memory.nodes
            self.memory.save_node(part, text)
        if self.telemetry is not None and not existed:
            self.telemetry.record("nodes", 0)

    def close(self):
        with self.memory.cv:
            self.stopping = True
            self.memory.cv.notify_all()
        if self.thread.is_alive():
            self.thread.join()
        self.pool.shutdown(wait=True)

    def build(self, part: Part):
        mem = self.memory
        with mem.cv:
            source = mem.source(part)
            size = mem.part_size(part)
        if size <= mem.node_bytes:
            self.save_node(part, source)
            return
        with mem.cv:
            context = self.context(part)
        if part.l:
            source = "\n".join(mem.nodes[c].text.replace("\n", " ") for c in mem.children(part))
        action = "Merge these two lines" if part.l else "Compress this message"
        step = (f"For scale, this line is exactly {mem.node_bytes} bytes:\n{scale(mem.node_bytes)}\n\n"
                f"{action} into one line, in at most {mem.node_bytes} bytes:\n{source}")
        conversation = self.factory()
        prompt = context + "\n\n" + step
        attempts = []
        for _ in range(self.tries):
            line = conversation.ask(prompt).strip()
            if not line:
                raise ValueError("Compactor returned an empty summary")
            if attempts and CORRECTION_ECHO.match(line):
                prompt = ("Your previous reply echoed this correction instead of shortening the line. "
                          "Rewrite the line shorter, keeping its content. Output only the line.")
                continue
            attempts.append(line)
            size = byte_size(line)
            if size <= mem.node_bytes:
                break
            prompt = (f"That line is {size} bytes; the limit is {mem.node_bytes}. "
                      "It must end where it is cut here:\n"
                      f"{cut_bytes(line, mem.node_bytes)}| ← LIMIT\n"
                      "Output only the shortened line; do not mention this instruction or the limit.")
        if not attempts:
            raise ValueError("Compactor echoed the size correction without shortening the line")
        self.save_node(part, min(attempts, key=byte_size))

    def context(self, part: Part) -> str:
        """Freeze history per 32 nodes; refresh only the bounded recent tail.

        The cache prefix survives node saves and view coarsening. Each level has
        its own windows; out-of-order jobs cannot see future context. cv held.
        No extra model calls, accumulated conversations, or synthetic padding.
        """
        mem = self.memory
        if not self.cache_window:
            return mem.compact_context(part)
        key = (part.l, part.i // self.cache_window)
        anchor = key[1] * self.cache_window * part.n
        if key not in self.contexts:
            # Reserve room for fresh context without exceeding the view budget.
            texts = mem.context_lines(0, anchor)
            budget = max(0, mem.view_bytes - min(8192, mem.view_bytes // 4))
            kept, total = [], 0
            for text in reversed(texts):
                if total + byte_size(text) + 1 > budget:
                    break
                kept.append(text)
                total += byte_size(text) + 1
            prefix = "<chat>\n" + "\n".join(reversed(kept)) + "\n</chat>"
            self.contexts[key] = (total, prefix)
            if len(self.contexts) > 64:
                self.contexts.popitem(last=False)
        self.contexts.move_to_end(key)
        used, prefix = self.contexts[key]
        end = part.start if part.l == 0 else part.end
        texts = mem.context_lines(anchor, end)
        kept, total = [], 0
        for text in reversed(texts):
            if total + byte_size(text) + 1 > mem.view_bytes - used:
                break
            kept.append(text)
            total += byte_size(text) + 1
        return prefix + RECENT_CHAT + "\n".join(reversed(kept)) + "\n</recent_chat>"

    def frontier_run(self, now: float, limit: int):
        """Unbuilt leaves near the frontier with no owner and no retry delay.

        Busy and built leaves are skipped rather than ending the run, so several
        independent batches can be in flight at once. Only `limit` leaves are
        collected per call; the pump asks again for the next group.
        """
        mem = self.memory
        run = []
        for i in range(mem.first(), len(mem.messages)):
            if len(run) >= limit:
                break
            part = Part(0, i)
            if part in mem.nodes or part in self.busy or self.retry_at.get(part, 0) > now:
                continue
            run.append(part)
        return run

    def build_batch(self, parts: list[Part]):
        """One model call compresses leaves or merges same-level pairs; unparseable output falls back."""
        mem = self.memory
        with mem.cv:
            sizes = [mem.part_size(part) for part in parts]
            sources = [mem.source(part) for part in parts]
            context = self.context(parts[0])
        pending = []
        for part, source, size in zip(parts, sources, sizes):
            if size <= mem.node_bytes:
                self.save_node(part, source)
            else:
                pending.append((part, source))
        if not pending:
            return
        if len(pending) == 1:
            self.build(pending[0][0])
            return
        conversation = self.factory()
        lines = self.ask_batch(conversation, context, [source for _, source in pending], parts[0].l)
        if lines is None:
            for part, _ in pending:
                self.build(part)
            return
        for (part, _), line in zip(pending, lines):
            if line and byte_size(line) <= mem.node_bytes:
                self.save_node(part, line)
            else:
                self.build(part)

    def ask_batch(self, conversation, context: str, sources: list[str], level: int = 0):
        mem = self.memory
        count = len(sources)
        if level == 0:
            noun, action = "messages", "Compress each of the following"
            messages = "\n\n".join(f"MESSAGE {index + 1}:\n{source}" for index, source in enumerate(sources))
        else:
            noun, action = "pairs", "Merge each of the following"
            messages = "\n\n".join(f"PAIR {index + 1}:\n{source}" for index, source in enumerate(sources))
        step = (f"For scale, each line is at most {mem.node_bytes} bytes:\n{scale(mem.node_bytes)}\n\n"
                f"{action} {count} {noun} into its own line, in at most {mem.node_bytes} "
                f"bytes each. Output exactly {count} lines and nothing else, one line per {noun[:-1]}, in the same "
                f"order. Begin each line with \"N. \" where N is the {noun[:-1]} number, then the summary.\n{messages}")
        reply = conversation.ask(context + "\n\n" + step).strip()
        lines = [line.strip() for line in reply.splitlines() if line.strip()]
        if len(lines) != count:
            return None
        result = []
        for index, line in enumerate(lines, 1):
            match = re.match(r"^(\d+)[.)]\s+(.*)$", line)
            if match and int(match.group(1)) != index:
                return None
            result.append(match.group(2).strip() if match else line)
        return result

    def _run(self, part: Part):
        try:
            self.build(part)
            with self.memory.cv:
                self.failed.pop(part, None)
                self.retry_at.pop(part, None)
        except Exception as exc:
            with self.memory.cv:
                if part not in self.failed:
                    self.report(f"Compaction {part.start}+{part.n} failed: {exc}; retrying in {self.retry}s")
                self.failed[part] = str(exc)
                self.retry_at[part] = time.monotonic() + self.retry
        finally:
            with self.memory.cv:
                self.busy.remove(part)
                self.memory.cv.notify_all()

    def _run_batch(self, parts: list[Part]):
        try:
            self.build_batch(parts)
            with self.memory.cv:
                for part in parts:
                    self.failed.pop(part, None)
                    self.retry_at.pop(part, None)
        except Exception as exc:
            with self.memory.cv:
                for part in parts:
                    if part not in self.failed:
                        self.report(f"Compaction {part.start}+{part.n} failed: {exc}; retrying in {self.retry}s")
                    self.failed[part] = str(exc)
                    self.retry_at[part] = time.monotonic() + self.retry
        finally:
            with self.memory.cv:
                for part in parts:
                    self.busy.discard(part)
                self.memory.cv.notify_all()

    def _fill_frontier(self, now: float, quota: int):
        """Keep at least `quota` busy slots on frontier leaves, merges notwithstanding."""
        leaves = sum(1 for part in self.busy if part.l == 0)
        while leaves < quota and len(self.busy) < self.jobs:
            run = self.frontier_run(now, max(1, self.batch))
            if not run:
                return
            self._submit(run)
            leaves += len(run)

    def _fill_candidates(self, now: float):
        level, group = None, []
        for part in self.memory.candidates():
            if len(self.busy) >= self.jobs:
                break
            if part in self.busy or self.retry_at.get(part, 0) > now:
                continue
            if group and part.l != level:
                self._submit(group)
                group = []
            level = part.l
            group.append(part)
            if len(group) >= self.batch:
                self._submit(group)
                group = []
        if group:
            self._submit(group)

    def _pump(self):
        mem = self.memory
        with mem.cv:
            while not self.stopping:
                now = time.monotonic()
                # Keep merges fed so the view stays compact; an over-budget view
                # favors merges (they are the only way down) without starving
                # leaves, whose context is capped independently.
                share = 4 if mem.view_size() > mem.view_bytes else 2
                self._fill_frontier(now, max(1, self.jobs // share))
                self._fill_candidates(now)
                self._fill_frontier(now, self.jobs)
                retry_times = [t for p, t in self.retry_at.items() if p not in self.busy and t > now]
                wait = max(0.01, min(retry_times) - now) if retry_times else None
                mem.cv.wait(wait)

    def _submit(self, group: list[Part]):
        if len(group) > 1:
            for part in group:
                self.busy.add(part)
            self.pool.submit(self._run_batch, list(group))
        else:
            self.busy.add(group[0])
            self.pool.submit(self._run, group[0])

    def settle(self, timeout: float | None = None, all_nodes: bool = False, cancelled=None):
        deadline = time.monotonic() + timeout if timeout is not None else None
        with self.memory.cv:
            while True:
                if cancelled is not None and cancelled():
                    raise InterruptedError("Memory wait cancelled")
                settled = self.memory.first() == len(self.memory.messages)
                if settled and (not all_nodes or not any(self.memory.candidates())):
                    return
                if self.stopping:
                    raise RuntimeError("Compactor stopped")
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("Waiting for summaries; raw messages remain durable. Check `optchat status`.")
                self.memory.cv.notify_all()
                wait = remaining
                if cancelled is not None:
                    wait = min(0.2, remaining) if remaining is not None else 0.2
                self.memory.cv.wait(wait)
