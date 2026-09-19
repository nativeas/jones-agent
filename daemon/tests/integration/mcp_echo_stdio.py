#!/usr/bin/env python3
"""Minimal stdio MCP server for `test_mcp_e2e.py` (Issue #17 §2's "接一个本地
stdio MCP（例如 Python 写的 5 行 echo server）"). Newline-delimited JSON-RPC 2.0
over stdio, hand-written against the MCP spec's `initialize`/`tools/list`/
`tools/call` shapes — no `mcp` SDK dependency, same "stdlib-only fake double"
precedent as `tests/fake_acp_agent.py` for ACP.

One tool: `echo(text: str) -> text`.
"""

from __future__ import annotations

import json
import sys

TOOL = {
    "name": "echo",
    "description": "Echoes back the given text.",
    "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
}


def _respond(req_id, result) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        req_id = msg.get("id")
        if method == "initialize":
            _respond(req_id, {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-03-26"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "jones-e2e-echo-stdio", "version": "0.1.0"},
            })
        elif method == "notifications/initialized":
            continue  # no response expected for a notification
        elif method == "tools/list":
            _respond(req_id, {"tools": [TOOL]})
        elif method == "tools/call":
            args = msg.get("params", {}).get("arguments", {})
            text = args.get("text", "")
            _respond(req_id, {"content": [{"type": "text", "text": text}], "isError": False})
        elif req_id is not None:
            _respond(req_id, None)


if __name__ == "__main__":
    main()
