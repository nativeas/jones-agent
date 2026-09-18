"""Built-in RPC methods: `daemon.ping`, `daemon.status` (design §4.1).

Session / worker / permission methods land with the issues that own them.
"""

from __future__ import annotations

import os
import time
from typing import Any

from jones_daemon import __version__
from jones_daemon.rpc.server import Connection, RpcServer

_START_TIME = time.monotonic()


async def daemon_ping(params: dict[str, Any], conn: Connection) -> dict[str, Any]:
    return {
        "version": __version__,
        "pid": os.getpid(),
        "uptime_s": round(time.monotonic() - _START_TIME, 3),
    }


async def daemon_status(params: dict[str, Any], conn: Connection) -> dict[str, Any]:
    # Session/worker orchestration lands in a later issue; report the shape now with
    # honest zero values rather than fabricating activity.
    return {
        "sessions_active": 0,
        "workers": 0,
        "memory_mb": 0,
    }


def register_builtin_methods(server: RpcServer) -> None:
    server.register("daemon.ping", daemon_ping)
    server.register("daemon.status", daemon_status)
