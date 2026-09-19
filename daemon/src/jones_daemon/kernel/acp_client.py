"""Hand-written ACP (Agent Client Protocol) client — the daemon's half of the
worker↔daemon wire (docs/design/00-foundation.md §3, §8.1-§8.3).

The daemon is the ACP *client*; each worker subprocess runs Hermes's `acp_adapter`
as the ACP *agent* (server). Transport is newline-delimited JSON-RPC 2.0 over the
worker's stdin/stdout (verified against `agent-client-protocol==0.9.0`'s
`acp/connection.py`, which frames on `readline()` — the same one-object-per-line
shape as our own `rpc/server.py`, not a coincidence, just two independent
implementations of the same idea).

01-w2-interfaces.md §2 explicitly allows hand-writing the wire instead of depending
on the `acp` PyPI package's types: "依赖 agent-client-protocol==0.9.0 的类型定义可选，
协议本身手写即可". Method/field names below (`sessionId`, `session/new`, ...) were
read directly out of that package's `acp/meta.py` and `acp/schema.py` in the
project's Hermes checkout, not guessed.

daemon -> worker (requests this client issues): `initialize`, `session/new`,
`session/prompt`, `session/cancel` (fire-and-forget notification, per
`acp/schema.py::CancelNotification` having no response type).

worker -> daemon (requests/notifications this client answers): `session/update`
(notification — routed to `on_session_update`), `session/request_permission`
(request — routed to `on_request_permission`, must return a JSON-RPC result).
Every other incoming method (`fs/read_text_file`, `terminal/*`, ...) gets an
honest "not supported" JSON-RPC error, never a silent drop or a crash — see
00-foundation.md §8.3's "待验证" note: daemon declares no fs/terminal
`clientCapabilities`, so Hermes should not call these, but if it ever does, the
worker gets a protocol-legal answer instead of daemon-side breakage.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from jones_daemon.logging import get_logger

logger = get_logger("acp_client")

PROTOCOL_VERSION = 1

# ACP wire method names (acp/meta.py AGENT_METHODS / CLIENT_METHODS in the pinned
# agent-client-protocol==0.9.0).
_METHOD_INITIALIZE = "initialize"
_METHOD_SESSION_NEW = "session/new"
_METHOD_SESSION_PROMPT = "session/prompt"
_METHOD_SESSION_CANCEL = "session/cancel"
_METHOD_SESSION_UPDATE = "session/update"
_METHOD_REQUEST_PERMISSION = "session/request_permission"

# Client methods this daemon deliberately does NOT implement (declared unsupported
# in `initialize()`'s clientCapabilities — see module docstring). Kept as a set so
# `_handle_incoming_request` can give each one the same honest, non-crashing
# "not supported" answer instead of a generic 500-style failure.
_UNSUPPORTED_CLIENT_METHODS = frozenset(
    {
        "fs/read_text_file",
        "fs/write_text_file",
        "terminal/create",
        "terminal/output",
        "terminal/release",
        "terminal/wait_for_exit",
        "terminal/kill",
    }
)

# ACP requests in flight can legitimately run for a long time (an LLM turn with
# tool calls) — only the handshake calls get a tight timeout; `prompt()` has none
# (cancellation is `cancel()`, not a client-side deadline).
_HANDSHAKE_TIMEOUT_S = 10.0


class AcpError(Exception):
    """A JSON-RPC error object the worker sent back for one of our requests."""

    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(error.get("message", "ACP error"))
        self.code = error.get("code")
        self.message = error.get("message")
        self.data = error.get("data")


class AcpProtocolError(Exception):
    """The worker sent something that isn't a well-formed JSON-RPC message, or the
    connection closed while a request was in flight."""


SessionUpdateHandler = Callable[[dict[str, Any]], Awaitable[None]]
RequestPermissionHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class AcpClient:
    """One instance per worker subprocess. `reader`/`writer` are the subprocess's
    stdout/stdin (`asyncio.subprocess.Process.stdout` / `.stdin`) — this class has
    no opinion on how the process was spawned, that's `workers/manager.py`'s job.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        on_session_update: SessionUpdateHandler,
        on_request_permission: RequestPermissionHandler,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._on_session_update = on_session_update
        self._on_request_permission = on_request_permission
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        # `session/request_permission` requests are answered off the read loop
        # (see `_handle_incoming_request`) — these are those in-flight answer
        # tasks, tracked only so `close()` can cancel/await them instead of
        # leaking them past this client's lifetime.
        self._permission_tasks: set[asyncio.Task[None]] = set()
        self._write_lock = asyncio.Lock()
        self._closed = False
        self._read_task = asyncio.create_task(self._read_loop())

    # -- lifecycle ----------------------------------------------------------

    async def close(self) -> None:
        self._closed = True
        self._read_task.cancel()
        try:
            await self._read_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - best-effort teardown
            pass
        for task in list(self._permission_tasks):
            task.cancel()
        for task in list(self._permission_tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001
                await task
        self._permission_tasks.clear()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(AcpProtocolError("connection closed"))
        self._pending.clear()

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(
                        "worker sent non-JSON line, ignoring",
                        extra={"detail": {"line": line[:200]}},
                    )
                    continue
                await self._handle_message(message)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - must not silently die, surface to pending waiters
            logger.error("ACP read loop crashed", exc_info=True)
        finally:
            # Any request still awaiting a response when the worker's stdout closes
            # (crash, or a normal exit mid-flight) must be unblocked with an honest
            # error, never left hanging forever (DEV.md 工程原则 #4: 诚实失败).
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(AcpProtocolError("worker stdout closed"))
            self._pending.clear()

    async def _handle_message(self, message: dict[str, Any]) -> None:
        if "method" in message:
            if "id" in message and message["id"] is not None:
                await self._handle_incoming_request(message)
            else:
                await self._handle_incoming_notification(message)
            return
        # Otherwise it's a response to one of our own requests.
        req_id = message.get("id")
        fut = self._pending.pop(req_id, None)
        if fut is None or fut.done():
            return
        if "error" in message:
            fut.set_exception(AcpError(message["error"]))
        else:
            fut.set_result(message.get("result"))

    async def _handle_incoming_request(self, message: dict[str, Any]) -> None:
        method = message["method"]
        params = message.get("params") or {}
        req_id = message["id"]
        if method == _METHOD_REQUEST_PERMISSION:
            # `on_request_permission` (SessionService's `_on_request_permission`)
            # can legitimately await a human for an unbounded amount of time — it
            # MUST NOT be awaited inline here. The read loop calls `_handle_message`
            # for every incoming line one at a time (see `_read_loop`); awaiting a
            # human-speed response inline would stall reading everything else the
            # worker sends for as long as the approval is pending, including the
            # eventual response to our own in-flight `prompt()` call and any
            # `session/cancel`-driven wind-down the worker tries to report. Round-1
            # review fix — see this PR report's "第 1 轮修复记录" for the deadlock
            # (`stop()`/a worker crash during a pending approval never producing
            # `run.terminated`) this closes.
            task = asyncio.create_task(self._answer_request_permission(req_id, params))
            self._permission_tasks.add(task)
            task.add_done_callback(self._permission_tasks.discard)
            return
        if method in _UNSUPPORTED_CLIENT_METHODS:
            await self._send({
                "jsonrpc": "2.0", "id": req_id,
                "error": {
                    "code": -32601,
                    "message": f"client does not implement {method} "
                    "(declared unsupported at initialize())",
                },
            })
            return
        logger.warning("unhandled ACP request from worker", extra={"detail": {"method": method}})
        await self._send({
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"unknown method: {method}"},
        })

    async def _answer_request_permission(self, req_id: Any, params: dict[str, Any]) -> None:
        try:
            result = await self._on_request_permission(params)
        except Exception as exc:  # noqa: BLE001 - must answer the worker, never leave it hanging
            logger.error(
                "on_request_permission handler raised", exc_info=True,
                extra={"detail": {"method": _METHOD_REQUEST_PERMISSION}},
            )
            try:
                await self._send({
                    "jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"},
                })
            except AcpProtocolError:
                pass  # connection already closed (e.g. the worker crashed meanwhile)
            return
        try:
            await self._send({"jsonrpc": "2.0", "id": req_id, "result": result})
        except AcpProtocolError:
            pass  # connection already closed (e.g. the worker crashed meanwhile)

    async def _handle_incoming_notification(self, message: dict[str, Any]) -> None:
        method = message["method"]
        params = message.get("params") or {}
        if method == _METHOD_SESSION_UPDATE:
            await self._on_session_update(params)
            return
        logger.debug("unhandled ACP notification from worker", extra={"detail": {"method": method}})

    # -- outgoing calls -------------------------------------------------------

    async def _send(self, obj: dict[str, Any]) -> None:
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        async with self._write_lock:
            if self._closed:
                raise AcpProtocolError("connection closed")
            self._writer.write(line.encode("utf-8"))
            await self._writer.drain()

    async def _call(
        self, method: str, params: dict[str, Any], *, timeout: float | None = None
    ) -> Any:
        req_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[req_id] = fut
        await self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        try:
            if timeout is not None:
                return await asyncio.wait_for(fut, timeout=timeout)
            return await fut
        finally:
            self._pending.pop(req_id, None)

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # -- Agent protocol surface the daemon uses (§8.3 "最常用的") --------------

    async def initialize(self) -> dict[str, Any]:
        return await self._call(
            _METHOD_INITIALIZE,
            {
                "protocolVersion": PROTOCOL_VERSION,
                # Empty/false on every optional capability: Jones's file/terminal
                # tools already run inside the worker via Hermes's own
                # tools/file_tools.py & tools/terminal_tool.py, the daemon doesn't
                # need Hermes calling back into it for those (00-foundation.md §8.3).
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {"name": "jones-daemon", "version": "0.1.0"},
            },
            timeout=_HANDSHAKE_TIMEOUT_S,
        )

    async def new_session(
        self, cwd: str, mcp_servers: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        return await self._call(
            _METHOD_SESSION_NEW,
            {"cwd": cwd, "mcpServers": mcp_servers or []},
            timeout=_HANDSHAKE_TIMEOUT_S,
        )

    async def prompt(
        self, session_id: str, text: str, *, message_id: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": text}],
        }
        if message_id is not None:
            params["messageId"] = message_id
        # No timeout: an agent turn (with tool calls / human approval waits) can
        # legitimately run for a long time. Termination is `cancel()`, not a
        # client-side deadline (PRD 9.3).
        return await self._call(_METHOD_SESSION_PROMPT, params)

    async def cancel(self, session_id: str) -> None:
        # `session/cancel` is a notification (acp/schema.py::CancelNotification has
        # no response type) — fire-and-forget, no `id`.
        await self._notify(_METHOD_SESSION_CANCEL, {"sessionId": session_id})
