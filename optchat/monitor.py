"""Read-only dashboard snapshot. Probing never starts or writes to the daemon."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import time
from pathlib import Path


def snapshot(home: Path, timeout: float = 2) -> dict:
    home = home.expanduser().resolve()
    result = {"home": str(home), "timestamp": time.time(), "running": False,
              "status": None, "metrics": None, "error": None, "disk_bytes": 0}
    path = home / "rpc.sock"
    if len(os.fsencode(path)) >= 100:
        digest = hashlib.sha256(os.fsencode(home)).hexdigest()[:20]
        path = Path("/tmp") / f"optchat-{os.getuid()}-{digest}" / "rpc.sock"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(path))
            sock.sendall(b'{"method":"status","params":{}}\n')
            with sock.makefile("rb") as stream:
                reply = json.loads(stream.readline(2_000_000))
            if "error" in reply:
                raise RuntimeError(reply["error"])
            result.update(running=True, status=reply["result"])
            result["metrics"] = reply["result"].get("metrics")
    except (OSError, ValueError, RuntimeError) as exc:
        result["error"] = str(exc)
    if result["metrics"] is None:
        try:
            result["metrics"] = json.loads((home / "metrics.json").read_text())
            result["metrics"]["active_summarizers"] = None
        except (OSError, ValueError):
            pass
    for directory, folders, files in os.walk(home, followlinks=False):
        folders[:] = [name for name in folders if not Path(directory, name).is_symlink()]
        for name in files:
            path = Path(directory, name)
            try:
                if path.is_file() and not path.is_symlink():
                    result["disk_bytes"] += path.stat().st_size
            except OSError:
                pass
    return result
