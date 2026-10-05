"""Fresh-turn CLI harnesses and optional persistent-session hook capture."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from .compactor import MASTER, VIEW_DOC, OPTCHAT_GUIDANCE


def encode(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def mcp_command(home: Path) -> tuple[str, list[str]]:
    root = str(Path(__file__).resolve().parent.parent)
    bootstrap = f"import sys; sys.path.insert(0, {root!r}); from optchat.cli import main; main()"
    return sys.executable, ["-c", bootstrap, "--home", str(home), "mcp"]


def agent_command(agent: str, home: Path, system: str, binary: str | None = None,
                  model: str | None = None, cwd: str | None = None,
                  sandbox: str | None = None, permission_mode: str | None = None) -> list[str]:
    executable, arguments = mcp_command(home)
    if agent == "codex":
        command = [binary or "codex", "exec", "--json", "--ephemeral", "--skip-git-repo-check",
                   "-c", "developer_instructions=" + json.dumps(system, ensure_ascii=False),
                   "-c", "mcp_servers.optchat.command=" + json.dumps(executable, ensure_ascii=False),
                   "-c", "mcp_servers.optchat.args=" + json.dumps(arguments, ensure_ascii=False)]
        if cwd:
            command += ["--cd", cwd]
        if model:
            command += ["--model", model]
        if sandbox:
            command += ["--sandbox", sandbox]
        return command + ["-"]
    if agent == "claude":
        config = {"mcpServers": {"optchat": {"command": executable, "args": arguments}}}
        command = [binary or "claude", "-p", "--output-format", "stream-json", "--verbose",
                   "--no-session-persistence", "--append-system-prompt", system,
                   "--mcp-config", json.dumps(config)]
        if model:
            command += ["--model", model]
        if permission_mode:
            command += ["--permission-mode", permission_mode]
        return command
    raise ValueError(f"Unknown agent {agent}")


class Events:
    """Capture completed entries, never deltas or model thoughts."""

    def __init__(self, client, agent: str, turn: str, show=print):
        self.client, self.agent, self.turn, self.show = client, agent, turn, show
        self.calls = set()
        self.seen = set()
        self.talks = 0
        self.error = None

    def log(self, kind: str, text: str, key: str):
        if key in self.seen:
            return
        self.client.call("append", kind=kind, text=text, event_key=f"wrapper:{self.turn}:{key}")
        self.seen.add(key)
        if kind == "talk":
            self.talks += 1
            self.show(text)

    def consume(self, event: dict):
        if self.agent == "codex":
            self._codex(event)
        else:
            self._claude(event)

    def _codex(self, event):
        type = event.get("type")
        if type in {"error", "turn.failed"}:
            self.error = encode(event.get("error", event.get("message", event)))
            return
        if type not in {"item.started", "item.completed"}:
            return
        item = event.get("item", {})
        kind, id = item.get("type"), item.get("id")
        if not id:
            raise ValueError("Codex item is missing its id")
        if kind == "agent_message" and type == "item.completed":
            self.log("talk", item.get("text", ""), f"talk:{id}")
        elif kind in {"command_execution", "mcp_tool_call", "web_search", "file_change", "collab_tool_call"}:
            if id not in self.calls:
                inputs = {k: v for k, v in item.items() if k not in
                          {"id", "status", "aggregated_output", "exit_code", "result", "error"}}
                self.log("tool", f"{kind}: {encode(inputs)}", f"tool:{id}")
                self.calls.add(id)
            if type == "item.completed":
                result = {k: v for k, v in item.items() if k in
                          {"aggregated_output", "exit_code", "result", "error", "status", "changes"}}
                self.log("echo", encode(result), f"echo:{id}")
        # reasoning and usage stay in the vendor's process, outside permanent memory.

    def _claude(self, event):
        if event.get("parent_tool_use_id"):
            return  # Subagent internals do not belong to the main log.
        type = event.get("type")
        message = event.get("message", {})
        id = event.get("uuid") or message.get("id")
        if type == "assistant":
            if not id:
                raise ValueError("Claude assistant message is missing its id")
            for index, block in enumerate(message.get("content", [])):
                key = f"{id}:{index}"
                if block.get("type") == "text":
                    self.log("talk", block["text"], f"talk:{key}")
                elif block.get("type") == "tool_use":
                    self.log("tool", f"{block['name']}: {encode(block.get('input', {}))}",
                             f"tool:{block['id']}")
        elif type == "user":
            for block in message.get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self.log("echo", encode({"tool_use_id": block["tool_use_id"],
                             "content": block.get("content", ""), "is_error": block.get("is_error", False)}),
                             f"echo:{block['tool_use_id']}")
        elif type == "result":
            if event.get("is_error"):
                self.error = encode(event.get("result", event.get("errors", "Claude turn failed")))
            elif not self.talks and event.get("result"):
                self.log("talk", event["result"], "talk:result")


def run_turn(client, agent: str, text: str, binary=None, model=None, cwd=None,
             instructions: str = "", timeout: float | None = None, show=print,
             sandbox=None, permission_mode=None):
    """A new child process for every user turn; no resume/continue flags."""
    turn = str(uuid.uuid4())
    key = f"wrapper:{turn}:user"
    begun = False
    process = None
    client.home.mkdir(parents=True, exist_ok=True)
    with (client.home / "turn.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another wrapper turn is running on this chat") from None
        try:
            begin = client.call("begin", text=text, event_key=key, timeout=timeout)
            begun = True
            system = MASTER + "\n" + OPTCHAT_GUIDANCE + "\n" + VIEW_DOC + "\n" + instructions
            command = agent_command(agent, client.home, system, binary, model, cwd, sandbox, permission_mode)
            payload = begin["view"] + "\n\n" + text
            # File-backed stdin avoids blocking when a very large paste is sent.
            with tempfile.TemporaryFile() as prompt:
                prompt.write(payload.encode())
                prompt.seek(0)
                process = subprocess.Popen(command, stdin=prompt, stdout=subprocess.PIPE,
                                           text=True, cwd=cwd,
                                           env={**os.environ, "OPTCHAT_WRAPPER": "1"})
                events = Events(client, agent, turn, show)
                assert process.stdout is not None
                try:
                    for line in process.stdout:
                        if line.strip():
                            events.consume(json.loads(line))
                    code = process.wait()
                finally:
                    process.stdout.close()
                if code or events.error:
                    raise RuntimeError(events.error or f"{agent} exited with status {code}")
                if not events.talks:
                    raise RuntimeError(f"{agent} completed without an assistant reply")
        except BaseException:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if not begun:
                # Preserve user input when settling is cancelled or times out.
                client.call("append", kind="user", text=text, event_key=key)
            raise


def hook(client, agent: str, payload: dict, timeout: float | None = None):
    if os.environ.get("OPTCHAT_WRAPPER") or os.environ.get("OPTCHAT_INTERNAL"):
        return {}  # The wrapper owns capture; the compactor must never capture itself.
    event = payload.get("hook_event_name")
    session = payload.get("session_id", "unknown")
    turn = payload.get("turn_id")
    prefix = f"hook:{agent}:{session}"
    if event == "UserPromptSubmit":
        key = f"{prefix}:{turn}:user" if turn else None
        # Begin snapshots settled history before appending the current prompt.
        # The exception path preserves input if the wait fails or is cancelled.
        try:
            begin = client.call("begin", text=payload.get("prompt", ""), event_key=key, timeout=timeout)
        except BaseException:
            client.call("append", kind="user", text=payload.get("prompt", ""), event_key=key)
            raise
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext":
                OPTCHAT_GUIDANCE + "\n" + VIEW_DOC + "\n" + begin["view"]}}
    if event in {"PreToolUse", "PostToolUse", "PostToolUseFailure"}:
        tool_id = payload.get("tool_use_id")
        # Only use a dedupe key when the vendor supplied a stable call identity.
        key = f"{prefix}:tool:{tool_id}" if tool_id else None
        text = f"{payload.get('tool_name', 'tool')}: {encode(payload.get('tool_input', {}))}"
        client.call("append", kind="tool", text=text, event_key=key)
        if event != "PreToolUse":
            client.call("append", kind="echo", text=encode(payload.get("tool_response", payload.get("error", ""))),
                        event_key=f"{prefix}:echo:{tool_id}" if tool_id else None)
    elif event == "Stop" and payload.get("last_assistant_message"):
        text = payload["last_assistant_message"]
        # Codex supplies turn_id; Claude Stop calls without it are append-only.
        key = f"{prefix}:{turn}:talk:{hashlib.sha256(text.encode()).hexdigest()}" if turn else None
        client.call("append", kind="talk", text=text, event_key=key)
    elif event == "SessionStart":
        client.call("status")
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext":
                OPTCHAT_GUIDANCE + "\n" + VIEW_DOC}}
    elif event in {"PreCompact", "PostCompact"}:
        client.call("status")  # Captured entries were already fsynced; start/wake the daemon.
    return {}


def hook_config(agent: str, home: Path) -> dict:
    import shlex
    executable, arguments = mcp_command(home)
    command = shlex.join([executable, *arguments[:-1], "hook", "--agent", agent])
    events = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "PreCompact", "PostCompact"]
    if agent == "claude":
        events.append("PostToolUseFailure")
    config = {"hooks": {event: [{"hooks": [{"type": "command", "command": command, "timeout": 600}]}]
                        for event in events}}
    if agent == "codex":
        # Keep the usual ~64k-token memory view inline, rather than Codex's
        # default 2,500-token preview. Larger unexpected output still spills.
        config["hooks"]["UserPromptSubmit"][0]["hooks"][0]["additionalContextLimit"] = 100_000
    return config
