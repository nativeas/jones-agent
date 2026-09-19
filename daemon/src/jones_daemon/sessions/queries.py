"""Synchronous SQLite access for sessions/turns/messages/runs/steps/queue/permission
decisions. Every function here takes the connection explicitly and does its own
commit — callers (sessions/service.py) always invoke these through
`store.run_in_db_thread`, never directly on the event loop thread (see
store/db.py's module docstring for why that invariant matters).

No ORM (DEV.md 工程原则 #6) — plain `sqlite3.Row` in, plain `dict` out via `_d()`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

_JSON_COLUMNS = {
    "content_json": "content",
    "args_json": "args",
    "attachments_json": "attachments",
    "request_json": "request",
}


def iso_now() -> str:
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _d(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """`sqlite3.Row` -> plain JSON-serializable dict, decoding the `*_json` columns
    into native values under their un-suffixed name (RPC responses go through
    `json.dumps`, and a caller has no use for a double-encoded JSON string)."""
    if row is None:
        return None
    out: dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if key in _JSON_COLUMNS and isinstance(value, str):
            out[_JSON_COLUMNS[key]] = json.loads(value)
        else:
            out[key] = value
    return out


def _rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [d for r in cur.fetchall() if (d := _d(r)) is not None]


# -- sessions -----------------------------------------------------------------


def get_main_session(conn: sqlite3.Connection) -> dict[str, Any] | None:
    return _d(conn.execute("SELECT * FROM sessions WHERE is_main = 1").fetchone())


def create_session(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    project_id: str,
    agent_id: str,
    parent_id: str | None,
    is_main: bool,
    mode: str,
    title: str | None,
) -> dict[str, Any]:
    now = iso_now()
    conn.execute(
        "INSERT INTO sessions (id, project_id, agent_id, parent_id, is_main, mode, title, "
        "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
        (session_id, project_id, agent_id, parent_id, int(is_main), mode, title, now, now),
    )
    conn.commit()
    return get_session(conn, session_id)  # type: ignore[return-value]


def get_session(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    return _d(conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone())


def list_sessions(conn: sqlite3.Connection, *, project_id: str | None) -> list[dict[str, Any]]:
    if project_id is not None:
        cur = conn.execute(
            "SELECT * FROM sessions WHERE project_id = ? ORDER BY created_at", (project_id,)
        )
    else:
        cur = conn.execute("SELECT * FROM sessions ORDER BY created_at")
    return _rows(cur)


def set_session_mode(conn: sqlite3.Connection, session_id: str, mode: str) -> dict[str, Any] | None:
    conn.execute(
        "UPDATE sessions SET mode = ?, updated_at = ? WHERE id = ?", (mode, iso_now(), session_id)
    )
    conn.commit()
    return get_session(conn, session_id)


def latest_turn(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    return _d(
        conn.execute(
            "SELECT * FROM turns WHERE session_id = ? ORDER BY created_at DESC LIMIT 1", (session_id,)
        ).fetchone()
    )


# -- turns / messages -----------------------------------------------------------


def next_seq(conn: sqlite3.Connection, session_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM messages WHERE session_id = ?", (session_id,)
    ).fetchone()
    return int(row["n"])


def create_turn_and_user_message(
    conn: sqlite3.Connection,
    *,
    turn_id: str,
    message_id: str,
    session_id: str,
    text: str,
    queued: bool,
) -> None:
    now = iso_now()
    status = "queued" if queued else "running"
    try:
        conn.execute(
            "INSERT INTO turns (id, session_id, user_message_id, run_id, status, created_at, updated_at) "
            "VALUES (?, ?, ?, NULL, ?, ?, ?)",
            (turn_id, session_id, message_id, status, now, now),
        )
        seq = next_seq(conn, session_id)
        conn.execute(
            "INSERT INTO messages (id, session_id, turn_id, role, content_json, seq, created_at, updated_at) "
            "VALUES (?, ?, ?, 'user', ?, ?, ?, ?)",
            (message_id, session_id, turn_id, json.dumps({"kind": "text", "text": text}), seq, now, now),
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise


def insert_assistant_message(conn: sqlite3.Connection, *, message_id: str, session_id: str, turn_id: str, kind: str) -> None:
    now = iso_now()
    seq = next_seq(conn, session_id)
    conn.execute(
        "INSERT INTO messages (id, session_id, turn_id, role, content_json, seq, created_at, updated_at) "
        "VALUES (?, ?, ?, 'assistant', ?, ?, ?, ?)",
        (message_id, session_id, turn_id, json.dumps({"kind": kind, "text": ""}), seq, now, now),
    )
    conn.commit()


def finalize_message(conn: sqlite3.Connection, message_id: str, *, kind: str, text: str) -> dict[str, Any] | None:
    conn.execute(
        "UPDATE messages SET content_json = ?, updated_at = ? WHERE id = ?",
        (json.dumps({"kind": kind, "text": text}), iso_now(), message_id),
    )
    conn.commit()
    return _d(conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone())


def list_turn_messages(
    conn: sqlite3.Connection, *, session_id: str, before_seq: int | None, limit: int
) -> list[dict[str, Any]]:
    if before_seq is not None:
        cur = conn.execute(
            "SELECT * FROM messages WHERE session_id = ? AND seq < ? ORDER BY seq DESC LIMIT ?",
            (session_id, before_seq, limit),
        )
    else:
        cur = conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY seq DESC LIMIT ?", (session_id, limit)
        )
    rows = _rows(cur)
    rows.reverse()
    return rows


# -- queue ----------------------------------------------------------------------


def enqueue(conn: sqlite3.Connection, *, session_id: str, turn_id: str, text: str, attachments: list[Any] | None) -> None:
    now = iso_now()
    row = conn.execute(
        "SELECT COALESCE(MAX(position), 0) + 1 AS n FROM queue_items WHERE session_id = ?", (session_id,)
    ).fetchone()
    position = int(row["n"])
    conn.execute(
        "INSERT INTO queue_items (id, session_id, text, attachments_json, position, state, turn_id, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
        (turn_id, session_id, text, json.dumps(attachments or []), position, turn_id, now, now),
    )
    conn.commit()


def list_queue_items(conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    return _rows(
        conn.execute(
            "SELECT * FROM queue_items WHERE session_id = ? AND state = 'pending' ORDER BY position",
            (session_id,),
        )
    )


def pop_next_queue_item(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM queue_items WHERE session_id = ? AND state = 'pending' ORDER BY position LIMIT 1",
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    item = _d(row)
    conn.execute(
        "UPDATE queue_items SET state = 'sent', updated_at = ? WHERE id = ?", (iso_now(), row["id"])
    )
    conn.commit()
    return item


def remove_queue_item(conn: sqlite3.Connection, *, session_id: str, item_id: str) -> str | None:
    """Deletes the queue row and returns its `turn_id` (so the caller can mark that
    Turn 'cancelled') — None if no such pending item existed."""
    row = conn.execute(
        "SELECT turn_id FROM queue_items WHERE id = ? AND session_id = ? AND state = 'pending'",
        (item_id, session_id),
    ).fetchone()
    if row is None:
        return None
    conn.execute("DELETE FROM queue_items WHERE id = ?", (item_id,))
    conn.commit()
    return row["turn_id"]


def reorder_queue_items(conn: sqlite3.Connection, *, session_id: str, item_ids: list[str]) -> None:
    try:
        for position, item_id in enumerate(item_ids, start=1):
            conn.execute(
                "UPDATE queue_items SET position = ?, updated_at = ? WHERE id = ? AND session_id = ? "
                "AND state = 'pending'",
                (position, iso_now(), item_id, session_id),
            )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise


# -- runs / steps -----------------------------------------------------------------


def create_run(conn: sqlite3.Connection, *, run_id: str, turn_id: str, session_id: str) -> None:
    now = iso_now()
    try:
        conn.execute(
            "INSERT INTO runs (id, task_id, turn_id, session_id, status, started_at, ended_at, "
            "terminated_kind, terminated_reason, prompt_snapshot_ref, created_at, updated_at) "
            "VALUES (?, NULL, ?, ?, 'running', ?, NULL, NULL, NULL, NULL, ?, ?)",
            (run_id, turn_id, session_id, now, now, now),
        )
        conn.execute(
            "UPDATE turns SET run_id = ?, status = 'running', updated_at = ? WHERE id = ?",
            (run_id, now, turn_id),
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise


def get_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    return _d(conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())


def mark_run_completed(conn: sqlite3.Connection, run_id: str, turn_id: str) -> None:
    now = iso_now()
    try:
        conn.execute(
            "UPDATE runs SET status = 'completed', ended_at = ?, updated_at = ? WHERE id = ?",
            (now, now, run_id),
        )
        conn.execute(
            "UPDATE turns SET status = 'completed', updated_at = ? WHERE id = ?", (now, turn_id)
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise


def mark_run_terminated(conn: sqlite3.Connection, run_id: str, turn_id: str, *, kind: str, reason: str) -> None:
    now = iso_now()
    try:
        conn.execute(
            "UPDATE runs SET status = 'terminated', ended_at = ?, terminated_kind = ?, "
            "terminated_reason = ?, updated_at = ? WHERE id = ?",
            (now, kind, reason, now, run_id),
        )
        conn.execute(
            "UPDATE turns SET status = 'terminated', updated_at = ? WHERE id = ?", (now, turn_id)
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise


def cancel_turn(conn: sqlite3.Connection, turn_id: str) -> None:
    conn.execute(
        "UPDATE turns SET status = 'cancelled', updated_at = ? WHERE id = ?", (iso_now(), turn_id)
    )
    conn.commit()


def interrupt_stale_runs(conn: sqlite3.Connection) -> int:
    """PRD 11.3 崩溃恢复: any Run/Turn still 'running' when the daemon starts means
    the previous process died mid-flight — mark them, never leave a UI showing
    "running" against a Run nothing is driving anymore (N07)."""
    now = iso_now()
    try:
        cur = conn.execute(
            "UPDATE runs SET status = 'terminated', ended_at = ?, terminated_kind = 'error', "
            "terminated_reason = 'daemon restarted while this Run was in progress', updated_at = ? "
            "WHERE status = 'running'",
            (now, now),
        )
        conn.execute(
            "UPDATE turns SET status = 'terminated', updated_at = ? WHERE status = 'running'", (now,)
        )
        conn.commit()
        return cur.rowcount
    except sqlite3.Error:
        conn.rollback()
        raise


def insert_step(
    conn: sqlite3.Connection, *, step_id: str, run_id: str, seq: int, tool: str, args: Any, status: str
) -> dict[str, Any] | None:
    now = iso_now()
    conn.execute(
        "INSERT INTO steps (id, run_id, seq, tool, args_json, result_summary, payload_ref, "
        "duration_ms, permission_id, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?)",
        (step_id, run_id, seq, tool, json.dumps(args if args is not None else {}), status, now, now),
    )
    conn.commit()
    return get_step(conn, step_id)


def get_step(conn: sqlite3.Connection, step_id: str) -> dict[str, Any] | None:
    return _d(conn.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone())


def update_step(
    conn: sqlite3.Connection,
    step_id: str,
    *,
    status: str | None,
    result_summary: str | None,
    duration_ms: int | None,
    permission_id: str | None = None,
) -> dict[str, Any] | None:
    fields, params = [], []
    for column, value in (
        ("status", status),
        ("result_summary", result_summary),
        ("duration_ms", duration_ms),
        ("permission_id", permission_id),
    ):
        if value is not None:
            fields.append(f"{column} = ?")
            params.append(value)
    if not fields:
        return get_step(conn, step_id)
    fields.append("updated_at = ?")
    params.append(iso_now())
    params.append(step_id)
    conn.execute(f"UPDATE steps SET {', '.join(fields)} WHERE id = ?", params)  # noqa: S608 - fixed column allowlist above, no user input in SQL text
    conn.commit()
    return get_step(conn, step_id)


def list_run_steps(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    return _rows(conn.execute("SELECT * FROM steps WHERE run_id = ? ORDER BY seq", (run_id,)))


# -- permission decisions ---------------------------------------------------------


def insert_permission_decision(
    conn: sqlite3.Connection, *, decision_id: str, step_id: str | None, gate: str, risk: str, request: dict[str, Any]
) -> None:
    now = iso_now()
    conn.execute(
        "INSERT INTO permission_decisions (id, step_id, gate, risk, decision, decided_by, "
        "request_json, decided_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'pending', NULL, ?, NULL, ?, ?)",
        (decision_id, step_id, gate, risk, json.dumps(request), now, now),
    )
    conn.commit()
    if step_id is not None:
        conn.execute(
            "UPDATE steps SET permission_id = ?, updated_at = ? WHERE id = ?", (decision_id, now, step_id)
        )
        conn.commit()


def decide_permission(conn: sqlite3.Connection, decision_id: str, *, decision: str, decided_by: str) -> dict[str, Any] | None:
    now = iso_now()
    conn.execute(
        "UPDATE permission_decisions SET decision = ?, decided_by = ?, decided_at = ?, updated_at = ? "
        "WHERE id = ?",
        (decision, decided_by, now, now, decision_id),
    )
    conn.commit()
    return _d(conn.execute("SELECT * FROM permission_decisions WHERE id = ?", (decision_id,)).fetchone())
