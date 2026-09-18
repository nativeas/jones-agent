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
    RpcError,
)

logger = get_logger("rpc")

Handler = Callable[[dict[str, Any], "Connection"], Awaitable[Any]]

# asyncio's StreamReader defaults to a 64 KiB line-buffer limit, which design §4.1
# payloads (session.send attachments, turn.messages batches, run.steps payloads)
# routinely exceed. 16 MiB gives headroom over any v0 payload while still bounding
# a single connection's per-line memory.
MAX_LINE_BYTES = 16 * 1024 * 1024


class Connection:
    """Per-client connection handle, passed to handlers so they can push notifications."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        self._lock = asyncio.Lock()

    async def _send(self, obj: dict[str, Any]) -> None:
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        async with self._lock:
            self._writer.write(line.encode("utf-8"))
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
        # slow session.send doesn't starve a growing number of tasks; sized well
        # above any realistic simultaneous request count for a single-user daemon.
        self._dispatch_semaphore = asyncio.Semaphore(64)

    def register(self, method: str, handler: Handler) -> None:
        self._methods[method] = handler

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
                task = asyncio.create_task(self._dispatch_bounded(conn, line))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except ConnectionResetError:
            pass
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            logger.info("client disconnected", extra={"detail": {"peer": str(peer)}})

    async def _dispatch_bounded(self, conn: Connection, line: bytes) -> None:
        async with self._dispatch_semaphore:
            await self._dispatch(conn, line)

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
            await conn._send({"jsonrpc": "2.0", "id": req_id, "result": result})

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
        await conn._send({"jsonrpc": "2.0", "id": req_id, "error": error})
