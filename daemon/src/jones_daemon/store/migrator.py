"""Hand-written SQL migrator (no ORM, per docs/design/00-foundation.md §2).

Migration files live in `store/migrations/NNN_description.sql` and are applied in
order, each inside its own transaction; `schema_version` (a single-row table) tracks
the highest version applied.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from pathlib import Path

from jones_daemon.logging import get_logger

logger = get_logger("migrator")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_FILENAME_RE = re.compile(r"^(\d+)_.*\.sql$")


def current_version(conn: sqlite3.Connection) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if exists is None:
        return 0
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    return row["version"] if row is not None else 0


def _discover_migrations(migrations_dir: Path) -> list[tuple[int, Path]]:
    found = []
    for path in migrations_dir.glob("*.sql"):
        match = _FILENAME_RE.match(path.name)
        if not match:
            continue
        found.append((int(match.group(1)), path))
    return sorted(found, key=lambda pair: pair[0])


def _db_file_path(conn: sqlite3.Connection) -> Path | None:
    row = conn.execute("PRAGMA database_list").fetchone()
    # Index (not `row["file"]`): callers may pass a connection without the Row
    # factory, and integer indexing works on both a plain tuple and sqlite3.Row.
    if row is None or not row[2]:
        return None
    return Path(row[2])


def _backup_before_migrating(conn: sqlite3.Connection) -> Path | None:
    """Copy jones.db aside before schema changes, per PRD 11.3 (升级不丢数据).

    No-ops for an in-memory database or one whose file doesn't exist yet (fresh
    install migrating from v0: nothing to lose).
    """
    db_file = _db_file_path(conn)
    if db_file is None or not db_file.exists():
        return None
    conn.commit()  # flush anything pending
    # store/db.py opens the connection in WAL mode, so recently committed data can
    # live only in jones.db-wal until it's checkpointed back — copying jones.db
    # alone without this would silently produce a backup that's missing data,
    # which is worse than no backup (looks safe, doesn't restore cleanly).
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    backup_path = db_file.with_name(f"{db_file.name}.bak")
    shutil.copy2(db_file, backup_path)
    logger.info("db backed up before migration", extra={"detail": {"backup": str(backup_path)}})
    return backup_path


def apply_pending(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> int:
    """Apply every migration newer than the current schema version. Returns the new version."""
    version = current_version(conn)
    pending = [(v, p) for v, p in _discover_migrations(migrations_dir) if v > version]
    if pending:
        _backup_before_migrating(conn)
    for file_version, path in pending:
        sql = path.read_text(encoding="utf-8")
        # executescript() runs each statement in the script as its own
        # auto-committed transaction unless the script itself opens one — so a
        # migration with more than one DDL statement (e.g. a later ALTER TABLE,
        # which SQLite doesn't support with IF NOT EXISTS) could previously fail
        # halfway through and leave schema_version pointing at the *old* version
        # while some of the new DDL had already been committed: a retry would then
        # re-run already-applied statements against a half-migrated schema and
        # error out permanently. Wrapping the script in an explicit transaction
        # makes each migration atomic: on failure nothing from it is kept.
        try:
            conn.executescript(f"BEGIN;\n{sql}\nCOMMIT;")
        except sqlite3.Error:
            conn.rollback()
            raise
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (file_version,))
        conn.commit()
        version = file_version
    return version
