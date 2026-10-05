Run `optchat run --agent codex 'message'` or `optchat chat --agent codex`.

Each turn runs `codex exec --json --ephemeral`, injects a settled pre-append view,
attaches the OptChat MCP tools, and stores completed talk/tool/result events.
No native session is resumed. Implementation lives in `optchat/adapters.py`.
