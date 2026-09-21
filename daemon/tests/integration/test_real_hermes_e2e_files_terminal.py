"""Real-Hermes end-to-end check for Issue #13/#14 (FR07 file five-piece +
FR08 terminal), gated the same way `test_real_hermes_e2e.py` is
(`JONES_E2E=1` and `ANTHROPIC_API_KEY` both present — see that file's module
docstring for the full rationale, unchanged here).

**Not run by this branch's own CI/local verification** — no `ANTHROPIC_API_KEY`
was available in this sandbox, and this branch does not source one from
`~/.hermes/.env`/`~/.hermes/auth.json` (present on this machine, but the
task instructions for this branch explicitly say not to read the user's own
config/credentials, and using a personal key without being asked to would
also spend the user's own quota). This file is written and reviewed for
correctness against the source evidence in `docs/design/00-foundation.md`
§8/§9 and the installed `hermes-agent` checkout, but the assertions inside
it are unverified against a real model in this environment — see the PR
report's "没做什么" section. A reviewer with a key can run it directly:

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

import os
import time

import pytest

from jones_daemon.workers import manager as manager_module
from jones_daemon.workers.manager import WorkerManager

from . import _provider_gate

pytestmark = pytest.mark.skipif(
    not (os.environ.get("JONES_E2E") and _provider_gate.configured_vendor()),
    reason=_provider_gate.NEEDS_REAL_MODEL_REASON,
)


def _with_model_config(hermes_home, **kwargs):
    """Same monkeypatch `test_real_hermes_e2e.py` uses — `_prepare_hermes_home`
    doesn't yet wire a real provider Key into a worker's `config.yaml` (see
    that file's own docstring); this appends the anthropic block
    01-w2-interfaces.md §3.1 documents."""
    real_prepare = manager_module._prepare_hermes_home
    real_prepare(hermes_home, **kwargs)
    config_path = hermes_home / "config.yaml"
    with config_path.open("a", encoding="utf-8") as fh:
        fh.write(_provider_gate.model_config_block())


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


async def test_real_hermes_terminal_stop_leaves_no_orphan_process(tmp_path):
    """G09: a real, long-running child process started by the `terminal`
    tool must be gone shortly after `session/cancel` — the actual proof
    `tests/test_cap_terminal_stop_cancel.py` (fake-agent level) cannot
    provide by itself. The command writes its OWN pid to a file (so this
    test can verify liveness without needing to parse Hermes's own output
    format) then sleeps far longer than this test's own timeout."""
    real_prepare = manager_module._prepare_hermes_home
    manager_module._prepare_hermes_home = _with_model_config
    manager = await _make_manager(tmp_path)
    try:
        project = tmp_path / "project"
        project.mkdir()
        pid_file = project / "child.pid"
        worker = await manager.ensure_started("e2e-terminal-g09", cwd=str(project))

        prompt_task = None
        import asyncio

        async def _run_prompt():
            return await worker.client.prompt(
                worker.acp_session_id,
                "Use the terminal tool to run exactly this command (a single call, do "
                f"not explain): `echo $$ > {pid_file} && sleep 60`",
            )

        prompt_task = asyncio.create_task(_run_prompt())

        # Wait for the child to actually start and record its own pid.
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and not pid_file.exists():
            await asyncio.sleep(0.1)
        assert pid_file.exists(), "terminal command never started (pid file missing)"
        child_pid = int(pid_file.read_text().strip())

        # Confirm it's actually alive before cancelling — a false "no
        # orphan" pass because the process never started would be worthless.
        os.kill(child_pid, 0)  # raises ProcessLookupError if already gone

        await worker.client.cancel(worker.acp_session_id)

        # Real Hermes's own reaping is adaptive-poll (5ms up to 200ms, see
        # `tools/environments/base.py::_wait_for_process`, source-verified —
        # cited in the PR report) — a few seconds of slack covers process-
        # group SIGTERM->SIGKILL escalation comfortably without being a
        # tight timing assertion.
        gone = False
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                gone = True
                break
            await asyncio.sleep(0.1)
        assert gone, (
            f"child pid {child_pid} is still alive 10s after cancel — orphan process (G09)"
        )

        prompt_task.cancel()
        try:
            await prompt_task
        except (asyncio.CancelledError, Exception):
            pass
    finally:
        manager_module._prepare_hermes_home = real_prepare
        await manager.stop()
