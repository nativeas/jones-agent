"""Real-Hermes end-to-end check (docs/design/01-w2-interfaces.md §2: "真实 Hermes
的端到端放 tests/integration/，用 JONES_E2E=1 门控（需要模型 Key，CI 不跑）").

Gated on `JONES_E2E=1` *and* `ANTHROPIC_API_KEY` actually being present in this
process's env — `JONES_E2E` alone isn't enough to justify a real network call
against a real model (01-w2-interfaces.md §3.1 has anthropic first in its
vendor table, hence the choice of which key to check for).

Known, documented gap this test works around rather than silently depends on:
`WorkerManager._prepare_hermes_home` (workers/manager.py) does not yet write
any `model:`/`providers:` block into a worker's `config.yaml` — wiring
`ProviderResolver.resolve()`'s `ProviderBinding.hermes_config`/`.env` into a
real worker launch is left as a documented gap (see sessions/service.py's
module docstring and this PR's report "契约变更"/"没做什么" sections). This test
therefore monkeypatches that one file in after `_prepare_hermes_home` runs,
using the exact shape 01-w2-interfaces.md §3.1 documents for `anthropic`,
rather than pretending the real wiring exists.
"""

from __future__ import annotations

import os

import pytest

from jones_daemon.workers import manager as manager_module
from jones_daemon.workers.manager import WorkerManager

pytestmark = pytest.mark.skipif(
    not (os.environ.get("JONES_E2E") and os.environ.get("ANTHROPIC_API_KEY")),
    reason="real-Hermes e2e: set JONES_E2E=1 and ANTHROPIC_API_KEY to run (see module docstring)",
)


async def test_real_worker_starts_and_completes_a_trivial_turn(tmp_path):
    real_prepare = manager_module._prepare_hermes_home

    def _prepare_with_model_config(hermes_home):
        real_prepare(hermes_home)
        config_path = hermes_home / "config.yaml"
        # Appended, not replacing what real_prepare wrote (plugins.enabled must
        # survive) — the exact shape 01-w2-interfaces.md §3.1 documents for
        # anthropic: no `providers:` block needed, just `model.provider`.
        with config_path.open("a", encoding="utf-8") as fh:
            # Model id from providers/catalog.py's own VENDORS["anthropic"] (the
            # cheapest of the three, since this only needs to prove the wire
            # works end to end, not produce a useful answer).
            fh.write("model:\n  default: claude-haiku-4-6\n  provider: anthropic\n")

    manager_module._prepare_hermes_home = _prepare_with_model_config

    events: list[dict] = []

    async def on_update(_session_id, params):
        events.append(params)

    async def on_permission(_session_id, params):
        options = params.get("options") or []
        allow = next((o for o in options if "allow" in o.get("kind", "")), options[0])
        return {"outcome": {"outcome": "selected", "optionId": allow["optionId"]}}

    async def on_crash(_session_id, _returncode):
        pass

    manager = WorkerManager(
        user_root=tmp_path,
        on_session_update=on_update,
        on_request_permission=on_permission,
        on_worker_crash=on_crash,
        # Default worker_cmd: `[sys.executable, "-m", "acp_adapter.entry"]` —
        # the real Hermes ACP server, not the fake agent.
        startup_timeout_s=30.0,
    )
    await manager.start()
    try:
        worker = await manager.ensure_started("e2e-1", cwd=str(tmp_path))
        assert worker.acp_session_id is not None
        response = await worker.client.prompt(
            worker.acp_session_id, "Reply with exactly one word: hello"
        )
        assert response.get("stopReason") in ("end_turn", "cancelled")
        deltas = [
            e for e in events
            if e.get("update", {}).get("sessionUpdate") == "agent_message_chunk"
        ]
        assert deltas, "expected at least one streamed message.delta from a real Turn"
    finally:
        manager_module._prepare_hermes_home = real_prepare
        await manager.stop()
