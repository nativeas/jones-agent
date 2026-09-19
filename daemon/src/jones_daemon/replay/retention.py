"""90-day (configurable) Run payload retention sweep — PRD 10.3 "Step 的大块
payload ... 默认保留 90 天，可配置", docs/design/02-w3-interfaces.md §2
"保留策略 90 天（settings.payload_retention_days），清理在空闲时跑".

Only ever deletes on-disk payload files (`replay/store.py::purge_run`) and clears
the DB's `payload_ref`/`prompt_snapshot_ref` pointers to them — Run/Step/
PermissionDecision *rows* are never touched here (PRD 10.3: those are permanent,
"回放的事实源"; only the large-payload attachment has a retention window).

`settings.payload_retention_days` isn't in `config/resolver.py::DEFAULT_SETTINGS`
(that file is C/#8#9's exclusive territory, not touched by this branch — see the
PR report's "契约变更" section) — `ConfigResolver.settings()` is a plain
`dict.update` merge of whatever JSON exists on disk, so a user who sets this key in
`settings.json` still gets it honored here via `.get(...)`; absent that, this
module's own `DEFAULT_RETENTION_DAYS` applies.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from jones_daemon.context import DaemonContext
from jones_daemon.logging import get_logger
from jones_daemon.replay import store as replay_store
from jones_daemon.sessions import queries
from jones_daemon.store import run_in_db_thread

logger = get_logger("replay.retention")

DEFAULT_RETENTION_DAYS = 90

# How often the idle-time sweep runs — not from settings (that config's job is
# "how long to keep", not "how often to check"); a slow poll is fine since payload
# deletion is not time-critical to the minute.
SWEEP_INTERVAL_S = 3600.0


def _retention_days(ctx: DaemonContext) -> int:
    settings = ctx.config.settings(None)
    value = settings.get("payload_retention_days", DEFAULT_RETENTION_DAYS)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return DEFAULT_RETENTION_DAYS
    return value


async def purge_expired_payloads(ctx: DaemonContext) -> int:
    """Delete on-disk payloads (and clear their DB refs) for every Run that ended
    before the retention cutoff and still has at least one payload on disk.
    Returns the number of Runs swept. A per-Run filesystem failure is logged and
    skipped (fail loud, not fail the whole sweep or fabricate success — DEV.md
    工程原则 #4) — its DB refs are deliberately left alone in that case, since they
    still point at a real (if undeletable) file."""
    days = _retention_days(ctx)
    cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    run_ids = await run_in_db_thread(queries.list_runs_with_payload_before, ctx.db, cutoff)
    purged = 0
    for run_id in run_ids:
        try:
            await asyncio.to_thread(replay_store.purge_run, ctx.paths.user_root(), run_id)
        except OSError:
            logger.error(
                "retention sweep: failed to delete payload directory for a Run",
                exc_info=True,
                extra={"detail": {"run_id": run_id}},
            )
            continue
        await run_in_db_thread(queries.clear_run_payload_refs, ctx.db, run_id)
        purged += 1
    if purged:
        logger.info(
            "retention sweep purged Run payloads",
            extra={"detail": {"count": purged, "retention_days": days}},
        )
    return purged


async def run_sweep_loop(ctx: DaemonContext) -> None:
    """Background loop started from `SessionService.startup()` / cancelled from
    `shutdown()` — "清理在空闲时跑": a low-frequency poll rather than tying
    payload cleanup to any per-Turn/per-Run hot path (DEV.md 工程原则 #3: 性能是
    需求 — this must never be on a request's critical path)."""
    try:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_S)
            try:
                await purge_expired_payloads(ctx)
            except Exception:  # noqa: BLE001 - one bad sweep must not kill the loop forever
                logger.error("retention sweep iteration failed", exc_info=True)
    except asyncio.CancelledError:
        raise
