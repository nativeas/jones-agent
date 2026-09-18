import asyncio
import json
import shutil
import tempfile
from pathlib import Path

import pytest

from jones_daemon.rpc.errors import INVALID_REQUEST, METHOD_NOT_FOUND, PARSE_ERROR, RpcError
from jones_daemon.rpc.methods import register_builtin_methods
from jones_daemon.rpc.server import RpcServer


@pytest.fixture
async def server():
    # AF_UNIX paths are capped at ~104 bytes on macOS; pytest's tmp_path nests deep
    # enough (test name + scratchpad prefix) to blow that budget, so use a short-lived
    # directory directly under the system temp root instead.
    short_dir = Path(tempfile.mkdtemp(prefix="jn-"))
    srv = RpcServer(short_dir / "t.sock")
    register_builtin_methods(srv)
    await srv.start()
    yield srv
    await srv.stop()
    shutil.rmtree(short_dir, ignore_errors=True)


async def _roundtrip(sock_path, request: dict) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    try:
        writer.write((json.dumps(request) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=2)
        return json.loads(line)
    finally:
        writer.close()


async def test_daemon_ping_roundtrip(server):
    response = await _roundtrip(
        server.socket_path, {"jsonrpc": "2.0", "id": "1", "method": "daemon.ping"}
    )
    assert response["id"] == "1"
    assert "error" not in response
    result = response["result"]
    assert "version" in result
    assert "pid" in result
    assert "uptime_s" in result


async def test_daemon_status_roundtrip(server):
    response = await _roundtrip(
        server.socket_path, {"jsonrpc": "2.0", "id": "2", "method": "daemon.status"}
    )
    assert response["result"] == {"sessions_active": 0, "workers": 0, "memory_mb": 0}


async def test_unknown_method_returns_method_not_found(server):
    response = await _roundtrip(
        server.socket_path, {"jsonrpc": "2.0", "id": "3", "method": "nope.nope"}
    )
    assert response["error"]["code"] == METHOD_NOT_FOUND


async def test_malformed_json_returns_parse_error(server):
    reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
    try:
        writer.write(b"{not json\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=2)
        response = json.loads(line)
        assert response["error"]["code"] == PARSE_ERROR
    finally:
        writer.close()


async def test_missing_jsonrpc_field_is_invalid_request(server):
    response = await _roundtrip(server.socket_path, {"id": "4", "method": "daemon.ping"})
    assert response["error"]["code"] == INVALID_REQUEST


async def test_handler_error_is_surfaced_with_app_code():
    srv = RpcServer(None)

    async def boom(params, conn):
        raise RpcError(1001, "session not found", {"id": params.get("id")})

    srv.register("session.get", boom)
    # Exercise dispatch directly to avoid standing up a second socket.
    from jones_daemon.rpc.server import Connection

    sent = []

    class FakeWriter:
        def write(self, data):
            sent.append(data)

        async def drain(self):
            return None

    conn = Connection(FakeWriter())
    request = {"jsonrpc": "2.0", "id": "5", "method": "session.get", "params": {"id": "x"}}
    await srv._dispatch(conn, json.dumps(request).encode() + b"\n")
    response = json.loads(sent[0])
    assert response["error"] == {"code": 1001, "message": "session not found", "data": {"id": "x"}}


async def test_multiple_requests_on_one_connection_are_framed_independently(server):
    reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
    try:
        writer.write(b'{"jsonrpc":"2.0","id":"a","method":"daemon.ping"}\n{"jsonrpc":"2.0","id":"b","method":"daemon.ping"}\n')
        await writer.drain()
        first = json.loads(await asyncio.wait_for(reader.readuntil(b"\n"), timeout=2))
        second = json.loads(await asyncio.wait_for(reader.readuntil(b"\n"), timeout=2))
        assert {first["id"], second["id"]} == {"a", "b"}
    finally:
        writer.close()
