"""SQLite connection helper.

One connection is opened and reused for the daemon's lifetime (DEV.md 工程原则 #3:
连接复用). WAL journal mode lets readers proceed while a writer holds the log, and
`synchronous=NORMAL` is safe under WAL (only risks losing the last commit on an OS
crash, not corruption) while avoiding an fsync on every transaction.

Every synchronous SQLite call — `connect()` included — must run on the single
dedicated worker thread below (`run_in_db_thread`), never directly on the asyncio
event loop thread. Two reasons, not one:
  1. `check_same_thread=True` (kept, not disabled) means a `sqlite3.Connection` may
     only be used from the thread that created it; calling `connect()` itself on
     the loop thread would make *that* the connection's home thread and defeat
     the whole point.
  2. Even a "fast" SQLite call is still blocking C code — running it inline on the
     loop thread stalls every other coroutine (all RPC connections) for its
     duration (DEV.md 工程原则 #3: 性能是需求).
`run_in_db_thread` uses a dedicated single-worker `ThreadPoolExecutor`, not the
bare default executor `asyncio.to_thread()` reaches for — the default pool has
multiple worker threads, so two calls could land on different ones and violate
`check_same_thread`'s "one connection, one thread" invariant. A single worker is
what actually makes that hold across every call, not just most of them.
"""

from __future__ import annotations

import asyncio
import functools
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_DB_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jones-db")


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


async def run_in_db_thread[T](fn: Callable[..., T], *args: object, **kwargs: object) -> T:
    """Run a synchronous DB callable on the dedicated single-worker DB thread."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_DB_EXECUTOR, functools.partial(fn, *args, **kwargs))
