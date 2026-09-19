import sqlite3

import pytest

from jones_daemon.store import migrator
from jones_daemon.store.db import connect


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row["name"] for row in rows}


def _latest_migration_version() -> int:
    # Not hardcoded to a fixed number: this worktree only carries its own migration
    # (004; see docs/design/01-w2-interfaces.md §0 — 002/003 belong to sibling W2
    # branches not present here), and other worktrees carry different subsets, so
    # "how many migrations exist" isn't a stable constant across branches/merges.
    return max(v for v, _ in migrator._discover_migrations(migrator.MIGRATIONS_DIR))


def test_empty_database_reports_version_zero(tmp_path):
    conn = connect(tmp_path / "jones.db")
    try:
        assert migrator.current_version(conn) == 0
    finally:
        conn.close()


def test_apply_pending_migrates_empty_db_to_latest(tmp_path):
    conn = connect(tmp_path / "jones.db")
    try:
        version = migrator.apply_pending(conn)

        latest = _latest_migration_version()
        assert version == latest
        assert migrator.current_version(conn) == latest
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
        assert version == _latest_migration_version()
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


def _backups(tmp_path):
    return set(tmp_path.glob("jones.db.bak-*"))


def test_apply_pending_backs_up_the_db_file_before_migrating(tmp_path):
    db_path = tmp_path / "jones.db"
    conn = connect(db_path)
    try:
        migrator.apply_pending(conn)
        backups = _backups(tmp_path)
        pending = migrator._discover_migrations(migrator.MIGRATIONS_DIR)
        # One backup per pending migration, capped by `maintenance.MAX_BACKUPS`
        # rotation (#23) — the *newest* ones survive.
        from jones_daemon.store.maintenance import MAX_BACKUPS  # noqa: PLC0415

        expected = [str(v) for v, _ in pending][-MAX_BACKUPS:]
        assert len(backups) == len(expected)
        backup_versions = {b.name.split("-", 3)[1] for b in backups}
        assert backup_versions == set(expected)
    finally:
        conn.close()


def test_apply_pending_skips_backup_when_nothing_is_pending(tmp_path):
    db_path = tmp_path / "jones.db"
    conn = connect(db_path)
    try:
        migrator.apply_pending(conn)  # v0 -> latest: db file exists by now, gets backed up
        for backup in _backups(tmp_path):
            backup.unlink()  # prove the *next* (no-op) call doesn't recreate one

        migrator.apply_pending(conn)  # already at latest: nothing pending
        assert not _backups(tmp_path)
    finally:
        conn.close()


def test_apply_pending_backs_up_each_pending_migration_under_its_own_filename(tmp_path):
    # Regression test: a single fixed `jones.db.bak` filename meant each
    # migration's backup overwrote the previous one — if several versions were
    # pending in one run and a later one failed, there was no way back to the
    # state before an *earlier*, already-successful migration. Each version
    # must get its own file, and none of them get overwritten by the next.
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "001_init.sql").write_text(
        "CREATE TABLE schema_version (version INTEGER NOT NULL);\n"
        "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"
    )
    (migrations_dir / "002_noop.sql").write_text("CREATE TABLE t2 (id INTEGER PRIMARY KEY);\n")
    db_path = tmp_path / "jones.db"
    conn = connect(db_path)
    try:
        migrator.apply_pending(conn, migrations_dir=migrations_dir)
        backups = _backups(tmp_path)
        assert len(backups) == 2
        names = {b.name.split("-", 3)[1] for b in backups}  # the "<version>" segment
        assert names == {"1", "2"}
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

        v2_backup = next(b for b in _backups(tmp_path) if b.name.split("-", 3)[1] == "2")
        backup_conn = sqlite3.connect(str(v2_backup))
        try:
            row = backup_conn.execute("SELECT v FROM t WHERE id = 1").fetchone()
            assert row == ("hello",)
        finally:
            backup_conn.close()
    finally:
        conn.close()


def test_backup_refuses_to_proceed_when_wal_checkpoint_cannot_fully_flush(tmp_path):
    # A second connection holding an open read snapshot blocks
    # wal_checkpoint(TRUNCATE) from fully flushing the WAL — this is the actual
    # condition the busy/incomplete check guards against, not a mocked return
    # value. Silently backing up (and migrating) anyway would produce a backup
    # that looks fine but is missing committed data.
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "001_init.sql").write_text(
        "CREATE TABLE schema_version (version INTEGER NOT NULL);\n"
        "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"
    )
    db_path = tmp_path / "jones.db"
    conn = connect(db_path)
    reader = None
    try:
        migrator.apply_pending(conn, migrations_dir=migrations_dir)  # v0 -> v1, backs up fine

        reader = sqlite3.connect(str(db_path))
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM t")  # opens a read snapshot the checkpoint can't pass

        conn.execute("INSERT INTO t (id) VALUES (1)")
        conn.commit()
        (migrations_dir / "002_noop.sql").write_text("CREATE TABLE t2 (id INTEGER PRIMARY KEY);\n")

        backups_before = _backups(tmp_path)
        with pytest.raises(RuntimeError, match="wal_checkpoint"):
            migrator.apply_pending(conn, migrations_dir=migrations_dir)

        # No new (possibly-incomplete) backup was left behind, and the failed
        # backup attempt must have aborted the migration too, not just logged
        # a warning and continued.
        assert _backups(tmp_path) == backups_before
        assert migrator.current_version(conn) == 1
    finally:
        if reader is not None:
            reader.close()
        conn.close()


def test_apply_pending_refuses_a_database_newer_than_this_build_knows(tmp_path):
    # 05-w6-interfaces.md §3.3: an older build must not silently no-op through a
    # database schema_version its own migrations set doesn't reach — it must
    # refuse to start against it, explicitly.
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "001_init.sql").write_text(
        "CREATE TABLE schema_version (version INTEGER NOT NULL);\n"
        "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"
    )
    db_path = tmp_path / "jones.db"
    conn = connect(db_path)
    try:
        migrator.apply_pending(conn, migrations_dir=migrations_dir)  # -> v1
        # Simulate a newer build having since migrated this same database further.
        conn.execute("UPDATE schema_version SET version = 99")
        conn.commit()
        backups_before = _backups(tmp_path)

        with pytest.raises(migrator.SchemaTooNewError, match="99"):
            migrator.apply_pending(conn, migrations_dir=migrations_dir)

        # Refusal happens before touching anything: version unchanged, no
        # (possibly-incomplete) backup left behind by the refused attempt.
        assert migrator.current_version(conn) == 99
        assert _backups(tmp_path) == backups_before
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
