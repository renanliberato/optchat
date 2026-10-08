# OptChat

One durable chat shared by Codex, Claude Code and OpenCode. Every wrapper turn starts a new
agent process with a summary view of the entire history followed by the new user
message. A background daemon compresses the append-only log into a binary tree;
the agent can recover exact original messages through `zoom`.

Implemented in Python 3.11+ with no runtime dependencies, for macOS/Linux.
Based on [Victor Taelin's OptChat specification](https://gist.github.com/VictorTaelin/91837951a5ce5b38f341ec1ba1df6449).

## Start

Install in a virtual environment, or use `python3 -m optchat` from this checkout:

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
source .venv/bin/activate

optchat init
optchat chat --agent codex --cwd /path/to/project --sandbox workspace-write
```

Install and authenticate the CLI you use (`codex`, `claude` or `opencode`) beforehand. The default compactor
uses `claude -p --model sonnet --effort medium`, with tools disabled. Its model
calls use your Claude account. `--summarizer codex` instead runs ephemeral
`codex exec` calls with `--summary-model` (default `gpt-6-luna`) and high
reasoning effort. `--summarizer opencode` uses `opencode run --format json` with
`opencode-go/deepseek-v4.1-flash` by default (override with `--summary-model`);
its compaction agent has all tools denied. `--summarizer openrouter` calls
OpenRouter's chat-completions API directly, without any agent harness, using
`openai/gpt-6-luna` and low reasoning effort by default (override the model
with `--summary-model` and the effort with `openrouter_reasoning_effort`); it
reads `openrouter_api_key` from `config.json`, falling back to
`OPENROUTER_API_KEY` in the daemon's environment. Every request demands
zero data retention (`provider.zdr` plus `data_collection: deny`), so a model
with no ZDR endpoint is unavailable; `openrouter_provider` adds further routing
preferences (for example `{"sort": "price"}`) without relaxing that. `--summarizer command`
runs a custom provider.

Inside `chat`, `/agent claude`, `/agent codex` and `/agent opencode` switch vendors while preserving
the same history; `/quit` exits. The daemon continues compacting after you exit.
Input is line-based. Pipe larger messages to `run`:

```sh
optchat run --agent codex 'Inspect the failing build'
optchat run --agent claude 'Continue using what Codex found'
optchat run --agent opencode --model opencode-go/deepseek-v4.1-flash 'Continue the work'
cat request.txt | optchat run --agent codex
optchat status
optchat stop
```

Use `--model` to select the master model, `--instructions` for a constant
instructions file (defaults to the working directory's `AGENTS.md`), and
`--permission-mode` for Claude's tool permissions. Agent permissions otherwise
use the vendor's configured defaults. Switching with `/agent` keeps the other
runner options; restart `chat` to change its model or permission options.

OpenCode defaults to `opencode-go/deepseek-v4.1-flash` for both chat and summaries.
It receives OptChat instructions and its `zoom`/`date` MCP server through temporary
inline configuration (`OPENCODE_CONFIG_CONTENT`), without editing your settings.
Fresh sessions are created each turn; OpenCode may retain them in its local database,
with sharing disabled. Its configured permissions apply; requests requiring approval
are rejected by the noninteractive CLI. Codex `--sandbox` and Claude
`--permission-mode` options are rejected for OpenCode. OpenCode lifecycle hooks
are not supported; use the `run`/`chat` wrapper for shared-memory capture.
Set `opencode_binary` in `config.json` to override the summarizer executable.
For a new OpenCode-backed chat directory:

```sh
optchat --home /path/to/new/chat init --summarizer opencode
optchat --home /path/to/new/chat chat --agent opencode
```

[OpenCode CLI](https://opencode.ai/docs/cli/) and
[inline configuration](https://opencode.ai/docs/config/) describe the runtime interface.

`OPTCHAT_HOME` or global `--home PATH` chooses a different memory directory.
The default is `~/.optchat`, shared across projects and vendors; use separate
directories if you want separate chats. The `--home` option precedes the command:

```sh
python3 -m optchat --home ./.optchat init
python3 -m optchat --home ./.optchat chat --agent codex
```

## Memory commands

```sh
optchat append note 'Imported decision: use append-only storage'
optchat append user < message.txt
optchat context --timeout 60       # waits until every view entry is summarized
optchat zoom 0 8                   # children of messages [0, 8)
optchat zoom 3 1                   # complete original message 3
optchat date 3
optchat compact --timeout 120      # waits for every possible tree node
optchat export memory.html
optchat import old-history.jsonl
```

Import accepts one JSON object per line with `text`, optional `kind` (default
`note`), and optional ISO `date`. It assigns new sequential IDs; reimporting the
same file is idempotent. All input is validated before writes begin. `--event-key`
on `append` also makes retries idempotent across restarts.

Native Codex backfill uses a reviewed JSON manifest of `{id, title, status}`
objects from the app's idle, unarchived chats (`idle` or `notLoaded`). It checks
archive state through Codex's read-only database, skips internal instructions,
reasoning and compaction snapshots, and deduplicates stable item IDs (including
fork history) and captured hook events. Review the dry run before applying:

```sh
python -m optchat.backfill idle-chats.json --report backfill-report.json
python -m optchat.backfill idle-chats.json --report backfill-report.json --apply
```

Recheck live app status before applying the manifest. Original timestamps are
retained; blocks append as historical memory. Tool outputs follow OptChat's
normal length cap. Source chats are not changed, archived, or resumed.

Storage is plain, durable JSONL:

```text
~/.optchat/
  config.json
  main/YYYY-MM-DD.jsonl   # {i, kind, text, size, date, event_key}
  tree/YYYY-MM-DD.jsonl   # {l, i, text, size}
  daemon.log
  writer.lock            # OS-managed lifetime flock, no PID or lock expiry
  startup.lock
  turn.lock
  rpc.sock               # private local RPC socket
```

The daemon owns the writer lock throughout its lifetime. Commands, hooks and MCP
connect to that daemon instead of writing independently. Each append uses a
single write followed by fsync; file creation also fsyncs its parent directory.
Invalid JSON tails are reported and skipped; missing newlines are repaired by
appending a newline. Missing or duplicate valid message IDs cause a clear error,
because silently renumbering history would invalidate the entire tree.

Back up `main/`, `tree/`, and `config.json`. They contain your complete captured
chat and potentially sensitive tool output. Logs are never rotated or deleted
by OptChat. Stop the daemon before a consistent filesystem backup or config
change; existing summaries are reused, never recomputed. Keep byte budgets
constant for a given chat to preserve its view behavior.

## Hooks and MCP

Wrappers automatically attach the MCP `zoom` and `date` tools and capture vendor
JSON streams. They do not require hook installation. Their subprocesses set
`OPTCHAT_WRAPPER=1`, suppressing duplicate capture from OptChat hooks.

For an existing persistent agent session, generate a configuration fragment:

```sh
optchat hooks codex > codex-hooks.json
optchat hooks claude > claude-hooks.json
```

Merge its `hooks` object into `.codex/hooks.json` (or `~/.codex/hooks.json`) for
Codex, or `.claude/settings.json` for Claude. Preserve existing handlers. In
Codex, review/trust hooks through `/hooks` and enable its hooks feature if needed.

To install this checkout for your user account after creating the virtualenv:

```sh
.venv/bin/python scripts/install-user.py
```

The installer preserves unrelated settings and hooks, backs up changed files in
`~/.optchat/install-backups/`, registers the OptChat MCP server with both agents,
and installs `~/.local/bin/optchat`. It adds standing instructions to Codex's
user `developer_instructions` and `~/.claude/CLAUDE.md`. The default Claude
compactor uses an absolute executable path so desktop launches can find it.
Review/trust the installed Codex hooks with `/hooks`, then start new sessions.
The checkout and its virtualenv must remain at their installed paths.

Codex app and local Claude Desktop **Code** sessions can use these user settings;
ordinary Claude Desktop **Chat** sessions do not run Claude Code hooks. Remote
Claude Code environments require a separate installation. Native desktop sessions
retain their native history; use `optchat run` or `optchat chat` for fresh turns.

The fragment captures user prompts, tool calls/results and final replies;
`UserPromptSubmit` waits for prior summaries and injects the history view.
`SessionStart` adds OptChat usage instructions, including how to navigate memory
with `zoom` and `date`; those instructions also precede every injected view and
are advertised by the MCP server. Hooks add model-visible context rather than
rewrite the vendor's system prompt. Wrappers add the same guidance to Codex's
developer instructions or Claude's appended system prompt.

Codex's generated prompt hook raises `additionalContextLimit` to 100,000 tokens
so a normal memory view stays inline. Claude limits hook context to 10,000
characters and spills larger output to a file; the guidance tells the agent to
read the complete saved output before using it. This remains dependent on the
agent following that instruction. The wrappers bypass hook-output limits by
passing the view directly as the turn input.

`PreCompact`/`PostCompact` verify the memory service is available; entries have
already been persisted individually. A summary timeout blocks prompt submission
instead of silently continuing without memory. Stop captures only the final
assistant message exposed by the hook; intermediate assistant text is captured
by the wrappers, which provide the more complete transcript. Stable vendor
tool/turn IDs prevent duplicate capture when available.

For hook sessions, attach `optchat mcp` as a stdio MCP server using your vendor's
MCP settings (wrappers do this automatically). Example Claude configuration:

```json
{"mcpServers":{"optchat":{"command":"optchat","args":["mcp"]}}}
```

Use an absolute executable path if your agent does not inherit the virtualenv's
PATH. Use the same `--home` on the hook and MCP configuration. See
[Codex hooks](https://developers.openai.com/codex/hooks),
[Codex noninteractive mode](https://developers.openai.com/codex/noninteractive),
[Claude CLI reference](https://code.claude.com/docs/en/cli-reference), and
[Claude hooks](https://code.claude.com/docs/en/hooks).

## Custom compactor

```sh
optchat --home /path/to/new-chat init --summarizer command \
  --summary-command 'python3 /absolute/path/to/provider.py'
```

The command receives JSON on stdin:

```json
{"system":"The compactor prompt","messages":[{"role":"user","content":"<chat>...</chat>\n\nCompression step"}]}
```

Print only the summary text to stdout, diagnostics to stderr, and exit nonzero
on failure. Corrections resend the complete conversation, including previous
assistant replies; the command must use it as conversation history. Commands
are argv arrays, never executed through a shell. Failures are not downgraded to
an excerpt; the one exception is a provider length-limit rejection, which cuts
the largest source by ten percent and retries. Other failures retry every ten
seconds; inspect `status` and `daemon.log`, or cancel a wait with Ctrl-C.

## Fidelity and limits

The engine follows the specification's binary nodes, free nodes, contextual
compression without addressing prefixes, exact UTF-8 byte accounting, five
oversize attempts keeping the shortest, ordered leaf compression, eight jobs,
incremental most-due merging, summary-only settling, and pre-append turn views.
Same-level nodes (consecutive leaves, or pairs of adjacent lines) are compressed
in one model call (`batch_leaves`, default 8, `1` disables batching), and several
independent frontier batches run at once; a reply that does not yield exactly one
valid line per item falls back to per-item calls.

Summarization now reuses an older context snapshot for each window of 32 nodes
at a given tree level (`summary_cache_window`; `0` restores the changing-view
layout). A fresh `<recent_chat>` tail carries intervening decisions, including
pending predecessors; the two blocks together stay within the context budget.
The snapshot survives view coarsening. Historical nodes crossing a cutoff are
split into their children, so later messages cannot leak into earlier summaries.
This also works with `batch_leaves: 1`: each node still has its own conversation
and correction history, while its input prefix is shared with other nodes.

OpenRouter nodes share a `session_id` for provider sticky routing. One actual
summary warms a cold prefix before other workers use it concurrently; no extra
warm-up request is made. On OpenAI GPT-5.6 and GPT-6 models, explicit caching marks
only the shared snapshot, leaving unique sources and fresh tails uncached and
avoiding cache-write charges on those one-off tokens. Other models retain
automatic caching. `openrouter_explicit_cache: false` disables the explicit
controls. Short prefixes below a provider's minimum and expired/evicted caches
can still miss; no artificial padding is added. ZDR and data-collection-deny
routing remain mandatory; extended cache retention is not requested.

To benchmark on a private history copy using different messages, rather than
identical-prompt replays:

```sh
python3 scripts/benchmark-summary-cache.py --start 40246 --count 8 \
  --budget .10 --output /tmp/optchat-cache-benchmark.json
```

This makes paid requests with the configured model/key. It stops starting calls
once reported spending reaches `--budget` (the final call can cross that amount).
Each arm has a cold benchmark namespace. Messages stream into isolated copies;
archived merge summaries drive view coarsening without buying extra merges.
The report separates first attempts from corrections, includes provider, actual
cost, tokens, latency, byte-limit compliance, and source/reference/new summaries
for manual fidelity review. It contains private history and is created with
mode 0600; keep it outside version control. The running chat is never modified.
Defaults are 512-byte summary targets and a 128,000-byte view budget. The budget
counts summary text, as in the spec; addressing markup adds overhead. Oversize
summaries or very small configured budgets can leave an irreducible view over
budget; history is never truncated to force a fit. A request the provider
rejects for exceeding its length limit is retried with the largest source cut
by ten percent, so a pathological message still becomes a summary.

The wrappers deliberately use fresh invocations (`codex exec --ephemeral` and
`claude -p --no-session-persistence`, or `opencode run`), without resume/continue. Hooks in a normal
session retain that session's native context and are an approximation.

CLI adapters cannot reproduce the reference API request layout, explicit cache
breakpoints, encrypted reasoning replay, or in-turn tool-result capping. They
pass view and prompt as ordered text to the vendor CLI, whose native system
prompt, workspace instructions, tools, caching and within-turn compaction still
apply. OptChat caps *stored* tool results at 30,000 characters (head plus tail),
but the vendor sees its own original result. Completed CLI events supply the
available tool records; raw API calls, hidden tool traffic and model thoughts
are not stored. The built-in Claude compactor temporarily persists a session per
node to support same-conversation size corrections; those are separate from
master turns and outside OptChat's transcript.

This version provides sequential interactive turns; mid-run user-message
injection, remote attachment, optional subagent/computer orchestration and
automatic Git backups are not implemented. The terminal keeps its usual
scrollback, and `export` produces a full standalone HTML browser.

## macOS menu bar dashboard

[OptChatBar](macos/README.md) shows local daemon health, message/summary totals,
pending work, concurrent summarizers, disk size, hourly processing, operation
latency, and reported summarizer tokens. Build with `./scripts/build-menubar.sh`,
then open `dist/OptChatBar.app`. It refreshes every five seconds and can select a
custom memory folder. A **Memory Viewer…** button opens a window that expands
summary lines into their children down to full messages. Monitoring and viewing
never start the daemon or trigger model calls; `optchat monitor` returns the
same read-only JSON snapshot and `optchat view` the view lines.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Tests are offline and require local Unix socket access. Integration tests use
fake Codex/Claude executables and a deterministic custom compactor, exercising
actual daemon processes, concurrent clients, restarts, CLI import/export, MCP,
and shared history across fresh vendor turns. No paid model calls run in tests.
