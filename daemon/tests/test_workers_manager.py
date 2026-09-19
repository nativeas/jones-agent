"""`WorkerManager` lifecycle tests against `fake_acp_agent.py` (docs/design/
00-foundation.md §8.1, 01-w2-interfaces.md §2 — isolated HERMES_HOME, the
YOLO/SAFE_MODE env strip, and the fail-closed startup self-check)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from jones_daemon.workers.manager import (
    DEFAULT_STARTUP_TIMEOUT_S,
    WorkerManager,
    WorkerStartupError,
    _prepare_hermes_home,
    _worker_env,
)

_FAKE_AGENT = str(Path(__file__).parent / "fake_acp_agent.py")


def _make_manager(tmp_path, **kwargs) -> WorkerManager:
    # `WorkerManager` builds each child's env from `os.environ` (`_worker_env`)
    # plus a future `extra_env` (provider keys) — there's no per-manager "mode"
    # knob, so tests select `fake_acp_agent.py`'s behavior via the
    # `FAKE_ACP_MODE` env var on *this* test process (set with
    # `monkeypatch.setenv` before calling this), which the child inherits.
    return WorkerManager(
        user_root=tmp_path,
        on_session_update=_noop_update,
        on_request_permission=_noop_permission,
        on_worker_crash=kwargs.pop("on_worker_crash", _noop_crash),
        worker_cmd=[sys.executable, _FAKE_AGENT],
        startup_timeout_s=kwargs.pop("startup_timeout_s", 5.0),
        idle_timeout_s=kwargs.pop("idle_timeout_s", 600.0),
    )


async def _noop_update(_session_id, _params):
    return None


async def _noop_permission(_session_id, _params):
    return {"outcome": {"outcome": "cancelled"}}


_crashes: list[tuple[str, int | None]] = []


async def _noop_crash(session_id, returncode):
    _crashes.append((session_id, returncode))


@pytest.fixture(autouse=True)
def _reset_crashes():
    _crashes.clear()
    yield
    _crashes.clear()


async def test_ensure_started_passes_self_check_and_isolates_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_ACP_MODE", "normal")
    manager = _make_manager(tmp_path)
    await manager.start()
    try:
        worker = await manager.ensure_started("s1", cwd="/tmp")
        assert worker.acp_session_id == "fake-session-1"
        assert worker.hermes_home == tmp_path / "workers" / "s1" / "hermes"
        assert (worker.hermes_home / "plugins" / "jones_gate" / "plugin.yaml").exists()
        assert (worker.hermes_home / "config.yaml").exists()
        config_text = (worker.hermes_home / "config.yaml").read_text()
        assert "jones_gate" in config_text
        assert "approvals.mode: off" not in config_text
        # Fetching an already-started session returns the same worker, not a
        # second spawn (idempotent ensure_started).
        again = await manager.ensure_started("s1", cwd="/tmp")
        assert again is worker
    finally:
        await manager.stop()


async def test_worker_subprocess_env_never_carries_yolo_or_safe_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    monkeypatch.setenv("HERMES_SAFE_MODE", "1")
    monkeypatch.setenv("FAKE_ACP_MODE", "normal")
    env = _worker_env(tmp_path / "hermes_home")
    assert "HERMES_YOLO_MODE" not in env
    assert "HERMES_SAFE_MODE" not in env
    assert env["HERMES_HOME"] == str(tmp_path / "hermes_home")


async def test_startup_self_check_rejects_a_worker_whose_probe_completes(tmp_path, monkeypatch):
    # Simulates jones_gate having silently failed to load (HERMES_SAFE_MODE) —
    # the probe tool ran to completion instead of being blocked.
    monkeypatch.setenv("FAKE_ACP_MODE", "probe_completes")
    manager = _make_manager(tmp_path)
    await manager.start()
    try:
        with pytest.raises(WorkerStartupError, match="ran to completion"):
            await manager.ensure_started("s1", cwd="/tmp")
        assert manager.get("s1") is None
    finally:
        await manager.stop()


async def test_startup_self_check_rejects_a_failed_probe_without_the_gate_marker(
    tmp_path, monkeypatch
):
    """Round-1 review fix: a `status: failed` probe event isn't by itself proof
    jones_gate did the blocking — it's exactly what a *missing* plugin (probe
    tool never registered at all, so Hermes fails the call as unknown) looks
    like too. The self-check must refuse to deliver this worker, the same as
    `probe_completes`, not treat "some failure happened" as good enough."""
    monkeypatch.setenv("FAKE_ACP_MODE", "probe_fails_unverified")
    manager = _make_manager(tmp_path)
    await manager.start()
    try:
        with pytest.raises(WorkerStartupError, match="verified-blocked"):
            await manager.ensure_started("s1", cwd="/tmp")
        assert manager.get("s1") is None
    finally:
        await manager.stop()


async def test_startup_self_check_rejects_a_worker_that_never_calls_the_probe(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_ACP_MODE", "no_probe_call")
    manager = _make_manager(tmp_path, startup_timeout_s=1.0)
    await manager.start()
    try:
        with pytest.raises(WorkerStartupError):
            await manager.ensure_started("s1", cwd="/tmp")
        assert manager.get("s1") is None
    finally:
        await manager.stop()


async def test_startup_self_check_rejects_a_worker_that_hangs_on_initialize(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_ACP_MODE", "hang_init")
    manager = _make_manager(tmp_path, startup_timeout_s=0.5)
    await manager.start()
    try:
        with pytest.raises(WorkerStartupError):
            await manager.ensure_started("s1", cwd="/tmp")
    finally:
        await manager.stop()


async def test_prepare_hermes_home_failure_surfaces_as_worker_startup_error(tmp_path, monkeypatch):
    """Round-1 review fix: `_prepare_hermes_home` ran outside any try/except in
    `_spawn_and_check` — a filesystem error there (permissions, disk full, a
    concurrent `rmtree` racing us, ...) used to escape as a bare `OSError`, which
    `SessionService._run_turn`'s `except WorkerStartupError` never caught, leaving
    the Run stuck 'running' forever with no `run.terminated` (contract §7 诚实失败).
    """
    monkeypatch.setenv("FAKE_ACP_MODE", "normal")

    def _boom(_hermes_home, **_kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr("jones_daemon.workers.manager._prepare_hermes_home", _boom)
    manager = _make_manager(tmp_path)
    await manager.start()
    try:
        with pytest.raises(WorkerStartupError, match="simulated disk failure"):
            await manager.ensure_started("s1", cwd="/tmp")
    finally:
        await manager.stop()


async def test_worker_crash_after_startup_invokes_the_crash_callback(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_ACP_MODE", "normal")
    manager = _make_manager(tmp_path)
    await manager.start()
    try:
        worker = await manager.ensure_started("s1", cwd="/tmp")
        worker.process.kill()
        await asyncio.wait_for(worker.process.wait(), timeout=5)
        # `_watch_exit` reacts to the process exit asynchronously — give it a
        # scheduling turn.
        for _ in range(50):
            if manager.get("s1") is None:
                break
            await asyncio.sleep(0.05)
        assert manager.get("s1") is None
        assert _crashes and _crashes[0][0] == "s1"
    finally:
        await manager.stop()


async def test_idle_worker_is_recycled_after_the_idle_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_ACP_MODE", "normal")
    manager = WorkerManager(
        user_root=tmp_path,
        on_session_update=_noop_update,
        on_request_permission=_noop_permission,
        on_worker_crash=_noop_crash,
        worker_cmd=[sys.executable, _FAKE_AGENT],
        idle_timeout_s=0.05,
    )
    # Speed up the reaper's own scan cadence for the test instead of waiting on
    # the real 30s production interval.
    import jones_daemon.workers.manager as manager_module

    monkeypatch.setattr(manager_module, "_IDLE_SCAN_INTERVAL_S", 0.05)
    await manager.start()
    try:
        await manager.ensure_started("s1", cwd="/tmp")
        for _ in range(60):
            if manager.get("s1") is None:
                break
            await asyncio.sleep(0.05)
        assert manager.get("s1") is None
    finally:
        await manager.stop()


async def test_a_busy_worker_is_never_reaped_even_past_the_idle_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_ACP_MODE", "normal")
    manager = WorkerManager(
        user_root=tmp_path,
        on_session_update=_noop_update,
        on_request_permission=_noop_permission,
        on_worker_crash=_noop_crash,
        worker_cmd=[sys.executable, _FAKE_AGENT],
        idle_timeout_s=0.05,
    )
    import jones_daemon.workers.manager as manager_module

    monkeypatch.setattr(manager_module, "_IDLE_SCAN_INTERVAL_S", 0.05)
    await manager.start()
    try:
        await manager.ensure_started("s1", cwd="/tmp")
        manager.mark_busy("s1", True)
        await asyncio.sleep(0.3)
        assert manager.get("s1") is not None
    finally:
        await manager.stop()


async def test_stop_worker_terminates_the_subprocess(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_ACP_MODE", "normal")
    manager = _make_manager(tmp_path)
    await manager.start()
    try:
        worker = await manager.ensure_started("s1", cwd="/tmp")
        process = worker.process
        await manager.stop_worker("s1", reason="test")
        assert manager.get("s1") is None
        await asyncio.wait_for(process.wait(), timeout=5)
        assert process.returncode is not None
    finally:
        await manager.stop()


@pytest.mark.parametrize("_run", range(5))
async def test_worker_startup_latency_measurement(tmp_path, monkeypatch, _run):
    """Not a pass/fail perf gate — records real startup latency samples against
    the fake agent so the PR report can cite an actual number (01-w2-interfaces.md
    §7: "worker 拉起时延...请在报告里给实测数字（假 ACP agent 也要测拉起时延）").
    A loose upper bound still guards against a startup that's egregiously slow
    (PRD 11.1's 2s target is for a *real* Hermes worker, not this double, so the
    bound here is generous headroom, not the target itself).
    """
    monkeypatch.setenv("FAKE_ACP_MODE", "normal")
    manager = _make_manager(tmp_path)
    await manager.start()
    try:
        await manager.ensure_started(f"s-{_run}", cwd="/tmp")
        assert manager.startup_latencies_s[-1] < DEFAULT_STARTUP_TIMEOUT_S
    finally:
        await manager.stop()


# -- `_prepare_hermes_home` mcp_servers/skill_dirs (Issue #17, FR13) -----------


def test_prepare_hermes_home_writes_mcp_servers_into_config_yaml(tmp_path):
    import yaml

    hermes_home = tmp_path / "hh"
    _prepare_hermes_home(
        hermes_home,
        mcp_servers=[
            {"name": "echo", "command": "python3", "args": ["server.py"], "env": {"A": "b"}},
            {"name": "docs", "url": "https://example.com/mcp", "headers": {}},
            {"name": "off", "command": "x", "enabled": False},
        ],
    )
    raw = (hermes_home / "config.yaml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(raw)
    assert parsed["plugins"]["enabled"] == ["jones_gate"]
    assert parsed["command_allowlist"] == []
    assert parsed["mcp_servers"] == {
        "echo": {"command": "python3", "args": ["server.py"], "env": {"A": "b"}},
        "docs": {"url": "https://example.com/mcp", "headers": {}},
    }


def test_prepare_hermes_home_with_no_mcp_servers_writes_empty_dict(tmp_path):
    import yaml

    hermes_home = tmp_path / "hh"
    _prepare_hermes_home(hermes_home)
    parsed = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))
    assert parsed["mcp_servers"] == {}


def test_prepare_hermes_home_symlinks_skill_dirs(tmp_path):
    real_skill = tmp_path / "some" / "research"
    real_skill.mkdir(parents=True)
    (real_skill / "SKILL.md").write_text("# research", encoding="utf-8")

    hermes_home = tmp_path / "hh"
    _prepare_hermes_home(hermes_home, skill_dirs=[real_skill])

    link = hermes_home / "skills" / "research"
    assert link.is_symlink()
    assert (link / "SKILL.md").read_text(encoding="utf-8") == "# research"


def test_prepare_hermes_home_skill_symlink_failure_does_not_raise(tmp_path, monkeypatch):
    def _boom(self, target, target_is_directory=False):
        raise OSError("simulated symlink failure")

    monkeypatch.setattr(Path, "symlink_to", _boom)
    hermes_home = tmp_path / "hh"
    _prepare_hermes_home(hermes_home, skill_dirs=[tmp_path / "some-skill"])  # must not raise
    assert (hermes_home / "skills").is_dir()
    assert not (hermes_home / "skills" / "some-skill").exists()


async def test_ensure_started_writes_the_mcp_servers_it_was_given(tmp_path, monkeypatch):
    """`ensure_started(..., mcp_servers=...)` (03-w4-interfaces.md §2; review
    round-2 finding #4 / controller ruling R-H4): the real spawn path writes
    an ALREADY-RESOLVED `mcp_servers` list into the worker's `config.yaml`.
    Resolving `ctx.config.mcp_servers(project_id)` itself is no longer
    `WorkerManager`'s job — see `ensure_started`'s own docstring for why (a
    real `ConfigResolver` does synchronous sqlite I/O that must not run on
    this class's event-loop thread) — that now happens in `sessions/
    service.py::_run_turn`, off-loop, before this method is ever called; see
    `test_sessions_service.py` for the caller-side degrade-on-failure
    coverage that scenario used to (incorrectly) live here as."""
    import yaml

    monkeypatch.setenv("FAKE_ACP_MODE", "normal")
    manager = _make_manager(tmp_path)
    await manager.start()
    try:
        worker = await manager.ensure_started(
            "s1", cwd="/tmp",
            mcp_servers=[{"name": "echo", "command": "python3", "args": [], "env": {}}],
        )
        parsed = yaml.safe_load((worker.hermes_home / "config.yaml").read_text(encoding="utf-8"))
        assert parsed["mcp_servers"] == {"echo": {"command": "python3", "args": [], "env": {}}}
    finally:
        await manager.stop()


async def test_ensure_started_without_mcp_servers_has_none(tmp_path, monkeypatch):
    """Every pre-existing test's `ensure_started(...)` call (no `mcp_servers=`)
    must keep behaving exactly as before this Issue's change: an empty
    `mcp_servers:` dict, never an error."""
    import yaml

    monkeypatch.setenv("FAKE_ACP_MODE", "normal")
    manager = _make_manager(tmp_path)
    await manager.start()
    try:
        worker = await manager.ensure_started("s1", cwd="/tmp")
        parsed = yaml.safe_load((worker.hermes_home / "config.yaml").read_text(encoding="utf-8"))
        assert parsed["mcp_servers"] == {}
    finally:
        await manager.stop()
