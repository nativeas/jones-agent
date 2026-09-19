"""Standalone repro for Issue #35 (see docs/design/02-w3-interfaces.md §1.2's
"已发现但不在本分支范围内修复的 bug" note): run as its own process
(`python repro_issue_35.py <tmp_dir>`), NOT under pytest —
`test_issue_35_repro.py` runs it as a subprocess with a wall-clock timeout so
a hang is a deterministic test FAILURE instead of an actually-frozen CI job.

Drives a full `SessionService` (the same object `__main__.py` builds) through
exactly the sequence docs/design/02-w3-interfaces.md §1.2 describes: one real
`session/request_permission` round trip (a `CUSTOM_PERMISSION_JSON` prompt,
answered "allow" via a real `permission_decide()` call — the same pattern
`tests/test_gates_sessions_integration.py` uses), then a second, completely
ordinary Turn on the SAME session, then `service.shutdown()` — the same call
`__main__.py`'s `_run()` makes on SIGTERM — all inside one `asyncio.run()`,
matching how the real daemon process runs. Prints progress markers and
`REPRO_OK` if teardown completes; a hang means the process never gets there
at all (the test's subprocess timeout is what actually catches that — this
script sets no internal timeout of its own, so as not to race the hang away).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from jones_daemon import paths  # noqa: E402
from jones_daemon.context import DaemonContext  # noqa: E402
from jones_daemon.projects.bootstrap import bootstrap_projects_and_agents  # noqa: E402
from jones_daemon.sessions.service import (  # noqa: E402
    DEFAULT_AGENT_ID,
    DEFAULT_PROJECT_ID,
    SessionService,
)
from jones_daemon.store import apply_pending, connect, run_in_db_thread  # noqa: E402

_FAKE_AGENT = str(Path(__file__).parent / "fake_acp_agent.py")


class _FakeServer:
    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, str, Any]] = []

    async def broadcast(self, session_id, method, params) -> None:
        self.broadcasts.append((session_id, method, params))

    def events(self, method: str) -> list[tuple[str, Any]]:
        return [(sid, p) for sid, m, p in self.broadcasts if m == method]


class _StubProviderResolver:
    def resolve(self, model_pref):
        return {"provider": "anthropic", "model": "claude-test", "env": {}, "hermes_config": {}}

    def list_models(self, provider):
        return []


class _StubConfigResolver:
    def settings(self, project_id):
        return {}

    def permissions(self, project_id):
        return {"rules": []}

    def mcp_servers(self, project_id):
        return []


async def _wait_until(predicate, *, timeout: float = 10.0, interval: float = 0.02) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition not met in time")


def _custom_permission_prompt(tool: str, args: dict) -> str:
    # Same encoding `test_gates_sessions_integration.py::_custom_permission_
    # prompt` uses (duplicated, not imported: this script must stay runnable
    # standalone, no pytest/conftest on its import path).
    from jones_daemon.kernel.plugin.jones_gate import _review_payload

    encoded = _review_payload.encode(tool, args, mode="task")
    payload = {"toolCall": {"toolCallId": "gate-1", "title": tool,
                             "rawInput": {"command": f"<{tool}> (plugin approval rule)",
                                          "description": encoded}}}
    return f"CUSTOM_PERMISSION_JSON:{json.dumps(payload)}"


async def main() -> None:
    home = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/jones_repro_35")
    home.mkdir(parents=True, exist_ok=True)
    os.environ["JONES_HOME"] = str(home)

    def _open():
        conn = connect(home / "jones.db")
        apply_pending(conn)
        bootstrap_projects_and_agents(conn)
        return conn

    conn = await run_in_db_thread(_open)

    server = _FakeServer()
    ctx = DaemonContext(
        db=conn, paths=paths, server=server,
        providers=_StubProviderResolver(), config=_StubConfigResolver(),
    )
    service = SessionService(ctx, worker_cmd=[sys.executable, _FAKE_AGENT])
    await service.startup()

    # task mode (not auto): a real `terminal` call is NEVER auto-allowed
    # (`permissions/review.py::_classify_terminal` never returns "low"), so
    # this deterministically takes the real user-gate wait -> `permission_
    # decide()` path, the actual round trip 02-w3-interfaces.md §1.2 reports.
    session = await service.create(
        project_id=DEFAULT_PROJECT_ID, agent_id=DEFAULT_AGENT_ID, mode="task", title="repro-35"
    )
    session_id = session["id"]

    await service.send(session_id, _custom_permission_prompt("terminal", {"command": "ls"}))
    await _wait_until(lambda: server.events("permission.requested"))
    pending = await service.permission_pending(session_id)
    await service.permission_decide(pending[0]["request_id"], "allow")
    await _wait_until(
        lambda: session_id not in service._turn_tasks or service._turn_tasks[session_id].done()
    )
    print("FIRST_TURN_DONE", flush=True)

    await service.send(session_id, "second, ordinary prompt, no permission needed")
    await _wait_until(
        lambda: session_id not in service._turn_tasks or service._turn_tasks[session_id].done()
    )
    print("SECOND_TURN_DONE", flush=True)

    await service.shutdown()
    print("REPRO_OK", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
