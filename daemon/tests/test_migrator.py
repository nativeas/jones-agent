import sqlite3

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
