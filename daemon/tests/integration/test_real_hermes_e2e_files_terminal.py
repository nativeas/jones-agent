"""Real-Hermes end-to-end check for Issue #13/#14 (FR07 file five-piece +
FR08 terminal), gated the same way `test_real_hermes_e2e.py` is
(`JONES_E2E=1` and one configured vendor key present — see that file's module
docstring for the full rationale, unchanged here).

`test_real_hermes_terminal_stop_leaves_no_orphan_process` (Issue #41): run 8
consecutive times against a real DeepSeek worker (`~/.hermes/.env`'s
`DEEPSEEK_API_KEY`, user-authorized for this branch) — see this PR's report
for the captured per-run output. It now verifies "Jones's own G09 fallback
(`SessionService.stop()`'s Issue #41 reaping) actually reaps it", NOT
"Hermes's own cancel-driven cleanup is clean" — that half remains genuinely
flaky against a real model (Issue #41's own repro: ~17%, a child left behind
in its own session/pgid, unreachable by `WorkerManager`'s pgid-based
`killpg`), and is exactly the gap this issue's fallback exists to cover
rather than depend on. The test drives the real, user-visible `SessionService
.stop()` (not `WorkerManager`/`AcpClient.cancel()` directly, unlike this
file's other test) — that's the one path Issue #41's controller ruling (R3)
wires the fallback into.

    JONES_E2E=1 ANTHROPIC_API_KEY=... uv run pytest -q \\
        tests/integration/test_real_hermes_e2e_files_terminal.py

Directory traversal ("目录遍历", FR07): source-verified against the
installed `hermes-agent` (`toolsets.py::_HERMES_CORE_TOOLS`) that there is
NO dedicated directory-listing tool in Hermes's core toolset — the closest
available primitives are `terminal ls`/`search_files`, exactly the fallback
docs/design/03-w4-interfaces.md §3 names ("核对 Hermes 用哪个工具遍历目录，
若无则 terminal ls/search_files 组合"). This test exercises the fallback via
`terminal ls`.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import pytest

from jones_daemon.context import DaemonContext
from jones_daemon.context import ProviderResolver as ProviderResolverProtocol
from jones_daemon.projects.bootstrap import bootstrap_projects_and_agents
from jones_daemon.projects.service import ProjectService
from jones_daemon.sessions.service import DEFAULT_AGENT_ID, SessionService
from jones_daemon.store import apply_pending, connect, run_in_db_thread
from jones_daemon.workers import manager as manager_module
from jones_daemon.workers.manager import WorkerManager

from . import _provider_gate

pytestmark = pytest.mark.skipif(
    not (os.environ.get("JONES_E2E") and _provider_gate.configured_vendor()),
    reason=_provider_gate.NEEDS_REAL_MODEL_REASON,
)


# Captured at import time, BEFORE any test monkeypatches `manager_module.
# _prepare_hermes_home` — bugfix, real-Hermes verification (this file was
# never actually run against a real model before now, per its own module
# docstring): `_with_model_config` used to look up `manager_module.
# _prepare_hermes_home` dynamically from inside its own body instead, which —
# once a test patches that name TO `_with_model_config` itself — resolves to
# itself and recurses infinitely. `test_real_hermes_e2e.py`'s equivalent
# closure (`real_prepare` captured as a local before patching) doesn't have
# this bug; this module-level version needs the same one-time capture.
_REAL_PREPARE_HERMES_HOME = manager_module._prepare_hermes_home


def _with_model_config(hermes_home, **kwargs):
    """Same monkeypatch `test_real_hermes_e2e.py` uses — `_prepare_hermes_home`
    doesn't yet wire a real provider Key into a worker's `config.yaml` (see
    that file's own docstring); this appends the anthropic block
    01-w2-interfaces.md §3.1 documents.

    Second, previously-undiscovered gap found while first actually running
    this file against a real model (Issue #38 fixed the self-check that used
    to block every real-Hermes E2E run before it got this far — see the PR
    report): `WorkerManager` alone never writes `<HERMES_HOME>/jones_gate.json`
    either (that's `permissions/gate_config.py::build`, called from
    `SessionService.send()`, which none of these `WorkerManager`-only E2E
    tests ever go through — same "documented gap this test works around"
    shape as the model-config block above, just not yet documented). Without
    it, `kernel/plugin/jones_gate/_config.py::load()` returns `FAIL_CLOSED`
    and `_decide()` blocks EVERY tool call, not just the startup probe (PRD
    5.5's fail-closed default) — confirmed against a real DeepSeek worker:
    the model correctly reported a tool failure result back to the user
    ("a safety gate... failing closed") rather than silently doing nothing,
    it just never got to actually write a file. A minimal, permissive config
    (`mode: "task"`, everything else empty/unrestricted) is enough for these
    tests, which only care whether the TOOLS themselves work end-to-end —
    FR05's actual gate behavior has its own dedicated coverage elsewhere in
    this test suite."""
    _REAL_PREPARE_HERMES_HOME(hermes_home, **kwargs)
    config_path = hermes_home / "config.yaml"
    with config_path.open("a", encoding="utf-8") as fh:
        fh.write(_provider_gate.model_config_block())
    gate_config = {
        "mode": "task",
        "user_root": str(hermes_home),
        "project_permissions_path": None,
        "rules": [],
        "rules_degraded": False,
        "tool_allowlist": [],
    }
    (hermes_home / "jones_gate.json").write_text(
        json.dumps(gate_config), encoding="utf-8"
    )


async def _make_manager(tmp_path, *, startup_timeout_s: float = 30.0) -> WorkerManager:
    events: list[dict] = []

    async def on_update(_session_id, params):
        events.append(params)

    async def on_permission(_session_id, params):
        # auto mode: allow everything so the Turn actually runs to
        # completion — this test is about the TOOLS working, not FR05's
        # gates (covered elsewhere in this branch's test suite).
        options = params.get("options") or []
        allow = next((o for o in options if "allow" in o.get("kind", "")), options[0])
        return {"outcome": {"outcome": "selected", "optionId": allow["optionId"]}}

    async def on_crash(_session_id, _returncode):
        pass

    manager = WorkerManager(
        user_root=tmp_path, on_session_update=on_update,
        on_request_permission=on_permission, on_worker_crash=on_crash,
        startup_timeout_s=startup_timeout_s,
    )
    manager.events = events  # type: ignore[attr-defined]
    await manager.start()
    return manager


class _FakeServer:
    """Minimal `ctx.server` double — same shape `tests/test_cap_terminal_
    stop_cancel.py`'s own copy uses, duplicated here rather than imported
    (this integration test file has no dependency on the unit test tree, by
    design — same reasoning `_make_manager` above already applies to
    `WorkerManager`'s on_update/on_permission callbacks)."""

    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, str, Any]] = []

    async def broadcast(self, session_id: str, method: str, params: Any) -> None:
        self.broadcasts.append((session_id, method, params))


class _StubProviderResolver(ProviderResolverProtocol):
    """`SessionService.__init__` requires one; the real provider Key/config
    for the worker itself is wired by `_with_model_config` below (appending
    straight to `config.yaml`, the same mechanism this file's other test
    already uses) — this stub is never actually consulted for that, `_run_
    turn`'s own resolve-then-discard pre-flight check (module docstring)
    just needs something that doesn't raise."""

    def resolve(self, model_pref: dict[str, Any] | None) -> Any:
        return {"provider": "anthropic", "model": "claude-test", "env": {}, "hermes_config": {}}

    def list_models(self, provider: str | None) -> list[dict[str, Any]]:
        return []


class _NullConfigResolver:
    def settings(self, project_id: str | None) -> dict[str, Any]:
        return {}

    def permissions(self, project_id: str | None) -> dict[str, Any]:
        return {}

    def mcp_servers(self, project_id: str | None) -> list[dict[str, Any]]:
        return []


async def _make_real_service(tmp_path, monkeypatch) -> SessionService:
    """A full `SessionService`, NOT `WorkerManager` alone (unlike `_make_
    manager` above) — Issue #41's fallback (`WorkerManager.reap_stop_
    orphans`) is wired into `SessionService.stop()`, the real user-visible
    path, so proving it against a real worker needs to go through `stop()`
    itself, not `AcpClient.cancel()` called directly on a bare `Worker`.
    `worker_cmd` is left at `SessionService`'s own default (real Hermes,
    `sys.executable -m acp_adapter.entry`) — no fake-agent override.

    `JONES_HOME` MUST be pinned to `tmp_path` here (unlike `_make_manager`
    above, which never touches it because a bare `WorkerManager` is handed
    its `user_root` directly as a constructor arg) — `SessionService.
    __init__` instead reads it off `ctx.paths.user_root()`
    (`paths.py::user_root()`'s own docstring: "honoring the JONES_HOME
    override"), which falls back to the REAL `~/.jones` when unset. Found
    the hard way while validating this test for R5: every worker this
    service spawns landed under the ACTUAL user's `~/.jones/runtime/
    workers/<session_id>/`, not `tmp_path` — real disk pollution (tirith
    binaries and all) in the user's real Jones home, cleaned up by hand
    once caught. `monkeypatch.setenv` here is what the fake-agent-backed
    harnesses elsewhere in this test suite (`tests/test_cap_terminal_stop_
    cancel.py::_make_service` et al.) already do for exactly this reason."""
    monkeypatch.setenv("JONES_HOME", str(tmp_path))

    def _open() -> Any:
        conn = connect(tmp_path / "jones.db")
        apply_pending(conn)
        bootstrap_projects_and_agents(conn)
        return conn

    conn = await run_in_db_thread(_open)
    from jones_daemon import paths

    ctx = DaemonContext(
        db=conn, paths=paths, server=_FakeServer(),
        providers=_StubProviderResolver(), config=_NullConfigResolver(),
    )
    service = SessionService(ctx)
    await service.worker_manager.start()
    return service


async def test_real_hermes_five_piece_file_tools_and_directory_listing(tmp_path):
    real_prepare = manager_module._prepare_hermes_home
    manager_module._prepare_hermes_home = _with_model_config
    manager = await _make_manager(tmp_path)
    try:
        project = tmp_path / "project"
        project.mkdir()
        worker = await manager.ensure_started("e2e-files-1", cwd=str(project))

        # write_file
        await worker.client.prompt(
            worker.acp_session_id,
            f"Use the write_file tool to create {project / 'a.txt'} with exactly the "
            "content 'hello jones' (no extra text). Do not explain, just call the tool.",
        )
        assert (project / "a.txt").exists(), "write_file did not create the file"

        # read_file
        response = await worker.client.prompt(
            worker.acp_session_id,
            f"Use the read_file tool to read {project / 'a.txt'} and reply with exactly "
            "its content, nothing else.",
        )
        assert response.get("stopReason") in ("end_turn", "cancelled")

        # patch
        await worker.client.prompt(
            worker.acp_session_id,
            f"Use the patch tool to replace 'hello' with 'goodbye' in {project / 'a.txt'}. "
            "Do not explain, just call the tool.",
        )
        assert "goodbye jones" in (project / "a.txt").read_text()

        # search_files
        (project / "b.txt").write_text("needle-marker-xyz\n")
        response = await worker.client.prompt(
            worker.acp_session_id,
            f"Use the search_files tool to search for the text 'needle-marker-xyz' under "
            f"{project}. Reply with the filename that matched, nothing else.",
        )
        assert response.get("stopReason") in ("end_turn", "cancelled")

        # directory traversal — no dedicated Hermes tool (see module
        # docstring); `terminal ls` is the documented fallback.
        response = await worker.client.prompt(
            worker.acp_session_id,
            f"Use the terminal tool to run `ls {project}` and reply with the output, "
            "nothing else.",
        )
        assert response.get("stopReason") in ("end_turn", "cancelled")
        terminal_calls = [
            e for e in manager.events  # type: ignore[attr-defined]
            if e.get("update", {}).get("sessionUpdate") == "tool_call"
            and e["update"].get("title", "").lower().find("terminal") != -1
        ]
        assert terminal_calls, "expected at least one terminal tool_call event for `ls`"
    finally:
        manager_module._prepare_hermes_home = real_prepare
        await manager.stop()


async def test_real_hermes_terminal_stop_leaves_no_orphan_process(tmp_path, monkeypatch):
    """G09 / Issue #41 — see this file's module docstring for the full
    "what this verifies now vs. before" story. A real, long-running child
    process started by the `terminal` tool must be gone shortly after the
    user-visible `SessionService.stop()` — regardless of whether Hermes's
    own cancel-driven cleanup managed it, because Jones's own fallback
    (`WorkerManager.reap_stop_orphans`) must catch it either way. The
    command writes its OWN pid to a file (so this test can verify liveness
    without needing to parse Hermes's own output format) then sleeps far
    longer than this test's own timeout."""
    real_prepare = manager_module._prepare_hermes_home
    manager_module._prepare_hermes_home = _with_model_config
    service = await _make_real_service(tmp_path, monkeypatch)
    try:
        project = tmp_path / "project"
        project.mkdir()
        project_row = await run_in_db_thread(ProjectService(service.ctx.db).create, str(project))
        session_row = await service.create(
            project_id=project_row["id"], agent_id=DEFAULT_AGENT_ID, mode="auto",
            title="issue-41-real-e2e",
        )
        session_id = session_row["id"]
        pid_file = project / "child.pid"

        await service.send(
            session_id,
            "Use the terminal tool to run exactly this command right now, in a single "
            f"call: `echo $$ > {pid_file} && sleep 60`. Do not explain, do not ask "
            "about timeouts or anything else, just call the tool immediately.",
        )

        async def _answer_any_pending_permission() -> None:
            # This command's shell syntax (`>` redirection, `&&`) makes
            # `permissions/review.py::classify()` return `risk="high"`
            # ("该命令无法静态分析，请人工确认") — real, measured behavior,
            # not a guess: `_decide_terminal_like_permission`'s `mode="auto"`
            # fast path only ever applies to `risk.level=="low"`, so this
            # command always reaches a real `permission.requested`
            # regardless of session mode. Answered here the same way a real
            # user clicking "allow" would.
            for entry in await service.permission_pending(session_id):
                await service.permission_decide(entry["request_id"], "allow")

        # Wait for the child to actually start and record its own pid.
        # Budget composed of measured real-model costs (Issue #40/#41
        # investigations, reproduced against real DeepSeek workers):
        # ~15s worker self-check, ~11-20s tirith auto-install on this
        # HERMES_HOME's first terminal use (`tools/tirith_security.py`,
        # this suite's own `_prepare_hermes_home` gives every worker a
        # fresh, empty `HERMES_HOME` so this is paid every run, not a one-
        # off warm-cache effect), and — new in this real-`SessionService`
        # E2E test, not present in the raw-`WorkerManager` version this
        # replaced — real review-gate latency: DeepSeek was observed to
        # spend a real 20-30s of its OWN deliberation (visible as a long
        # run of `message.delta` chunks) before actually emitting the tool
        # call this command's `permission.requested` depends on, even
        # though the prompt explicitly asks for no explanation. 120s is
        # comfortable margin over the sum of all of that.
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline and not pid_file.exists():
            await _answer_any_pending_permission()
            await asyncio.sleep(0.1)
        assert pid_file.exists(), "terminal command never started (pid file missing)"
        child_pid = int(pid_file.read_text().strip())

        # Confirm it's actually alive before stopping — a false "no orphan"
        # pass because the process never started would be worthless.
        os.kill(child_pid, 0)  # raises ProcessLookupError if already gone

        result = await service.stop(session_id)
        assert result["stopped"] is True

        # `SessionService.stop()`'s Issue #41 fallback runs BACKGROUNDED
        # (see its own docstring): its own ~3s cancel grace
        # (`_ORPHAN_CANCEL_GRACE_S`) plus up to ~5s SIGTERM->SIGKILL
        # escalation (`_ORPHAN_TERM_GRACE_S`) is the ceiling regardless of
        # what Hermes's own concurrent cleanup does — generous margin below,
        # not a tight timing assertion.
        gone = False
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                gone = True
                break
            await asyncio.sleep(0.1)
        assert gone, (
            f"child pid {child_pid} is still alive 30s after stop() — Issue #41's "
            "fallback did not reap it (G09)"
        )
    finally:
        manager_module._prepare_hermes_home = real_prepare
        await service.shutdown()
