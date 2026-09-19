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
import json
import logging
import os
import shutil
import sqlite3
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from jones_daemon import paths
from jones_daemon.config.ids import now_iso
from jones_daemon.logging import get_logger
from jones_daemon.replay import store as replay_store
from jones_daemon.rpc.errors import INVALID_STATE, NOT_FOUND, RpcError
from jones_daemon.store.db import run_in_db_thread

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
LOG_RETENTION_DAYS = 7
LOG_ROTATION_INTERVAL_S = 3600.0  # matches replay/retention.py's idle-sweep cadence
# Round-1 review: same cadence as log rotation above, reused (not a new number to
# justify) for `run_redaction_self_check_loop` — see that function's docstring for
# why a *periodic* self-check, not just a one-shot startup call, is what it takes
# for the "最近 100 条 RPC 响应样本" half of the contract to ever see real data.
REDACTION_CHECK_INTERVAL_S = LOG_ROTATION_INTERVAL_S


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


def rotate_logs(logs_dir: Path, *, retention_days: int = LOG_RETENTION_DAYS) -> int:
    """Delete any file under `logs_dir` whose mtime is older than `retention_days`
    (PRD 10.3 "守护进程 / worker 日志 ... 滚动保留 7 天"). Returns the count removed.

    Caveat (honest, not silently glossed over — round-1 review, narrowed but not
    eliminated): the daemon's two launchd-redirected files (`daemon.out.log`/
    `daemon.err.log`, launchd's `StandardOutPath`/`StandardErrorPath` — see
    `service.py::render_plist`) are continuously appended to by the OS redirect
    for as long as the daemon runs, so their mtime never falls behind
    `retention_days` while the process is up — this sweep can't truncate a file
    that's open and being written by another process without an out-of-band
    log-reopen signal (SIGHUP-style), which is out of this issue's scope. The
    daemon's own structured log stream no longer has this problem: `daemon.log`
    (written by `logging.py::configure_logging`'s `TimedRotatingFileHandler`)
    rotates and prunes itself, real 7-day retention enforced by the logging
    module directly, independent of this sweep. What this sweep *does* clean up:
    any other, non-continuously-written file that lands under `logs_dir` (a
    one-shot diagnostic dump, an already-rotated `daemon.log.2026-09-01` past its
    retention window if the handler's own pruning ever lagged) once it's
    actually stale.

    Round-2 review: this used to sweep `daemon.log` itself too, purely on
    mtime — and a quiet, always-on desktop daemon that goes `retention_days`
    without emitting a single log line (real: the rotation loops below, a clean
    redaction self-check pass, a `daemon.status` call — none of them log)
    crosses that cutoff while `logging.py::configure_logging`'s
    `TimedRotatingFileHandler` still holds an open fd on the very file this was
    about to `unlink()`. Python's `BaseRotatingHandler` tolerates its source
    file vanishing (an `os.path.exists` guard in `rotate()`), so nothing
    crashes — the handler just keeps writing lines into an unlinked inode that
    nothing can read, silently, until its own next midnight rollover reopens a
    fresh file. Up to ~24h of logs lost is exactly the "错误永不静默" failure
    this issue exists to prevent, so any file currently open by a handler on
    the `jones_daemon` logger is excluded from this sweep regardless of mtime —
    real 7-day retention for that file is the handler's own job (`backupCount`
    in `configure_logging`), not this sweep's."""
    cutoff = time.time() - retention_days * 86400
    removed = 0
    if not logs_dir.exists():
        return 0
    active = _active_log_handler_paths()
    for path in logs_dir.iterdir():
        if not path.is_file():
            continue
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in active:
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            logger.error(
                "failed to remove stale log file during rotation",
                exc_info=True,
                extra={"detail": {"path": str(path)}},
            )
    return removed


def _active_log_handler_paths() -> set[Path]:
    """Absolute, resolved paths of every file the `jones_daemon` logger currently
    has an open handler on (round-2 review, see `rotate_logs`'s own docstring for
    why this must never be swept by mtime alone). `FileHandler`/
    `TimedRotatingFileHandler` both expose the file they're writing to as
    `.baseFilename` (an absolute path, set by `FileHandler.__init__`)."""
    out: set[Path] = set()
    for handler in logging.getLogger("jones_daemon").handlers:
        base = getattr(handler, "baseFilename", None)
        if not base:
            continue
        try:
            out.add(Path(base).resolve())
        except OSError:
            out.add(Path(base))
    return out


async def run_log_rotation_loop(logs_dir: Path) -> None:
    """Background loop, same shape as `replay/retention.py::run_sweep_loop`:
    started/cancelled from `__main__.py`'s own lifecycle (this branch doesn't own
    `SessionService.startup`/`shutdown`, see 04-w5-interfaces.md §1)."""
    try:
        while True:
            await asyncio.sleep(LOG_ROTATION_INTERVAL_S)
            try:
                rotate_logs(logs_dir)
            except Exception:  # noqa: BLE001 - one bad sweep must not kill the loop forever
                logger.error("log rotation sweep iteration failed", exc_info=True)
    except asyncio.CancelledError:
        raise


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


# Round-2 review: nothing bounded the haystack this scan builds. Log files are
# unbounded by construction (`rotate_logs`'s own docstring: it can't truncate
# the daemon's own live `daemon.out.log`/`daemon.err.log`), and this pass runs
# every `REDACTION_CHECK_INTERVAL_S`, forever, for the life of the daemon — a
# hard ceiling on what one pass reads/scans is what keeps that "runs forever"
# property from also meaning "grows unbounded forever". Recent bytes are what
# matter for catching a leak soon after it happens, not a file's entire
# history — a tail read, not the whole file.
MAX_SCAN_BYTES_PER_FILE = 2 * 1024 * 1024  # 2MB tail per individual file scanned
MAX_HAYSTACK_CHARS = 8 * 1024 * 1024  # hard ceiling on the joined haystack itself


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
    see `startup_key_redaction_self_check`). For each key of at least 8
    characters, slides an 8-character window across it (the contract's "任意 8
    字节子串" — deliberately finer-grained than "does the whole key appear",
    since a wider window could miss a key that got truncated mid-string by some
    upstream formatting bug) and checks membership in the concatenated
    haystacks, itself capped at `MAX_HAYSTACK_CHARS` (round-2 review — see that
    constant's own comment).

    Returns the list of provider names that had at least one hit — never the
    matched substring or the key itself, so this check's own error report can't
    become a second leak of the very thing it's flagging.
    """
    haystack = "\n".join(haystacks)
    if len(haystack) > MAX_HAYSTACK_CHARS:
        haystack = haystack[-MAX_HAYSTACK_CHARS:]
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
    on an unbounded loop, see `MAX_SCAN_BYTES_PER_FILE`'s comment). A failure to
    read is logged and treated as empty, not fatal to the rest of a scan pass —
    same "skip, don't crash the sweep" shape every other file-touching function
    in this module already uses."""
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


def _recent_payload_texts(runs_dir: Path, *, limit: int) -> list[str]:
    """Every file's tail under the `limit` most-recently-created `runs/<id>/`
    directories. Run ids are ULIDs (`config/ids.py::new_ulid`) — lexicographic
    order on the directory name IS chronological order, so this needs no
    per-file `stat()` to find "most recent": one `iterdir()` + a sort of the
    (cheap, name-only) directory listing, then read only inside the chosen
    dirs."""
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


async def startup_key_redaction_self_check(
    *,
    vault: Any,
    logs_dir: Path,
    conn: sqlite3.Connection,
    runs_dir: Path,
    recent_response_samples: Iterable[bytes],
    on_hit: Any,
) -> list[str]:
    """One pass of `scan_for_leaked_keys` over real inputs: every log file under
    `logs_dir`, the most-recent Step args / Message content rows in `jones.db`,
    the most-recent Run payload files under `runs_dir` (round-2 review — see
    `_recent_db_texts`/`_recent_payload_texts`), plus whatever
    `recent_response_samples` the caller hands in (the server's last ~100 RPC
    response bodies). Reads log/payload files off the event loop thread via
    `asyncio.to_thread` (file I/O, DEV.md 工程原则 #3: 性能是需求 — must not block
    the loop), the `jones.db` read via `store.run_in_db_thread` (the shared
    connection is `check_same_thread=True` — only usable from its own dedicated
    thread, never the event loop or a generic `to_thread` worker), and — round-1
    review — runs the CPU-bound scan itself off-thread too, not just the I/O.
    `on_hit(code, message, detail)` is called (once, with every hit provider
    name) iff there's at least one hit — the caller wires this to `RpcServer`
    broadcasting `daemon.error` (00-foundation.md §4.2: "daemon.error ... 永不
    静默"); this function itself never silently returns "clean" without having
    actually looked at real data — vault access failures (VaultError) propagate,
    they are not swallowed into a false-clean result.

    Round-1 review: called once, this can't do what its own name promises —
    at true daemon startup (the moment `__main__.py` used to call this)
    `recent_response_samples` is necessarily empty (`RpcServer.serve_forever()`
    hasn't accepted a first client yet), so the "response samples" half of the
    contract silently scanned nothing, every run, forever. Call this repeatedly
    via `run_redaction_self_check_loop` below instead of once — see its
    docstring."""

    def _read_logs() -> list[str]:
        if not logs_dir.exists():
            return []
        return [_read_tail(path) for path in logs_dir.iterdir() if path.is_file()]

    def _load_keys() -> dict[str, str]:
        # Round-2 review: this used to call `vault.get(name)` once per
        # configured provider — each call independently re-reads and
        # re-decrypts the whole vault file (`Vault._read_entries()`), so N
        # configured providers meant N redundant full-vault decrypts every
        # single pass, forever. `entries()` reads it once.
        return vault.entries()

    configured_keys, log_texts, db_texts, payload_texts = await asyncio.gather(
        asyncio.to_thread(_load_keys),
        asyncio.to_thread(_read_logs),
        run_in_db_thread(_recent_db_texts, conn, limit=REDACTION_DB_SCAN_LIMIT),
        asyncio.to_thread(
            _recent_payload_texts, runs_dir, limit=REDACTION_RECENT_RUNS_SCANNED
        ),
    )
    response_texts = [
        b.decode("utf-8", errors="replace") if isinstance(b, bytes) else str(b)
        for b in recent_response_samples
    ]
    # Round-1 review: `scan_for_leaked_keys` (the join plus an 8-char sliding
    # window per configured key over the whole haystack) is pure CPU, not I/O —
    # unlike the reads above, it was never in the `to_thread` gather, so it ran
    # straight on the event loop. Log files are unbounded (see `rotate_logs`'s
    # own docstring: it *can't* truncate the daemon's own live
    # `daemon.out.log`/`daemon.err.log`), so this must not block RPC dispatch
    # while it scans however large those have grown.
    hits = await asyncio.to_thread(
        scan_for_leaked_keys,
        configured_keys,
        [*log_texts, *db_texts, *payload_texts, *response_texts],
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
    conn: sqlite3.Connection,
    runs_dir: Path,
    recent_response_samples: Callable[[], Iterable[bytes]],
    on_hit: Any,
    interval_s: float = REDACTION_CHECK_INTERVAL_S,
) -> None:
    """Background loop around `startup_key_redaction_self_check`, same
    started/cancelled-from-`__main__.py` lifecycle as `run_log_rotation_loop`
    above (04-w5-interfaces.md §1).

    Round-1 review: a single call at process startup is structurally unable to
    ever see a real RPC response — the daemon hasn't accepted its first client
    connection yet at the point `__main__.py` used to fire this once. Looping
    it, instead, means every pass after the first can actually see whatever
    responses have gone out since — the contract's "最近 100 条 RPC 响应样本"
    half of the check only ever does real work this way. The first iteration
    below still runs immediately (not after the first `interval_s` sleep, unlike
    `run_log_rotation_loop`): even with empty response samples, the log-file
    half of the check is worth running as early in startup as the surrounding
    async setup in `__main__.py` allows, not delayed by a full hour.

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
                    conn=conn,
                    runs_dir=runs_dir,
                    recent_response_samples=recent_response_samples(),
                    on_hit=on_hit,
                )
            except Exception:  # noqa: BLE001 - one bad pass must not kill the loop forever
                logger.error("key-redaction self-check pass failed", exc_info=True)
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        raise
