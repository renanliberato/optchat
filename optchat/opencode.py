"""OpenCode CLI runtime configuration and completed JSON event parsing."""

from __future__ import annotations

import json
import os

DEFAULT_MODEL = "opencode-go/deepseek-v4.1-flash"
AGENT = "optchat"


def command(binary: str = "opencode", model: str | None = None) -> list[str]:
    return [binary, "run", "--format", "json", "--agent", AGENT,
            "--model", model or DEFAULT_MODEL]


def environment(system: str, mcp: list[str] | None = None, internal: bool = False) -> dict:
    # Preserve caller runtime overrides and provider authentication. Inline
    # configuration avoids writing to either the project or global settings.
    config = json.loads(os.environ.get("OPENCODE_CONFIG_CONTENT", "{}"))
    agent = {"description": "OptChat fresh-turn agent", "mode": "primary", "prompt": system}
    if internal:
        agent["permission"] = {"*": "deny"}
        config["instructions"] = []
        config["mcp"] = {name: {"enabled": False} for name in config.get("mcp", {})}
    elif mcp:
        config.setdefault("mcp", {})["optchat"] = {"type": "local", "command": mcp, "enabled": True}
        agent["permission"] = {"optchat_*": "allow"}
    config.setdefault("agent", {})[AGENT] = agent
    config["share"] = "disabled"
    config["autoupdate"] = False
    return {**os.environ, "OPENCODE_CONFIG_CONTENT": json.dumps(config, ensure_ascii=False),
            "OPTCHAT_INTERNAL" if internal else "OPTCHAT_WRAPPER": "1"}


def completed(event: dict):
    """Yield (kind, text, stable key), excluding deltas and reasoning."""
    part = event.get("part", {})
    kind = event.get("type")
    if kind == "text" and part.get("time", {}).get("end"):
        if not part.get("id"):
            raise ValueError("OpenCode text part is missing its id")
        yield "talk", part.get("text", ""), f"talk:{part['id']}"
    elif kind == "tool_use" and part.get("state", {}).get("status") in {"completed", "error"}:
        identity = part.get("callID") or part.get("id")
        if not identity:
            raise ValueError("OpenCode tool part is missing its id")
        state = part["state"]
        yield "tool", f"{part.get('tool', 'tool')}: {json.dumps(state.get('input', {}), ensure_ascii=False)}", f"tool:{identity}"
        result = {key: state[key] for key in ("status", "output", "error", "metadata") if key in state}
        yield "echo", json.dumps(result, ensure_ascii=False), f"echo:{identity}"
