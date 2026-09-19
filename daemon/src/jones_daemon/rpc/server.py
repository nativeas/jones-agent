"""NDJSON JSON-RPC 2.0 server over an asyncio Unix domain socket.

Transport: one JSON object per line (docs/design/00-foundation.md §4). Framing uses
`StreamReader.readuntil`, which buffers incrementally instead of concatenating whole
messages by hand (DEV.md 工程原则 #3: 性能是需求 — IPC 用 NDJSON 流式, 不做大 JSON 一次性序列化).

The method registry is a plain dict so new methods can be registered without touching
this module (design §4.1 lists the full v0 surface; this file only wires up
`daemon.ping` / `daemon.status` per the foundation issue scope).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from jones_daemon.logging import get_logger
from jones_daemon.rpc.errors import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    TOO_MANY_REQUESTS,
    RpcError,
)

logger = get_logger("rpc")

Handler = Callable[[dict[str, Any], "Connection"], Awaitable[Any]]

# asyncio's StreamReader defaults to a 64 KiB line-buffer limit, which design §4.1
# payloads (session.send attachments, turn.messages batches, run.steps payloads)
# routinely exceed. 16 MiB gives headroom over any v0 payload while still bounding
# a single connection's per-line memory.
MAX_LINE_BYTES = 16 * 1024 * 1024

# Two separate caps, deliberately not equal (round 2 review flagged them being
# the same number as a coincidence, not a design — this is the fix):
#
# - MAX_INFLIGHT_PER_CONNECTION bounds how many requests any *one* connection may
#   have awaiting a handler at once. If this equaled the global cap, a single
#   connection could alone saturate MAX_INFLIGHT_GLOBAL and starve every other
#   connection's requests without ever hitting its own per-connection limit.
#   16 is comfortably above any realistic single-client burst (the daemon's own
#   RPC client issues requests one at a time per logical call) while leaving most
#   of the global budget for other connections.
# - MAX_INFLIGHT_GLOBAL bounds total concurrent in-flight handler calls across
#   *all* connections (see `RpcServer._dispatch_semaphore`), sized well above any
#   realistic simultaneous request count for a single-user daemon.
MAX_INFLIGHT_PER_CONNECTION = 16
MAX_INFLIGHT_GLOBAL = 64

# Issue #23 (04-w5-interfaces.md §5): the periodic Key-redaction self-check
# (`store/maintenance.py::run_redaction_self_check_loop`) scans "最近 100 条 RPC
# 响应样本" alongside log files — this is that buffer's size. A plain bounded
# ring, not a persisted log: it only ever needs to answer "did a response body
# in the recent past contain a configured key", never survive a restart. Round-1
# review: this used to be read exactly once, before startup had accepted its
# first client connection — always empty in practice. It's now polled
# periodically instead, so it actually gets read while it holds real data.
RECENT_RESPONSES_MAXLEN = 100

# Round-2 review: this buffer used to hold each response's *entire* serialized
# body — `run.payload`'s own `limit` is allowed to be `None` (read to EOF), and
# `MAX_LINE_BYTES` allows a 16MB line, so 100 entries of that could pin
# hundreds of MB to 1.6GB in a desktop daemon that's supposed to be idle-cheap
# (DEV.md 工程原则 #3), just to satisfy an 8-byte substring scan. The redaction
# scan only ever needs a bounded prefix of each response to do its job — a
# truncated sample still contains any leaked key that isn't itself split across
# the truncation boundary, the same trade every other bound in this self-check
# already makes (`store/maintenance.py::MAX_SCAN_BYTES_PER_FILE`/
# `MAX_HAYSTACK_CHARS`).
RECENT_RESPONSE_SAMPLE_MAX_CHARS = 4096


def _peek_request_id(line: bytes) -> Any:
    """Best-effort extraction of `id` from a line we're rejecting without a full
    dispatch (the in-flight cap) — so the error response still correlates to the
    right pending call on the client side instead of always using `null`. Never
    raises: a line that isn't valid JSON just gets `id: null`, same as any other
    envelope we can't make sense of (see `_dispatch`'s own INVALID_REQUEST path).
    """
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj.get("id") if isinstance(obj, dict) else None


class Connection:
    """Per-client connection handle, passed to handlers so they can push notifications."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        self._lock = asyncio.Lock()
        # Requests from this connection currently dispatched (task created) but
        # not yet responded to — see MAX_INFLIGHT_PER_CONNECTION.
        self.inflight = 0
        # Session ids this connection has `session.subscribe`d to (design §4.2:
        # notifications are delivered "按 session 订阅") — a plain set, not
        # asyncio-guarded: only ever mutated from this connection's own dispatch
        # tasks, which already serialize through `_dispatch_bounded`'s per-request
        # handling of a single reader loop (see `_handle_client`).
        self.subscriptions: set[str] = set()

    async def _send(self, obj: dict[str, Any]) -> None:
        await self._send_line(json.dumps(obj, ensure_ascii=False))

    async def _send_line(self, line: str) -> None:
        """Write an already-serialized response line. Split out of `_send` (round-2
        review) so `RpcServer._dispatch`/`_respond_error` can serialize a response
        exactly once and reuse that same string both to write to the socket and
        to sample into `_recent_responses`, instead of `json.dumps`-ing the same
        object twice per response — once here, once for the sample."""
        async with self._lock:
            self._writer.write((line + "\n").encode("utf-8"))
            await self._writer.drain()

    async def notify(self, method: str, params: Any = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._send(payload)


class RpcServer:
    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self._methods: dict[str, Handler] = {}
        self._server: asyncio.base_events.Server | None = None
        # Bounds concurrent in-flight handler calls across all connections so one
        # slow session.send doesn't starve a growing number of tasks — see
        # MAX_INFLIGHT_GLOBAL for why this is a distinct, larger number than the
        # per-connection cap.
        self._dispatch_semaphore = asyncio.Semaphore(MAX_INFLIGHT_GLOBAL)
        # Every currently-connected client, so `broadcast()` (design §4.2's
        # per-session notification delivery — added here by A/#10, the one
        # rpc/server.py file every W2 branch may extend additively, see
        # 01-w2-interfaces.md §2 "加法不改法") has something to fan out to. A plain
        # set: connections add themselves in `_handle_client` and remove
        # themselves in its `finally`, both on the event loop thread.
        self._connections: set[Connection] = set()
        # Additive, Issue #23 (04-w5-interfaces.md §5): a bounded-size prefix
        # (RECENT_RESPONSE_SAMPLE_MAX_CHARS) of the last RECENT_RESPONSES_MAXLEN
        # response bodies (success or error) this server sent, fed to the
        # startup Key-redaction self-check (`store/maintenance.py::
        # startup_key_redaction_self_check`) — not the full body (round-2
        # review, see RECENT_RESPONSE_SAMPLE_MAX_CHARS's comment). A `deque`
        # with `maxlen` set drops the oldest entry itself on overflow — no
        # separate trim step, and no unbounded growth for a long-lived daemon.
        self._recent_responses: deque[str] = deque(maxlen=RECENT_RESPONSES_MAXLEN)

    def register(self, method: str, handler: Handler) -> None:
        self._methods[method] = handler

    def recent_response_samples(self) -> list[str]:
        """Snapshot of the last `RECENT_RESPONSES_MAXLEN` response bodies sent —
        see `_recent_responses`'s docstring."""
        return list(self._recent_responses)

    async def broadcast(self, session_id: str, method: str, params: Any) -> None:
        """Push a notification to every connection currently subscribed to
        `session_id` (`session.subscribe`, design §4.2). Best-effort per connection:
        one connection's write failing (e.g. it disconnected between the
        subscription check and the write) must not stop delivery to the others —
        `_handle_client`'s own reader loop is what notices a dead connection and
        removes it from `_connections`, this method just skips over the gap.
        """
        # Snapshot before iterating: `notify()` awaits (holds `conn._lock`, drains
        # the socket), during which `_handle_client`'s finally block could mutate
        # `_connections` for an unrelated client disconnecting concurrently —
        # iterating a live set across an await point is a `RuntimeError: Set
        # changed size during iteration` waiting to happen, not a hypothetical.
        targets = [c for c in self._connections if session_id in c.subscriptions]
        for conn in targets:
            try:
                await conn.notify(method, params)
            except (ConnectionError, OSError) as exc:
                logger.debug(
                    "broadcast to a subscribed connection failed, skipping",
                    extra={
                        "detail": {"session_id": session_id, "method": method, "error": str(exc)}
                    },
                )

    async def broadcast_all(self, method: str, params: Any) -> None:
        """Push a notification to EVERY currently-connected client, regardless
        of `session.subscribe` state (controller ruling R-H3, Issue #17/#19;
        design §4.2: `daemon.error`, "永不静默" — a leaked-key hit from the
        startup redaction self-check, Issue #23, is exactly this shape too).

        `broadcast()` above only reaches connections subscribed to one
        specific `session_id` — right for a per-session event like `message.
        delta`, wrong for a daemon-wide notification a client should see even
        if it hasn't subscribed to (or has a different session focused than)
        the one that triggered it (e.g. `mcp_server_down`/`capability_drift`).
        Same additive-only, best-effort-per-connection shape as `broadcast()`
        (01-w2-interfaces.md §2 "加法不改法" — this file is the one every
        W2+ branch may extend, never rewrite)."""
        targets = list(self._connections)
        for conn in targets:
            try:
                await conn.notify(method, params)
            except (ConnectionError, OSError) as exc:
                logger.debug(
                    "broadcast_all to a connection failed, skipping",
                    extra={"detail": {"method": method, "error": str(exc)}},
                )

    async def start(self) -> None:
        if self.socket_path.exists():
            self.socket_path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self.socket_path), limit=MAX_LINE_BYTES
        )
        # Socket files are created under the process umask (world-readable by
        # default on macOS), which would let any other local account connect to
        # the full RPC surface (design §4.1: provider keys, tool execution,
        # permission decisions). Lock it down to the owner explicitly.
        os.chmod(self.socket_path, 0o600)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self.socket_path.exists():
            self.socket_path.unlink()

    async def serve_forever(self) -> None:
        assert self._server is not None, "call start() first"
        async with self._server:
            await self._server.serve_forever()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        conn = Connection(writer)
        self._connections.add(conn)
        peer = writer.get_extra_info("peername") or "unix"
        logger.info("client connected", extra={"detail": {"peer": str(peer)}})
        tasks: set[asyncio.Task[None]] = set()
        try:
            while True:
                try:
                    line = await reader.readuntil(b"\n")
                except asyncio.IncompleteReadError as exc:
                    if exc.partial:
                        await self._respond_error(
                            conn, None, PARSE_ERROR, "incomplete message at EOF"
                        )
                    break
                except asyncio.LimitOverrunError as exc:
                    # readuntil() does NOT consume its buffer when it raises this —
                    # looping straight back to readuntil() re-scans the same bytes
                    # and raises again forever (a tight, never-awaited busy loop that
                    # also writes an error response every iteration). `exc.consumed`
                    # is exactly how many bytes are safe to drain with readexactly()
                    # (which has no separator-search limit) before retrying; this is
                    # the documented pattern for recovering from LimitOverrunError.
                    try:
                        await reader.readexactly(exc.consumed)
                    except asyncio.IncompleteReadError:
                        break
                    await self._respond_error(
                        conn,
                        None,
                        PARSE_ERROR,
                        f"message exceeds {MAX_LINE_BYTES} byte line limit, frame dropped",
                    )
                    continue
                if not line.strip():
                    continue
                if conn.inflight >= MAX_INFLIGHT_PER_CONNECTION:
                    # Reject immediately rather than creating a task: creating one
                    # anyway and having it self-reject on entry would still let an
                    # unbounded number of already-parsed lines pile up as Task
                    # objects between here and whenever the event loop gets around
                    # to running each of them.
                    await self._respond_error(
                        conn,
                        _peek_request_id(line),
                        TOO_MANY_REQUESTS,
                        "too many in-flight requests on this connection "
                        f"(max {MAX_INFLIGHT_PER_CONNECTION})",
                    )
                    continue
                conn.inflight += 1
                task = asyncio.create_task(self._dispatch_bounded(conn, line))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except ConnectionResetError:
            logger.debug(
                "connection reset while reading", extra={"detail": {"peer": str(peer)}}
            )
        finally:
            self._connections.discard(conn)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            logger.info("client disconnected", extra={"detail": {"peer": str(peer)}})

    async def _dispatch_bounded(self, conn: Connection, line: bytes) -> None:
        try:
            async with self._dispatch_semaphore:
                await self._dispatch(conn, line)
        finally:
            conn.inflight -= 1

    async def _dispatch(self, conn: Connection, line: bytes) -> None:
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            await self._respond_error(conn, None, PARSE_ERROR, f"invalid JSON: {exc}")
            return

        is_valid_envelope = (
            isinstance(request, dict) and request.get("jsonrpc") == "2.0" and "method" in request
        )
        if not is_valid_envelope:
            bad_id = request.get("id") if isinstance(request, dict) else None
            await self._respond_error(conn, bad_id, INVALID_REQUEST, "not a JSON-RPC 2.0 request")
            return

        req_id = request.get("id")
        method = request["method"]
        params = request.get("params") or {}
        if not isinstance(params, dict):
            await self._respond_error(conn, req_id, INVALID_PARAMS, "params must be an object")
            return

        handler = self._methods.get(method)
        if handler is None:
            await self._respond_error(conn, req_id, METHOD_NOT_FOUND, f"unknown method: {method}")
            return

        try:
            result = await handler(params, conn)
        except RpcError as exc:
            await self._respond_error(conn, req_id, exc.code, exc.message, exc.detail)
            return
        except Exception as exc:  # noqa: BLE001 - must surface as a structured RPC error, never swallow
            logger.error("handler raised", exc_info=True, extra={"detail": {"method": method}})
            await self._respond_error(conn, req_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
            return

        if req_id is not None:
            response = {"jsonrpc": "2.0", "id": req_id, "result": result}
            await self._sample_and_send(conn, response)

    async def _respond_error(
        self,
        conn: Connection,
        req_id: Any,
        code: int,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if detail is not None:
            error["data"] = detail
        response = {"jsonrpc": "2.0", "id": req_id, "error": error}
        await self._sample_and_send(conn, response)

    async def _sample_and_send(self, conn: Connection, response: dict[str, Any]) -> None:
        """Serialize `response` exactly once — reused both as the wire line and
        as the (truncated) redaction-scan sample, instead of `json.dumps`-ing
        the same object twice per response on the event loop thread (round-2
        review; see `RECENT_RESPONSE_SAMPLE_MAX_CHARS`'s comment for the size
        bound)."""
        line = json.dumps(response, ensure_ascii=False)
        self._recent_responses.append(line[:RECENT_RESPONSE_SAMPLE_MAX_CHARS])
        await conn._send_line(line)
