"""`store/maintenance.py` — real deletion (G20), backup/log/cache housekeeping, and
the Key-redaction scan (Issue #23, 04-w5-interfaces.md §5)."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest

from jones_daemon import paths
from jones_daemon.replay import store as replay_store
from jones_daemon.rpc.errors import RpcError
from jones_daemon.sessions import queries
from jones_daemon.store import apply_pending, connect, maintenance, run_in_db_thread


@pytest.fixture
def conn(tmp_path, monkeypatch) -> sqlite3.Connection:
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))
    c = connect(paths.db_path())
    apply_pending(c)
    yield c
    c.close()


@pytest.fixture
async def scan_conn(conn):
    """A second connection to the same `jones.db`, opened ON the dedicated DB-
    thread executor (`store.run_in_db_thread`) — unlike the `conn` fixture
    above (opened on the pytest test thread for the many synchronous tests in
    this file that call `maintenance.*` directly). `startup_key_redaction_self_
    check`'s own DB scan goes through `run_in_db_thread` internally (it must,
    in production, since the daemon's real shared connection is born on that
    same executor thread — `store/db.py`'s `check_same_thread=True`), so a
    connection born on a different thread would trip that same guard here. The
    two connections see the same committed rows (WAL, same file on disk)."""
    c = await run_in_db_thread(connect, paths.db_path())
    yield c
    await run_in_db_thread(c.close)


def _seed_session(conn, *, session_id="s1", is_main=False, parent_id=None) -> None:
    queries.create_session(
        conn,
        session_id=session_id,
        project_id="proj_default",
        agent_id="agent_default",
        parent_id=parent_id,
        is_main=is_main,
        mode="task",
        title="t",
    )


def _seed_full_run(conn, *, session_id: str, run_id: str, status="completed") -> str:
    """Session must already exist. Returns the step_id."""
    turn_id = f"{run_id}-turn"
    queries.create_turn_and_user_message(
        conn, turn_id=turn_id, message_id=f"{run_id}-msg", session_id=session_id, text="hi",
        queued=False,
    )
    queries.create_run(conn, run_id=run_id, turn_id=turn_id, session_id=session_id)
    if status != "running":
        conn.execute(
            "UPDATE runs SET status = ?, ended_at = 'now' WHERE id = ?", (status, run_id)
        )
        conn.commit()
    step = queries.insert_step(
        conn, step_id=f"{run_id}-step1", run_id=run_id, seq=1, tool="read_file", args={},
        status="completed",
    )
    queries.insert_permission_decision(
        conn, decision_id=f"{run_id}-pd1", step_id=step["id"], gate="review", risk="low",
        request={"tool": "read_file"},
    )
    return step["id"]


# --- delete_session ------------------------------------------------------------


def test_delete_session_cascades_every_row_and_purges_payload(conn, tmp_path):
    _seed_session(conn, session_id="s1")
    _seed_full_run(conn, session_id="s1", run_id="r1")
    queries.create_turn_and_user_message(
        conn, turn_id="q-turn", message_id="q-msg", session_id="s1", text="queued",
        queued=True,
    )
    queries.enqueue(conn, session_id="s1", turn_id="q-turn", text="queued", attachments=None)

    replay_store.write_payload(paths.user_root(), "r1", 1, b"payload bytes", "json")
    payload_dir = paths.user_root() / "runs" / "r1"
    assert payload_dir.exists()

    maintenance.delete_session(conn, paths.user_root(), "s1")

    assert conn.execute("SELECT 1 FROM sessions WHERE id='s1'").fetchone() is None
    assert conn.execute("SELECT 1 FROM turns WHERE session_id='s1'").fetchone() is None
    assert conn.execute("SELECT 1 FROM messages WHERE session_id='s1'").fetchone() is None
    assert conn.execute("SELECT 1 FROM runs WHERE session_id='s1'").fetchone() is None
    assert conn.execute("SELECT 1 FROM steps WHERE id='r1-step1'").fetchone() is None
    assert conn.execute("SELECT 1 FROM permission_decisions WHERE id='r1-pd1'").fetchone() is None
    assert conn.execute("SELECT 1 FROM queue_items WHERE session_id='s1'").fetchone() is None
    assert not payload_dir.exists()


def test_delete_session_refuses_the_main_session(conn):
    _seed_session(conn, session_id="main1", is_main=True)
    with pytest.raises(RpcError, match="main session"):
        maintenance.delete_session(conn, paths.user_root(), "main1")
    assert conn.execute("SELECT 1 FROM sessions WHERE id='main1'").fetchone() is not None


def test_delete_session_refuses_when_a_child_session_still_references_it(conn):
    _seed_session(conn, session_id="parent1")
    _seed_session(conn, session_id="child1", parent_id="parent1")
    with pytest.raises(RpcError, match="child session"):
        maintenance.delete_session(conn, paths.user_root(), "parent1")
    assert conn.execute("SELECT 1 FROM sessions WHERE id='parent1'").fetchone() is not None


def test_delete_session_refuses_when_a_run_is_still_running(conn):
    _seed_session(conn, session_id="s1")
    _seed_full_run(conn, session_id="s1", run_id="r1", status="running")
    with pytest.raises(RpcError, match="running"):
        maintenance.delete_session(conn, paths.user_root(), "s1")
    assert conn.execute("SELECT 1 FROM sessions WHERE id='s1'").fetchone() is not None


def test_delete_session_raises_not_found_for_an_unknown_id(conn):
    with pytest.raises(RpcError):
        maintenance.delete_session(conn, paths.user_root(), "does-not-exist")


def test_delete_session_purges_the_worker_home_directory(conn):
    # Round-2 review: `workers/manager.py::_hermes_home_for` materializes real
    # per-session state (config.yaml, the jones_gate plugin copy,
    # jones_tools.json — MCP credentials among them) under
    # `paths.worker_home_dir(user_root, session_id)`; nothing purged it before.
    _seed_session(conn, session_id="s1")
    worker_home = paths.worker_home_dir(paths.user_root(), "s1")
    (worker_home / "hermes").mkdir(parents=True)
    (worker_home / "hermes" / "config.yaml").write_text("mcp_servers: {}")

    maintenance.delete_session(conn, paths.user_root(), "s1")

    assert not worker_home.exists()


def test_delete_session_raises_partial_delete_error_when_worker_home_purge_fails(
    conn, monkeypatch
):
    _seed_session(conn, session_id="s1")
    worker_home = paths.worker_home_dir(paths.user_root(), "s1")
    worker_home.mkdir(parents=True)

    def _boom(path):
        raise OSError("simulated purge failure")

    monkeypatch.setattr(maintenance.shutil, "rmtree", _boom)

    with pytest.raises(maintenance.PartialDeleteError) as exc_info:
        maintenance.delete_session(conn, paths.user_root(), "s1")

    assert exc_info.value.detail["stage"] == "purge_worker_home"
    assert exc_info.value.detail["fully_deleted"] is False
    assert conn.execute("SELECT 1 FROM sessions WHERE id='s1'").fetchone() is None


def test_delete_session_does_not_touch_an_unrelated_session(conn):
    _seed_session(conn, session_id="s1")
    _seed_session(conn, session_id="s2")
    _seed_full_run(conn, session_id="s2", run_id="r2")

    maintenance.delete_session(conn, paths.user_root(), "s1")

    assert conn.execute("SELECT 1 FROM sessions WHERE id='s2'").fetchone() is not None
    assert conn.execute("SELECT 1 FROM runs WHERE id='r2'").fetchone() is not None


def test_delete_session_raises_partial_delete_error_when_a_run_purge_fails_partway(
    conn, monkeypatch
):
    # 04-w5-interfaces.md §6 "诚实失败": the SQLite rows are already committed by
    # the time `replay_store.purge_run` runs — a failure here must not surface as
    # a bare exception. Two runs; the first purges fine, the second raises.
    _seed_session(conn, session_id="s1")
    _seed_full_run(conn, session_id="s1", run_id="r1")
    _seed_full_run(conn, session_id="s1", run_id="r2")

    real_purge = replay_store.purge_run

    def _flaky_purge(user_root, run_id):
        if run_id == "r2":
            raise OSError("simulated purge failure")
        real_purge(user_root, run_id)

    monkeypatch.setattr(maintenance.replay_store, "purge_run", _flaky_purge)

    with pytest.raises(maintenance.PartialDeleteError) as exc_info:
        maintenance.delete_session(conn, paths.user_root(), "s1")

    detail = exc_info.value.detail
    assert detail["fully_deleted"] is False
    assert detail["purged_run_ids"] == ["r1"]
    assert detail["failed_run_id"] == "r2"
    # The SQLite cascade is not rolled back by this — it already committed;
    # "partial" here means "rows gone, payload partly not" not "nothing happened".
    assert conn.execute("SELECT 1 FROM sessions WHERE id='s1'").fetchone() is None


def test_delete_session_partial_delete_error_marked_fully_deleted_on_checkpoint_busy(
    conn, monkeypatch
):
    # The opposite shape: rows + every run's payload are genuinely gone, only
    # the trailing WAL checkpoint stayed busy — `fully_deleted` must say True so
    # the RPC layer doesn't report a completed delete as a failure.
    _seed_session(conn, session_id="s1")
    _seed_full_run(conn, session_id="s1", run_id="r1")

    def _always_busy(conn, **kwargs):
        raise maintenance.CheckpointBusyError("simulated busy")

    monkeypatch.setattr(maintenance, "checkpoint_truncate_or_raise", _always_busy)

    with pytest.raises(maintenance.PartialDeleteError) as exc_info:
        maintenance.delete_session(conn, paths.user_root(), "s1")

    assert exc_info.value.detail["fully_deleted"] is True
    assert conn.execute("SELECT 1 FROM sessions WHERE id='s1'").fetchone() is None


# --- delete_run ------------------------------------------------------------------


def test_delete_run_cascades_and_purges_payload_and_nulls_the_turn_reference(conn):
    _seed_session(conn, session_id="s1")
    _seed_full_run(conn, session_id="s1", run_id="r1")
    replay_store.write_payload(paths.user_root(), "r1", 1, b"x", "json")
    payload_dir = paths.user_root() / "runs" / "r1"

    maintenance.delete_run(conn, paths.user_root(), "r1")

    assert conn.execute("SELECT 1 FROM runs WHERE id='r1'").fetchone() is None
    assert conn.execute("SELECT 1 FROM steps WHERE id='r1-step1'").fetchone() is None
    assert conn.execute("SELECT 1 FROM permission_decisions WHERE id='r1-pd1'").fetchone() is None
    assert not payload_dir.exists()
    # the Session and its Turn survive — only the Run's own trail is gone.
    assert conn.execute("SELECT 1 FROM sessions WHERE id='s1'").fetchone() is not None
    turn = conn.execute("SELECT run_id FROM turns WHERE id='r1-turn'").fetchone()
    assert turn is not None
    assert turn["run_id"] is None


def test_delete_run_refuses_a_running_run(conn):
    _seed_session(conn, session_id="s1")
    _seed_full_run(conn, session_id="s1", run_id="r1", status="running")
    with pytest.raises(RpcError, match="running"):
        maintenance.delete_run(conn, paths.user_root(), "r1")


def test_delete_run_raises_not_found(conn):
    with pytest.raises(RpcError):
        maintenance.delete_run(conn, paths.user_root(), "does-not-exist")


def test_delete_run_raises_partial_delete_error_when_purge_fails(conn, monkeypatch):
    _seed_session(conn, session_id="s1")
    _seed_full_run(conn, session_id="s1", run_id="r1")

    def _boom(user_root, run_id):
        raise OSError("simulated purge failure")

    monkeypatch.setattr(maintenance.replay_store, "purge_run", _boom)

    with pytest.raises(maintenance.PartialDeleteError) as exc_info:
        maintenance.delete_run(conn, paths.user_root(), "r1")

    assert exc_info.value.detail["fully_deleted"] is False
    assert conn.execute("SELECT 1 FROM runs WHERE id='r1'").fetchone() is None


def test_delete_run_raises_partial_delete_error_marked_fully_deleted_when_only_checkpoint_is_busy(
    conn, monkeypatch
):
    _seed_session(conn, session_id="s1")
    _seed_full_run(conn, session_id="s1", run_id="r1")

    def _always_busy(conn, **kwargs):
        raise maintenance.CheckpointBusyError("simulated busy")

    monkeypatch.setattr(maintenance, "checkpoint_truncate_or_raise", _always_busy)

    with pytest.raises(maintenance.PartialDeleteError) as exc_info:
        maintenance.delete_run(conn, paths.user_root(), "r1")

    assert exc_info.value.detail["fully_deleted"] is True


# --- delete_project ----------------------------------------------------------------


def test_delete_project_removes_row_and_attachments_and_checkpoints(conn, tmp_path):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    from jones_daemon.projects.service import ProjectService

    project = ProjectService(conn).create(str(workdir))
    attachments = paths.project_attachments_dir(project["id"])
    (attachments / "f.txt").write_text("x")

    maintenance.delete_project(conn, paths.user_root(), project["id"])

    assert conn.execute("SELECT 1 FROM projects WHERE id=?", (project["id"],)).fetchone() is None
    assert not attachments.exists()


def test_delete_project_refuses_when_sessions_reference_it(conn, tmp_path):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    from jones_daemon.projects.service import ProjectService

    project = ProjectService(conn).create(str(workdir))
    conn.execute(
        "INSERT INTO sessions(id, project_id, agent_id, is_main, mode, status, "
        "created_at, updated_at) VALUES ('s1', ?, 'agent_default', 0, 'chat', 'active', "
        "'now', 'now')",
        (project["id"],),
    )
    conn.commit()

    with pytest.raises(RpcError, match="session"):
        maintenance.delete_project(conn, paths.user_root(), project["id"])


def test_delete_project_refuses_when_a_goal_references_it(conn, tmp_path):
    # Round-1 review: `001_init.sql`'s `goals.project_id REFERENCES projects(id)`
    # was never checked here — a goal left pointing at the Project made the
    # `DELETE FROM projects` below raise a raw `sqlite3.IntegrityError` instead
    # of this explicit, structured refusal.
    workdir = tmp_path / "proj"
    workdir.mkdir()
    from jones_daemon.projects.service import ProjectService

    project = ProjectService(conn).create(str(workdir))
    conn.execute(
        "INSERT INTO goals(id, project_id, title, status, created_at, updated_at) "
        "VALUES ('g1', ?, 'goal title', 'active', 'now', 'now')",
        (project["id"],),
    )
    conn.commit()

    with pytest.raises(RpcError, match="goal"):
        maintenance.delete_project(conn, paths.user_root(), project["id"])
    row = conn.execute("SELECT 1 FROM projects WHERE id=?", (project["id"],)).fetchone()
    assert row is not None


def test_delete_project_refuses_when_a_cron_references_it(conn, tmp_path):
    # Same gap as the goals test above, for `crons.project_id NOT NULL
    # REFERENCES projects(id)` — this is the one L/w5/20-cron makes reachable
    # for real once it lands in the same wave.
    workdir = tmp_path / "proj"
    workdir.mkdir()
    from jones_daemon.projects.service import ProjectService

    project = ProjectService(conn).create(str(workdir))
    conn.execute(
        "INSERT INTO crons(id, project_id, agent_id, expr, prompt, mode, created_at, updated_at) "
        "VALUES ('c1', ?, 'agent_default', '* * * * *', 'do it', 'task', 'now', 'now')",
        (project["id"],),
    )
    conn.commit()

    with pytest.raises(RpcError, match="cron"):
        maintenance.delete_project(conn, paths.user_root(), project["id"])
    row = conn.execute("SELECT 1 FROM projects WHERE id=?", (project["id"],)).fetchone()
    assert row is not None


def test_delete_project_rolls_back_and_leaves_no_open_transaction_on_failure(conn, tmp_path):
    # Round-1 review: before this fix, a `DELETE FROM projects` that raised
    # (e.g. the raw `IntegrityError` from the two tests above, pre-fix) left
    # `conn` — the daemon's one long-lived shared connection (`__main__.py`) —
    # sitting in an implicitly-open transaction forever, the same class of bug
    # `providers/methods.py`'s "Round 2 review" comment already fixed once
    # elsewhere. `delete_session`/`delete_run` already guard their own commit
    # with `except sqlite3.Error: conn.rollback()`; this proves `delete_project`
    # now does too. Forces the failure via a `BEFORE DELETE` trigger (`sqlite3.
    # Connection` is a C type — its `commit` method can't be monkeypatched)
    # since, post-fix, every real FK a `projects` row can be referenced by is
    # refused before the `DELETE` even runs — there's no naturally-occurring
    # `IntegrityError` left to trigger this path with real data anymore.
    workdir = tmp_path / "proj"
    workdir.mkdir()
    from jones_daemon.projects.service import ProjectService

    project = ProjectService(conn).create(str(workdir))
    conn.execute(
        "CREATE TRIGGER test_boom_on_project_delete BEFORE DELETE ON projects "
        "BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        maintenance.delete_project(conn, paths.user_root(), project["id"])

    assert conn.in_transaction is False


def test_delete_project_raises_partial_delete_error_when_attachments_purge_fails(
    conn, tmp_path, monkeypatch
):
    # Round-2 review: `delete_project` never had `delete_session`/`delete_run`'s
    # own `PartialDeleteError` contract for the two steps that run after its
    # `DELETE FROM projects` has already committed (04-w5-interfaces.md §6
    # "诚实失败") — a bare `OSError` used to reach `rpc/server.py::_dispatch`'s
    # generic `except Exception` as an opaque INTERNAL_ERROR with no
    # `daemon.error` broadcast.
    workdir = tmp_path / "proj"
    workdir.mkdir()
    from jones_daemon.projects.service import ProjectService

    project = ProjectService(conn).create(str(workdir))
    attachments = paths.project_attachments_dir(project["id"])
    (attachments / "f.txt").write_text("x")

    def _boom(path):
        raise OSError("simulated purge failure")

    monkeypatch.setattr(maintenance.shutil, "rmtree", _boom)

    with pytest.raises(maintenance.PartialDeleteError) as exc_info:
        maintenance.delete_project(conn, paths.user_root(), project["id"])

    assert exc_info.value.detail["stage"] == "purge_attachments"
    assert exc_info.value.detail["fully_deleted"] is False
    assert conn.execute("SELECT 1 FROM projects WHERE id=?", (project["id"],)).fetchone() is None


def test_delete_project_partial_delete_error_marked_fully_deleted_on_checkpoint_busy(
    conn, tmp_path, monkeypatch
):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    from jones_daemon.projects.service import ProjectService

    project = ProjectService(conn).create(str(workdir))

    def _always_busy(conn, **kwargs):
        raise maintenance.CheckpointBusyError("simulated busy")

    monkeypatch.setattr(maintenance, "checkpoint_truncate_or_raise", _always_busy)

    with pytest.raises(maintenance.PartialDeleteError) as exc_info:
        maintenance.delete_project(conn, paths.user_root(), project["id"])

    assert exc_info.value.detail["fully_deleted"] is True
    assert conn.execute("SELECT 1 FROM projects WHERE id=?", (project["id"],)).fetchone() is None


# --- export_session ----------------------------------------------------------------


def test_export_session_writes_a_json_document_with_every_table(conn):
    _seed_session(conn, session_id="s1")
    _seed_full_run(conn, session_id="s1", run_id="r1")
    queries.create_turn_and_user_message(
        conn, turn_id="q-turn", message_id="q-msg", session_id="s1", text="queued",
        queued=True,
    )
    queries.enqueue(conn, session_id="s1", turn_id="q-turn", text="queued", attachments=None)

    path = maintenance.export_session(conn, paths.user_root(), "s1")

    assert path.exists()
    import json

    doc = json.loads(path.read_text())
    assert doc["session"]["id"] == "s1"
    assert len(doc["turns"]) == 2  # the send-turn + the queued turn
    assert len(doc["runs"]) == 1
    assert len(doc["steps"]) == 1
    assert len(doc["permission_decisions"]) == 1
    assert len(doc["queue_items"]) == 1
    # lands inside the Project's documented attachments directory, not a new
    # top-level "exports/" this issue's own directory audit would reject.
    assert path.parent == paths.user_root() / "projects" / "proj_default"


def test_export_session_raises_not_found(conn):
    with pytest.raises(RpcError):
        maintenance.export_session(conn, paths.user_root(), "does-not-exist")


def test_export_session_delete_after_deletes_only_once_the_file_is_written(conn):
    _seed_session(conn, session_id="s1")

    path = maintenance.export_session(conn, paths.user_root(), "s1", delete_after=True)

    assert path.exists()
    assert conn.execute("SELECT 1 FROM sessions WHERE id='s1'").fetchone() is None


def test_export_session_writes_durably_via_fsync_not_a_plain_write_text(conn, monkeypatch):
    # Round-2 review: the docstring promises "fully written" before
    # `delete_after` can ever run — `Path.write_text` alone doesn't back that
    # promise (bytes can still be in the page cache on crash). This proves the
    # actual write path goes through `os.fsync`, the same technique
    # `secrets/vault.py::Vault._write_entries` uses.
    _seed_session(conn, session_id="s1")
    fsync_calls = []
    real_fsync = maintenance.os.fsync
    monkeypatch.setattr(
        maintenance.os, "fsync", lambda fd: (fsync_calls.append(fd), real_fsync(fd))[1]
    )

    path = maintenance.export_session(conn, paths.user_root(), "s1")

    assert path.exists()
    assert path.read_text()  # real content landed
    # One fsync for the tmp file's data, one for the containing directory.
    assert len(fsync_calls) == 2


def test_export_session_delete_after_partial_failure_still_reports_the_export_path(
    conn, monkeypatch
):
    # Round-2 review: `delete_after=True`'s inner `delete_session` can itself
    # raise `PartialDeleteError` — the export file is already durably on disk
    # at that point (written before `delete_session` is ever called), so the
    # exception must carry the export path forward rather than losing it.
    _seed_session(conn, session_id="s1")

    def _always_busy(conn, **kwargs):
        raise maintenance.CheckpointBusyError("simulated busy")

    monkeypatch.setattr(maintenance, "checkpoint_truncate_or_raise", _always_busy)

    with pytest.raises(maintenance.PartialDeleteError) as exc_info:
        maintenance.export_session(conn, paths.user_root(), "s1", delete_after=True)

    assert exc_info.value.detail["fully_deleted"] is True
    export_path = Path(exc_info.value.detail["export_path"])
    assert export_path.exists()
    assert conn.execute("SELECT 1 FROM sessions WHERE id='s1'").fetchone() is None


# --- backup rotation ---------------------------------------------------------------


def test_rotate_backups_keeps_only_the_n_most_recent(tmp_path):
    db_path = tmp_path / "jones.db"
    db_path.write_text("x")
    names = []
    for i in range(8):
        name = f"jones.db.bak-{i}-{1000 + i}"
        (tmp_path / name).write_text("backup")
        names.append(name)

    removed = maintenance.rotate_backups(db_path, keep=5)

    remaining = {p.name for p in tmp_path.glob("jones.db.bak-*")}
    assert remaining == set(names[-5:])
    assert {p.name for p in removed} == set(names[:-5])


def test_rotate_backups_is_a_noop_when_within_the_limit(tmp_path):
    db_path = tmp_path / "jones.db"
    db_path.write_text("x")
    (tmp_path / "jones.db.bak-1-100").write_text("x")

    removed = maintenance.rotate_backups(db_path, keep=5)

    assert removed == []
    assert (tmp_path / "jones.db.bak-1-100").exists()


# --- log rotation --------------------------------------------------------------------


def test_rotate_logs_deletes_only_files_older_than_the_retention_window(tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    old_file = logs_dir / "old.log"
    new_file = logs_dir / "new.log"
    old_file.write_text("old")
    new_file.write_text("new")
    old_ts = time.time() - 8 * 86400
    import os

    os.utime(old_file, (old_ts, old_ts))

    removed = maintenance.rotate_logs(logs_dir, retention_days=7)

    assert removed == 1
    assert not old_file.exists()
    assert new_file.exists()


def test_rotate_logs_on_a_missing_directory_is_a_noop(tmp_path):
    assert maintenance.rotate_logs(tmp_path / "does-not-exist") == 0


def test_rotate_logs_never_deletes_a_file_an_active_handler_still_has_open(tmp_path):
    # Round-2 review: a quiet daemon that goes `retention_days` without logging
    # a single line lets `daemon.log`'s mtime cross the cutoff while
    # `logging.py::configure_logging`'s `TimedRotatingFileHandler` still holds
    # an open fd on it — this sweep must never unlink a file a handler on the
    # `jones_daemon` logger is still writing to, regardless of mtime.
    import logging

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    active_path = logs_dir / "daemon.log"
    handler = logging.FileHandler(active_path)
    logger = logging.getLogger("jones_daemon")
    logger.addHandler(handler)
    try:
        active_path.write_text("still being written")
        old_ts = time.time() - 8 * 86400
        import os

        os.utime(active_path, (old_ts, old_ts))

        removed = maintenance.rotate_logs(logs_dir, retention_days=7)

        assert removed == 0
        assert active_path.exists()
    finally:
        logger.removeHandler(handler)
        handler.close()


# --- clear_cache ---------------------------------------------------------------------


def test_clear_cache_removes_every_entry_and_leaves_the_directory_usable(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "a.txt").write_text("x")
    (cache_dir / "sub").mkdir()
    (cache_dir / "sub" / "b.txt").write_text("y")

    removed = maintenance.clear_cache(cache_dir)

    assert removed == 2  # two top-level entries: a.txt, sub/
    assert cache_dir.exists()
    assert list(cache_dir.iterdir()) == []


def test_clear_cache_on_a_missing_directory_is_a_noop(tmp_path):
    assert maintenance.clear_cache(tmp_path / "does-not-exist") == 0


# --- checkpoint_truncate_or_raise ----------------------------------------------------


def test_checkpoint_truncate_or_raise_succeeds_with_no_blocker(tmp_path):
    conn = connect(tmp_path / "jones.db")
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO t (id) VALUES (1)")
        conn.commit()
        maintenance.checkpoint_truncate_or_raise(conn)  # must not raise
    finally:
        conn.close()


def test_checkpoint_truncate_or_raise_raises_when_another_connection_blocks_it(tmp_path):
    db_path = tmp_path / "jones.db"
    conn = connect(db_path)
    reader = None
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.commit()
        reader = sqlite3.connect(str(db_path))
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM t")  # opens a read snapshot that blocks TRUNCATE

        conn.execute("INSERT INTO t (id) VALUES (1)")
        conn.commit()

        with pytest.raises(maintenance.CheckpointBusyError):
            maintenance.checkpoint_truncate_or_raise(conn, max_retries=2, max_backoff_s=0.01)
    finally:
        if reader is not None:
            reader.close()
        conn.close()


# --- scan_for_leaked_keys --------------------------------------------------------------


def test_scan_for_leaked_keys_detects_a_hit():
    hits = maintenance.scan_for_leaked_keys(
        {"anthropic": "sk-ant-abcdef1234567890"},
        ["some log line mentioning sk-ant-abcdef1234567890 in the clear"],
    )
    assert hits == ["anthropic"]


def test_scan_for_leaked_keys_is_clean_when_only_the_hint_is_present():
    # key_hint only ever exposes the last 4 chars (PRD FR04) — that alone must
    # never trip this scan (it's the intentionally-displayed part, not a leak).
    hits = maintenance.scan_for_leaked_keys(
        {"anthropic": "sk-ant-abcdef1234567890"},
        ["the configured key ends in ...7890"],
    )
    assert hits == []


def test_scan_for_leaked_keys_ignores_short_or_empty_keys():
    hits = maintenance.scan_for_leaked_keys({"x": "short", "y": ""}, ["short short short"])
    assert hits == []


def test_scan_for_leaked_keys_checks_multiple_providers_independently():
    hits = maintenance.scan_for_leaked_keys(
        {"a": "sk-aaaaaaaaaaaaaaaa", "b": "sk-bbbbbbbbbbbbbbbb"},
        ["log has sk-aaaaaaaaaaaaaaaa but not the other one"],
    )
    assert hits == ["a"]


# --- startup_key_redaction_self_check / run_redaction_self_check_loop ----------------
# Round-1 review: this wiring (as opposed to the pure `scan_for_leaked_keys`
# function above) had zero test coverage — the one layer that could have caught
# both "the response-sample half never sees anything" and "on_hit never fires".


class _FakeVault:
    def __init__(self, keys: dict[str, str]) -> None:
        self._keys = keys

    def names(self) -> list[str]:
        return list(self._keys)

    def get(self, name: str) -> str:
        return self._keys[name]

    def entries(self) -> dict[str, str]:
        return dict(self._keys)


async def test_startup_key_redaction_self_check_reads_real_logs_and_calls_on_hit(
    scan_conn, tmp_path
):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "daemon.log").write_text("leaked: sk-ant-abcdef1234567890 in the clear")

    hit_calls = []

    async def on_hit(code, message, detail):
        hit_calls.append((code, message, detail))

    hits = await maintenance.startup_key_redaction_self_check(
        vault=_FakeVault({"anthropic": "sk-ant-abcdef1234567890"}),
        logs_dir=logs_dir,
        conn=scan_conn,
        runs_dir=tmp_path / "runs",
        recent_response_samples=[],
        on_hit=on_hit,
    )

    assert hits == ["anthropic"]
    assert len(hit_calls) == 1
    code, _message, detail = hit_calls[0]
    assert code == "key_redaction_failed"
    assert detail == {"providers": ["anthropic"]}


async def test_startup_key_redaction_self_check_scans_response_samples_too(scan_conn, tmp_path):
    logs_dir = tmp_path / "logs"  # left empty — the hit must come from responses alone
    logs_dir.mkdir()
    hit_calls = []

    async def on_hit(code, message, detail):
        hit_calls.append((code, message, detail))

    hits = await maintenance.startup_key_redaction_self_check(
        vault=_FakeVault({"openai": "sk-oa-abcdef1234567890"}),
        logs_dir=logs_dir,
        conn=scan_conn,
        runs_dir=tmp_path / "runs",
        recent_response_samples=[b'{"result":"sk-oa-abcdef1234567890 leaked here"}'],
        on_hit=on_hit,
    )

    assert hits == ["openai"]
    assert len(hit_calls) == 1


async def test_startup_key_redaction_self_check_does_not_call_on_hit_when_clean(
    scan_conn, tmp_path
):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "daemon.log").write_text("nothing sensitive here")

    async def on_hit(code, message, detail):
        raise AssertionError("on_hit must not fire when there is no leak")

    hits = await maintenance.startup_key_redaction_self_check(
        vault=_FakeVault({"anthropic": "sk-ant-abcdef1234567890"}),
        logs_dir=logs_dir,
        conn=scan_conn,
        runs_dir=tmp_path / "runs",
        recent_response_samples=[],
        on_hit=on_hit,
    )
    assert hits == []


# --- round-2 review: G03's own "全文 grep" also covers the replay sinks (jones.db's ------
# --- steps.args_json/messages.content_json, and runs/<id>/ payload files) --------------


async def test_startup_key_redaction_self_check_scans_recent_step_args(conn, scan_conn, tmp_path):
    _seed_session(conn, session_id="s1")
    _seed_full_run(conn, session_id="s1", run_id="r1")
    conn.execute(
        "UPDATE steps SET args_json = ? WHERE id = 'r1-step1'",
        ('{"header": "Authorization: Bearer sk-ant-abcdef1234567890"}',),
    )
    conn.commit()
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()  # left empty — the hit must come from the DB alone

    hit_calls = []

    async def on_hit(code, message, detail):
        hit_calls.append(detail)

    hits = await maintenance.startup_key_redaction_self_check(
        vault=_FakeVault({"anthropic": "sk-ant-abcdef1234567890"}),
        logs_dir=logs_dir,
        conn=scan_conn,
        runs_dir=tmp_path / "runs",
        recent_response_samples=[],
        on_hit=on_hit,
    )

    assert hits == ["anthropic"]
    assert hit_calls == [{"providers": ["anthropic"]}]


async def test_startup_key_redaction_self_check_scans_recent_run_payload_files(scan_conn, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()  # left empty — the hit must come from the payload file alone
    runs_dir = tmp_path / "runs"
    replay_store.write_payload(
        tmp_path, "r1", 1, b"curl -H 'Authorization: Bearer sk-ant-abcdef1234567890'", "txt"
    )

    hit_calls = []

    async def on_hit(code, message, detail):
        hit_calls.append(detail)

    hits = await maintenance.startup_key_redaction_self_check(
        vault=_FakeVault({"anthropic": "sk-ant-abcdef1234567890"}),
        logs_dir=logs_dir,
        conn=scan_conn,
        runs_dir=runs_dir,
        recent_response_samples=[],
        on_hit=on_hit,
    )

    assert hits == ["anthropic"]
    assert hit_calls == [{"providers": ["anthropic"]}]


async def test_run_redaction_self_check_loop_runs_immediately_then_on_every_interval(
    scan_conn, tmp_path
):
    # Round-1 review: a one-shot call at process startup can never see a real
    # RPC response (the server hasn't accepted a client yet at that instant) —
    # this proves the loop (a) checks right away, not after the first sleep, and
    # (b) re-reads `recent_response_samples` fresh on each pass rather than a
    # snapshot frozen at loop-start, so a response that arrives *between*
    # passes gets scanned on the next one.
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    live_samples: list[bytes] = []  # empty at loop start, like a real cold boot
    passes = 0
    hit_calls = []

    async def on_hit(code, message, detail):
        hit_calls.append(detail)

    def _samples():
        nonlocal passes
        passes += 1
        return list(live_samples)

    task = asyncio.create_task(
        maintenance.run_redaction_self_check_loop(
            vault=_FakeVault({"anthropic": "sk-ant-abcdef1234567890"}),
            logs_dir=logs_dir,
            conn=scan_conn,
            runs_dir=tmp_path / "runs",
            recent_response_samples=_samples,
            on_hit=on_hit,
            interval_s=0.01,
        )
    )
    try:
        # First pass must happen immediately, well before interval_s could have
        # elapsed on its own, and with genuinely nothing to find yet.
        for _ in range(200):
            if passes >= 1:
                break
            await asyncio.sleep(0.005)
        assert passes >= 1
        assert hit_calls == []

        # A "response" shows up only now — between passes, not at loop start.
        live_samples.append(b'{"result":"sk-ant-abcdef1234567890 leaked here"}')

        for _ in range(200):
            if hit_calls:
                break
            await asyncio.sleep(0.005)
        assert hit_calls == [{"providers": ["anthropic"]}]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_run_redaction_self_check_loop_survives_a_failing_pass(scan_conn, tmp_path):
    # A malfunctioning check (e.g. vault access blows up) must be logged and
    # skipped, not crash the loop — DEV.md 诚实失败 applies to the guard's own
    # plumbing too, but a broken self-check must not itself take the daemon down.
    class _BoomVault:
        def entries(self):
            raise RuntimeError("vault unavailable")

    passes = 0

    def _samples():
        nonlocal passes
        passes += 1
        return []

    async def _noop(*_args):
        return None

    task = asyncio.create_task(
        maintenance.run_redaction_self_check_loop(
            vault=_BoomVault(),
            logs_dir=tmp_path / "logs",
            conn=scan_conn,
            runs_dir=tmp_path / "runs",
            recent_response_samples=_samples,
            on_hit=_noop,
            interval_s=0.01,
        )
    )
    try:
        for _ in range(200):
            if passes >= 2:
                break
            await asyncio.sleep(0.005)
        assert passes >= 2  # kept looping past the first failing pass
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
