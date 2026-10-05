Generate with `optchat hooks claude`. Merge the fragment into
`.claude/settings.json`, preserving existing hooks. Capture and context injection
live in `optchat/adapters.py`. Use the same `OPTCHAT_HOME` or `--home` as your MCP
server and Codex adapter.

Persistent Claude sessions keep their native context. Use the wrapper for fresh
turns.
