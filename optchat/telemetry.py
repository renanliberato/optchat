"""Small durable aggregates; never stores prompts, replies, or credentials."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


class Telemetry:
    def __init__(self, home: Path):
        self.path = home / "metrics.json"
        self.lock = threading.RLock()
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {"since": time.time(), "operations": {}, "hourly": {},
                         "tokens": {}, "usage_calls": 0, "unreported_calls": 0}
        defaults = {"since": time.time(), "operations": {}, "hourly": {},
                    "tokens": {}, "usage_calls": 0, "unreported_calls": 0}
        if not isinstance(self.data, dict):
            self.data = defaults
        else:
            for key, value in defaults.items():
                if not isinstance(self.data.get(key), type(value)):
                    self.data[key] = value
            # Invalid nested aggregates are disposable; durable chat remains untouched.
            if any(not isinstance(item, dict) or not all(isinstance(item.get(key), (int, float))
                    for key in ("count", "errors", "total_ms", "max_ms"))
                   for item in self.data["operations"].values()):
                self.data["operations"] = {}
            self.data["hourly"] = {key: value for key, value in self.data["hourly"].items()
                                   if key.isdigit() and isinstance(value, dict)}
            self.data["tokens"] = {key: value for key, value in self.data["tokens"].items()
                                   if isinstance(value, (int, float)) and value >= 0}
        self.active = 0

    def _save(self):
        # Telemetry must never turn a successful durable memory operation into a failure.
        try:
            temporary = self.path.with_suffix(".tmp")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as file:
                json.dump(self.data, file)
            os.replace(temporary, self.path)
        except OSError:
            pass

    def record(self, operation: str, seconds: float, success: bool = True, count: int = 1):
        with self.lock:
            item = self.data["operations"].setdefault(operation, {"count": 0, "errors": 0,
                "total_ms": 0, "max_ms": 0})
            item["count"] += count
            item["errors"] += 0 if success else count
            item["total_ms"] += seconds * 1000
            item["max_ms"] = max(item["max_ms"], seconds * 1000)
            hour = int(time.time() // 3600) * 3600
            bucket = self.data["hourly"].setdefault(str(hour), {})
            bucket[operation] = bucket.get(operation, 0) + count
            self.data["hourly"] = {key: value for key, value in self.data["hourly"].items()
                                   if int(key) >= hour - 23 * 3600}
            self._save()

    def usage(self, usage: dict | None):
        with self.lock:
            self.data["usage_calls" if usage is not None else "unreported_calls"] += 1
            for key, value in (usage or {}).items():
                if isinstance(value, (int, float)) and value >= 0:
                    self.data["tokens"][key] = self.data["tokens"].get(key, 0) + value
            self._save()

    def snapshot(self):
        with self.lock:
            return {**json.loads(json.dumps(self.data)), "active_summarizers": self.active}

    def conversation(self, factory):
        telemetry = self
        class MeasuredConversation:
            def __init__(self):
                self.inner = factory()

            def ask(self, text):
                started, success = time.monotonic(), False
                with telemetry.lock:
                    telemetry.active += 1
                try:
                    reply = self.inner.ask(text)
                    telemetry.usage(getattr(self.inner, "last_usage", None))
                    success = True
                    return reply
                finally:
                    if not success:
                        telemetry.usage(None)
                    with telemetry.lock:
                        telemetry.active -= 1
                    telemetry.record("summarize", time.monotonic() - started, success)
        return MeasuredConversation
