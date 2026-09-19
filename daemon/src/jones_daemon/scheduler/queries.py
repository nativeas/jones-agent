"""Synchronous SQLite access for `crons` / `tasks` (cron-sourced rows only) / the
system messages this feature posts back into the main session. Same convention as
`sessions/queries.py`: every function takes the connection explicitly, does its own
commit, and is always called through `store.run_in_db_thread` by `scheduler/service.py`
— never directly on the event loop thread (see `store/db.py`'s module docstring).

Deliberately self-contained (no import from `sessions/queries.py`, even its public
`iso_now`/row-shaping helpers) — 04-w5-interfaces.md §1's ownership table gives this
branch exclusive use of `scheduler/`, and every other query module in this codebase
(`projects/service.py::_row_to_project`, `agents/service.py::_row_to_agent`) already
keeps its own small row-shaping helper rather than sharing one, so duplicating a
~10-line helper here follows the existing convention, not against it.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from jones_daemon.config.ids import new_ulid, now_iso

FAILURE_THRESHOLD = 3


def _cron_row(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _task_row(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _message_row(row: sqlite3.Row) -> dict[str, Any]:
    out = {k: row[k] for k in row.keys() if k != "content_json"}
    out["content"] = json.loads(row["content_json"])
    return out


# -- crons ----------------------------------------------------------------------


def list_crons(conn: sqlite3.Connection, *, project_id: str | None = None) -> list[dict[str, Any]]:
    if project_id is not None:
        cur = conn.execute(
            "SELECT * FROM crons WHERE project_id = ? ORDER BY created_at", (project_id,)
        )
    else:
        cur = conn.execute("SELECT * FROM crons ORDER BY created_at")
    return [_cron_row(r) for r in cur.fetchall()]


def list_enabled_crons(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cur = conn.execute("SELECT * FROM crons WHERE enabled = 1 ORDER BY created_at")
    return [_cron_row(r) for r in cur.fetchall()]


def get_cron(conn: sqlite3.Connection, cron_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM crons WHERE id = ?", (cron_id,)).fetchone()
    return None if row is None else _cron_row(row)


def create_cron(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    agent_id: str,
    name: str,
    expr: str,
    prompt: str,
    mode: str,
    enabled: bool,
    next_run_at: str | None,
) -> dict[str, Any]:
    cron_id = new_ulid()
    now = now_iso()
    conn.execute(
        "INSERT INTO crons (id, project_id, agent_id, name, expr, prompt, mode, enabled, "
        "last_run_at, next_run_at, fail_count, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 0, ?, ?)",
        (
            cron_id, project_id, agent_id, name, expr, prompt, mode,
            int(enabled), next_run_at, now, now,
        ),
    )
    conn.commit()
    row = get_cron(conn, cron_id)
    assert row is not None  # noqa: S101 - just inserted above
    return row


def update_cron(
    conn: sqlite3.Connection,
    cron_id: str,
    *,
    project_id: str,
    agent_id: str,
    name: str,
    expr: str,
    prompt: str,
    mode: str,
    enabled: bool,
    next_run_at: str | None,
) -> dict[str, Any] | None:
    conn.execute(
        "UPDATE crons SET project_id = ?, agent_id = ?, name = ?, expr = ?, prompt = ?, "
        "mode = ?, enabled = ?, next_run_at = ?, fail_count = 0, updated_at = ? WHERE id = ?",
        (
            project_id, agent_id, name, expr, prompt, mode,
            int(enabled), next_run_at, now_iso(), cron_id,
        ),
    )
    conn.commit()
    return get_cron(conn, cron_id)


def delete_cron(conn: sqlite3.Connection, cron_id: str) -> None:
    conn.execute("DELETE FROM crons WHERE id = ?", (cron_id,))
    conn.commit()


def set_cron_next_run_at(conn: sqlite3.Connection, cron_id: str, next_run_at: str | None) -> None:
    conn.execute(
        "UPDATE crons SET next_run_at = ?, updated_at = ? WHERE id = ?",
        (next_run_at, now_iso(), cron_id),
    )
    conn.commit()


def mark_cron_dispatched(
    conn: sqlite3.Connection, cron_id: str, *, last_run_at: str, next_run_at: str | None
) -> None:
    """Bookkeeping written right before a trigger is acted on (dispatched or
    skipped for overlap) — see `service.py::_dispatch`'s docstring for why this
    always runs first, unconditionally."""
    conn.execute(
        "UPDATE crons SET last_run_at = ?, next_run_at = ?, updated_at = ? WHERE id = ?",
        (last_run_at, next_run_at, now_iso(), cron_id),
    )
    conn.commit()


def record_cron_success(conn: sqlite3.Connection, cron_id: str) -> None:
    conn.execute(
        "UPDATE crons SET fail_count = 0, updated_at = ? WHERE id = ?", (now_iso(), cron_id)
    )
    conn.commit()


def record_cron_failure(conn: sqlite3.Connection, cron_id: str) -> int:
    """Increment `fail_count`; auto-disable at `FAILURE_THRESHOLD` (PRD 12.3:
    "连续失败 3 次自动停用"). Returns the new `fail_count` so the caller knows
    whether it just crossed the threshold (to post the "已自动停用" notice) without
    a second read."""
    try:
        conn.execute(
            "UPDATE crons SET fail_count = fail_count + 1, updated_at = ? WHERE id = ?",
            (now_iso(), cron_id),
        )
        row = conn.execute("SELECT fail_count FROM crons WHERE id = ?", (cron_id,)).fetchone()
        fail_count = int(row["fail_count"]) if row is not None else 0
        if fail_count >= FAILURE_THRESHOLD:
            conn.execute(
                "UPDATE crons SET enabled = 0, updated_at = ? WHERE id = ?", (now_iso(), cron_id)
            )
        conn.commit()
        return fail_count
    except sqlite3.Error:
        conn.rollback()
        raise


# -- tasks (source='cron' only — this branch never reads/writes any other source) ----


def create_cron_task(
    conn: sqlite3.Connection, *, session_id: str, cron_id: str, title: str | None
) -> dict[str, Any]:
    task_id = new_ulid()
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id, session_id, goal_id, cron_id, source, status, title, "
        "created_at, updated_at) VALUES (?, ?, NULL, ?, 'cron', 'running', ?, ?, ?)",
        (task_id, session_id, cron_id, title, now, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row is not None  # noqa: S101 - just inserted above
    return _task_row(row)


# -- system messages posted back to the main session -----------------------------


def insert_system_message(
    conn: sqlite3.Connection, *, session_id: str, text: str, meta: dict[str, Any]
) -> dict[str, Any]:
    """A `role=system` message with no `turn_id` (nullable per `001_init.sql` —
    this is not part of any Turn's request/response pair, it's the scheduler
    speaking into the main session on its own). `content_json` follows the same
    `{"kind", "text"}` shape every other message in this schema uses, plus a
    `meta` object carrying whatever structured detail the caller has (cron id,
    child session id, run id, an error card) — the renderer's job, not this
    module's, to turn that into a clickable link / styled card."""
    message_id = new_ulid()
    now = now_iso()
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM messages WHERE session_id = ?", (session_id,)
    ).fetchone()
    seq = int(row["n"])
    content = json.dumps({"kind": "cron_result", "text": text, "meta": meta})
    conn.execute(
        "INSERT INTO messages (id, session_id, turn_id, role, content_json, seq, "
        "created_at, updated_at) VALUES (?, ?, NULL, 'system', ?, ?, ?, ?)",
        (message_id, session_id, content, seq, now, now),
    )
    conn.commit()
    out = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    assert out is not None  # noqa: S101 - just inserted above
    return _message_row(out)
