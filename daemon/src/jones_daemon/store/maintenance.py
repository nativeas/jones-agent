"""Real deletion, backup/log/cache housekeeping, and the Key-redaction self-check —
Issue #23 (docs/design/04-w5-interfaces.md §5, PRD §10.4/§11.3, G03/G19/G20, N02).

Every function here does synchronous sqlite + filesystem I/O, same contract as
`projects/service.py`/`sessions/queries.py`: callers on the asyncio event loop must
offload through `store.run_in_db_thread` (`store/db.py`).

**真删 (G20)**: "SQLite 行 + payload 文件 + 向量分片一起删" (PRD 10.4). This module
owns the SQLite-row-cascade + payload-file half for Session/Run/Project; the vector
shard half is a hook only (memory/FR18 is P1, no vector store lands in this repo yet
— see `docs/spikes/03-vector-store.md`, which already specifies the real delete()
contract this module's `checkpoint_truncate_or_raise` mirrors for when that store
exists).

No table here declares `ON DELETE CASCADE` (00-foundation.md §5's schema doesn't),
and `store/db.py::connect()` runs with `PRAGMA foreign_keys=ON` — so every delete
below removes child rows before parent rows, in FK-safe order, inside one
transaction. A cascade that fails partway rolls back entirely (DEV.md 诚实失败: no
"half deleted" state) rather than leaving orphaned rows.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import shutil
import sqlite3
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from jones_daemon import paths
from jones_daemon.config.ids import now_iso
from jones_daemon.logging import get_logger
from jones_daemon.replay import store as replay_store
from jones_daemon.rpc.errors import INVALID_STATE, NOT_FOUND, RpcError

logger = get_logger("store.maintenance")


class PartialDeleteError(RuntimeError):
    """A delete's SQLite rows are already committed (irreversible at this point —
    see each function's own transaction below) but a step *after* the commit
    failed partway: purging a Run's payload directory, or the trailing WAL
    checkpoint. 04-w5-interfaces.md §6: "任何删除半途失败 → 记录部分完成状态并
    daemon.error, 不假装删完" — this type is what lets the RPC layer (which has
    the `RpcServer` needed to broadcast `daemon.error`, unlike this module) tell
    the two shapes of "半途" apart instead of both surfacing as an opaque
    `INTERNAL_ERROR`:

    - `detail["fully_deleted"] is False`: rows are gone but at least one payload
      directory was *not* purged — genuinely incomplete, the caller must not
      report this as a plain success.
    - `detail["fully_deleted"] is True`: rows + every payload directory are
      gone; only the trailing `wal_checkpoint(TRUNCATE)` stayed busy
      (`CheckpointBusyError`) — the delete itself fully succeeded, so the
      caller must *not* turn this into a failure response (that would be
      reporting a completed delete as failed, the opposite mistake), while
      still surfacing the anomaly via `daemon.error` for whoever is watching
      disk/WAL health.

    `detail` is JSON-safe (str/int/list/dict only) and meant to be broadcast
    verbatim as the `daemon.error` payload's `detail` field."""

    def __init__(self, message: str, *, detail: dict[str, Any]) -> None:
        super().__init__(message)
        self.detail = detail


class CheckpointBusyError(RuntimeError):
    """`wal_checkpoint(TRUNCATE)` stayed busy through every retry — some other
    connection's read snapshot/lock never let go in time. The caller must not treat
    the delete as fully done: the SQLite rows are gone (already committed before
    this runs) but the -wal file may still carry the old bytes on disk a beat
    longer than usual. See `docs/spikes/03-vector-store.md` §8 `delete()` for the
    contract this mirrors (same retry budget, same "raise, don't swallow" rule)."""


def checkpoint_truncate_or_raise(
    conn: sqlite3.Connection, *, max_retries: int = 5, max_backoff_s: float = 1.0
) -> None:
    """Run `PRAGMA wal_checkpoint(TRUNCATE)`, retrying with exponential backoff
    while the returned `busy` bit is set (another connection's read transaction is
    blocking the truncate), capped at `max_retries` attempts / `max_backoff_s` per
    wait. Raises `CheckpointBusyError` if it's still busy after the budget is
    spent — never silently returns as if the WAL had been flushed when it wasn't
    (docs/spikes/03-vector-store.md §8, control 者 third-round ruling, reused
    verbatim here per 04-w5-interfaces.md §5's "wal_checkpoint(TRUNCATE)（busy →
    重试 → 抛错，按 spike 03 契约）")."""
    busy = log = checkpointed = None
    delay = 0.0
    for _ in range(max_retries):
        if delay:
            time.sleep(delay)
        busy, log, checkpointed = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if not busy:
            return
        delay = min(max_backoff_s, (delay or 0.01) * 2)
    raise CheckpointBusyError(
        f"wal_checkpoint(TRUNCATE) still busy after {max_retries} retries "
        f"(busy={busy}, log={log}, checkpointed={checkpointed})"
    )


# --- Session real-delete (G20) ------------------------------------------------


def delete_session(conn: sqlite3.Connection, user_root: Path, session_id: str) -> None:
    """真删一个 Session：级联 turns/messages/runs/steps/permission_decisions/
    queue_items 的 SQLite 行 + 每个关联 Run 的 `runs/<id>/` payload 目录 + 这个
    Session 的 worker `HERMES_HOME`（`paths.worker_home_dir`，round-2 review：
    之前真删完全没碰过它，带凭据的目录永久留在磁盘上），最后
    `wal_checkpoint(TRUNCATE)`（04-w5-interfaces.md §5）。

    Refuses (not a silent cascade into more than the contract lists) rather than:
      - deleting the main session (PRD 00-foundation.md §5: "不可删除"),
      - orphaning a child Session's `parent_id` FK (self-referential —
        `sessions.parent_id REFERENCES sessions(id)` — deleting a session with
        live children would otherwise surface as a raw `sqlite3.IntegrityError`;
        same defensive pattern `projects/service.py::delete` already uses for its
        own FK-referencing rows),
      - deleting a Session with a Run still `running` (nothing else in this
        codebase stops a live worker's ACP event stream before its target rows
        vanish out from under it — refusing is honest, silently deleting while a
        worker still writes Steps for this Run would not be).

    The SQLite cascade itself is one transaction (rolls back whole on failure,
    see the `try`/`except sqlite3.Error` below) — but payload-purge and the
    trailing checkpoint run *after* that commit, where a rollback is no longer
    possible. A failure in either of those raises `PartialDeleteError` instead
    of a bare exception (04-w5-interfaces.md §6, see that type's docstring).
    """
    row = conn.execute("SELECT is_main FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise RpcError(NOT_FOUND, f"session not found: {session_id}")
    if row["is_main"]:
        raise RpcError(INVALID_STATE, "cannot delete the main session")

    child_count = conn.execute(
        "SELECT COUNT(*) AS c FROM sessions WHERE parent_id = ?", (session_id,)
    ).fetchone()["c"]
    if child_count:
        raise RpcError(
            INVALID_STATE,
            f"session has {child_count} child session(s); delete them first",
            {"child_count": child_count},
        )

    running_count = conn.execute(
        "SELECT COUNT(*) AS c FROM runs WHERE session_id = ? AND status = 'running'",
        (session_id,),
    ).fetchone()["c"]
    if running_count:
        raise RpcError(
            INVALID_STATE,
            "session has a Run still running; stop it before deleting",
            {"running_count": running_count},
        )

    run_ids = [
        r["id"]
        for r in conn.execute("SELECT id FROM runs WHERE session_id = ?", (session_id,)).fetchall()
    ]

    try:
        conn.execute(
            "DELETE FROM permission_decisions WHERE step_id IN "
            "(SELECT id FROM steps WHERE run_id IN (SELECT id FROM runs WHERE session_id = ?))",
            (session_id,),
        )
        conn.execute(
            "DELETE FROM steps WHERE run_id IN (SELECT id FROM runs WHERE session_id = ?)",
            (session_id,),
        )
        conn.execute("DELETE FROM runs WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM tasks WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM queue_items WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM turns WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM goals WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise

    # Round-1 review (04-w5-interfaces.md §6 "诚实失败"): the rows above are
    # already committed, so a failure in either step below is *not* a rolled-
    # back no-op — it's a real partial-completion state that must be reported
    # as such (`PartialDeleteError`), not left to surface as a bare exception
    # (N09-style "error happened but said nothing actionable").
    purged_run_ids: list[str] = []
    for run_id in run_ids:
        try:
            replay_store.purge_run(user_root, run_id)
        except OSError as exc:
            remaining = [r for r in run_ids if r not in purged_run_ids and r != run_id]
            raise PartialDeleteError(
                f"session {session_id}: SQLite rows deleted, but purging payload "
                f"for run {run_id} failed after {len(purged_run_ids)}/{len(run_ids)} "
                "run(s) already purged",
                detail={
                    "session_id": session_id,
                    "stage": "purge_run",
                    "purged_run_ids": purged_run_ids,
                    "failed_run_id": run_id,
                    "remaining_run_ids": remaining,
                    "fully_deleted": False,
                },
            ) from exc
        purged_run_ids.append(run_id)

    # Round-2 review: `workers/manager.py::_hermes_home_for` materializes real,
    # per-session state at `paths.worker_home_dir(user_root, session_id)` —
    # `config.yaml` (MCP server credentials among its contents,
    # `capabilities/mcp_config.py`), the `jones_gate` plugin copy,
    # `jones_tools.json` — every time a worker for this session spawns
    # (`_prepare_hermes_home`). Nothing purged it: `session.delete` left it on
    # disk forever, the one real per-Session directory this module's own G20
    # "真删" contract never reached. Same partial-completion handling as the run
    # payloads above — this runs after the SQLite commit too.
    worker_home = paths.worker_home_dir(user_root, session_id)
    if worker_home.exists():
        try:
            shutil.rmtree(worker_home)
        except OSError as exc:
            raise PartialDeleteError(
                f"session {session_id}: SQLite rows and run payloads deleted, but "
                "purging the worker's HERMES_HOME failed",
                detail={
                    "session_id": session_id,
                    "stage": "purge_worker_home",
                    "purged_run_ids": purged_run_ids,
                    "fully_deleted": False,
                },
            ) from exc

    try:
        checkpoint_truncate_or_raise(conn)
    except CheckpointBusyError as exc:
        raise PartialDeleteError(
            f"session {session_id}: rows, every run's payload, and the worker's "
            "HERMES_HOME are deleted, but the WAL checkpoint is still busy",
            detail={
                "session_id": session_id,
                "stage": "checkpoint",
                "purged_run_ids": purged_run_ids,
                "fully_deleted": True,
            },
        ) from exc


# --- Run real-delete (G20, 04-w5-interfaces.md §5 "新增到 v0 表") --------------


def delete_run(conn: sqlite3.Connection, user_root: Path, run_id: str) -> None:
    """真删一个 Run：级联 steps/permission_decisions 的行 + `runs/<id>/` payload
    目录 + checkpoint。刻意不删这个 Run 所属的 Turn/Session（那是 `session.delete`
    的范围）——只清掉这一条 Run 自己的痕迹，并把仍指着它的 `turns.run_id` 置空
    （该列本身不是声明的 FK，不清也不会报错，但留着一个指向不存在的 Run 的指针
    会让 `turn.messages`/回放 UI 以为还有一个 Run 可查，是 N09"错误被静默"的另一
    种形态——诚实地断开这个引用）。"""
    row = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise RpcError(NOT_FOUND, f"run not found: {run_id}")
    if row["status"] == "running":
        raise RpcError(INVALID_STATE, "cannot delete a Run that is still running")

    try:
        conn.execute(
            "DELETE FROM permission_decisions WHERE step_id IN "
            "(SELECT id FROM steps WHERE run_id = ?)",
            (run_id,),
        )
        conn.execute("DELETE FROM steps WHERE run_id = ?", (run_id,))
        conn.execute(
            "UPDATE turns SET run_id = NULL, updated_at = ? WHERE run_id = ?",
            (now_iso(), run_id),
        )
        conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise

    # Same partial-completion contract as `delete_session` above — the row is
    # already committed by this point.
    try:
        replay_store.purge_run(user_root, run_id)
    except OSError as exc:
        raise PartialDeleteError(
            f"run {run_id}: SQLite row deleted, but purging its payload directory "
            "failed",
            detail={"run_id": run_id, "stage": "purge_run", "fully_deleted": False},
        ) from exc

    try:
        checkpoint_truncate_or_raise(conn)
    except CheckpointBusyError as exc:
        raise PartialDeleteError(
            f"run {run_id}: row and payload are deleted, but the WAL checkpoint "
            "is still busy",
            detail={"run_id": run_id, "stage": "checkpoint", "fully_deleted": True},
        ) from exc


# --- Project real-delete (already existed as ProjectService.delete; this is the ---
# --- "storage mechanics" half it now calls, per 04-w5-interfaces.md §5 "project. --
# --- delete（已有，改为调 maintenance）" -------------------------------------------


def delete_project(conn: sqlite3.Connection, user_root: Path, project_id: str) -> None:
    """Refuses if any Session, Agent, Goal, or Cron still references `project_id`
    — *every* table `001_init.sql` declares a `project_id` FK on: `sessions`,
    `agents`, `goals.project_id REFERENCES projects(id)`, and
    `crons.project_id NOT NULL REFERENCES projects(id)`. (Round-1 review: the
    first cut of this function, moved verbatim from `ProjectService.delete`,
    only checked the first two — `crons`/`goals` didn't exist as a real
    reference path yet at the time, but `store/db.py::connect()` runs with
    `PRAGMA foreign_keys=ON`, so a Project with a live cron or goal now raises a
    raw `sqlite3.IntegrityError` on the `DELETE` below instead of this explicit
    refusal.) Then deletes the `projects` row, the Project's attachments
    directory (`paths.project_attachments_dir`), and checkpoints — the latter two
    steps run after the row's `commit()`, so a failure in either raises
    `PartialDeleteError` rather than a bare exception (round-2 review; same
    contract as `delete_session`/`delete_run`, see that type's docstring).
    Existence and the "can't delete the default Project" business rule stay in
    `ProjectService.delete` — those are Project-domain rules, not storage
    mechanics, and `DEFAULT_PROJECT_ID` lives in `projects/service.py`, not here
    (importing it back would be a circular import: `projects/service.py` is the
    caller of this function)."""
    session_count = conn.execute(
        "SELECT COUNT(*) AS c FROM sessions WHERE project_id = ?", (project_id,)
    ).fetchone()["c"]
    if session_count:
        raise RpcError(
            INVALID_STATE,
            f"project has {session_count} session(s); remove or reassign them first",
            {"session_count": session_count},
        )

    agent_count = conn.execute(
        "SELECT COUNT(*) AS c FROM agents WHERE project_id = ?", (project_id,)
    ).fetchone()["c"]
    if agent_count:
        raise RpcError(
            INVALID_STATE,
            f"project has {agent_count} agent(s); remove or reassign them first",
            {"agent_count": agent_count},
        )

    goal_count = conn.execute(
        "SELECT COUNT(*) AS c FROM goals WHERE project_id = ?", (project_id,)
    ).fetchone()["c"]
    if goal_count:
        raise RpcError(
            INVALID_STATE,
            f"project has {goal_count} goal(s); remove or reassign them first",
            {"goal_count": goal_count},
        )

    cron_count = conn.execute(
        "SELECT COUNT(*) AS c FROM crons WHERE project_id = ?", (project_id,)
    ).fetchone()["c"]
    if cron_count:
        raise RpcError(
            INVALID_STATE,
            f"project has {cron_count} cron(s); remove or reassign them first",
            {"cron_count": cron_count},
        )

    try:
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise

    # Round-2 review: everything below runs after the row above is already
    # committed — a rollback is no longer possible, so a failure here is a real
    # partial-completion state, same as `delete_session`/`delete_run` (04-w5-
    # interfaces.md §6 "诚实失败"). This used to raise bare `OSError`/
    # `CheckpointBusyError`, which `rpc/server.py::_dispatch`'s generic `except
    # Exception` turns into an opaque `INTERNAL_ERROR` with no `daemon.error`
    # broadcast — the exact defect round-1 review already fixed for
    # `delete_session`/`delete_run`, just never carried over here.
    attachments_dir = user_root / "projects" / project_id
    if attachments_dir.exists():
        try:
            shutil.rmtree(attachments_dir)
        except OSError as exc:
            raise PartialDeleteError(
                f"project {project_id}: SQLite row deleted, but purging its "
                "attachments directory failed",
                detail={
                    "project_id": project_id,
                    "stage": "purge_attachments",
                    "fully_deleted": False,
                },
            ) from exc

    try:
        checkpoint_truncate_or_raise(conn)
    except CheckpointBusyError as exc:
        raise PartialDeleteError(
            f"project {project_id}: row and attachments are deleted, but the WAL "
            "checkpoint is still busy",
            detail={"project_id": project_id, "stage": "checkpoint", "fully_deleted": True},
        ) from exc


# --- session.export (04-w5-interfaces.md §5: "导出后删除") -----------------------


def _export_rows(conn: sqlite3.Connection, table: str, session_id: str) -> list[dict[str, Any]]:
    # `table` is always one of this function's own fixed call sites below, never
    # external input.
    query = f"SELECT * FROM {table} WHERE session_id = ? ORDER BY created_at"  # noqa: S608
    return [dict(r) for r in conn.execute(query, (session_id,)).fetchall()]


def export_session(
    conn: sqlite3.Connection,
    user_root: Path,
    session_id: str,
    *,
    delete_after: bool = False,
) -> Path:
    """Write the full Session — turns, messages, runs, steps, permission_decisions,
    queue_items — as one JSON document, for the user's "查看/导出/删除" (PRD 10.3).
    Written under `<user_root>/projects/<project_id>/<session_id>-export-<ts>.json`
    — inside the Project's already-documented attachments directory (PRD 10.2 lists
    `projects/<project-id>/attachments/`; introducing a brand-new top-level
    `exports/` directory not in that tree would itself fail this issue's own
    directory-audit test), not a temp/cache location (PRD 10.3 marks `cache/` as
    "可随时清空" — an export the user asked to keep must not live somewhere that
    can vanish on the next `daemon.clear_cache`).

    Written durably, not just atomically (round-2 review — the same fix, and the
    same "atomicity isn't durability" reasoning, `secrets/vault.py::
    Vault._write_entries` already applies to `vault.enc`): tmp file, `fsync` the
    file, `os.replace`, then `fsync` the containing directory. A plain
    `Path.write_text` can return with the bytes still sitting in the OS page
    cache — a crash right after leaves a 0-byte or missing export file even
    though this function's own contract below promises `delete_after` never
    deletes before the export is "fully written".

    `delete_after=True` calls `delete_session` only after the export file is
    durably written — export-then-delete, never the reverse, so a crash between
    the two steps leaves the (undeleted) Session and its export both intact rather
    than a deleted Session with no export to show for it.
    """
    session = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if session is None:
        raise RpcError(NOT_FOUND, f"session not found: {session_id}")
    session_d = dict(session)

    steps_rows = conn.execute(
        "SELECT s.* FROM steps s JOIN runs r ON r.id = s.run_id WHERE r.session_id = ? "
        "ORDER BY s.run_id, s.seq",
        (session_id,),
    ).fetchall()
    permission_rows = conn.execute(
        "SELECT pd.* FROM permission_decisions pd JOIN steps s ON s.id = pd.step_id "
        "JOIN runs r ON r.id = s.run_id WHERE r.session_id = ? ORDER BY pd.created_at",
        (session_id,),
    ).fetchall()

    document = {
        "exported_at": now_iso(),
        "session": session_d,
        "turns": _export_rows(conn, "turns", session_id),
        "messages": _export_rows(conn, "messages", session_id),
        "runs": _export_rows(conn, "runs", session_id),
        "steps": [dict(r) for r in steps_rows],
        "permission_decisions": [dict(r) for r in permission_rows],
        "queue_items": _export_rows(conn, "queue_items", session_id),
    }

    dest_dir = user_root / "projects" / session_d["project_id"]
    dest_dir.mkdir(parents=True, exist_ok=True)
    ts = now_iso().replace(":", "").replace(".", "")
    dest_path = dest_dir / f"{session_id}-export-{ts}.json"
    _write_file_durably(dest_path, json.dumps(document, ensure_ascii=False, indent=2))

    if delete_after:
        try:
            delete_session(conn, user_root, session_id)
        except PartialDeleteError as exc:
            # The export itself is already durably on disk at this point — a
            # `fully_deleted: True` partial failure (only the trailing WAL
            # checkpoint stayed busy) must not make the caller believe the
            # export is missing too. Stash the path in the same `detail` dict
            # `daemon.error`/the RPC layer already surface, so a caller that
            # sees `fully_deleted: True` can still report success with a path
            # instead of losing it (see `sessions/methods.py::session_export`).
            exc.detail["export_path"] = str(dest_path)
            raise

    return dest_path


def _write_file_durably(path: Path, text: str) -> None:
    """Same technique as `secrets/vault.py::Vault._write_entries` (round-2
    review — reused here for `session.export`, not just credentials): write to a
    sibling tmp file, `fsync` its fd, atomically `os.replace` it into place, then
    `fsync` the containing directory so the rename itself is durable too. A plain
    `Path.write_text` is atomic (a reader never sees a half-written file) but not
    durable (the bytes can still be sitting in the page cache when the process
    dies) — for an export the user explicitly asked to keep, that gap matters as
    much as it does for `vault.enc`."""
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", closefd=True) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    os.replace(tmp_path, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


# --- Backup / log / cache housekeeping (04-w5-interfaces.md §5) ------------------

MAX_BACKUPS = 5
# Round-3 review (controller ruling R-O3): `logs/` mtime-based rotation
# (`rotate_logs`/`run_log_rotation_loop` used to live here) is gone — it only
# ever existed to work around `daemon.log` being an unbounded, continuously-
# open file `TimedRotatingFileHandler` couldn't fully own; `logging.py::
# configure_logging` now rotates `daemon.log` itself on size
# (`RotatingFileHandler`), and the stderr stream (the other thing that used to
# land under `logs_dir` via launchd's redirect) is only opened for an actual
# tty now, not under launchd — so nothing unmanaged is left under `logs_dir`
# for a sweep to clean. Same cadence number kept for the redaction self-check
# loop below (unrelated to log rotation, was only ever borrowing this
# constant, not a new one to justify).
REDACTION_CHECK_INTERVAL_S = 3600.0  # matches replay/retention.py's idle-sweep cadence


def rotate_backups(db_path: Path, *, keep: int = MAX_BACKUPS) -> list[Path]:
    """Keep only the `keep` most-recently-created `<db_path>.bak-*` files
    (`store/migrator.py::_backup_before_migrating` names them
    `jones.db.bak-<version>-<time.time_ns()>`, so the numeric suffix after the
    last `-` already sorts chronologically). Returns the paths that were removed.
    A file that fails to delete (permissions, already gone) is logged and skipped,
    not fatal to the rest of the prune (DEV.md 诚实失败: log it, don't crash
    startup/migration over stale-backup cleanup)."""
    backups = sorted(
        db_path.parent.glob(f"{db_path.name}.bak-*"),
        key=lambda p: p.name.rsplit("-", 1)[-1],
    )
    to_remove = backups[:-keep] if keep > 0 else backups
    removed: list[Path] = []
    for path in to_remove:
        try:
            path.unlink()
        except OSError:
            logger.error(
                "failed to remove old db backup during rotation",
                exc_info=True,
                extra={"detail": {"path": str(path)}},
            )
            continue
        removed.append(path)
    return removed


# Round-3 review (controller ruling R-O3): `rotate_logs`/`run_log_rotation_loop`/
# `_active_log_handler_paths` used to live here -- a mtime sweep over `logs_dir`
# built specifically to work around `daemon.log` being an unbounded,
# continuously-open file no `TimedRotatingFileHandler` could fully own on its
# own, plus launchd's `daemon.out.log`/`daemon.err.log` redirects that no
# sweep could ever touch while the process was up (that half was always a
# documented, unfixed gap -- see this module's git history for the old
# docstring). Both problems are gone now, not worked around:
# `logging.py::configure_logging` rotates `daemon.log` itself on size
# (`RotatingFileHandler`, 10MB x 7 files), and the stderr stream is only
# opened for an actual tty -- under launchd (no tty) nothing is written to
# `logs_dir` outside `daemon.log` at all, so there is nothing left for a
# periodic sweep to clean up. `__main__.py` no longer starts this loop.


def clear_cache(cache_dir: Path) -> int:
    """`daemon.clear_cache` RPC (04-w5-interfaces.md §5): delete every entry under
    `cache_dir` (PRD 10.3: "网页抓取缓存、模型响应缓存 ... 可随时清空"), recreate
    the now-empty directory (callers elsewhere expect `paths.cache_dir()` to always
    exist), and return how many top-level entries were removed."""
    if not cache_dir.exists():
        return 0
    removed = 0
    for entry in cache_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
        removed += 1
    return removed


# --- Startup Key-redaction self-check (G03, N02, 04-w5-interfaces.md §5) --------


def _sliding_windows(value: str, size: int = 8) -> Iterable[str]:
    if len(value) < size:
        return
    for i in range(len(value) - size + 1):
        yield value[i : i + size]


# Round-2 review: nothing bounded a single file's own read. This pass runs
# every `REDACTION_CHECK_INTERVAL_S`, forever, for the life of the daemon — a
# hard ceiling on what one pass reads/scans per file is what keeps that "runs
# forever" property from also meaning "grows unbounded forever". Recent bytes
# are what matter for catching a leak soon after it happens, not a file's
# entire history — a tail read (or, for logs since round-3, an incremental
# read of only what's new), not the whole file every pass.
MAX_SCAN_BYTES_PER_FILE = 2 * 1024 * 1024  # 2MB cap per individual file read

# Round-3 review (review item 1, controller ruling R-O2): the self-check used
# to join every source's texts into one haystack and *then* truncate that
# combined string from the tail to a single global `MAX_HAYSTACK_CHARS`. Log
# files were always first in that join, and `_recent_payload_texts` has no
# per-pass aggregate cap of its own (a Run can have dozens to hundreds of
# payload files, `replay/store.py::write_payload` writes one file per step) —
# so once the combined haystack crossed the global cap, the join-then-truncate
# order silently dropped the *earliest* source in the list first, which was
# always the log files — 04-w5 §5's own named sink — with no warning and the
# self-check still reporting "clean". Each of this pass's four sources now
# gets its own independent budget instead, applied *before* any of them are
# joined together (`_cap_haystack_source`, used once per source in
# `_redaction_scan_pass`) — no single source can crowd another out, and
# hitting a source's own cap logs a warning rather than staying silent.
MAX_HAYSTACK_CHARS_PER_SOURCE = 2 * 1024 * 1024  # 2MB cap, independently, per source


def _cap_haystack_source(texts: Iterable[str], *, source: str) -> str:
    """Join `texts` and cap the result to `MAX_HAYSTACK_CHARS_PER_SOURCE`,
    keeping the most-recent (tail) bytes — logging once, at the point this
    source's own budget is hit, instead of silently discarding data (see
    `MAX_HAYSTACK_CHARS_PER_SOURCE`'s own comment for why this replaced a
    single post-join truncation)."""
    joined = "\n".join(texts)
    if len(joined) > MAX_HAYSTACK_CHARS_PER_SOURCE:
        logger.warning(
            "redaction self-check: %s scan input exceeded its per-source budget, "
            "truncating to the most recent bytes",
            source,
            extra={
                "detail": {
                    "source": source,
                    "total_chars": len(joined),
                    "cap_chars": MAX_HAYSTACK_CHARS_PER_SOURCE,
                }
            },
        )
        joined = joined[-MAX_HAYSTACK_CHARS_PER_SOURCE:]
    return joined


def scan_for_leaked_keys(
    configured_keys: dict[str, str],
    haystacks: Iterable[str],
) -> list[str]:
    """Runtime guard behind G03 ("全文 grep 已配置的 Key 字符串, 命中数为 0") /
    N02 — this is the *daemon's own* self-check at startup, not just a test-suite
    grep (04-w5-interfaces.md §5: "启动期 Key 脱敏自检 ... 命中即 daemon.error").

    `configured_keys` is `{provider_name: full_key_value}` (from
    `Vault.entries()`); `haystacks` is every string to scan (log file contents,
    recent RPC response samples — see `rpc/server.py`'s response ring buffer —
    and, round-2 review, recent Step args / Message content / Run payload text,
    see `_redaction_scan_pass`). Round-3 review: each entry in `haystacks` is
    expected to already be capped by the caller (`_cap_haystack_source`) —
    this function applies no further truncation of its own, it just joins and
    scans exactly what it's given. For each key of at least 8 characters,
    slides an 8-character window across it (the contract's "任意 8 字节子串" —
    deliberately finer-grained than "does the whole key appear", since a wider
    window could miss a key that got truncated mid-string by some upstream
    formatting bug) and checks membership in the concatenated haystacks.

    Returns the list of provider names that had at least one hit — never the
    matched substring or the key itself, so this check's own error report can't
    become a second leak of the very thing it's flagging.
    """
    haystack = "\n".join(haystacks)
    hits: list[str] = []
    for name, key in configured_keys.items():
        if not key:
            continue
        if any(window in haystack for window in _sliding_windows(key)):
            hits.append(name)
    return hits


def _read_tail(path: Path, max_bytes: int = MAX_SCAN_BYTES_PER_FILE) -> str:
    """The last `max_bytes` of `path`, decoded leniently — never the whole file
    (round-2 review: a file this reads can be arbitrarily large and this runs
    on an unbounded loop, see `MAX_SCAN_BYTES_PER_FILE`'s comment). Used for
    Run payload files, which (unlike logs since round-3) have no natural
    "since last pass" position to track — a fresh Run's payload directory is
    either scanned or it isn't yet, there's no append-in-place file to track
    an offset into. A failure to read is logged and treated as empty, not
    fatal to the rest of a scan pass — same "skip, don't crash the sweep"
    shape every other file-touching function in this module already uses."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        logger.warning(
            "redaction self-check: could not read a file, skipping",
            extra={"detail": {"path": str(path)}},
        )
        return ""


# Round-3 review (controller ruling R-O2): logs used to be re-read in full
# (well, in full up to `MAX_SCAN_BYTES_PER_FILE`'s tail) on *every* pass —
# for an hourly-forever loop, that's the same bytes scanned again and again
# for as long as a file stays under the cap. This tracks, per log file, the
# byte offset already scanned as of the *previous* pass — persisted under
# `runtime/` (survives a daemon restart, not just successive loop iterations
# within one process) — so each pass only reads what's newly appended since
# then.
REDACTION_SCAN_STATE_FILENAME = "redaction_scan_state.json"


def _redaction_state_path(runtime_dir: Path) -> Path:
    return runtime_dir / REDACTION_SCAN_STATE_FILENAME


def _load_log_offsets(state_path: Path) -> dict[str, dict[str, int]]:
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, int]] = {}
    for key, entry in raw.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        offset, inode = entry.get("offset"), entry.get("inode")
        if isinstance(offset, int) and isinstance(inode, int):
            result[key] = {"offset": offset, "inode": inode}
    return result


def _save_log_offsets(state_path: Path, offsets: dict[str, dict[str, int]]) -> None:
    # Soft state, not the vault/export durability contract (`_write_file_
    # durably` above): losing the last update to a crash just means the next
    # pass re-scans a bit more (or from the front) for the one file that lost
    # its offset — never a skipped sink — so a plain replace is enough here,
    # no fsync dance.
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = state_path.with_name(f".{state_path.name}.tmp-{os.getpid()}")
        tmp_path.write_text(json.dumps(offsets), encoding="utf-8")
        os.replace(tmp_path, state_path)
    except OSError:
        logger.warning(
            "redaction self-check: failed to persist log scan offsets, the "
            "next pass may re-scan more than just the newly-appended bytes",
            exc_info=True,
        )


def _read_new_bytes(path: Path, *, size: int, last_offset: int, max_bytes: int) -> tuple[str, int]:
    """Bytes appended to `path` (whose current size the caller already knows,
    as `size`) since `last_offset`, capped at `max_bytes` (the
    should-not-happen-at-this-loop's-hourly-cadence, but not impossible, case
    where a burst of writes between passes exceeds it — still bounded, tail
    of the new bytes). If the file shrank below `last_offset` (truncated out
    from under this scan), the remembered position no longer means anything
    for this file's *current* bytes, so this restarts from the front (still
    capped the same way). Returns `(text, new_offset)` — the caller persists
    `new_offset` so the next pass only reads what's newly appended since this
    one, instead of re-reading the same bytes forever."""
    start = last_offset if last_offset <= size else 0
    if size - start > max_bytes:
        start = size - max_bytes
    try:
        with path.open("rb") as f:
            f.seek(start)
            data = f.read()
    except OSError:
        logger.warning(
            "redaction self-check: could not read a log file, skipping",
            extra={"detail": {"path": str(path)}},
        )
        return "", last_offset
    return data.decode("utf-8", errors="replace"), size


def _read_logs_incremental(logs_dir: Path, state_path: Path) -> list[str]:
    """Every log file under `logs_dir`, but only the bytes appended since this
    function's own previous pass (round-3 review, controller ruling R-O2) —
    tracked per-file by resolved path in the small JSON state file
    `state_path` names.

    A path alone isn't a stable file identity here: `logging.py`'s
    `RotatingFileHandler` rotates by *renaming* (`daemon.log` -> `.1`, the old
    `.1` -> `.2`, and so on), so `daemon.log.1` names a different file's bytes
    after every rotation even though the offset is persisted under that same
    path string — and the freshly-renamed-in file is typically the same
    ~`DAEMON_LOG_MAX_BYTES` size as the stale persisted offset for that path,
    so a naive `last_offset <= size` check reads it as "nothing new" and
    silently skips it (round-3-followup review). Each entry therefore also
    records the file's inode; a path whose current inode doesn't match the
    persisted one is a different file wearing an old name, so this restarts
    that path from byte 0 (logged once) instead of reusing a stale offset
    that happens to still fit within the new file's size.

    Offsets for files that no longer exist under `logs_dir` are dropped from
    the persisted state on every pass, so a long-lived daemon rotating
    through many log filenames doesn't grow this state file forever."""
    if not logs_dir.exists():
        return []
    offsets = _load_log_offsets(state_path)
    updated: dict[str, dict[str, int]] = {}
    texts: list[str] = []
    for path in logs_dir.iterdir():
        if not path.is_file():
            continue
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        try:
            st = path.stat()
        except OSError:
            # Gone between `iterdir()` and here — skip this pass for it; not
            # writing an entry for `key` just drops its offset from the
            # persisted state, same "soft state, worst case rescan more"
            # tolerance `_save_log_offsets` already documents.
            continue
        entry = offsets.get(key)
        if entry is not None and entry["inode"] == st.st_ino:
            last_offset = entry["offset"]
        else:
            if entry is not None:
                logger.warning(
                    "redaction self-check: log file changed identity under "
                    "an existing name (rotated in), rescanning from the start",
                    extra={"detail": {"path": key}},
                )
            last_offset = 0
        text, new_offset = _read_new_bytes(
            path, size=st.st_size, last_offset=last_offset, max_bytes=MAX_SCAN_BYTES_PER_FILE
        )
        if text:
            texts.append(text)
        updated[key] = {"offset": new_offset, "inode": st.st_ino}
    _save_log_offsets(state_path, updated)
    return texts


# Round-2 review (G03: "全文 grep 已配置的 Key ... 命中数为 0"): `runs/<run-id>/`
# payload files and `jones.db`'s `steps.args_json`/`messages.content_json` are
# real sinks a leaked Key can land in (an agent running a `curl -H
# "Authorization: Bearer sk-..."` tool call writes the header into both), and
# neither was ever part of this scan's haystack — only log files and RPC
# response samples were. Bounded the same way as everything else in this
# module's self-check: most-recent-N rather than full history, so an
# hourly-forever pass stays cheap regardless of how long the daemon has been
# running (`steps`/`messages` have no index on `created_at` to filter by time
# cheaply, but a `rowid`-order `LIMIT` needs no index — SQLite walks the
# table's own btree from the end).
REDACTION_DB_SCAN_LIMIT = 500  # most-recent rows scanned per table, per pass
REDACTION_RECENT_RUNS_SCANNED = 50  # most-recently-created runs/<id>/ dirs scanned per pass


def _recent_db_texts(conn: sqlite3.Connection, *, limit: int) -> list[str]:
    texts = [
        row[0]
        for row in conn.execute("SELECT args_json FROM steps ORDER BY rowid DESC LIMIT ?", (limit,))
    ]
    texts.extend(
        row[0]
        for row in conn.execute(
            "SELECT content_json FROM messages ORDER BY rowid DESC LIMIT ?", (limit,)
        )
    )
    return texts


def _recent_db_texts_readonly(db_path: Path, *, limit: int) -> list[str]:
    """Same query as `_recent_db_texts` above, against a fresh, short-lived
    read-only connection to `db_path` instead of the daemon's shared
    `check_same_thread=True` connection (round-3 review, controller ruling
    R-O2: "扫描在 DB 线程之外的独立 executor 跑，不占事件循环也不占 DB 线程"). This
    always runs on `_REDACTION_EXECUTOR`'s own worker thread, never
    `store/db.py`'s dedicated DB thread — opening the *shared* connection
    here would violate that connection's own thread-affinity invariant
    (`check_same_thread=True`). WAL mode lets this reader proceed
    concurrently with the writer connection without blocking it; a `mode=ro`
    URI connection additionally refuses to create the file if it's somehow
    missing rather than silently starting a new, empty database. Any
    `sqlite3.Error` here (connect or query) is left to propagate — same "one
    bad pass is logged and skipped, not silently reported as a clean scan"
    contract `run_redaction_self_check_loop` already applies to every other
    failure in a pass, not a new partial-success shape just for this source."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=True)
    try:
        return _recent_db_texts(conn, limit=limit)
    finally:
        conn.close()


def _recent_payload_texts(runs_dir: Path, *, limit: int) -> list[str]:
    """Every file's tail under the `limit` most-recently-created `runs/<id>/`
    directories. Run ids are ULIDs (`config/ids.py::new_ulid`) — lexicographic
    order on the directory name IS chronological order, so this needs no
    per-file `stat()` to find "most recent": one `iterdir()` + a sort of the
    (cheap, name-only) directory listing, then read only inside the chosen
    dirs. `limit` bounds *which Run directories* get scanned; each individual
    file inside them is separately capped by `_read_tail`'s own
    `MAX_SCAN_BYTES_PER_FILE`, and the combined result of every file across
    every chosen Run is, in turn, capped as its own source by
    `_cap_haystack_source` before it's ever joined with any other source —
    round-3 review, review item 1: this source has no *count* cap on files
    per Run (a Run can have dozens to hundreds of payload files), so the
    per-source char budget is what actually keeps one Run's payload total
    from crowding out logs/DB/responses, not this function's own `limit`."""
    if not runs_dir.exists():
        return []
    run_dirs = sorted((p for p in runs_dir.iterdir() if p.is_dir()), reverse=True)[:limit]
    out: list[str] = []
    for run_dir in run_dirs:
        try:
            entries = list(run_dir.iterdir())
        except OSError:
            continue
        for path in entries:
            if path.is_file():
                out.append(_read_tail(path))
    return out


# Round-3 review (controller ruling R-O2): a dedicated, single-worker executor
# for the redaction scan — separate from both the asyncio event loop and
# `store/db.py`'s own dedicated DB-thread executor (`_DB_EXECUTOR`), which
# ordinary RPC traffic depends on staying responsive. Same shape/reasoning as
# `store/db.py::run_in_db_thread`: a single worker (not the bare default pool
# `asyncio.to_thread()` reaches for) keeps every pass's work — the vault
# decrypt, the incremental log reads, the short-lived read-only DB connection,
# the payload reads, and the CPU-bound substring scan itself — sequential on
# one thread, never competing with itself.
_REDACTION_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jones-redaction")


async def _run_in_redaction_thread[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_REDACTION_EXECUTOR, functools.partial(fn, *args, **kwargs))


def _redaction_scan_pass(
    *,
    vault: Any,
    logs_dir: Path,
    db_path: Path,
    runs_dir: Path,
    state_path: Path,
    response_texts: list[str],
) -> list[str]:
    """The entire redaction-check pass, synchronous — meant to run on
    `_REDACTION_EXECUTOR`'s single dedicated worker thread via
    `_run_in_redaction_thread`, never called directly from async code. Loads
    the configured keys (round-2 review: `vault.entries()` reads+decrypts the
    whole vault file once, not once per configured provider), reads each
    source, caps each source independently (`_cap_haystack_source` — review
    item 1), then runs the actual substring scan."""
    configured_keys = vault.entries()
    log_texts = _read_logs_incremental(logs_dir, state_path)
    db_texts = _recent_db_texts_readonly(db_path, limit=REDACTION_DB_SCAN_LIMIT)
    payload_texts = _recent_payload_texts(runs_dir, limit=REDACTION_RECENT_RUNS_SCANNED)
    haystacks = [
        _cap_haystack_source(log_texts, source="logs"),
        _cap_haystack_source(db_texts, source="db"),
        _cap_haystack_source(payload_texts, source="run_payloads"),
        _cap_haystack_source(response_texts, source="rpc_responses"),
    ]
    return scan_for_leaked_keys(configured_keys, haystacks)


async def startup_key_redaction_self_check(
    *,
    vault: Any,
    logs_dir: Path,
    db_path: Path,
    runs_dir: Path,
    runtime_dir: Path,
    recent_response_samples: Iterable[bytes],
    on_hit: Any,
) -> list[str]:
    """One pass of `scan_for_leaked_keys` over real inputs: every log file
    under `logs_dir` (only the bytes appended since the previous pass — see
    `_read_logs_incremental`), the most-recent Step args / Message content
    rows in `jones.db` (a fresh read-only connection to `db_path`, not the
    daemon's shared one — see `_recent_db_texts_readonly`), the most-recent
    Run payload files under `runs_dir`, plus whatever `recent_response_
    samples` the caller hands in (the server's last `RECENT_RESPONSES_MAXLEN`
    RPC response bodies).

    Round-3 review (controller ruling R-O2): every I/O step and the CPU-bound
    scan itself now run together, on `_REDACTION_EXECUTOR`'s one dedicated
    thread (`_redaction_scan_pass`, via `_run_in_redaction_thread`) — separate
    from both the asyncio event loop and `store/db.py`'s own dedicated
    DB-thread executor, so a slow or large scan pass competes with neither
    ordinary RPC dispatch nor ordinary DB reads/writes for a thread. (This
    supersedes the round-1 review design of splitting the work between
    `asyncio.to_thread`'s shared default pool for file I/O and `store.
    run_in_db_thread` for the DB query — both threads this pass no longer
    touches.)

    `on_hit(code, message, detail)` is called (once, with every hit provider
    name) iff there's at least one hit — the caller wires this to `RpcServer`
    broadcasting `daemon.error` (00-foundation.md §4.2: "daemon.error ... 永不
    静默"); this function itself never silently returns "clean" without having
    actually looked at real data — vault access failures (`VaultError`)
    propagate, they are not swallowed into a false-clean result.

    Round-1 review: called once, this can't do what its own name promises —
    at true daemon startup (the moment `__main__.py` used to call this)
    `recent_response_samples` is necessarily empty (`RpcServer.serve_forever()`
    hasn't accepted a first client yet), so the "response samples" half of the
    contract silently scanned nothing, every run, forever. Call this
    repeatedly via `run_redaction_self_check_loop` below instead of once —
    see its docstring."""
    response_texts = [
        b.decode("utf-8", errors="replace") if isinstance(b, bytes) else str(b)
        for b in recent_response_samples
    ]
    hits = await _run_in_redaction_thread(
        _redaction_scan_pass,
        vault=vault,
        logs_dir=logs_dir,
        db_path=db_path,
        runs_dir=runs_dir,
        state_path=_redaction_state_path(runtime_dir),
        response_texts=response_texts,
    )
    if hits:
        logger.error(
            "startup key-redaction self-check found a configured key substring "
            "in logs or recent RPC responses",
            extra={"detail": {"providers": hits}},
        )
        await on_hit(
            "key_redaction_failed",
            "a configured provider key was found unredacted in logs or a recent RPC response",
            {"providers": hits},
        )
    return hits


async def run_redaction_self_check_loop(
    *,
    vault: Any,
    logs_dir: Path,
    db_path: Path,
    runs_dir: Path,
    runtime_dir: Path,
    recent_response_samples: Callable[[], Iterable[bytes]],
    on_hit: Any,
    interval_s: float = REDACTION_CHECK_INTERVAL_S,
) -> None:
    """Background loop around `startup_key_redaction_self_check`, same
    started/cancelled-from-`__main__.py` lifecycle every other background loop
    in this module uses (04-w5-interfaces.md §1).

    Round-1 review: a single call at process startup is structurally unable to
    ever see a real RPC response — the daemon hasn't accepted its first client
    connection yet at the point `__main__.py` used to fire this once. Looping
    it, instead, means every pass after the first can actually see whatever
    responses have gone out since — the response-samples half of the check
    only ever does real work this way. The first iteration below still runs
    immediately (not after the first `interval_s` sleep): even with empty
    response samples, the log-file half of the check is worth running as
    early in startup as the surrounding async setup in `__main__.py` allows,
    not delayed by a full hour.

    `recent_response_samples` is a zero-arg callable (`RpcServer.
    recent_response_samples`, a bound method) rather than a fixed iterable —
    each pass needs a *fresh* snapshot of whatever the server's ring buffer
    holds at that moment, not the one snapshot taken when this loop started."""
    try:
        while True:
            try:
                await startup_key_redaction_self_check(
                    vault=vault,
                    logs_dir=logs_dir,
                    db_path=db_path,
                    runs_dir=runs_dir,
                    runtime_dir=runtime_dir,
                    recent_response_samples=recent_response_samples(),
                    on_hit=on_hit,
                )
            except Exception:  # noqa: BLE001 - one bad pass must not kill the loop forever
                logger.error("key-redaction self-check pass failed", exc_info=True)
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        raise
