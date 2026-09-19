import asyncio
import json
import logging
import shutil
import stat
import tempfile
from pathlib import Path

import pytest

from jones_daemon.rpc.errors import (
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    TOO_MANY_REQUESTS,
    RpcError,
)
from jones_daemon.rpc.methods import register_builtin_methods
from jones_daemon.rpc.server import MAX_INFLIGHT_PER_CONNECTION, MAX_LINE_BYTES, RpcServer


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
    result = response["result"]
    assert result["sessions_active"] == 0
    assert result["workers"] == 0
    # memory_mb must be a real measurement (resource.getrusage), not a fabricated
    # constant — a running process always has *some* RSS, so it must be > 0.
    assert isinstance(result["memory_mb"], (int, float))
    assert result["memory_mb"] > 0


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


async def test_socket_is_created_owner_only(server):
    mode = stat.S_IMODE(server.socket_path.stat().st_mode)
    assert mode == 0o600


async def test_large_but_in_budget_request_round_trips(server):
    # A payload comfortably inside the raised StreamReader limit (design §4.1
    # payloads like session.send attachments or a batch of messages) must not be
    # treated as oversized just because it's well past the old 64 KiB default.
    big_text = "x" * (200 * 1024)
    request = {"jsonrpc": "2.0", "id": "big", "method": "daemon.ping", "params": {"note": big_text}}
    response = await _roundtrip(server.socket_path, request)
    assert "error" not in response
    assert response["id"] == "big"


async def test_oversized_line_is_dropped_without_a_busy_loop(server):
    # Regression test for the LimitOverrunError busy loop: readuntil() does not
    # consume its buffer when it raises, so naively looping back to readuntil()
    # re-raises on the same bytes forever. A single oversized line must produce a
    # bounded, small number of error responses (one per limit-sized chunk drained
    # while searching for the terminating newline) rather than spinning.
    reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
    try:
        oversized = b'{"jsonrpc":"2.0","id":"1","method":"daemon.ping","params":{"note":"'
        oversized += b"x" * (MAX_LINE_BYTES + 1024)
        oversized += b'"}}\n'
        writer.write(oversized)
        await writer.drain()

        responses = []
        for _ in range(5):
            try:
                line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=2)
            except (TimeoutError, asyncio.IncompleteReadError):
                break
            responses.append(json.loads(line))

        assert len(responses) <= 2
        assert all(r.get("error", {}).get("code") == PARSE_ERROR for r in responses)

        # The connection must still be alive and usable afterwards (not wedged).
        follow_up_request = {"jsonrpc": "2.0", "id": "2", "method": "daemon.ping"}
        follow_up = await _roundtrip(server.socket_path, follow_up_request)
        assert "error" not in follow_up
    finally:
        writer.close()


async def test_concurrent_requests_on_one_connection_do_not_serialize(server):
    # session.stop / permission.decide must be able to reach the daemon while a
    # slow session.send is still running on the same connection (the frontend
    # opens exactly one socket). Register a handler that blocks until released,
    # and confirm a second request completes while the first is still pending.
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(params, conn):
        started.set()
        await release.wait()
        return {"slow": True}

    server.register("test.slow", slow)

    reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
    try:
        writer.write(b'{"jsonrpc":"2.0","id":"slow","method":"test.slow"}\n')
        await writer.drain()
        await asyncio.wait_for(started.wait(), timeout=2)

        writer.write(b'{"jsonrpc":"2.0","id":"fast","method":"daemon.ping"}\n')
        await writer.drain()
        fast_response = json.loads(await asyncio.wait_for(reader.readuntil(b"\n"), timeout=2))
        assert fast_response["id"] == "fast"
        assert "error" not in fast_response

        release.set()
        slow_response = json.loads(await asyncio.wait_for(reader.readuntil(b"\n"), timeout=2))
        assert slow_response["id"] == "slow"
    finally:
        writer.close()


async def test_connection_reset_while_reading_is_logged_not_silently_dropped(caplog):
    # Regression test: `except ConnectionResetError: pass` swallowed a genuine
    # disconnect condition with zero trace — DEV.md 工程原则 #4 (诚实失败) requires
    # every `except` to at least log with context, never bare `pass`.
    srv = RpcServer(None)

    class ResetReader:
        async def readuntil(self, separator):
            raise ConnectionResetError("reset by peer")

    class FakeWriter:
        def write(self, data):
            pass

        async def drain(self):
            return None

        def close(self):
            pass

        async def wait_closed(self):
            return None

        def get_extra_info(self, name):
            return "test-peer"

    with caplog.at_level(logging.DEBUG, logger="jones_daemon.rpc"):
        await srv._handle_client(ResetReader(), FakeWriter())

    assert any(
        "connection reset" in record.message and record.levelname == "DEBUG"
        for record in caplog.records
    )


async def test_connection_inflight_cap_rejects_overflow_instead_of_queueing(server):
    # Regression test: without a per-connection cap, a connection that dispatches
    # faster than its handlers finish could pile up an unbounded number of tasks
    # behind the global semaphore. Fill this connection's entire in-flight budget
    # with requests that block until released, then confirm one more gets an
    # immediate JSON-RPC error instead of joining an ever-growing queue.
    release = asyncio.Event()
    started = 0

    async def blocking(params, conn):
        nonlocal started
        started += 1
        await release.wait()
        return {"ok": True}

    server.register("test.block", blocking)

    reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
    try:
        payload = "".join(
            json.dumps({"jsonrpc": "2.0", "id": f"b{i}", "method": "test.block"}) + "\n"
            for i in range(MAX_INFLIGHT_PER_CONNECTION)
        )
        writer.write(payload.encode())
        await writer.drain()

        for _ in range(200):
            if started >= MAX_INFLIGHT_PER_CONNECTION:
                break
            await asyncio.sleep(0.01)
        assert started == MAX_INFLIGHT_PER_CONNECTION

        overflow_req = json.dumps({"jsonrpc": "2.0", "id": "overflow", "method": "daemon.ping"})
        writer.write((overflow_req + "\n").encode())
        await writer.drain()
        overflow_response = json.loads(await asyncio.wait_for(reader.readuntil(b"\n"), timeout=2))
        assert overflow_response["id"] == "overflow"
        assert overflow_response["error"]["code"] == TOO_MANY_REQUESTS

        release.set()
        # Drain the blocked responses so they don't race writer.close() below.
        for _ in range(MAX_INFLIGHT_PER_CONNECTION):
            await asyncio.wait_for(reader.readuntil(b"\n"), timeout=2)

        # The cap is per-connection headroom, not a one-shot trip: once those
        # requests finished and freed their slots, this same connection can
        # dispatch again normally.
        writer.write(
            (json.dumps({"jsonrpc": "2.0", "id": "after", "method": "daemon.ping"}) + "\n").encode()
        )
        await writer.drain()
        after_response = json.loads(await asyncio.wait_for(reader.readuntil(b"\n"), timeout=2))
        assert after_response["id"] == "after"
        assert "error" not in after_response
    finally:
        writer.close()


# -- broadcast / per-session subscriptions (added by A/#10, design §4.2) ---------


async def test_broadcast_delivers_only_to_subscribed_connections(server):
    # A raw handler standing in for `session.subscribe` (sessions/methods.py owns
    # the real one) — this file tests the rpc/server.py mechanism A is allowed to
    # add, independent of the sessions/ package.
    async def subscribe(params, conn):
        conn.subscriptions.add(params["id"])
        return {"subscribed": True}

    server.register("test.subscribe", subscribe)

    reader_a, writer_a = await asyncio.open_unix_connection(str(server.socket_path))
    reader_b, writer_b = await asyncio.open_unix_connection(str(server.socket_path))
    try:
        # Only connection A subscribes to session "s1".
        writer_a.write(b'{"jsonrpc":"2.0","id":"1","method":"test.subscribe","params":{"id":"s1"}}\n')
        await writer_a.drain()
        await asyncio.wait_for(reader_a.readuntil(b"\n"), timeout=2)  # consume the ack

        await server.broadcast("s1", "message.delta", {"delta": "hi"})

        note = json.loads(await asyncio.wait_for(reader_a.readuntil(b"\n"), timeout=2))
        assert note == {"jsonrpc": "2.0", "method": "message.delta", "params": {"delta": "hi"}}

        # Connection B never subscribed — prove it got nothing by sending it a
        # request afterwards and confirming that's the *first* thing it reads
        # (a leaked broadcast would have arrived first and broken this parse).
        writer_b.write(b'{"jsonrpc":"2.0","id":"2","method":"daemon.ping"}\n')
        await writer_b.drain()
        response_b = json.loads(await asyncio.wait_for(reader_b.readuntil(b"\n"), timeout=2))
        assert response_b["id"] == "2"
    finally:
        writer_a.close()
        writer_b.close()


async def test_broadcast_to_no_subscribers_is_a_silent_no_op(server):
    # Must not raise just because nobody is listening for this session.
    await server.broadcast("nobody-subscribed", "message.delta", {"delta": "x"})


async def test_unsubscribe_stops_further_broadcasts(server):
    async def subscribe(params, conn):
        conn.subscriptions.add(params["id"])
        return {}

    async def unsubscribe(params, conn):
        conn.subscriptions.discard(params["id"])
        return {}

    server.register("test.subscribe", subscribe)
    server.register("test.unsubscribe", unsubscribe)

    reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
    try:
        writer.write(b'{"jsonrpc":"2.0","id":"1","method":"test.subscribe","params":{"id":"s1"}}\n')
        await writer.drain()
        await asyncio.wait_for(reader.readuntil(b"\n"), timeout=2)

        writer.write(b'{"jsonrpc":"2.0","id":"2","method":"test.unsubscribe","params":{"id":"s1"}}\n')
        await writer.drain()
        await asyncio.wait_for(reader.readuntil(b"\n"), timeout=2)

        await server.broadcast("s1", "message.delta", {"delta": "should not arrive"})

        # Confirm nothing arrived by racing a fresh request past it.
        writer.write(b'{"jsonrpc":"2.0","id":"3","method":"daemon.ping"}\n')
        await writer.drain()
        response = json.loads(await asyncio.wait_for(reader.readuntil(b"\n"), timeout=2))
        assert response["id"] == "3"
    finally:
        writer.close()


# -- broadcast_all / recent_response_samples (added by O/#23, 04-w5-interfaces.md
# §5's rpc/server.py 加法扩展, for the Key-redaction self-check) ------------------


async def test_broadcast_all_delivers_to_every_connection_regardless_of_subscription(server):
    # Unlike `broadcast()`, `broadcast_all` isn't scoped to a session — daemon.error
    # from the redaction self-check has no session to be "about", so every
    # connection must get it, subscribed or not.
    reader_a, writer_a = await asyncio.open_unix_connection(str(server.socket_path))
    reader_b, writer_b = await asyncio.open_unix_connection(str(server.socket_path))
    try:
        # A full round trip on each connection first — `_handle_client` adding a
        # connection to `self._connections` happens on a task scheduled by the
        # accept callback, not synchronously when `open_unix_connection` returns
        # client-side, so `broadcast_all` right after connecting could otherwise
        # race a connection that isn't registered yet.
        for reader, writer in ((reader_a, writer_a), (reader_b, writer_b)):
            writer.write(b'{"jsonrpc":"2.0","id":"warmup","method":"daemon.ping"}\n')
            await writer.drain()
            await asyncio.wait_for(reader.readuntil(b"\n"), timeout=2)

        await server.broadcast_all("daemon.error", {"code": "x"})

        note_a = json.loads(await asyncio.wait_for(reader_a.readuntil(b"\n"), timeout=2))
        note_b = json.loads(await asyncio.wait_for(reader_b.readuntil(b"\n"), timeout=2))
        expected = {"jsonrpc": "2.0", "method": "daemon.error", "params": {"code": "x"}}
        assert note_a == expected
        assert note_b == expected
    finally:
        writer_a.close()
        writer_b.close()


async def test_broadcast_all_to_a_connection_that_already_disconnected_does_not_raise(server):
    async def _boom_notify(method, params=None):
        raise ConnectionError("peer gone")

    from jones_daemon.rpc.server import Connection

    dead = Connection.__new__(Connection)
    dead.notify = _boom_notify
    server._connections.add(dead)
    try:
        await server.broadcast_all("daemon.error", {"code": "x"})
    finally:
        server._connections.discard(dead)


async def test_recent_response_samples_is_empty_before_any_request(server):
    # This is exactly the gap round-1 review flagged: at true process startup,
    # before any client has connected, the buffer the redaction self-check reads
    # is necessarily this — empty.
    assert server.recent_response_samples() == []


async def test_recent_response_samples_captures_successful_and_error_responses(server):
    await _roundtrip(server.socket_path, {"jsonrpc": "2.0", "id": "1", "method": "daemon.ping"})
    await _roundtrip(server.socket_path, {"jsonrpc": "2.0", "id": "2", "method": "nope.nope"})

    samples = server.recent_response_samples()
    assert len(samples) == 2
    parsed = [json.loads(s) for s in samples]
    assert parsed[0]["id"] == "1" and "result" in parsed[0]
    assert parsed[1]["id"] == "2" and "error" in parsed[1]


async def test_recent_response_samples_is_bounded(server):
    from jones_daemon.rpc.server import RECENT_RESPONSES_MAXLEN

    for i in range(RECENT_RESPONSES_MAXLEN + 5):
        await _roundtrip(
            server.socket_path, {"jsonrpc": "2.0", "id": str(i), "method": "daemon.ping"}
        )

    samples = server.recent_response_samples()
    assert len(samples) == RECENT_RESPONSES_MAXLEN
    # the oldest entries were dropped, not the newest.
    ids = [json.loads(s)["id"] for s in samples]
    assert ids[-1] == str(RECENT_RESPONSES_MAXLEN + 4)


async def test_broadcast_to_a_connection_that_already_disconnected_does_not_raise(server):
    class DeadConnLikeWriter:
        async def notify(self, method, params):
            raise ConnectionError("peer gone")

    from jones_daemon.rpc.server import Connection

    dead = Connection.__new__(Connection)
    dead.subscriptions = {"s1"}

    async def _boom_notify(method, params=None):
        raise ConnectionError("peer gone")

    dead.notify = _boom_notify
    server._connections.add(dead)
    try:
        # Must swallow the per-connection failure and not blow up the whole
        # broadcast for whoever else is subscribed.
        await server.broadcast("s1", "message.delta", {"delta": "x"})
    finally:
        server._connections.discard(dead)
