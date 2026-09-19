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
import shutil
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from jones_daemon.config.ids import now_iso
from jones_daemon.logging import get_logger
from jones_daemon.replay import store as replay_store
from jones_daemon.rpc.errors import INVALID_STATE, NOT_FOUND, RpcError

logger = get_logger("store.maintenance")


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
    queue_items 的 SQLite 行 + 每个关联 Run 的 `runs/<id>/` payload 目录，最后
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

    for run_id in run_ids:
        replay_store.purge_run(user_root, run_id)

    checkpoint_truncate_or_raise(conn)


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

    replay_store.purge_run(user_root, run_id)
    checkpoint_truncate_or_raise(conn)


# --- Project real-delete (already existed as ProjectService.delete; this is the ---
# --- "storage mechanics" half it now calls, per 04-w5-interfaces.md §5 "project. --
# --- delete（已有，改为调 maintenance）" -------------------------------------------


def delete_project(conn: sqlite3.Connection, user_root: Path, project_id: str) -> None:
    """Refuses if any Session or Agent still references `project_id` (same rule
    `ProjectService.delete` enforced inline before this existed — moved here
    verbatim, not changed), then deletes the `projects` row, the Project's
    attachments directory (`paths.project_attachments_dir`), and checkpoints.
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

    conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    conn.commit()

    attachments_dir = user_root / "projects" / project_id
    if attachments_dir.exists():
        shutil.rmtree(attachments_dir)

    checkpoint_truncate_or_raise(conn)


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

    `delete_after=True` calls `delete_session` only after the export file is fully
    written and closed — export-then-delete, never the reverse, so a crash between
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
    dest_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")

    if delete_after:
        delete_session(conn, user_root, session_id)

    return dest_path


# --- Backup / log / cache housekeeping (04-w5-interfaces.md §5) ------------------

MAX_BACKUPS = 5
LOG_RETENTION_DAYS = 7
LOG_ROTATION_INTERVAL_S = 3600.0  # matches replay/retention.py's idle-sweep cadence


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

    Caveat (honest, not silently glossed over): the daemon's two live log files
    (`daemon.out.log`/`daemon.err.log`, launchd's `StandardOutPath`/
    `StandardErrorPath` — see `service.py::render_plist`) are continuously
    appended to by the OS redirect for as long as the daemon runs, so their mtime
    never falls behind `retention_days` while the process is up — this sweep
    can't truncate a file that's open and being written by another process
    without an out-of-band log-reopen signal (SIGHUP-style), which is out of this
    issue's scope. What this *does* clean up: any other, non-continuously-written
    file that lands under `logs_dir` (a one-shot diagnostic dump, a rotated
    `.log.1` some future logrotate-alike produces) once it's actually stale."""
    cutoff = time.time() - retention_days * 86400
    removed = 0
    if not logs_dir.exists():
        return 0
    for path in logs_dir.iterdir():
        if not path.is_file():
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


def scan_for_leaked_keys(
    configured_keys: dict[str, str],
    haystacks: Iterable[str],
) -> list[str]:
    """Runtime guard behind G03 ("全文 grep 已配置的 Key 字符串, 命中数为 0") /
    N02 — this is the *daemon's own* self-check at startup, not just a test-suite
    grep (04-w5-interfaces.md §5: "启动期 Key 脱敏自检 ... 命中即 daemon.error").

    `configured_keys` is `{provider_name: full_key_value}` (from `Vault.names()` +
    `Vault.get()`); `haystacks` is every string to scan (log file contents, recent
    RPC response samples — see `rpc/server.py`'s response ring buffer). For each
    key of at least 8 characters, slides an 8-character window across it (the
    contract's "任意 8 字节子串" — deliberately finer-grained than "does the whole
    key appear", since a wider window could miss a key that got truncated
    mid-string by some upstream formatting bug) and checks membership in the
    concatenated haystacks.

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


async def startup_key_redaction_self_check(
    *,
    vault: Any,
    logs_dir: Path,
    recent_response_samples: Iterable[bytes],
    on_hit: Any,
) -> list[str]:
    """Wires `scan_for_leaked_keys` to real inputs at daemon startup: every log
    file under `logs_dir`, plus the last ~100 RPC response samples the server
    already buffers. Reads log files off the event loop thread via
    `asyncio.to_thread` (file I/O, DEV.md 工程原则 #3: 性能是需求 — must not block
    the loop during startup). `on_hit(code, message, detail)` is called (once,
    with every hit provider name) iff there's at least one hit — the caller wires
    this to `RpcServer` broadcasting `daemon.error` (00-foundation.md §4.2:
    "daemon.error ... 永不静默"); this function itself never silently returns
    "clean" without having actually looked at real data — vault access failures
    (VaultError) propagate, they are not swallowed into a false-clean result.
    """

    def _read_logs() -> list[str]:
        if not logs_dir.exists():
            return []
        out = []
        for path in logs_dir.iterdir():
            if not path.is_file():
                continue
            try:
                out.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                logger.warning(
                    "redaction self-check: could not read a log file, skipping",
                    extra={"detail": {"path": str(path)}},
                )
        return out

    def _load_keys() -> dict[str, str]:
        return {name: vault.get(name) or "" for name in vault.names()}

    configured_keys, log_texts = await asyncio.gather(
        asyncio.to_thread(_load_keys), asyncio.to_thread(_read_logs)
    )
    response_texts = [
        b.decode("utf-8", errors="replace") if isinstance(b, bytes) else str(b)
        for b in recent_response_samples
    ]
    hits = scan_for_leaked_keys(configured_keys, [*log_texts, *response_texts])
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
