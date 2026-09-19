"""FR07's "写入前后 diff 在 Step 中可见" — Issue #13.

Source-verified (see `sessions/service.py::_extract_diff_content`'s
docstring) that Hermes sends the diff as an ACP diff-kind `ToolCallContent`
(`{"type":"diff","path":...,"newText":...,"oldText":...}`) on ONE of two
places depending on whether the edit auto-approves — never on the
`write_file`/`patch` completion (`tool_call_update`) event itself:

1. The `tool_call` "started" event's `content` (`_handle_tool_call_start`),
   when the edit auto-approves — no `session/request_permission` round trip
   happens at all in that case.
2. The `session/request_permission` REQUEST's own `toolCall.content`
   (`_on_request_permission`), when the edit needs a human decision — under
   a DIFFERENT, Hermes-invented `toolCallId` than the real ACP one
   `_handle_tool_call_start` already registered for the same call (see that
   method's docstring for the source evidence and the sequential-dispatch
   argument this depends on).

Both paths stash the diff on `_TurnContext.step_diffs`, keyed by the REAL
step id; `_handle_tool_call_update` pops and merges it into the Step's
`result_summary`/payload when that step's completion event arrives. This
file drives all three methods directly against a real `SessionService` (a
real Run/Turn/Step exist in the DB via one real, held-open Turn — see
`_open_turn` — so `_TurnContext`'s `run_id`/`turn_id` satisfy the `steps`
table's real FK constraints) rather than trying to make `fake_acp_agent.py`
replay Hermes's two-id shape (it does not, and is not this branch's file to
change — see its own docstring's `CUSTOM_PERMISSION_JSON` section, which
reuses ONE id for both the request and the completion, unlike real Hermes).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from jones_daemon.context import DaemonContext
from jones_daemon.context import ProviderResolver as ProviderResolverProtocol
from jones_daemon.projects.bootstrap import bootstrap_projects_and_agents
from jones_daemon.sessions import queries
from jones_daemon.sessions.service import (
    DEFAULT_AGENT_ID,
    DEFAULT_PROJECT_ID,
    SessionService,
    _extract_diff_content,
)
from jones_daemon.store import apply_pending, connect, run_in_db_thread

_FAKE_AGENT = str(Path(__file__).parent / "fake_acp_agent.py")


# ---------------------------------------------------------------------------
# Unit: _extract_diff_content
# ---------------------------------------------------------------------------


def test_extract_diff_content_from_content_array():
    content = [{"type": "diff", "path": "/x/y.py", "newText": "new", "oldText": "old"}]
    diff = _extract_diff_content(content)
    assert diff == {"path": "/x/y.py", "old_text": "old", "new_text": "new"}


def test_extract_diff_content_new_file_has_no_old_text():
    content = [{"type": "diff", "path": "/x/new.py", "newText": "hello"}]
    diff = _extract_diff_content(content)
    assert diff == {"path": "/x/new.py", "old_text": None, "new_text": "hello"}


def test_extract_diff_content_ignores_non_diff_blocks():
    content = [{"type": "text", "text": "Preparing write to /x/y.py."}]
    assert _extract_diff_content(content) is None


def test_extract_diff_content_handles_missing_content():
    assert _extract_diff_content(None) is None
    assert _extract_diff_content("not a list or dict") is None


# ---------------------------------------------------------------------------
# Integration (SessionService-level): the three-method round trip
# ---------------------------------------------------------------------------


class _FakeServer:
    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, str, Any]] = []

    async def broadcast(self, session_id: str, method: str, params: Any) -> None:
        self.broadcasts.append((session_id, method, params))

    def events(self, method: str) -> list[tuple[str, Any]]:
        return [(sid, p) for sid, m, p in self.broadcasts if m == method]


class _StubProviderResolver(ProviderResolverProtocol):
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


async def _make_service(tmp_path, monkeypatch) -> SessionService:
    monkeypatch.setenv("JONES_HOME", str(tmp_path))
    monkeypatch.setenv("FAKE_ACP_MODE", "normal")

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
    service = SessionService(ctx, worker_cmd=[sys.executable, _FAKE_AGENT])
    await service.worker_manager.start()
    return service


async def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


async def _open_turn(service: SessionService, tmp_path):
    """Hold one real Turn open (a real run_id/turn_id the `steps` FK needs)
    long enough to drive `_handle_tool_call_start`/`_on_request_permission`/
    `_handle_tool_call_update` directly against its real `_TurnContext`."""
    row = await service.create(
        project_id=DEFAULT_PROJECT_ID, agent_id=DEFAULT_AGENT_ID, mode="auto", title="diff"
    )
    session_id = row["id"]
    await service.send(session_id, "SLEEP_MS:2000 hold this turn open")
    await _wait_until(lambda: session_id in service._active_turns)
    return session_id, service._active_turns[session_id]


async def test_auto_approved_edit_diff_lands_in_the_step(tmp_path, monkeypatch):
    service = await _make_service(tmp_path, monkeypatch)
    try:
        session_id, ctx_turn = await _open_turn(service, tmp_path)
        tool_call_id = "real-tc-1"
        await service._handle_tool_call_start(
            ctx_turn,
            {
                "toolCallId": tool_call_id, "title": "write_file", "status": "pending",
                "content": [
                    {"type": "diff", "path": "/repo/f.py",
                     "newText": "new body", "oldText": "old body"}
                ],
            },
        )
        step_id = ctx_turn.tool_call_steps[tool_call_id]
        assert ctx_turn.step_diffs[step_id] == {
            "path": "/repo/f.py", "old_text": "old body", "new_text": "new body",
        }

        await service._handle_tool_call_update(
            ctx_turn, {"toolCallId": tool_call_id, "status": "completed", "rawOutput": None},
        )
        # Popped once merged in — no leak across steps.
        assert step_id not in ctx_turn.step_diffs

        row = await run_in_db_thread(queries.get_step, service.ctx.db, step_id)
        summary = json.loads(row["result_summary"])
        assert summary["diff"] == {
            "path": "/repo/f.py", "old_text": "old body", "new_text": "new body",
        }
    finally:
        await service.shutdown()


async def test_needs_approval_edit_diff_correlates_to_the_real_step(tmp_path, monkeypatch):
    """The needs-approval path: `_on_request_permission` receives the diff
    under a DIFFERENT synthetic `toolCallId` than the real one
    `_handle_tool_call_start` already assigned — verifies the "most recently
    started, still-open step" fallback correlation (see that method's
    docstring)."""
    service = await _make_service(tmp_path, monkeypatch)
    try:
        session_id, ctx_turn = await _open_turn(service, tmp_path)
        real_tool_call_id = "real-tc-2"
        await service._handle_tool_call_start(
            ctx_turn,
            # No `content` here — this is the needs-approval path, where
            # Hermes's own `_START_CONTENT_BUILDERS["write_file"]` renders
            # plain text ("Preparing write to ...") instead of a diff block
            # (see `_extract_diff_content`'s docstring on `_handle_tool_call_
            # start`).
            {"toolCallId": real_tool_call_id, "title": "write_file", "status": "pending"},
        )
        real_step_id = ctx_turn.tool_call_steps[real_tool_call_id]
        assert real_step_id not in ctx_turn.step_diffs

        params = {
            "sessionId": session_id,
            "toolCall": {
                # Hermes's edit-approval channel's OWN id, never equal to
                # `real_tool_call_id` (`acp_adapter/edit_approval.py::
                # build_acp_edit_tool_call`'s `edit-approval-{n}` counter).
                "toolCallId": "edit-approval-7", "title": "write_file",
                "rawInput": {
                    "tool": "write_file",
                    "arguments": {"path": "/repo/g.py", "content": "new content"},
                },
                "content": [
                    {"type": "diff", "path": "/repo/g.py",
                     "newText": "new content", "oldText": "old content"}
                ],
            },
            "options": [
                {"optionId": "opt-allow-once", "kind": "allow_once"},
                {"optionId": "opt-reject-once", "kind": "reject_once"},
            ],
        }
        task = asyncio.create_task(service._on_request_permission(session_id, params))
        try:
            # The diff-stash runs synchronously before this call ever awaits
            # the pending decision — no arbitrary sleep needed, just enough
            # scheduling for the coroutine to reach its first real await.
            await _wait_until(lambda: real_step_id in ctx_turn.step_diffs, timeout=2.0)
            assert ctx_turn.step_diffs[real_step_id] == {
                "path": "/repo/g.py", "old_text": "old content", "new_text": "new content",
            }

            pending = await service.permission_pending(session_id)
            assert len(pending) == 1
            await service.permission_decide(pending[0]["request_id"], "allow")
            await task
        finally:
            if not task.done():
                task.cancel()

        await service._handle_tool_call_update(
            ctx_turn,
            {"toolCallId": real_tool_call_id, "status": "completed", "rawOutput": None},
        )
        row = await run_in_db_thread(queries.get_step, service.ctx.db, real_step_id)
        summary = json.loads(row["result_summary"])
        assert summary["diff"] == {
            "path": "/repo/g.py", "old_text": "old content", "new_text": "new content",
        }
    finally:
        await service.shutdown()


async def test_a_tool_without_a_diff_keeps_the_old_result_summary_shape(tmp_path, monkeypatch):
    """No regression: a tool call with no diff still serializes `raw_output`
    directly (not wrapped in `{"raw_output": ...}`) — byte-for-byte the same
    shape as before this branch, see `_handle_tool_call_update`'s comment."""
    service = await _make_service(tmp_path, monkeypatch)
    try:
        session_id, ctx_turn = await _open_turn(service, tmp_path)
        tool_call_id = "real-tc-3"
        await service._handle_tool_call_start(
            ctx_turn, {"toolCallId": tool_call_id, "title": "read_file", "status": "pending"},
        )
        step_id = ctx_turn.tool_call_steps[tool_call_id]
        await service._handle_tool_call_update(
            ctx_turn,
            {"toolCallId": tool_call_id, "status": "completed", "rawOutput": {"content": "hi"}},
        )
        row = await run_in_db_thread(queries.get_step, service.ctx.db, step_id)
        assert json.loads(row["result_summary"]) == {"content": "hi"}
    finally:
        await service.shutdown()
