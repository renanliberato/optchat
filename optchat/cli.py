"""OptChat CLI. All memory operations go through the single-writer daemon."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

from .adapters import hook, hook_config, run_turn
from .daemon import Client, DEFAULT_CONFIG, serve
from .mcp import serve_stdio
from .opencode import DEFAULT_MODEL as OPENCODE_MODEL


def parser():
    cli = argparse.ArgumentParser(description="Durable shared memory and fresh Codex/Claude/OpenCode turns")
    cli.add_argument("--home", default=os.environ.get("OPTCHAT_HOME", "~/.optchat"),
                     help="Shared chat directory (default: OPTCHAT_HOME or ~/.optchat)")
    commands = cli.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create configuration; does not install agent hooks")
    init.add_argument("--summarizer", choices=["claude", "codex", "opencode", "command"], default="claude")
    init.add_argument("--summary-command", help="Custom provider command, parsed as argv (no shell)")
    init.add_argument("--summary-model", help="Default: sonnet for claude, gpt-6-luna for codex, opencode-go/deepseek-v4.1-flash for opencode")
    init.add_argument("--node-bytes", type=int, default=512)
    init.add_argument("--view-bytes", type=int, default=128_000)
    init.add_argument("--batch-leaves", type=int, default=8, help="Same-level nodes compressed per model call")
    append = commands.add_parser("append", help="Append a verbatim message")
    append.add_argument("kind", choices=["user", "talk", "tool", "echo", "note"])
    append.add_argument("text", nargs="?", help="Read stdin if omitted")
    append.add_argument("--event-key")
    for name in ("context", "compact"):
        command = commands.add_parser(name, help="Wait for summaries" if name == "context" else "Wait for the complete tree")
        command.add_argument("--timeout", type=float, help="Abort the wait after this many seconds")
    zoom = commands.add_parser("zoom", help="Open a summary or retrieve a full original message")
    zoom.add_argument("id", type=int)
    zoom.add_argument("n", type=int)
    date = commands.add_parser("date")
    date.add_argument("id", type=int)
    commands.add_parser("view", help="Read-only memory view lines for the viewer window")
    commands.add_parser("status")
    commands.add_parser("monitor", help="Read-only dashboard JSON; never starts the daemon")
    commands.add_parser("serve", help="Run the daemon in the foreground")
    commands.add_parser("stop", help="Stop the local daemon after in-flight compactions")
    commands.add_parser("mcp", help="Serve zoom and date over MCP stdio")
    export = commands.add_parser("export", help="Write a browsable, standalone HTML memory snapshot")
    export.add_argument("path", type=Path)
    import_ = commands.add_parser("import", help="Append JSONL history; new IDs are assigned in input order")
    import_.add_argument("path", type=Path)
    config = commands.add_parser("hooks", help="Print hook configuration to merge into agent settings")
    config.add_argument("agent", choices=["codex", "claude"])
    hooks = commands.add_parser("hook", help="Handle a lifecycle hook JSON object from stdin")
    hooks.add_argument("--agent", required=True, choices=["codex", "claude"])
    hooks.add_argument("--timeout", type=float, default=540)
    for name in ("run", "chat"):
        runner = commands.add_parser(name, help="Start a fresh agent turn" if name == "run" else "Interactive fresh-turn chat")
        runner.add_argument("--agent", choices=["codex", "claude", "opencode"], default="codex")
        runner.add_argument("--binary", help="Agent executable path (also useful for integration testing)")
        runner.add_argument("--model")
        runner.add_argument("--sandbox", choices=["read-only", "workspace-write"],
                            help="Codex tool sandbox; otherwise use its configured default")
        runner.add_argument("--permission-mode", choices=["acceptEdits", "auto", "manual", "dontAsk", "plan"],
                            help="Claude tool permission mode; otherwise use its configured default")
        runner.add_argument("--cwd", type=Path, default=Path.cwd())
        runner.add_argument("--instructions", type=Path, help="Additional instructions file (constant across turns)")
        runner.add_argument("--timeout", type=float, help="Maximum wait for prior summaries")
        if name == "run":
            runner.add_argument("text", nargs="?", help="Read stdin if omitted")
    return cli


def output(value):
    print(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2))


def runner_options(args):
    cwd = args.cwd.expanduser().resolve()
    if not cwd.is_dir():
        raise ValueError(f"Working directory does not exist: {cwd}")
    instructions = args.instructions
    if instructions is None and (cwd / "AGENTS.md").exists():
        instructions = cwd / "AGENTS.md"
    return {"agent": args.agent, "binary": args.binary, "model": args.model, "cwd": str(cwd),
            "instructions": instructions.read_text() if instructions else "", "timeout": args.timeout,
            "sandbox": args.sandbox, "permission_mode": args.permission_mode}


def execute(args):
    home = Path(args.home).expanduser().resolve()
    client = Client(home)
    command = args.command
    if command == "init":
        if args.node_bytes < 64 or args.view_bytes < args.node_bytes:
            raise ValueError("Require node-bytes >= 64 and view-bytes >= node-bytes")
        if args.summarizer == "command" and not args.summary_command:
            raise ValueError("--summarizer command requires --summary-command")
        model = args.summary_model or {"codex": "gpt-6-luna", "opencode": OPENCODE_MODEL}.get(args.summarizer, "sonnet")
        config = {**DEFAULT_CONFIG, "summarizer": args.summarizer, "summary_model": model,
                  "node_bytes": args.node_bytes, "view_bytes": args.view_bytes,
                  "batch_leaves": args.batch_leaves}
        if args.summary_command:
            config["summary_command"] = shlex.split(args.summary_command)
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Never change the budget of an existing view or overwrite configuration silently.
        fd = os.open(home / "config.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump(config, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        output(str(home / "config.json"))
    elif command == "serve":
        serve(home)
    elif command == "stop":
        output(Client(home, autostart=False).call("shutdown"))
    elif command == "append":
        text = args.text if args.text is not None else sys.stdin.read()
        output(client.call("append", kind=args.kind, text=text, event_key=args.event_key))
    elif command in {"context", "compact"}:
        output(client.call(command, timeout=args.timeout))
    elif command == "zoom":
        output(client.call("zoom", id=args.id, n=args.n))
    elif command == "date":
        output(client.call("date", id=args.id))
    elif command == "view":
        output(client.call("view"))
    elif command == "status":
        output(client.call("status"))
    elif command == "monitor":
        from .monitor import snapshot
        output(snapshot(home))
    elif command == "export":
        args.path.write_text(client.call("export"), encoding="utf-8")
        output(str(args.path.resolve()))
    elif command == "import":
        # Validate the entire input before any write, then make re-running the import idempotent.
        import hashlib
        data = args.path.read_bytes()
        records = [json.loads(line) for line in data.splitlines() if line.strip()]
        for record in records:
            if record.get("kind", "note") not in {"user", "talk", "tool", "echo", "note"} or not isinstance(record.get("text"), str):
                raise ValueError("Import expects JSONL objects with text and optional kind/date")
            if record.get("date"):
                from datetime import datetime
                datetime.fromisoformat(record["date"])
        digest = hashlib.sha256(data).hexdigest()
        for index, record in enumerate(records):
            client.call("append", kind=record.get("kind", "note"), text=record["text"],
                        date=record.get("date"), event_key=f"import:{digest}:{index}")
        output({"imported": len(records)})
    elif command == "hooks":
        output(hook_config(args.agent, home))
    elif command == "hook":
        output(hook(client, args.agent, json.load(sys.stdin), args.timeout))
    elif command == "mcp":
        serve_stdio(client)
    elif command == "run":
        text = args.text if args.text is not None else sys.stdin.read()
        run_turn(client, text=text, **runner_options(args))
    elif command == "chat":
        options = runner_options(args)
        print(client.call("context", timeout=args.timeout))
        print("Enter a message; /agent codex, /agent claude or /agent opencode switches agents; /quit exits.")
        while True:
            try:
                text = input("you> ")
            except EOFError:
                break
            if text == "/quit":
                break
            if text in {"/agent codex", "/agent claude", "/agent opencode"}:
                options["agent"] = text.split()[1]
                print(f"Using {options['agent']} with the same memory.")
                continue
            if not text.strip():
                continue
            try:
                run_turn(client, text=text, **options)
            except KeyboardInterrupt:
                print("\nTurn cancelled; captured messages remain in the log.")
            except Exception as exc:
                print(f"optchat: {exc}", file=sys.stderr)


def main():
    args = parser().parse_args()
    try:
        execute(args)
    except KeyboardInterrupt:
        print("optchat: cancelled", file=sys.stderr)
        raise SystemExit(130) from None
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"optchat: {exc}", file=sys.stderr)
        # Hook exit 1 is nonblocking in vendor CLIs. A missing memory view must
        # block UserPromptSubmit rather than silently run without its history.
        raise SystemExit(2 if args.command == "hook" else 1) from None
