import sqlite3

import pytest

from jones_daemon.store import migrator
from jones_daemon.store.db import connect


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row["name"] for row in rows}


def test_empty_database_reports_version_zero(tmp_path):
    conn = connect(tmp_path / "jones.db")
    try:
        assert migrator.current_version(conn) == 0
    finally:
        conn.close()


def test_apply_pending_migrates_empty_db_to_v1(tmp_path):
    conn = connect(tmp_path / "jones.db")
    try:
        version = migrator.apply_pending(conn)

        assert version == 1
        assert migrator.current_version(conn) == 1
        tables = _table_names(conn)
        for expected in [
            "projects",
            "agents",
            "sessions",
            "turns",
            "messages",
            "tasks",
            "runs",
            "steps",
            "permission_decisions",
            "queue_items",
            "goals",
            "crons",
            "providers",
            "schema_version",
        ]:
            assert expected in tables
    finally:
        conn.close()


def test_apply_pending_is_idempotent(tmp_path):
    conn = connect(tmp_path / "jones.db")
    try:
        migrator.apply_pending(conn)
        version = migrator.apply_pending(conn)
        assert version == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()["n"] == 1
    finally:
        conn.close()


def test_main_session_uniqueness_is_enforced(tmp_path):
    conn = connect(tmp_path / "jones.db")
    try:
        migrator.apply_pending(conn)
        conn.execute(
            "INSERT INTO projects(id, path, name, created_at, updated_at) "
            "VALUES ('p1', '/tmp/p1', 'p1', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO agents(id, name, created_at, updated_at) VALUES ('a1', 'a1', 'now', 'now')"
        )
        session_cols = "id, project_id, agent_id, is_main, mode, status, created_at, updated_at"
        conn.execute(
            f"INSERT INTO sessions({session_cols}) "
            "VALUES ('s1', 'p1', 'a1', 1, 'chat', 'active', 'now', 'now')"
        )
        conn.commit()

        with_error = None
        try:
            conn.execute(
                f"INSERT INTO sessions({session_cols}) "
                "VALUES ('s2', 'p1', 'a1', 1, 'chat', 'active', 'now', 'now')"
            )
        except sqlite3.IntegrityError as exc:
            with_error = exc
        assert with_error is not None
    finally:
        conn.close()


def test_apply_pending_backs_up_the_db_file_before_migrating(tmp_path):
    db_path = tmp_path / "jones.db"
    conn = connect(db_path)
    try:
        migrator.apply_pending(conn)
        backup = tmp_path / "jones.db.bak"
        assert backup.exists()
    finally:
        conn.close()


def test_apply_pending_skips_backup_when_nothing_is_pending(tmp_path):
    db_path = tmp_path / "jones.db"
    conn = connect(db_path)
    try:
        migrator.apply_pending(conn)  # v0 -> v1: db file exists by now, gets backed up
        backup = tmp_path / "jones.db.bak"
        backup.unlink()  # prove the *next* (no-op) call doesn't recreate it

        migrator.apply_pending(conn)  # already at v1: nothing pending
        assert not backup.exists()
    finally:
        conn.close()


def test_backup_captures_committed_wal_data_via_checkpoint(tmp_path):
    # store/db.py opens the connection in WAL mode: a committed row can live only
    # in jones.db-wal until checkpointed back into jones.db. A backup that just
    # copies jones.db without checkpointing first would silently miss it.
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "001_init.sql").write_text(
        "CREATE TABLE schema_version (version INTEGER NOT NULL);\n"
        "CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT);\n"
    )
    db_path = tmp_path / "jones.db"
    conn = connect(db_path)
    try:
        migrator.apply_pending(conn, migrations_dir=migrations_dir)
        conn.execute("INSERT INTO t (id, v) VALUES (1, 'hello')")
        conn.commit()

        # A second migration triggers a second backup, now with committed data
        # sitting in the WAL that a naive file copy would miss.
        (migrations_dir / "002_noop.sql").write_text("CREATE TABLE t2 (id INTEGER PRIMARY KEY);\n")
        migrator.apply_pending(conn, migrations_dir=migrations_dir)

        backup_conn = sqlite3.connect(str(tmp_path / "jones.db.bak"))
        try:
            row = backup_conn.execute("SELECT v FROM t WHERE id = 1").fetchone()
            assert row == ("hello",)
        finally:
            backup_conn.close()
    finally:
        conn.close()


def test_apply_pending_rolls_back_a_failed_migration_atomically(tmp_path):
    # A migration with more than one DDL statement (the real-world trigger: an
    # ALTER TABLE, which SQLite doesn't support with IF NOT EXISTS) can fail
    # partway through. Without an explicit transaction, the statements before the
    # failure point would already be committed — leaving a half-applied schema
    # while schema_version still says v0, so a retry re-runs them against a
    # database that no longer matches what the migration assumes.
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "001_init.sql").write_text(
        "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"
        "INSERT INTO t (id) VALUES (1);\n"
        "SELECT * FROM does_not_exist;\n"
    )
    conn = connect(tmp_path / "jones.db")
    try:
        with pytest.raises(sqlite3.Error):
            migrator.apply_pending(conn, migrations_dir=migrations_dir)

        assert migrator.current_version(conn) == 0
        # the whole script was rolled back, not just left unversioned
        assert "t" not in _table_names(conn)
    finally:
        conn.close()
