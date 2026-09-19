"""G15 (PRD 12.1: "agent 尝试读 ~/.ssh、~/.aws、~/.jones/secrets/，均被拒且走权限
闸提示") + FR07's "越界路径走权限闸" — Issue #13.

Two layers of tests:
- Unit: `permissions/defaults.py`'s path matching, and `permissions/review.py::
  classify()`'s risk output for `read_file`/`search_files`/`write_file`/`patch`/
  `terminal` calls that touch a default-deny sensitive location or escape the
  Project workspace. This is where the real assertions about WHICH paths are
  covered live.
- Integration (`SessionService`-level, `fake_acp_agent.py`): proves a `high`
  classification actually reaches the daemon's user gate instead of auto-
  allowing — i.e. that `_on_request_permission`'s decision tree really does
  treat this module's `high` the way G15 requires ("均被拒且走权限闸提示":
  never silently executed, always a `permission.requested` broadcast in every
  mode, denied by default unless a human/rule explicitly allows it).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from jones_daemon.context import DaemonContext
from jones_daemon.context import ProviderResolver as ProviderResolverProtocol
from jones_daemon.kernel.plugin.jones_gate import _review_payload
from jones_daemon.permissions import defaults
from jones_daemon.permissions.review import classify
from jones_daemon.projects.bootstrap import bootstrap_projects_and_agents
from jones_daemon.sessions.service import DEFAULT_AGENT_ID, DEFAULT_PROJECT_ID, SessionService
from jones_daemon.store import apply_pending, connect, run_in_db_thread

_FAKE_AGENT = str(Path(__file__).parent / "fake_acp_agent.py")


# ---------------------------------------------------------------------------
# Unit: permissions/defaults.py
# ---------------------------------------------------------------------------


def test_ssh_dir_is_a_default_deny_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    resolved = (tmp_path / ".ssh" / "id_rsa").resolve()
    assert defaults.matches(resolved) == (tmp_path / ".ssh").resolve()


def test_aws_and_jones_secrets_are_default_deny_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert defaults.matches((tmp_path / ".aws" / "credentials").resolve()) is not None
    assert defaults.matches((tmp_path / ".jones" / "secrets" / "vault.enc").resolve()) is not None


def test_real_browser_profile_is_a_default_deny_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    chrome = (
        tmp_path / "Library" / "Application Support" / "Google" / "Chrome" / "Default" / "Cookies"
    )
    assert defaults.matches(chrome.resolve()) is not None


def test_keychain_is_a_default_deny_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    keychain = (tmp_path / "Library" / "Keychains" / "login.keychain-db").resolve()
    assert defaults.matches(keychain) is not None


def test_an_unrelated_path_does_not_match(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert defaults.matches((tmp_path / "projects" / "repo" / "README.md").resolve()) is None


# ---------------------------------------------------------------------------
# Unit: permissions/review.py::classify() — read_file/search_files/write_file/patch
# ---------------------------------------------------------------------------


def test_read_file_under_ssh_is_high_risk(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    risk = classify(
        "read_file", {"path": str(tmp_path / ".ssh" / "id_rsa")}, cwd=str(tmp_path / "repo")
    )
    assert risk.level == "high"


def test_read_file_under_aws_is_high_risk(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    risk = classify(
        "read_file", {"path": str(tmp_path / ".aws" / "credentials")}, cwd=str(tmp_path / "repo")
    )
    assert risk.level == "high"


def test_read_file_under_jones_secrets_is_high_risk(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    risk = classify(
        "read_file", {"path": str(tmp_path / ".jones" / "secrets" / "vault.enc")},
        cwd=str(tmp_path / "repo"),
    )
    assert risk.level == "high"


def test_search_files_under_ssh_is_high_risk(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    risk = classify(
        "search_files", {"path": str(tmp_path / ".ssh"), "pattern": "*"}, cwd=str(tmp_path / "repo")
    )
    assert risk.level == "high"


def test_write_file_under_browser_profile_is_high_risk(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    target = (
        tmp_path / "Library" / "Application Support" / "Google" / "Chrome" / "Default" / "Cookies"
    )
    risk = classify("write_file", {"path": str(target)}, cwd=str(tmp_path / "repo"))
    assert risk.level == "high"


def test_read_file_inside_workspace_stays_low(tmp_path):
    risk = classify("read_file", {"path": str(tmp_path / "src" / "main.py")}, cwd=str(tmp_path))
    assert risk.level == "low"


def test_read_file_outside_workspace_is_high_fr07(tmp_path):
    # FR07's "越界路径走权限闸" — a genuinely unrelated, non-sensitive path
    # that simply isn't under the Project's workspace root.
    other = tmp_path.parent / "not-the-workspace" / "file.txt"
    risk = classify("read_file", {"path": str(other)}, cwd=str(tmp_path))
    assert risk.level == "high"


def test_search_files_with_no_path_defaults_to_low():
    # search_files with no `path` searches the whole workspace by Hermes's
    # own default — not evidence of anything suspicious (see
    # `permissions/review.py::_classify_read`'s docstring).
    risk = classify("search_files", {"pattern": "*.py"})
    assert risk.level == "low"


# ---------------------------------------------------------------------------
# Unit: permissions/review.py::classify() — terminal touching a sensitive path
# ---------------------------------------------------------------------------


def test_terminal_cat_ssh_key_is_high_risk(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    risk = classify("terminal", {"command": f"cat {tmp_path / '.ssh' / 'id_rsa'}"})
    assert risk.level == "high"


def test_terminal_cat_ssh_key_via_tilde_is_high_risk(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    risk = classify("terminal", {"command": "cat ~/.ssh/id_rsa"})
    assert risk.level == "high"


# ---------------------------------------------------------------------------
# Integration (SessionService-level, fake_acp_agent.py): G15's "均被拒且走
# 权限闸提示" — a high-risk sensitive-path call must reach the user gate
# (never silently auto-allow), in every mode, and stays denied unless a human
# explicitly allows it.
# ---------------------------------------------------------------------------


class _FakeServer:
    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, str, Any]] = []

    async def broadcast(self, session_id: str, method: str, params: Any) -> None:
        self.broadcasts.append((session_id, method, params))

    def events(self, method: str) -> list[tuple[str, Any]]:
        return [(sid, p) for sid, m, p in self.broadcasts if m == method]


class _StubProviderResolver(ProviderResolverProtocol):
    """Duplicated, not imported — see `tests/test_gates_sessions_integration.py`'s
    `_StubProviderResolver` docstring for why this codebase's convention is a
    per-file fake, not a cross-file import."""

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


async def _new_session(service: SessionService, *, mode: str = "auto") -> str:
    row = await service.create(
        project_id=DEFAULT_PROJECT_ID, agent_id=DEFAULT_AGENT_ID, mode=mode, title="g15"
    )
    return row["id"]


async def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> None:
    import asyncio

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


def _custom_permission_prompt(tool: str, args: dict[str, Any], *, mode: str) -> str:
    encoded = _review_payload.encode(tool, args, mode=mode)
    payload = {
        "toolCall": {
            "toolCallId": "g15-1", "title": tool,
            "rawInput": {"command": f"<{tool}> (plugin approval rule)", "description": encoded},
        }
    }
    return f"CUSTOM_PERMISSION_JSON:{json.dumps(payload)}"


@pytest.mark.parametrize("mode", ["auto", "task"])
async def test_read_file_under_ssh_goes_to_user_gate_not_auto_allowed(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("HOME", str(tmp_path))
    service = await _make_service(tmp_path, monkeypatch)
    try:
        session_id = await _new_session(service, mode=mode)
        prompt = _custom_permission_prompt(
            "read_file", {"path": str(tmp_path / ".ssh" / "id_rsa")}, mode=mode
        )
        await service.send(session_id, prompt)
        await _wait_until(lambda: service.ctx.server.events("permission.requested"))
        requested = service.ctx.server.events("permission.requested")[0][1]
        assert requested["risk"] == "high"
        # G15's "走权限闸提示" — the card is not a bare risk level, it names
        # what was hit.
        assert any(".ssh" in r for r in requested["reasons"])
        # Denied by default (nobody approved it) -> the pending request stays
        # pending until this test explicitly decides it, matching "均被拒".
        pending = await service.permission_pending(session_id)
        assert len(pending) == 1
        await service.permission_decide(pending[0]["request_id"], "deny")
        await _wait_until(lambda: service.ctx.server.events("permission.decided"))
        decided = service.ctx.server.events("permission.decided")[0][1]
        assert decided["decision"] == "deny"
    finally:
        await service.shutdown()
