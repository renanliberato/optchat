Run `optchat run --agent claude 'message'` or `optchat chat --agent claude`.

Each turn runs `claude -p --output-format stream-json --verbose
--no-session-persistence`, injects a settled pre-append view, attaches the OptChat
MCP tools, and stores completed talk/tool/result events. No native session is
resumed. Implementation lives in `optchat/adapters.py`.
