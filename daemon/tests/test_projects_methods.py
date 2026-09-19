import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from jones_daemon import paths
from jones_daemon.projects.methods import register
from jones_daemon.rpc.server import RpcServer
from jones_daemon.store import apply_pending, connect, run_in_db_thread


@pytest.fixture
async def server_and_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))

    # Handlers run the connection through `run_in_db_thread` (see
    # projects/methods.py), and store/db.py's check_same_thread=True connection
    # must be *opened* on that same dedicated thread — see store/db.py's module
    # docstring. Mirrors __main__.py's `_open_store()`.
    def _open() -> object:
        c = connect(paths.db_path())
        apply_pending(c)
        return c

    conn = await run_in_db_thread(_open)
    ctx = SimpleNamespace(db=conn)

    # AF_UNIX path length limit (see tests/test_rpc.py) — short dir, not tmp_path.
    short_dir = Path(tempfile.mkdtemp(prefix="jn-"))
    srv = RpcServer(short_dir / "t.sock")
    register(srv, ctx)
    await srv.start()
    yield srv, tmp_path
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


async def test_project_create_then_list_roundtrip(server_and_dir):
    server, tmp_path = server_and_dir
    workdir = tmp_path / "proj"
    workdir.mkdir()

    created = await _call(server.socket_path, "project.create", {"path": str(workdir)})
    assert "error" not in created
    project_id = created["result"]["id"]

    listed = await _call(server.socket_path, "project.list", {})
    assert any(p["id"] == project_id for p in listed["result"])


async def test_project_create_without_path_is_invalid_params(server_and_dir):
    server, _ = server_and_dir
    response = await _call(server.socket_path, "project.create", {})
    assert response["error"]["code"] == -32602


async def test_project_delete_roundtrip(server_and_dir):
    server, tmp_path = server_and_dir
    workdir = tmp_path / "proj2"
    workdir.mkdir()
    created = await _call(server.socket_path, "project.create", {"path": str(workdir)})
    project_id = created["result"]["id"]

    deleted = await _call(server.socket_path, "project.delete", {"id": project_id})
    assert deleted["result"] == {"deleted": True}

    listed = await _call(server.socket_path, "project.list", {})
    assert all(p["id"] != project_id for p in listed["result"])


async def test_project_delete_partial_failure_broadcasts_daemon_error(server_and_dir, monkeypatch):
    # Round-2 review: `project.delete` used to have no `PartialDeleteError`
    # translation at all — a purge failure after the SQLite row's own commit
    # fell straight through to `rpc/server.py::_dispatch`'s generic `except
    # Exception`, an opaque INTERNAL_ERROR with no `daemon.error` broadcast.
    # `project.delete` now reuses `sessions/methods.py::_run_delete_honestly`,
    # the same translation `session.delete`/`run.delete` already had.
    server, tmp_path = server_and_dir
    workdir = tmp_path / "proj3"
    workdir.mkdir()
    created = await _call(server.socket_path, "project.create", {"path": str(workdir)})
    project_id = created["result"]["id"]
    # `delete_project` only touches the attachments directory if one actually
    # exists — give it something to (fail to) purge.
    (paths.project_attachments_dir(project_id) / "f.txt").write_text("x")

    from jones_daemon.store import maintenance

    # Patches `maintenance.delete_project`'s own call only — `shutil.rmtree` is
    # the same module-global function every other `shutil.rmtree` call site in
    # the process (including this fixture's own teardown) would otherwise also
    # hit, so this targets `maintenance.shutil.rmtree` specifically rather than
    # the `shutil` module directly, and restores itself once the delete call
    # returns.
    real_rmtree = maintenance.shutil.rmtree
    calls = 0

    def _boom(path, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated purge failure")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(maintenance.shutil, "rmtree", _boom)

    reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
    try:
        envelope = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "project.delete",
            "params": {"id": project_id},
        }
        writer.write((json.dumps(envelope) + "\n").encode())
        await writer.drain()

        first = json.loads(await asyncio.wait_for(reader.readuntil(b"\n"), timeout=2))
        second = json.loads(await asyncio.wait_for(reader.readuntil(b"\n"), timeout=2))
    finally:
        writer.close()

    from jones_daemon.rpc.errors import INVALID_STATE

    notification = first if "method" in first else second
    response = second if "method" in first else first
    assert notification["method"] == "daemon.error"
    assert notification["params"]["code"] == "delete_partially_failed"
    assert response["id"] == "1"
    assert response["error"]["code"] == INVALID_STATE
