"""A local single-writer service shared by CLI commands, hooks, and MCP."""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import signal
import select
import socket
import socketserver
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

from .compactor import Compactor, summarizer_factory
from .memory import Memory, Part
from .telemetry import Telemetry

DEFAULT_CONFIG = {"node_bytes": 512, "view_bytes": 128_000, "jobs": 8, "tries": 5,
                  "retry_seconds": 10, "summarizer": "claude", "summary_model": "sonnet",
                  "summary_timeout": 180, "batch_leaves": 8, "summary_cache_window": 32}


def socket_path(home: Path) -> Path:
    path = home / "rpc.sock"
    if len(os.fsencode(path)) < 100:
        return path
    digest = hashlib.sha256(os.fsencode(home)).hexdigest()[:20]
    folder = Path("/tmp") / f"optchat-{os.getuid()}-{digest}"
    folder.mkdir(mode=0o700, exist_ok=True)
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError(f"Unsafe socket directory: {folder}")
    return folder / "rpc.sock"


def load_config(home: Path) -> dict:
    path = home / "config.json"
    config = dict(DEFAULT_CONFIG)
    if path.exists():
        config.update(json.loads(path.read_text()))
    return config


class Service:
    def __init__(self, home: Path):
        config = load_config(home)
        self.memory = Memory(home, config["node_bytes"], config["view_bytes"])
        self.telemetry = Telemetry(home)
        factory = self.telemetry.conversation(summarizer_factory(config))
        self.compactor = Compactor(self.memory, factory, config["jobs"],
                                   config["tries"], config["retry_seconds"],
                                   report=lambda text: print(text, file=sys.stderr, flush=True),
                                   batch=config.get("batch_leaves", 8), telemetry=self.telemetry,
                                   cache_window=config.get("summary_cache_window", 32))
        self.compactor.start()

    def dispatch(self, method: str, params: dict, cancelled=None):
        started, success = time.monotonic(), False
        try:
            result = self._dispatch(method, params, cancelled)
            success = True
            return result
        finally:
            if method != "status":
                self.telemetry.record("fetch" if method == "context" else method,
                                      time.monotonic() - started, success)

    def _dispatch(self, method: str, params: dict, cancelled=None):
        mem, compactor = self.memory, self.compactor
        timeout = params.get("timeout")
        deadline = time.monotonic() + timeout if timeout is not None else None

        def settle(all_nodes=False):
            remaining = max(0, deadline - time.monotonic()) if deadline is not None else None
            compactor.settle(remaining, all_nodes, cancelled)
        if method == "append":
            return vars(mem.append(**params))
        if method == "context":
            while True:
                settle()
                with mem.cv:
                    # Recheck under the same lock used by append.
                    if mem.first() == len(mem.messages):
                        return mem.render()
        if method == "begin":
            while True:
                settle()
                with mem.cv:
                    if mem.first() != len(mem.messages):
                        continue
                    view = mem.render()
                    message = mem.append("user", params["text"], params.get("event_key"))
                    return {"view": view, "message_id": message.i}
        if method == "zoom":
            return mem.zoom(**params)
        if method == "date":
            return mem.date(**params)
        if method == "view":
            with mem.cv:
                return {"messages": len(mem.messages), "settled": mem.first() == len(mem.messages),
                        "view_bytes": mem.view_size(), "view_budget": mem.view_bytes,
                        "lines": [{"start": part.start, "n": part.n, "built": part in mem.nodes,
                                   "text": mem.text(part)} for part in mem.view]}
        if method == "compact":
            settle(all_nodes=True)
            return self.dispatch("status", {})
        if method == "export":
            return mem.export_html()
        if method == "status":
            with mem.cv:
                return {"messages": len(mem.messages), "nodes": len(mem.nodes),
                        "view_parts": len(mem.view), "view_bytes": mem.view_size(),
                        "view_budget": mem.view_bytes, "settled": mem.first() == len(mem.messages),
                        "pending_leaves": sum(Part(0, i) not in mem.nodes for i in range(len(mem.messages))),
                        "queued_nodes": sum(p not in compactor.busy for p in mem.candidates()),
                        "worker_limit": compactor.jobs, "pid": os.getpid(),
                        "summary_cache_window": compactor.cache_window,
                        "cached_context_windows": len(compactor.contexts),
                        "metrics": self.telemetry.snapshot(),
                        "busy": [f"{p.start}+{p.n}" for p in sorted(compactor.busy)],
                        "failures": {f"{p.start}+{p.n}": e for p, e in compactor.failed.items()}}
        raise ValueError(f"Unknown method: {method}")


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def serve(home: Path):
    os.umask(0o077)
    service = Service(home)  # Lifetime writer lock precedes socket replacement.
    path = socket_path(home)
    path.unlink(missing_ok=True)

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            def cancelled():
                readable, _, _ = select.select([self.connection], [], [], 0)
                return bool(readable) and not self.connection.recv(1, socket.MSG_PEEK)

            try:
                request = json.loads(self.rfile.readline())
                if request["method"] == "shutdown":
                    result = "stopping"
                    threading.Thread(target=server.shutdown, daemon=True).start()
                else:
                    result = service.dispatch(request["method"], request.get("params", {}), cancelled)
                reply = {"result": result}
            except Exception as exc:
                reply = {"error": str(exc)}
            try:
                self.wfile.write((json.dumps(reply, ensure_ascii=False) + "\n").encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = Server(str(path), Handler)
    os.chmod(path, 0o600)
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        service.compactor.close()
        service.memory.close()
        path.unlink(missing_ok=True)


class Client:
    def __init__(self, home: Path, autostart: bool = True):
        self.home = Path(home).expanduser().resolve()
        self.autostart = autostart

    def _connect(self):
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = socket_path(self.home)
        with (self.home / "startup.lock").open("a+") as startup:
            fcntl.flock(startup, fcntl.LOCK_EX)
            return self._connect_locked(path)

    def _connect_locked(self, path):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(str(path))
            return sock
        except (FileNotFoundError, ConnectionRefusedError):
            sock.close()
        if not self.autostart or os.environ.get("OPTCHAT_NO_AUTOSTART"):
            raise RuntimeError("OptChat daemon is not running")
        log_fd = os.open(self.home / "daemon.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        log = os.fdopen(log_fd, "ab")
        env = dict(os.environ)
        # Also work from a source checkout, when the agent/MCP changes cwd.
        source_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = source_root + os.pathsep + env.get("PYTHONPATH", "")
        try:
            process = subprocess.Popen([sys.executable, "-m", "optchat", "--home", str(self.home), "serve"],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       cwd=str(self.home), env=env, start_new_session=True)
        finally:
            log.close()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(str(path))
                # Reap detached children when this client process stays alive.
                threading.Thread(target=process.wait, daemon=True).start()
                return sock
            except (FileNotFoundError, ConnectionRefusedError):
                sock.close()
                if process.poll() is not None:
                    raise RuntimeError(f"Daemon exited during startup; see {self.home / 'daemon.log'}")
                time.sleep(0.05)
        threading.Thread(target=process.wait, daemon=True).start()
        raise RuntimeError(f"Daemon did not start; see {self.home / 'daemon.log'}")

    def call(self, method: str, **params):
        with self._connect() as sock:
            sock.sendall((json.dumps({"method": method, "params": params}, ensure_ascii=False) + "\n").encode())
            with sock.makefile("rb") as stream:
                line = stream.readline()
            if not line:
                raise RuntimeError("Daemon disconnected; check daemon.log")
            reply = json.loads(line)
            if "error" in reply:
                raise RuntimeError(reply["error"])
            return reply["result"]
