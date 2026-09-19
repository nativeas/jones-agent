"""RPC `daemon.clear_cache` (design §4.1, Issue #23, 04-w5-interfaces.md §5).

`register(server, ctx)` follows the module convention every branch uses
(01-w2-interfaces.md §0): a thin `params -> store.maintenance call -> result`
translation, no state of its own.
"""

from __future__ import annotations

from typing import Any

from jones_daemon.context import DaemonContext
from jones_daemon.rpc.server import Connection, RpcServer
from jones_daemon.store import maintenance, run_in_db_thread


def register(server: RpcServer, ctx: DaemonContext) -> None:
    async def daemon_clear_cache(params: dict[str, Any], conn: Connection) -> dict[str, Any]:
        # `clear_cache` only touches the filesystem (no sqlite3.Connection), but
        # still runs on the DB thread to keep it off the event loop (DEV.md 工程
        # 原则 #3: 性能是需求) — `run_in_db_thread`'s single dedicated worker is
        # available for this even though nothing here needs its thread-affinity
        # guarantee specifically.
        removed = await run_in_db_thread(maintenance.clear_cache, ctx.paths.cache_dir())
        return {"cleared": removed}

    server.register("daemon.clear_cache", daemon_clear_cache)
