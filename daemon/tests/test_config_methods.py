"""RPC `settings.get`/`settings.set` (config/methods.py, issue #8/#9).

Review round 2, finding #5 (follow-up to round 1's #5): `settings.get` with
scope='project' is a pure read but used to call `project_root(..., create=True)`
under the hood, resurrecting `<project>/.jones` for a project directory the user
had deleted. This is the concrete RPC-reachable case the round-1 fix (AgentStore
only) missed — see paths.py's `project_settings_path`/`project_permissions_path`/
`project_mcp_path` and their `create=False` callers in config/resolver.py and
config/methods.py.
"""

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from jones_daemon import paths
from jones_daemon.config.methods import register
from jones_daemon.rpc.server import RpcServer
from jones_daemon.store import apply_pending, connect, run_in_db_thread


@pytest.fixture
async def server_and_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))

    def _open() -> object:
        c = connect(paths.db_path())
        apply_pending(c)
        return c

    conn = await run_in_db_thread(_open)
    ctx = SimpleNamespace(db=conn)

    short_dir = Path(tempfile.mkdtemp(prefix="jn-"))
    srv = RpcServer(short_dir / "t.sock")
    register(srv, ctx)
    await srv.start()
    yield srv, tmp_path, conn
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


async def test_settings_get_project_scope_does_not_resurrect_deleted_project_dir(
    server_and_dir,
):
    server, tmp_path, conn = server_and_dir

    def _make_project() -> dict:
        from jones_daemon.projects.service import ProjectService

        return ProjectService(conn).create(str(tmp_path / "proj"))

    (tmp_path / "proj").mkdir()
    project = await run_in_db_thread(_make_project)
    shutil.rmtree(project["path"])
    assert not (Path(project["path"]) / ".jones").exists()

    response = await _call(
        server.socket_path, "settings.get", {"scope": "project", "project_id": project["id"]}
    )

    assert "error" not in response
    assert response["result"] == {}
    assert not (Path(project["path"]) / ".jones").exists()
