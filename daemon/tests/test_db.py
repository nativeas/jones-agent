import asyncio
import sqlite3
import threading

import pytest

from jones_daemon.store.db import connect, run_in_db_thread


def test_run_in_db_thread_always_uses_the_same_dedicated_thread():
    # `asyncio.to_thread()` / the default executor hands work to *whichever* pool
    # thread is free, which would violate check_same_thread=True's "one
    # connection, one thread" invariant across calls. The dedicated
    # single-worker executor must always run on the exact same thread.
    caller_thread = threading.get_ident()

    async def _collect() -> list[int]:
        return await asyncio.gather(*[run_in_db_thread(threading.get_ident) for _ in range(8)])

    idents = asyncio.run(_collect())
    assert len(set(idents)) == 1
    assert idents[0] != caller_thread


def test_connection_opened_on_the_db_thread_rejects_use_from_another_thread(tmp_path):
    # connect() itself must run on the dedicated thread too: a check_same_thread
    # connection's home thread is whichever thread called sqlite3.connect(), not
    # wherever it's later used from. This proves that invariant actually holds —
    # using the connection from this (different) thread must fail loudly.
    db_path = tmp_path / "t.db"

    async def _open() -> sqlite3.Connection:
        return await run_in_db_thread(connect, db_path)

    conn = asyncio.run(_open())
    try:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
    finally:
        asyncio.run(run_in_db_thread(conn.close))


def test_queries_against_a_db_thread_connection_work_from_that_thread(tmp_path):
    db_path = tmp_path / "t.db"

    async def _run() -> int:
        conn = await run_in_db_thread(connect, db_path)
        try:
            row = await run_in_db_thread(lambda: conn.execute("SELECT 1 AS n").fetchone())
            return row["n"]
        finally:
            await run_in_db_thread(conn.close)

    assert asyncio.run(_run()) == 1
