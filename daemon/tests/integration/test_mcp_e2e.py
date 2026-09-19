"""Real-Hermes MCP end-to-end check (Issue #17 §2 / FR13's acceptance
criterion: "stdio 与 HTTP 各接一个 MCP Server"). Same gating as
`test_real_hermes_e2e.py` (`JONES_E2E=1` + `ANTHROPIC_API_KEY`) and for the
same reason — a real worker's startup self-check alone already needs one real
model Turn (`workers/manager.py::_startup_self_check`), so there's no cheaper
way to prove MCP wiring end to end than a real Hermes worker.

Proves `capabilities/mcp_config.py` + `workers/manager.py::_prepare_hermes_
home`'s `mcp_servers` wiring: a stdio server (`mcp_echo_stdio.py`) and an HTTP
server (`mcp_echo_http.py`) configured the same way `ctx.config.mcp_servers
(project_id)` would hand them to `WorkerManager`, both connected by the real
worker, both tools callable by the real model.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from jones_daemon.workers.manager import WorkerManager

pytestmark = pytest.mark.skipif(
    not (os.environ.get("JONES_E2E") and os.environ.get("ANTHROPIC_API_KEY")),
    reason="real-Hermes MCP e2e: set JONES_E2E=1 and ANTHROPIC_API_KEY to run (see module "
    "docstring)",
)

_STDIO_SERVER = str(Path(__file__).parent / "mcp_echo_stdio.py")
_HTTP_SERVER = str(Path(__file__).parent / "mcp_echo_http.py")

_PROMPT = (
    "Call the `echo` tool from the `stdio_echo` MCP server with text='stdio-ok', "
    "then call the `echo` tool from the `http_echo` MCP server with text='http-ok'. "
    "Call both tools before replying. Do not explain, just call them."
)


@pytest.fixture
def http_echo_server():
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, test-only
        [sys.executable, _HTTP_SERVER, "0"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.monotonic() + 5.0
        port = None
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if line.startswith("LISTENING_ON="):
                port = int(line.strip().split("=", 1)[1])
                break
        assert port is not None, "http echo server never reported its port"
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


async def test_stdio_and_http_mcp_tools_are_both_callable_by_a_real_worker(
    tmp_path, http_echo_server
):
    class _McpConfigResolver:
        def mcp_servers(self, project_id: str | None) -> list[dict[str, Any]]:
            return [
                {"name": "stdio_echo", "transport": "stdio",
                 "command": sys.executable, "args": [_STDIO_SERVER], "env": {}},
                {"name": "http_echo", "transport": "http", "url": http_echo_server, "headers": {}},
            ]

    events: list[dict[str, Any]] = []

    async def on_update(_session_id, params):
        events.append(params)

    async def on_permission(_session_id, params):
        options = params.get("options") or []
        allow = next((o for o in options if "allow" in o.get("kind", "")), options[0])
        return {"outcome": {"outcome": "selected", "optionId": allow["optionId"]}}

    async def on_crash(_session_id, _returncode):
        pass

    manager = WorkerManager(
        user_root=tmp_path,
        on_session_update=on_update,
        on_request_permission=on_permission,
        on_worker_crash=on_crash,
        startup_timeout_s=30.0,
        config=_McpConfigResolver(),
    )
    await manager.start()
    try:
        worker = await manager.ensure_started("e2e-mcp-1", cwd=str(tmp_path), project_id="p1")
        assert worker.acp_session_id is not None

        response = await worker.client.prompt(worker.acp_session_id, _PROMPT)
        assert response.get("stopReason") in ("end_turn", "cancelled")

        tool_calls = [
            e["update"] for e in events
            if e.get("update", {}).get("sessionUpdate") in ("tool_call", "tool_call_update")
        ]
        called_tool_names = {
            (tc.get("title") or "") for tc in tool_calls
        }
        # Real MCP tool names follow `mcp__<server>__<tool>`
        # (`tools/mcp_tool_schema.py::build_mcp_tool_name`, source-verified —
        # see `capabilities/registry.py`'s docstring).
        assert any("stdio_echo" in name for name in called_tool_names), called_tool_names
        assert any("http_echo" in name for name in called_tool_names), called_tool_names

        outputs = [tc.get("rawOutput") for tc in tool_calls if tc.get("status") == "completed"]
        flat = " ".join(str(o) for o in outputs)
        assert "stdio-ok" in flat
        assert "http-ok" in flat
    finally:
        await manager.stop()
