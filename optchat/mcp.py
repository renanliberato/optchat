"""Minimal newline-framed MCP stdio server. The daemon remains the writer."""

import json
import sys

from .compactor import OPTCHAT_GUIDANCE, VIEW_DOC

TOOLS = [
    {"name": "zoom", "description": "Open the line id+n of the view into the two lines of n/2 under it; n = 1 gives the message whole.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "integer", "minimum": 0},
                     "n": {"type": "integer", "minimum": 1}}, "required": ["id", "n"], "additionalProperties": False}},
    {"name": "date", "description": "The date and time of message id.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "integer", "minimum": 0}},
                     "required": ["id"], "additionalProperties": False}},
]


def handle(request, client):
    if "id" not in request:
        return None
    id, method = request["id"], request.get("method")
    if method == "initialize":
        result = {"protocolVersion": request.get("params", {}).get("protocolVersion", "2024-11-05"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "optchat", "version": "0.1.0"},
                  "instructions": OPTCHAT_GUIDANCE + "\n" + VIEW_DOC}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = request.get("params", {})
        try:
            if params.get("name") not in {"zoom", "date"}:
                raise ValueError("Unknown tool")
            text = client.call(params["name"], **params.get("arguments", {}))
            result = {"content": [{"type": "text", "text": text}]}
        except Exception as exc:
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": id, "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": id, "result": result}


def serve_stdio(client):
    for line in sys.stdin:
        try:
            request = json.loads(line)
            reply = handle(request, client)
        except Exception as exc:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
        if reply is not None:
            print(json.dumps(reply, ensure_ascii=False), flush=True)
