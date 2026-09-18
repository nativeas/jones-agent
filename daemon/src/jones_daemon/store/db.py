"""SQLite connection helper.

One connection is opened and reused for the daemon's lifetime (DEV.md 工程原则 #3:
连接复用). WAL journal mode lets readers proceed while a writer holds the log, and
`synchronous=NORMAL` is safe under WAL (only risks losing the last commit on an OS
crash, not corruption) while avoiding an fsync on every transaction.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
