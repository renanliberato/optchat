Generate with `optchat hooks codex`. Merge the fragment into `.codex/hooks.json`
or `~/.codex/hooks.json`, preserve existing hooks, and review/trust it with
Codex's `/hooks`. Capture and context injection live in `optchat/adapters.py`.
Use the same `OPTCHAT_HOME` or `--home` as your MCP server and Claude adapter.

Persistent Codex sessions keep their native context. Use the wrapper for fresh
turns.
