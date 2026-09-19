#!/usr/bin/env python3
"""Minimal Streamable-HTTP MCP server for `test_mcp_e2e.py` (Issue #17 §2's
"...与一个 HTTP MCP"). stdlib `http.server` only, one POST endpoint answering
each JSON-RPC request with a single `application/json` response body (the
simplest conforming shape the Streamable HTTP transport allows for a server
that never needs to push an unsolicited message — no SSE, no session-id
negotiation) — same "5 行 echo server" spirit as `mcp_echo_stdio.py`, adapted
for HTTP.

Usage: `python mcp_echo_http.py <port>`; binds 127.0.0.1 only. Same single
`echo(text)` tool as the stdio double.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOL = {
    "name": "echo",
    "description": "Echoes back the given text.",
    "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args) -> None:  # noqa: A002 - stdlib signature
        pass  # quiet; the test harness captures the server's own stdout for readiness instead

    def do_POST(self) -> None:  # noqa: N802 - stdlib method name
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            msg = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        method = msg.get("method")
        req_id = msg.get("id")
        if method == "initialize":
            result = {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-03-26"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "jones-e2e-echo-http", "version": "0.1.0"},
            }
        elif method == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return
        elif method == "tools/list":
            result = {"tools": [TOOL]}
        elif method == "tools/call":
            args = msg.get("params", {}).get("arguments", {})
            result = {"content": [{"type": "text", "text": args.get("text", "")}], "isError": False}
        else:
            result = None

        payload = json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"LISTENING_ON={server.server_address[1]}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
