"""The durable log, binary summary tree, and incremental view."""

from __future__ import annotations

import fcntl
import html
import json
import os
import threading
import warnings
from dataclasses import asdict, dataclass
from datetime import datetime
from fractions import Fraction
from pathlib import Path

KINDS = {"user", "talk", "tool", "echo", "note"}
PLACEHOLDER = "(not summarized yet: zoom it)"
CAP = 30_000


def byte_size(text: str) -> int:
    return len(text.encode("utf-8"))


def cut_bytes(text: str, limit: int) -> str:
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def cap_result(text: str, limit: int = CAP) -> str:
    if len(text) <= limit:
        return text
    # Count the marker in the cap; retain both ends, including trailing errors.
    omitted = len(text) - limit
    while True:
        marker = f"\n[OptChat: {omitted} characters omitted]\n"
        keep = max(0, limit - len(marker))
        new_omitted = len(text) - keep
        if new_omitted == omitted:
            break
        omitted = new_omitted
    head = (keep + 1) // 2
    tail = keep // 2
    return text[:head] + marker + (text[-tail:] if tail else "")


@dataclass(frozen=True, order=True)
class Part:
    l: int
    i: int

    @property
    def n(self) -> int:
        return 1 << self.l

    @property
    def start(self) -> int:
        return self.i * self.n

    @property
    def end(self) -> int:
        return self.start + self.n


@dataclass(frozen=True)
class Message:
    i: int
    kind: str
    text: str
    size: int
    date: str
    event_key: str | None = None

    @property
    def source(self) -> str:
        return f"{self.kind}: {self.text}"


@dataclass(frozen=True)
class Node:
    l: int
    i: int
    text: str
    size: int


class Memory:
    """One process owns a chat; threads share this object through a condition."""

    def __init__(self, home: Path, node_bytes: int = 512, view_bytes: int = 128_000):
        if node_bytes < 64 or view_bytes < node_bytes:
            raise ValueError("Require node_bytes >= 64 and view_bytes >= node_bytes")
        self.home = Path(home).resolve()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock_file = (self.home / "writer.lock").open("a+")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock_file.close()
            raise RuntimeError(f"Another writer owns {self.home}") from None
        self.node_bytes, self.view_bytes = node_bytes, view_bytes
        self.cv = threading.Condition(threading.RLock())
        self.messages: list[Message] = []
        self.nodes: dict[Part, Node] = {}
        self.keys: dict[str, int] = {}
        self.view: list[Part] = []
        self.closed = False
        try:
            self._load()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        with self.cv:
            self.closed = True
            self.cv.notify_all()
        self._lock_file.close()  # OS releases the lock even after a crash.

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _records(self, directory: str):
        folder = self.home / directory
        folder.mkdir(exist_ok=True, mode=0o700)
        for path in sorted(folder.glob("*.jsonl")):
            data = path.read_bytes()
            for number, line in enumerate(data.splitlines(), 1):
                try:
                    yield json.loads(line)
                except (ValueError, UnicodeError):
                    warnings.warn(f"Skipping torn/invalid JSON: {path}:{number}")
            if data and not data.endswith(b"\n"):
                self._write(path, b"\n")

    def _load(self) -> None:
        records = sorted(self._records("main"), key=lambda r: r["i"])
        for record in records:
            message = Message(**record)
            if message.i != len(self.messages):
                raise ValueError("Log has duplicate or missing message IDs; refusing to renumber history")
            if message.kind not in KINDS or message.size != byte_size(message.source):
                raise ValueError(f"Invalid message record {message.i}")
            self.messages.append(message)
            if message.event_key:
                self.keys[message.event_key] = message.i
        for record in self._records("tree"):
            node = Node(**record)
            part = Part(node.l, node.i)
            if node.l < 0 or node.i < 0 or part.end > len(self.messages):
                raise ValueError(f"Invalid tree node {part}")
            if not node.text.strip() or node.size != byte_size(node.text):
                raise ValueError(f"Invalid summary size {part}")
            if part in self.nodes:
                raise ValueError(f"Duplicate summary {part}")
            self.nodes[part] = node
        for part in self.nodes:
            if part.l and any(child not in self.nodes for child in self.children(part)):
                raise ValueError(f"Summary has missing children: {part}")
        for i in range(len(self.messages)):
            self.view.append(Part(0, i))
            self.fit(i + 1)

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        existed = path.exists()
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            if os.write(fd, data) != len(data):
                raise OSError("Short append; restart to recover the torn line")
            os.fsync(fd)
        finally:
            os.close(fd)
        if not existed:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

    def _save(self, directory: str, record: dict) -> None:
        day = datetime.now().astimezone().date().isoformat()
        data = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        self._write(self.home / directory / f"{day}.jsonl", data)

    def append(self, kind: str, text: str, event_key: str | None = None,
               date: str | None = None) -> Message:
        if kind not in KINDS or not isinstance(text, str):
            raise ValueError("Invalid message kind or text")
        with self.cv:
            if event_key and event_key in self.keys:
                return self.messages[self.keys[event_key]]
            if kind == "echo":
                text = cap_result(text)
            message = Message(len(self.messages), kind, text, byte_size(f"{kind}: {text}"),
                              date or datetime.now().astimezone().isoformat(), event_key)
            self._save("main", asdict(message))
            self.messages.append(message)
            if event_key:
                self.keys[event_key] = message.i
            self.view.append(Part(0, message.i))
            self.fit()
            self.cv.notify_all()
            return message

    @staticmethod
    def children(part: Part) -> tuple[Part, Part]:
        return Part(part.l - 1, 2 * part.i), Part(part.l - 1, 2 * part.i + 1)

    def text(self, part: Part) -> str:
        node = self.nodes.get(part)
        return node.text if node else PLACEHOLDER

    def view_size(self) -> int:
        return sum(byte_size(self.text(part)) for part in self.view)

    def fit(self, total: int | None = None) -> None:
        """Append/coarsen only. Select the oldest pair relative to its size."""
        total = len(self.messages) if total is None else total
        size = self.view_size()
        while size > self.view_bytes:
            candidates = []
            for index, (a, b) in enumerate(zip(self.view, self.view[1:])):
                parent = Part(a.l + 1, a.i // 2)
                if a.l == b.l and a.i % 2 == 0 and b.i == a.i + 1 and parent in self.nodes:
                    due = Fraction(total - a.start, 1 << (a.l + 2))
                    candidates.append((due, -index, parent))
            if not candidates:
                break
            _, negative_index, parent = max(candidates)
            index = -negative_index
            a, b = self.view[index:index + 2]
            size += byte_size(self.text(parent)) - byte_size(self.text(a)) - byte_size(self.text(b))
            self.view[index:index + 2] = [parent]

    def save_node(self, part: Part, text: str) -> None:
        if not text.strip():
            raise ValueError("Empty summary")
        with self.cv:
            if part in self.nodes:
                return
            if part.l and any(child not in self.nodes for child in self.children(part)):
                raise ValueError("A parent needs exactly two built children")
            node = Node(part.l, part.i, text, byte_size(text))
            self._save("tree", asdict(node))
            self.nodes[part] = node
            self.fit()
            self.cv.notify_all()

    def first(self) -> int:
        return next((p.start for p in self.view if p not in self.nodes), len(self.messages))

    def ready(self, part: Part) -> bool:
        return (part.i < len(self.messages) if part.l == 0 else
                all(c in self.nodes for c in self.children(part)))

    def candidates(self):
        first = self.first()
        for l in range(len(self.messages).bit_length()):
            for i in range(len(self.messages) // (1 << l)):
                part = Part(l, i)
                end = i if l == 0 else part.end
                if part not in self.nodes and end <= first and self.ready(part):
                    yield part

    def source(self, part: Part) -> str:
        if part.l == 0:
            return self.messages[part.i].source
        return "\n".join(self.nodes[c].text for c in self.children(part))

    def compact_context(self, part: Part) -> str:
        end = part.start if part.l == 0 else part.end
        texts = []
        for p in self.view:
            if p.start >= end:
                break
            if p not in self.nodes:
                raise RuntimeError("Compactor context contains an unbuilt summary")
            texts.append(self.nodes[p].text.replace("\n", " "))
        return "<chat>\n" + "\n".join(texts) + "\n</chat>"

    def render(self, require_settled: bool = True) -> str:
        with self.cv:
            if require_settled and self.first() != len(self.messages):
                raise RuntimeError("History is not summarized yet")
            return "<chat>\n" + "\n".join(
                f"{p.start}+{p.n}|{self.text(p).replace(chr(10), ' ')}" for p in self.view
            ) + "\n</chat>"

    def zoom(self, id: int, n: int) -> str:
        with self.cv:
            if (type(id) is not int or type(n) is not int or id < 0 or n < 1 or
                    n & (n - 1) or id % n or id + n > len(self.messages)):
                raise ValueError(f"No line {id}+{n}.")
            if n == 1:
                return f"{id}+0|{self.messages[id].source}"
            parent = Part(n.bit_length() - 1, id // n)
            children = self.children(parent)
            if any(c not in self.nodes for c in children):
                raise ValueError(f"No built line {id}+{n}.")
            return "\n".join(f"{p.start}+{p.n}|{self.text(p).replace(chr(10), ' ')}" for p in children)

    def date(self, id: int) -> str:
        with self.cv:
            if type(id) is not int or not 0 <= id < len(self.messages):
                raise ValueError(f"No message {id}.")
            return datetime.fromisoformat(self.messages[id].date).astimezone().isoformat()

    def export_html(self) -> str:
        with self.cv:
            sections = ["<!doctype html><meta charset='utf-8'><title>OptChat memory</title>",
                        "<style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:20px}"
                        "pre{white-space:pre-wrap;overflow-wrap:anywhere}article{border-top:1px solid #ccc}"
                        "small{color:#666}</style><h1>OptChat memory</h1><h2>Current view</h2><pre>",
                        html.escape(self.render(False)), "</pre><h2>ROOT</h2>"]
            for message in self.messages:
                sections.append(f"<article id='message-{message.i}'><h3>{message.i} · {message.kind}</h3>"
                                f"<small>{html.escape(message.date)} · {message.size} bytes</small>"
                                f"<pre>{html.escape(message.text)}</pre></article>")
            for l in sorted({p.l for p in self.nodes}):
                sections.append(f"<h2>Tree level {l}</h2>")
                for p in sorted((p for p in self.nodes if p.l == l), key=lambda p: p.i):
                    node = self.nodes[p]
                    dates = f"{self.messages[p.start].date} — {self.messages[p.end - 1].date}"
                    sections.append(f"<article><h3>{p.start}+{p.n}</h3><small>{html.escape(dates)}"
                                    f" · {node.size} bytes</small><pre>{html.escape(node.text)}</pre></article>")
            return "\n".join(sections)
