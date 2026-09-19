"""Jones `mcp.json` entry <-> Hermes `config.yaml`'s `mcp_servers:` dict (Issue #17,
FR13). `config/resolver.py::ConfigResolver.mcp_servers()` (C/#8-#9, 01-w2-interfaces.md
§4.1) only specifies `{"name": string, ...}` — the rest was left for whoever actually
consumes it. This module is that specification, made concrete here (03-w4-interfaces.md
§2 is the contract this amends — see the PR report's "契约变更").

## Concrete `mcp.json` server entry schema

```jsonc
// stdio transport
{"name": "echo", "transport": "stdio", "command": "python3", "args": ["server.py"],
 "env": {"FOO": "bar"}, "enabled": true}
// HTTP (Streamable HTTP by default; "transport": "sse" opts into legacy SSE)
{"name": "docs", "transport": "http", "url": "https://example.com/mcp",
 "headers": {"Authorization": "Bearer ..."}, "enabled": true}
```

`transport` is optional and inferred when absent: `command` present -> stdio,
`url` present -> http. `enabled` defaults to `true`; `enabled: false` (or a
missing/malformed entry) drops the server from what's written to the worker's
`config.yaml` entirely — matching `acp_adapter/session.py::_make_agent`'s own
`cfg.get("enabled", True) is not False` filter for computing `configured_mcp_
servers`, so a Jones-disabled server is invisible to Hermes exactly the way an
absent one would be.

## Hermes's per-server `config.yaml` shape (source-verified)

`tools/mcp_tool_server_run.py::MCPServerTask._prepare_run` distinguishes stdio
vs HTTP purely by key presence (`"url" in config` -> HTTP; see `_is_http()`);
`_mcp_server_config()` in the installed `hermes-agent`'s `acp_adapter/server.py`
(the ACP-side converter for the OTHER mcp-server channel, `session/new`'s
`mcpServers` param — Jones doesn't use that channel, see the PR report for why
`config.yaml` is the right one) confirms the exact same two shapes:
  - stdio: `{"command": str, "args": [str, ...], "env": {str: str, ...}}`
  - http:  `{"url": str, "headers": {str: str, ...}}` (+ optional `"transport":
    "sse"` for legacy SSE, `_prepare_run`'s `config.get("transport") != "sse"`
    branch)
"""

from __future__ import annotations

from typing import Any


def hermes_mcp_server_config(entry: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """One Jones `mcp.json` entry -> `(name, hermes_config)`, or `None` if the
    entry is malformed or explicitly disabled. Never raises — a malformed
    third-party-authored entry (hand-edited `mcp.json`) is dropped, not fatal
    to every other configured server (DEV.md 工程原则 #4: 诚实失败 — dropping
    silently would be the wrong kind of "never fails", so callers should log
    what got dropped; see `workers/manager.py::_prepare_hermes_home`)."""
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        return None
    if entry.get("enabled", True) is False:
        return None

    command = entry.get("command")
    url = entry.get("url")
    transport = entry.get("transport")

    if isinstance(command, str) and command and not (isinstance(url, str) and url):
        args = [a for a in (entry.get("args") or []) if isinstance(a, str)]
        env_raw = entry.get("env") or {}
        env = {k: v for k, v in env_raw.items() if isinstance(k, str) and isinstance(v, str)}
        return name, {"command": command, "args": args, "env": env}

    if isinstance(url, str) and url:
        headers_raw = entry.get("headers") or {}
        headers = {
            k: v for k, v in headers_raw.items() if isinstance(k, str) and isinstance(v, str)
        }
        cfg: dict[str, Any] = {"url": url, "headers": headers}
        if transport == "sse":
            cfg["transport"] = "sse"
        return name, cfg

    return None


def hermes_mcp_servers_dict(entries: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """`ctx.config.mcp_servers(project_id)`'s list -> the dict `config.yaml`'s
    `mcp_servers:` key wants (name -> per-server config), dropping anything
    `hermes_mcp_server_config` can't place. Last entry for a duplicate `name`
    wins (mirrors `config/resolver.py::mcp_servers()`'s own by-name merge, which
    a project-level entry can already override at that layer)."""
    out: dict[str, dict[str, Any]] = {}
    for entry in entries or []:
        converted = hermes_mcp_server_config(entry)
        if converted is not None:
            out[converted[0]] = converted[1]
    return out
