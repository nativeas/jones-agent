"""`sessions/methods.py::session_export` routes `delete_after: true` through
`SessionService.delete_guard` (Issue #23 round-3 review, review item 2):
before this fix, `session.export {delete_after: true}` called
`store/maintenance.py::export_session` (and its inner `delete_session`)
directly, bypassing the same lock/worker-stop guard `session.delete` already
goes through — an idle-but-still-alive worker's `HERMES_HOME` (real
credentials) could be `rmtree`d out from under a process that was still
running, and the `send()`/`_run_turn` race window `delete_guard` exists to
close (see that method's own docstring) stayed open on this second path.

Exercised against the real RPC dispatch (a live `RpcServer` on a Unix socket),
not just `SessionService` directly, because the bug was specifically in the
`sessions/methods.py` handler's own routing — a `SessionService`-level test
alone (`test_sessions_service.py::test_delete_guard_*`) cannot see whether the
handler actually calls `delete_guard` at all.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from jones_daemon.context import DaemonContext, NullConfigResolver
from jones_daemon.context import ProviderResolver as ProviderResolverProtocol
from jones_daemon.projects.bootstrap import bootstrap_projects_and_agents
from jones_daemon.rpc.server import RpcServer
from jones_daemon.sessions import methods as sessions_methods
from jones_daemon.sessions.service import DEFAULT_AGENT_ID, DEFAULT_PROJECT_ID
from jones_daemon.store import apply_pending, connect, run_in_db_thread


class _StubProviderResolver(ProviderResolverProtocol):
    def resolve(self, model_pref: dict[str, Any] | None) -> Any:
        return {"provider": "anthropic", "model": "claude-test", "env": {}, "hermes_config": {}}

    def list_models(self, provider: str | None) -> list[dict[str, Any]]:
        return []


@pytest.fixture
async def server_and_service(tmp_path, monkeypatch):
    monkeypatch.setenv("JONES_HOME", str(tmp_path))
    from jones_daemon import paths

    def _open() -> Any:
        conn = connect(paths.db_path())
        apply_pending(conn)
        bootstrap_projects_and_agents(conn)
        return conn

    conn = await run_in_db_thread(_open)

    # AF_UNIX path length limit (see tests/test_rpc.py) — short dir, not tmp_path.
    short_dir = Path(tempfile.mkdtemp(prefix="jn-"))
    srv = RpcServer(short_dir / "t.sock")
    ctx = DaemonContext(
        db=conn,
        paths=paths,
        server=srv,
        providers=_StubProviderResolver(),
        config=NullConfigResolver(),
    )
    service = sessions_methods.register(srv, ctx)
    await srv.start()
    yield srv, service
    await srv.stop()
    await run_in_db_thread(conn.close)
    shutil.rmtree(short_dir, ignore_errors=True)


async def _call(sock_path, method, params) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    try:
        envelope = {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}
        writer.write((json.dumps(envelope) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=2)
        return json.loads(line)
    finally:
        writer.close()


async def test_export_without_delete_after_does_not_call_delete_guard(server_and_service):
    server, service = server_and_service
    created = await _call(
        server.socket_path,
        "session.create",
        {
            "project_id": DEFAULT_PROJECT_ID,
            "agent_id": DEFAULT_AGENT_ID,
            "mode": "task",
            "title": "s",
        },
    )
    session_id = created["result"]["id"]

    calls = []
    real_delete_guard = service.delete_guard

    async def _spy(session_id_, fn, *args, **kwargs):
        calls.append(session_id_)
        return await real_delete_guard(session_id_, fn, *args, **kwargs)

    service.delete_guard = _spy

    response = await _call(server.socket_path, "session.export", {"id": session_id})

    assert "error" not in response
    assert calls == []  # a plain export never needs the guard


async def test_export_with_delete_after_routes_through_delete_guard(server_and_service):
    # The actual regression: `delete_after: true` must go through the same
    # guard `session.delete` uses, not call `maintenance.export_session`
    # directly.
    server, service = server_and_service
    created = await _call(
        server.socket_path,
        "session.create",
        {
            "project_id": DEFAULT_PROJECT_ID,
            "agent_id": DEFAULT_AGENT_ID,
            "mode": "task",
            "title": "s",
        },
    )
    session_id = created["result"]["id"]

    calls = []
    real_delete_guard = service.delete_guard

    async def _spy(session_id_, fn, *args, **kwargs):
        calls.append(session_id_)
        return await real_delete_guard(session_id_, fn, *args, **kwargs)

    service.delete_guard = _spy

    response = await _call(
        server.socket_path, "session.export", {"id": session_id, "delete_after": True}
    )

    assert "error" not in response, response
    assert response["result"]["path"]
    assert calls == [session_id]
