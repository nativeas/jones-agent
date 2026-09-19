"""send → first `message.delta` runtime overhead (docs/design/05-w6-interfaces.md
§3.1 "send → 首条 message.delta 运行时开销").

Measured against a worker that is already warm (started in a fixture step before
timing begins) — cold-start cost is `test_worker_cold_start.py`'s job, not this
one's; double-counting it here would conflate two different PRD 11.1 rows. What
this isolates is: `AcpClient.prompt()` round trip + jones-daemon's own dispatch of
the resulting `session/update` notification back up to the `on_session_update`
callback — i.e. "运行时开销" per the design doc's own phrasing, with the fake
agent's near-zero "model" turnaround standing in for PRD 11.1's "模型 RTT" term
(which this test cannot measure without a real provider call).

Threshold: PRD 11.1 "首个 Turn 首 token ≤ 模型 RTT + 800 ms" — 800ms is explicitly
"运行时 + worker 拉起 + 记忆检索的总开销上限"; since worker startup is excluded here
(see above), 800ms is a conservative (looser, not tighter) ceiling for this
narrower measurement.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

from jones_daemon.workers.manager import WorkerManager

_FAKE_AGENT = str(Path(__file__).resolve().parents[1] / "fake_acp_agent.py")
_THRESHOLD_MS = 800.0


async def test_send_to_first_delta(tmp_path, monkeypatch, perf_record):
    monkeypatch.setenv("FAKE_ACP_MODE", "normal")
    first_delta_at: list[float] = []
    got_first_delta = asyncio.Event()

    async def _on_update(_session_id: str, params: dict[str, Any]) -> None:
        update = params.get("update", params)  # tolerate either shape, see AcpClient
        if update.get("sessionUpdate") == "agent_message_chunk" and not first_delta_at:
            first_delta_at.append(time.monotonic())
            got_first_delta.set()

    async def _noop_permission(_session_id, _params):
        return {"outcome": {"outcome": "cancelled"}}

    async def _noop_crash(_session_id, _returncode):
        return None

    manager = WorkerManager(
        user_root=tmp_path,
        on_session_update=_on_update,
        on_request_permission=_noop_permission,
        on_worker_crash=_noop_crash,
        worker_cmd=[sys.executable, _FAKE_AGENT],
        startup_timeout_s=5.0,
        idle_timeout_s=600.0,
    )
    await manager.start()
    try:
        worker = await manager.ensure_started("s1", cwd=str(tmp_path))  # warm-up, not timed

        assert worker.client is not None
        assert worker.acp_session_id is not None
        start = time.monotonic()
        prompt_task = asyncio.create_task(
            worker.client.prompt(worker.acp_session_id, "hello, any non-marker text")
        )
        await asyncio.wait_for(got_first_delta.wait(), timeout=5.0)
        elapsed_ms = (first_delta_at[0] - start) * 1000
        await prompt_task  # let the turn finish cleanly before manager.stop() below

        passed = perf_record(
            "send_to_first_delta_ms",
            elapsed_ms,
            "ms",
            _THRESHOLD_MS,
            "PRD 11.1 首个 Turn 首 token ≤ 模型 RTT + 800ms（此处 RTT≈0，仅测运行时开销）",
            detail={"agent": "fake_acp_agent.py", "worker": "pre-warmed"},
        )
        assert passed, f"send→first delta {elapsed_ms:.1f}ms exceeds {_THRESHOLD_MS}ms"
    finally:
        await manager.stop()
