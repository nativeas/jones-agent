import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from jones_daemon import paths
from jones_daemon.agents.methods import register
from jones_daemon.rpc.server import RpcServer
from jones_daemon.store import apply_pending, connect, run_in_db_thread


@pytest.fixture
async def server(tmp_path, monkeypatch):
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
    yield srv
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


async def test_agent_list_includes_the_seeded_default_agent(server):
    response = await _call(server.socket_path, "agent.list", {})
    assert "error" not in response
    assert any(a["id"] == "agent_default" for a in response["result"])


async def test_agent_upsert_then_get_roundtrip(server):
    created = await _call(server.socket_path, "agent.upsert", {"name": "Scout", "tone": "dry"})
    assert "error" not in created
    agent_id = created["result"]["id"]

    fetched = await _call(server.socket_path, "agent.get", {"id": agent_id})
    assert fetched["result"]["name"] == "Scout"
    assert fetched["result"]["tone"] == "dry"


async def test_agent_upsert_without_name_is_invalid_params(server):
    response = await _call(server.socket_path, "agent.upsert", {"persona": "no name"})
    assert response["error"]["code"] == -32602


async def test_agent_get_missing_id_is_invalid_params(server):
    response = await _call(server.socket_path, "agent.get", {})
    assert response["error"]["code"] == -32602


async def test_agent_delete_the_default_agent_is_rejected_with_invalid_state(server):
    response = await _call(server.socket_path, "agent.delete", {"id": "agent_default"})
    assert response["error"]["code"] == 1002  # invalid_state


async def test_agent_delete_roundtrip(server):
    created = await _call(server.socket_path, "agent.upsert", {"name": "Temp"})
    agent_id = created["result"]["id"]

    deleted = await _call(server.socket_path, "agent.delete", {"id": agent_id})
    assert deleted["result"] == {"deleted": True}

    missing = await _call(server.socket_path, "agent.get", {"id": agent_id})
    assert missing["error"]["code"] == 1001  # not_found
