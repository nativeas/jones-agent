"""Worker cold start: `WorkerManager.ensure_started()` spawn → passed self-check
(docs/design/05-w6-interfaces.md §3.1 "worker 拉起（假 ACP agent 与真实 Hermes 两档，
后者 JONES_E2E）").

Compared against PRD 11.1's "冷启动 worker ≤ 2 s" row ("新 Session 首次拉起 Python
worker 子进程；后续复用"). `workers/manager.py`'s own `DEFAULT_STARTUP_TIMEOUT_S`
comment already points here for "measured numbers against the fake test agent" —
this is that measurement.

Two tiers, matching the design doc:
- `test_worker_cold_start_fake_agent`: always runs, against `fake_acp_agent.py`
  (stdlib-only double, same one `test_workers_manager.py` uses) — this is the
  number that actually gates `make check-daemon`.
- `test_worker_cold_start_real_hermes`: gated behind `JONES_E2E=1` *and*
  `ANTHROPIC_API_KEY` (same double-gate `tests/integration/test_real_hermes_e2e.py`
  uses — confirmed by running it here: Hermes's `AIAgent` init validates the
  configured provider/model before `session/new` even returns, well before any
  prompt is sent, so a real key is load-bearing for this tier even though the
  measured window never issues a real model call) and skips itself when
  `hermes-agent` isn't importable — real worker startup includes Hermes's own
  `discover_and_load()`/plugin/tool registration, which the fake agent doesn't
  exercise at all, so this is the only tier that actually measures what a real
  user's first Session hits.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

from jones_daemon.workers.manager import WorkerManager

_FAKE_AGENT = str(Path(__file__).resolve().parents[1] / "fake_acp_agent.py")
_THRESHOLD_MS = 2000.0


async def _noop_update(_session_id, _params):
    return None


async def _noop_permission(_session_id, _params):
    return {"outcome": {"outcome": "cancelled"}}


async def _noop_crash(_session_id, _returncode):
    return None


async def test_worker_cold_start_fake_agent(tmp_path, monkeypatch, perf_record):
    monkeypatch.setenv("FAKE_ACP_MODE", "normal")
    manager = WorkerManager(
        user_root=tmp_path,
        on_session_update=_noop_update,
        on_request_permission=_noop_permission,
        on_worker_crash=_noop_crash,
        worker_cmd=[sys.executable, _FAKE_AGENT],
        startup_timeout_s=5.0,
        idle_timeout_s=600.0,
    )
    await manager.start()
    try:
        start = time.monotonic()
        worker = await manager.ensure_started("s1", cwd=str(tmp_path))
        elapsed_ms = (time.monotonic() - start) * 1000
        assert worker.acp_session_id is not None

        passed = perf_record(
            "worker_cold_start_fake_agent_ms",
            elapsed_ms,
            "ms",
            _THRESHOLD_MS,
            "PRD 11.1 冷启动 worker ≤ 2s",
            detail={"agent": "fake_acp_agent.py"},
        )
        assert passed, (
            f"worker cold start (fake agent) {elapsed_ms:.1f}ms exceeds {_THRESHOLD_MS}ms"
        )
    finally:
        await manager.stop()


def _hermes_importable() -> bool:
    return importlib.util.find_spec("hermes_cli") is not None


async def test_worker_cold_start_real_hermes(tmp_path, perf_record):
    import pytest

    if not (os.environ.get("JONES_E2E") == "1" and os.environ.get("ANTHROPIC_API_KEY")):
        pytest.skip(
            "set JONES_E2E=1 and ANTHROPIC_API_KEY to run against a real Hermes worker "
            "(needs `uv sync --group worker`) — see tests/integration/test_real_hermes_e2e.py"
        )
    if not _hermes_importable():
        pytest.skip("hermes-agent not importable — `uv sync --group worker` was not run")

    manager = WorkerManager(
        user_root=tmp_path,
        on_session_update=_noop_update,
        on_request_permission=_noop_permission,
        on_worker_crash=_noop_crash,
        worker_cmd=[sys.executable, "-m", "acp_adapter.entry"],
        startup_timeout_s=20.0,
        idle_timeout_s=600.0,
    )
    await manager.start()
    try:
        start = time.monotonic()
        worker = await manager.ensure_started("s1", cwd=str(tmp_path))
        elapsed_ms = (time.monotonic() - start) * 1000
        assert worker.acp_session_id is not None

        passed = perf_record(
            "worker_cold_start_real_hermes_ms",
            elapsed_ms,
            "ms",
            _THRESHOLD_MS,
            "PRD 11.1 冷启动 worker ≤ 2s",
            detail={"agent": "real hermes-agent (acp_adapter.entry)"},
        )
        assert passed, (
            f"worker cold start (real hermes) {elapsed_ms:.1f}ms exceeds {_THRESHOLD_MS}ms"
        )
    finally:
        await manager.stop()
