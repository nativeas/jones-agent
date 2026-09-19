"""`store/maintenance.py` — real deletion (G20), backup/log/cache housekeeping, and
the Key-redaction scan (Issue #23, 04-w5-interfaces.md §5)."""

from __future__ import annotations

import sqlite3
import time

import pytest

from jones_daemon import paths
from jones_daemon.replay import store as replay_store
from jones_daemon.rpc.errors import RpcError
from jones_daemon.sessions import queries
from jones_daemon.store import apply_pending, connect, maintenance


@pytest.fixture
def conn(tmp_path, monkeypatch) -> sqlite3.Connection:
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))
    c = connect(paths.db_path())
    apply_pending(c)
    yield c
    c.close()


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


def test_delete_session_does_not_touch_an_unrelated_session(conn):
    _seed_session(conn, session_id="s1")
    _seed_session(conn, session_id="s2")
    _seed_full_run(conn, session_id="s2", run_id="r2")

    maintenance.delete_session(conn, paths.user_root(), "s1")

    assert conn.execute("SELECT 1 FROM sessions WHERE id='s2'").fetchone() is not None
    assert conn.execute("SELECT 1 FROM runs WHERE id='r2'").fetchone() is not None


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
