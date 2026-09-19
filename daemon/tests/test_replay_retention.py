"""`replay/retention.py` — the 90-day (configurable) payload purge sweep
(Issue #12, docs/design/02-w3-interfaces.md §2: "保留策略 90 天
（settings.payload_retention_days），清理在空闲时跑")."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from jones_daemon.context import DaemonContext
from jones_daemon.replay import retention
from jones_daemon.replay import store as replay_store
from jones_daemon.sessions import queries
from jones_daemon.store import apply_pending, connect, run_in_db_thread


class _FakeServer:
    async def broadcast(self, session_id: str, method: str, params: Any) -> None:
        pass


class _StubConfig:
    """Minimal `ConfigResolver` — only `settings()` matters to this module."""

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self._settings = settings or {}

    def settings(self, project_id: str | None) -> dict[str, Any]:
        return dict(self._settings)

    def permissions(self, project_id: str | None) -> Any:
        return {}

    def mcp_servers(self, project_id: str | None) -> list[dict[str, Any]]:
        return []


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


async def _make_ctx(
    tmp_path, monkeypatch, *, settings: dict[str, Any] | None = None
) -> DaemonContext:
    monkeypatch.setenv("JONES_HOME", str(tmp_path))

    def _open() -> Any:
        conn = connect(tmp_path / "jones.db")
        apply_pending(conn)
        return conn

    conn = await run_in_db_thread(_open)
    from jones_daemon import paths

    return DaemonContext(
        db=conn, paths=paths, server=_FakeServer(), providers=None, config=_StubConfig(settings)
    )


def _seed_terminated_run(conn, *, session_id: str, run_id: str, ended_at_iso: str) -> None:
    queries.create_session(
        conn, session_id=session_id, project_id="proj_default", agent_id="agent_default",
        parent_id=None, is_main=False, mode="task", title="t",
    )
    turn_id = f"{run_id}-turn"
    queries.create_turn_and_user_message(
        conn, turn_id=turn_id, message_id=f"{run_id}-msg", session_id=session_id, text="x",
        queued=False,
    )
    queries.create_run(conn, run_id=run_id, turn_id=turn_id, session_id=session_id)
    conn.execute(
        "UPDATE runs SET status='terminated', ended_at=?, terminated_kind='error', "
        "terminated_reason='x' WHERE id=?",
        (ended_at_iso, run_id),
    )
    conn.commit()


async def test_purge_expired_payloads_deletes_old_runs_only(tmp_path, monkeypatch) -> None:
    ctx = await _make_ctx(tmp_path, monkeypatch)
    now = datetime.now(UTC)
    old_iso = _iso(now - timedelta(days=91))
    recent_iso = _iso(now - timedelta(days=1))

    def _seed(conn):
        _seed_terminated_run(conn, session_id="s-old", run_id="run-old", ended_at_iso=old_iso)
        _seed_terminated_run(
            conn, session_id="s-recent", run_id="run-recent", ended_at_iso=recent_iso
        )
        queries.insert_step(
            conn, step_id="step-old", run_id="run-old", seq=1, tool="t", args={}, status="completed"
        )
        queries.update_step(
            conn, "step-old", status=None, result_summary=None, duration_ms=None,
            payload_ref="run-old/1.json",
        )
        queries.insert_step(
            conn, step_id="step-recent", run_id="run-recent", seq=1, tool="t", args={},
            status="completed",
        )
        queries.update_step(
            conn, "step-recent", status=None, result_summary=None, duration_ms=None,
            payload_ref="run-recent/1.json",
        )

    await run_in_db_thread(_seed, ctx.db)
    replay_store.write_payload(ctx.paths.user_root(), "run-old", 1, b"old data", "json")
    replay_store.write_payload(ctx.paths.user_root(), "run-recent", 1, b"recent data", "json")

    purged = await retention.purge_expired_payloads(ctx)

    assert purged == 1
    assert not (ctx.paths.user_root() / "runs" / "run-old").exists()
    assert (ctx.paths.user_root() / "runs" / "run-recent").exists()

    old_step = await run_in_db_thread(queries.get_step, ctx.db, "step-old")
    recent_step = await run_in_db_thread(queries.get_step, ctx.db, "step-recent")
    assert old_step["payload_ref"] is None
    assert recent_step["payload_ref"] == "run-recent/1.json"


async def test_purge_expired_payloads_skips_a_run_with_no_payload_on_disk(
    tmp_path, monkeypatch
) -> None:
    """`list_runs_with_payload_before` only returns candidates that still have a
    ref to clean up — a Run that ended long ago but never had a payload (all
    read-only Steps, say) must not be treated as a purge candidate at all."""
    ctx = await _make_ctx(tmp_path, monkeypatch)
    old_iso = _iso(datetime.now(UTC) - timedelta(days=200))

    def _seed(conn):
        _seed_terminated_run(conn, session_id="s1", run_id="run-no-payload", ended_at_iso=old_iso)

    await run_in_db_thread(_seed, ctx.db)

    purged = await retention.purge_expired_payloads(ctx)
    assert purged == 0


async def test_retention_days_defaults_to_90_when_unset_or_invalid(tmp_path, monkeypatch) -> None:
    ctx = await _make_ctx(tmp_path, monkeypatch, settings={})
    assert retention._retention_days(ctx) == 90  # noqa: SLF001 - internal, unit-tested directly

    ctx2 = await _make_ctx(tmp_path, monkeypatch, settings={"payload_retention_days": -5})
    assert retention._retention_days(ctx2) == 90  # noqa: SLF001

    ctx3 = await _make_ctx(
        tmp_path, monkeypatch, settings={"payload_retention_days": "not a number"}
    )
    assert retention._retention_days(ctx3) == 90  # noqa: SLF001


async def test_retention_days_honors_a_configured_override(tmp_path, monkeypatch) -> None:
    ctx = await _make_ctx(tmp_path, monkeypatch, settings={"payload_retention_days": 7})
    assert retention._retention_days(ctx) == 7  # noqa: SLF001

    # And it actually changes sweep behavior, not just the number itself: a Run
    # that ended 10 days ago is expired under a 7-day policy.
    old_iso = _iso(datetime.now(UTC) - timedelta(days=10))

    def _seed(conn):
        _seed_terminated_run(conn, session_id="s1", run_id="run-10d", ended_at_iso=old_iso)
        queries.insert_step(
            conn, step_id="step-10d", run_id="run-10d", seq=1, tool="t", args={}, status="completed"
        )
        queries.update_step(
            conn, "step-10d", status=None, result_summary=None, duration_ms=None,
            payload_ref="run-10d/1.json",
        )

    await run_in_db_thread(_seed, ctx.db)
    replay_store.write_payload(ctx.paths.user_root(), "run-10d", 1, b"x", "json")

    purged = await retention.purge_expired_payloads(ctx)
    assert purged == 1


async def test_run_sweep_loop_purges_periodically_and_stops_cleanly(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(retention, "SWEEP_INTERVAL_S", 0.01)
    ctx = await _make_ctx(tmp_path, monkeypatch)
    old_iso = _iso(datetime.now(UTC) - timedelta(days=91))

    def _seed(conn):
        _seed_terminated_run(conn, session_id="s1", run_id="run-loop", ended_at_iso=old_iso)
        queries.insert_step(
            conn, step_id="step-loop", run_id="run-loop", seq=1, tool="t", args={},
            status="completed",
        )
        queries.update_step(
            conn, "step-loop", status=None, result_summary=None, duration_ms=None,
            payload_ref="run-loop/1.json",
        )

    await run_in_db_thread(_seed, ctx.db)
    replay_store.write_payload(ctx.paths.user_root(), "run-loop", 1, b"x", "json")

    task = asyncio.create_task(retention.run_sweep_loop(ctx))
    try:
        for _ in range(200):
            if not (ctx.paths.user_root() / "runs" / "run-loop").exists():
                break
            await asyncio.sleep(0.01)
        assert not (ctx.paths.user_root() / "runs" / "run-loop").exists()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
