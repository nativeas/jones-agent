"""Built-in RPC methods: `daemon.ping`, `daemon.status` (design §4.1).

Session / worker / permission methods land with the issues that own them.
"""

from __future__ import annotations

import os
import resource
import sys
import time
from typing import TYPE_CHECKING, Any

from jones_daemon import __version__
from jones_daemon.rpc.server import Connection, RpcServer

if TYPE_CHECKING:
    from jones_daemon.sessions.service import SessionService

_START_TIME = time.monotonic()


def _memory_mb() -> float:
    """Process peak resident set size in MB, via `resource.getrusage` (stdlib,
    no new dependency — psutil isn't already a project dependency, see
    daemon/pyproject.toml). `ru_maxrss` is a high-water mark, not "memory right
    now" — the closest honest number the stdlib gives us without reading
    /proc; reporting *something real* here beats fabricating a live-looking
    figure (DEV.md 工程原则 #4: 诚实失败).

    Units differ by platform: bytes on macOS/BSD, kilobytes on Linux.
    """
    ru_maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return round(ru_maxrss / (1024 * 1024), 1)
    return round(ru_maxrss / 1024, 1)


async def daemon_ping(params: dict[str, Any], conn: Connection) -> dict[str, Any]:
    return {
        "version": __version__,
        "pid": os.getpid(),
        "uptime_s": round(time.monotonic() - _START_TIME, 3),
    }


async def daemon_status(params: dict[str, Any], conn: Connection) -> dict[str, Any]:
    # Session/worker orchestration lands in a later issue; report the shape now with
    # honest zero values rather than fabricating activity. memory_mb is real (see
    # _memory_mb) — the process actually has a memory footprint today even
    # without any sessions running, so 0 there would itself be a fabrication.
    return {
        "sessions_active": 0,
        "workers": 0,
        "memory_mb": _memory_mb(),
    }


def register_builtin_methods(server: RpcServer) -> None:
    server.register("daemon.ping", daemon_ping)
    server.register("daemon.status", daemon_status)


def register_daemon_status(server: RpcServer, session_service: SessionService) -> None:
    """02-w3-interfaces.md §2 集成收口 #2: replace the honest-zero placeholder
    `daemon_status` above with real `sessions_active`/`workers` counts, once a
    `SessionService` (and the `WorkerManager` it owns) actually exist.

    A separate function, not a parameter threaded through `register_builtin_methods`,
    because `__main__.py` registers the builtins *before* constructing
    `SessionService` (every other module's `register(server, ctx)` runs after) —
    `RpcServer.register()` is a plain dict assignment (`rpc/server.py`), so calling
    this afterward with the same method name is a normal, supported overwrite, not
    a hack. Takes `session_service` directly (not `ctx`) per design's "通过 ctx 注入
    的引用，不用全局变量" — the reference itself is what matters, `ctx` doesn't
    carry a `SessionService` field and adding one isn't this file's call.
    """

    async def daemon_status_live(params: dict[str, Any], conn: Connection) -> dict[str, Any]:
        return {
            # A Session counts as "active" while it has a Turn actually running —
            # the same set `SessionService.stop()`/`_advance_queue()` check.
            "sessions_active": len(session_service.active_turn_session_ids()),
            "workers": session_service.worker_manager.worker_count(),
            "memory_mb": _memory_mb(),
        }

    server.register("daemon.status", daemon_status_live)
