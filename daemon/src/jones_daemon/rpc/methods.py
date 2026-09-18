"""Built-in RPC methods: `daemon.ping`, `daemon.status` (design §4.1).

Session / worker / permission methods land with the issues that own them.
"""

from __future__ import annotations

import os
import resource
import sys
import time
from typing import Any

from jones_daemon import __version__
from jones_daemon.rpc.server import Connection, RpcServer

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
