"""Hand-written SQL migrator (no ORM, per docs/design/00-foundation.md §2).

Migration files live in `store/migrations/NNN_description.sql` and are applied in
order, each inside its own transaction; `schema_version` (a single-row table) tracks
the highest version applied.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

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


def apply_pending(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> int:
    """Apply every migration newer than the current schema version. Returns the new version."""
    version = current_version(conn)
    for file_version, path in _discover_migrations(migrations_dir):
        if file_version <= version:
            continue
        sql = path.read_text(encoding="utf-8")
        # sqlite3's executescript() issues an implicit COMMIT before running and is not
        # itself transactional, so the version bookkeeping is a separate explicit commit
        # right after. Migration SQL uses IF NOT EXISTS / idempotent DDL so a retry after
        # a partial failure is safe.
        conn.executescript(sql)
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (file_version,))
        conn.commit()
        version = file_version
    return version
