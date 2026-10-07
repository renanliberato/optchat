# OptChatBar

A native macOS 14+ menu bar dashboard for local OptChat, inspired by
[CodexBar](https://github.com/steipete/CodexBar). Built with AppKit, SwiftUI, and
Swift Charts; no Swift package dependencies and no Dock icon.

From the repository root:

```sh
./scripts/build-menubar.sh
open dist/OptChatBar.app
```

Requires Xcode/Swift 6 and Python 3.11+. The builder prefers this checkout's
`.venv/bin/python`; override it with `OPTCHAT_PYTHON=/absolute/path/to/python3`.
The app bundles the OptChat probe code and records the interpreter path, so
Python must remain installed at that path. The `.app` is locally ad-hoc signed,
not notarized for distribution. It can be moved elsewhere on this Mac.

The menu bar shows **OC** and a status dot. Click it to open statistics. Green
means the daemon is reachable, the view is settled, and no compaction is busy;
blue means processing, amber means waiting, red means a failed node awaiting
retry, and gray means offline. View readiness is also displayed independently,
since higher tree merges can continue after the view settles. The popover fits
the current screen and scrolls its statistics while keeping its title visible.

The app refreshes every five seconds. It uses `OPTCHAT_HOME`, a folder previously
chosen with **Change…**, or `~/.optchat`, in that order. **Change…** can select
hidden memory folders. It never starts/stops the daemon, appends chat messages,
or triggers model calls. **Open memory folder** reveals the local data.

## Memory viewer

**Memory Viewer…** in the popover footer opens a resizable window over the
current memory view. Click a line to expand it into the two summaries it was
made from, recursively down to full original messages; unbuilt lines are dimmed.
**Live** refreshes the view every five seconds, and Refresh updates on demand.
The window is read-only: it never appends messages, starts the daemon, or
triggers model calls. It reads through `optchat view`, `optchat zoom`, and
`optchat date` (see `macos/Sources/OptChatBar/Viewer/`).

## Measurements

- **Messages / summary nodes:** durable totals already exposed by the daemon.
- **Pending messages:** leaves without summary nodes, including assigned work.
- **Queued nodes:** currently eligible compaction nodes not assigned to workers.
  Ancestors that depend on unfinished nodes are not ready to queue yet.
- **Summarizers:** model calls currently executing, versus the configured worker
  limit. A batch can own many nodes with one model call; these are not master
  Codex/Claude sessions or all agents running on the computer.
- **Disk:** apparent bytes of regular files under the memory folder, including
  logs/config/backups/metrics; excludes symlinks. This is not filesystem allocation.
- **Processed chart:** successfully saved nodes per hour, for the last 24 hours;
  includes verbatim copies, leaves, and merges. Hours use local display time.
- **Latency:** `summarize` shows the median duration of the latest 30 provider
  invocations (including correction attempts); `fetch`, `compact`, and `zoom`
  show arithmetic means since telemetry began. Failed calls are included.
- **Tokens:** summarizer-only reported uncached input, output, cache-read and
  cache-write tokens. Codex's cached input is subtracted from its total input;
  Claude reports these categories separately. Custom providers and failed
  calls without usage are counted as unreported. These are token counts, not
  billed dollars or master-agent usage. Fetch/zoom/compact RPCs themselves do
  not invoke a model; background summarize calls account for model tokens.

Telemetry starts with the updated daemon; historical latency and token use
cannot be reconstructed. Restart it once after updating, allowing current work
to finish: `optchat stop`, then `optchat status` after shutdown completes. Old
running daemons remain readable, with unavailable fields shown as a dash.

Aggregates live in `metrics.json` with owner-only permissions. Operation counts,
latency totals/maxima, the latest 30 durations per operation, token totals, and
24 hourly buckets survive restart; the active-call gauge resets. No prompt/reply
contents are recorded in metrics.
Metrics writes are best effort and do not invalidate successful memory writes.

The same read-only snapshot is available through `optchat monitor`, including
when the daemon is offline. Unlike `optchat status`, it never autostarts a daemon.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
CLANG_MODULE_CACHE_PATH="$PWD/build/clang-cache" \
SWIFTPM_MODULECACHE_OVERRIDE="$PWD/build/swift-cache" \
swift test --disable-sandbox --package-path macos --scratch-path build/swift
```

Python integration tests use temporary Unix sockets and fake providers; they
make no paid model calls. They cover RPC metrics, compaction, restart, offline
probing, token normalization, concurrency, and telemetry failures. Swift tests
cover health precedence, older-daemon JSON, chart hours, and short-screen layout.

To run the real AppKit popover smoke test against the selected memory folder:

```sh
dist/OptChatBar.app/Contents/MacOS/OptChatBar \
  --replace --layout-check /tmp/optchat-layout
```

It replaces only other OptChatBar instances, opens its own popover, records its
window/display bounds and dashboard PNG, and exits nonzero if clipped. Run with
`OPTCHAT_HOME=/path/to/disposable/memory` to test an offline folder. `--replace
--show` replaces a previously built version and opens the live dashboard.
