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


def test_uppercase_ssh_variant_matches_case_insensitively(tmp_path, monkeypatch):
    # Round 2 review finding #1 (critical): macOS's default filesystem
    # (APFS) is case-insensitive but case-preserving — `~/.SSH` and
    # `~/.ssh` are the SAME directory on disk (verified: `ls -ld ~/.SSH` on
    # a real macOS APFS volume returns the real `~/.ssh`'s own stat line).
    # `matches()` used to compare with case-sensitive `==`/`relative_to()`,
    # so this spelling silently fell through to `None` (and from there to
    # `low` in the review gate) even though it resolves to the exact same
    # inode as the lowercase form the test above already covers.
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".ssh").mkdir()
    resolved = (tmp_path / ".SSH" / "id_rsa").resolve(strict=False)
    assert defaults.matches(resolved) is not None


def test_ancestor_root_under_finds_ssh_under_home(tmp_path, monkeypatch):
    # Round 2 review findings #2/#5: the reverse direction from `matches()` —
    # `ancestor_root_under(home)` must find `~/.ssh` as a root CONTAINED by
    # `home`, not just the other way around.
    monkeypatch.setenv("HOME", str(tmp_path))
    assert defaults.ancestor_root_under(tmp_path.resolve()) == (tmp_path / ".ssh").resolve()


def test_ancestor_root_under_an_unrelated_dir_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    other = tmp_path / "projects" / "repo"
    other.mkdir(parents=True)
    assert defaults.ancestor_root_under(other.resolve()) is None


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


def test_read_file_under_uppercase_ssh_variant_is_high_risk(tmp_path, monkeypatch):
    # Round 2 review finding #1: same repro as `test_uppercase_ssh_variant_
    # matches_case_insensitively` above, one layer up through `classify()`.
    monkeypatch.setenv("HOME", str(tmp_path))
    risk = classify(
        "read_file", {"path": str(tmp_path / ".SSH" / "id_rsa")}, cwd=str(tmp_path / "repo")
    )
    assert risk.level == "high"


def test_read_file_unresolvable_user_home_does_not_raise(tmp_path):
    # Round 2 review finding #6 (important): `Path('~nosuchuser/x').
    # expanduser()` raises `RuntimeError` (Python 3.12), not `OSError` —
    # `_classify_path_access`'s `except OSError` used to let it escape
    # `classify()` entirely (main already had this for write_file/patch;
    # Issue #13 routing read_file/search_files through the same function
    # widened the trigger surface without widening the `except`). Fails
    # closed: `high`, never a crash.
    risk = classify("read_file", {"path": "~nosuchuser12345/x"}, cwd=str(tmp_path))
    assert risk.level == "high"


def test_search_files_unresolvable_user_home_does_not_raise(tmp_path):
    risk = classify(
        "search_files", {"path": "~nosuchuser12345/x", "pattern": "*"}, cwd=str(tmp_path)
    )
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


def test_search_files_with_no_path_and_no_cwd_is_medium_not_low():
    # Round 1 fix, review findings #2/#5: a missing `path` used to be a
    # special always-`low` case — strictly SAFER than passing `search_files`'
    # own documented default (`path="."`) explicitly, which already
    # classified `medium`/`high` depending on `cwd`. `classify()` now
    # substitutes `"."` before this ever reaches `_classify_read`, so a
    # missing path and an explicit `path="."` are the exact same call.
    risk = classify("search_files", {"pattern": "*.py"})
    assert risk.level == "medium"


def test_search_files_missing_path_matches_explicit_path_dot(tmp_path):
    # Same repro, with a `cwd` this time — missing `path` and explicit
    # `path="."` must classify identically (review finding #2's exact
    # complaint: omitting the argument was strictly lower-risk than
    # spelling out its own default).
    cwd = str(tmp_path)
    no_path = classify("search_files", {"pattern": "BEGIN RSA"}, cwd=cwd)
    explicit_dot = classify("search_files", {"pattern": "BEGIN RSA", "path": "."}, cwd=cwd)
    assert no_path.level == explicit_dot.level


def test_search_files_default_path_under_home_placeholder_is_not_low(tmp_path, monkeypatch):
    # Review finding #5's exact repro: the DEFAULT Project's `cwd` is
    # `$HOME` (01-w2-interfaces.md §2.2's documented placeholder) — a
    # `search_files` call with no `path` at that `cwd` recursively greps the
    # entire home directory, `~/.ssh`/`~/.aws`/`~/.jones/secrets` included.
    # Must never be `low` (which `auto`/`task` mode auto-allows with zero
    # user-gate visibility).
    monkeypatch.setenv("HOME", str(tmp_path))
    risk = classify("search_files", {"pattern": "BEGIN OPENSSH PRIVATE KEY"}, cwd=str(tmp_path))
    assert risk.level != "low"


def test_read_file_under_home_placeholder_normal_file_is_low(tmp_path, monkeypatch):
    # Review finding #3: unlike `search_files` (a recursive directory scan,
    # see the test above), `read_file` only ever discloses the ONE path it
    # names — an ordinary, non-sensitive file must stay `low` even when the
    # session's `cwd` is today's `$HOME` placeholder, or PRD 9.1's "任务模式:
    # 只读工具直接放行" / G06 breaks for every single read in task/auto mode
    # (this is the exact scenario `tests/test_gates_sessions_integration.py`'s
    # G06 tests had to swap `read_file` out for `browser_navigate` to keep
    # passing — see git history for that workaround, now unnecessary).
    monkeypatch.setenv("HOME", str(tmp_path))
    risk = classify(
        "read_file", {"path": str(tmp_path / "project" / "src" / "main.py")}, cwd=str(tmp_path)
    )
    assert risk.level == "low"


# ---------------------------------------------------------------------------
# Unit: permissions/defaults.py::is_credential_filename() + review.py::
# classify() — controller ruling R-I1 (round 3, 2026-09-19): a credential-
# SHAPED filename earns `medium` (walks the review gate -> user gate) even
# when it isn't under any `SENSITIVE_HOME_RELATIVE_DIRS`/`SENSITIVE_ABSOLUTE_
# DIRS` denylist root — the ruling's own three test categories.
# ---------------------------------------------------------------------------


def test_is_credential_filename_matches_ruling_r_i1_patterns():
    for name in (
        ".env", ".env.local", "id_rsa", "id_ed25519.pub", "server.pem", "client.key",
        "cert.p12", "cert.pfx", "vault.kdbx", ".netrc", ".npmrc", ".pypirc",
        ".git-credentials", "known_hosts", "authorized_keys", "api_token.txt",
        "SECRET.yaml", "db_credential.json",
    ):
        assert defaults.is_credential_filename(name), name


def test_is_credential_filename_does_not_match_ordinary_names():
    for name in ("main.py", "README.md", "a.txt", "index.html", "identity.py"):
        assert not defaults.is_credential_filename(name), name


def test_read_file_dotenv_under_home_placeholder_is_medium(tmp_path, monkeypatch):
    # Ruling R-I1's first repro: `~/.env` isn't under any directory denylist
    # root, but the filename alone is a credential signal — `medium`, not
    # `low` (would auto-allow in task/auto mode with no user-gate stop) and
    # not `high`/deny (it's a name-only signal, weaker than a denylist hit).
    monkeypatch.setenv("HOME", str(tmp_path))
    risk = classify("read_file", {"path": str(tmp_path / ".env")}, cwd=str(tmp_path))
    assert risk.level == "medium"


def test_read_file_id_rsa_outside_denylist_dir_is_medium(tmp_path, monkeypatch):
    # Ruling R-I1's second repro: `~/proj/id_rsa` — `proj/` is an ordinary
    # project directory, not `~/.ssh`, so the directory denylist doesn't fire;
    # the `id_*` filename pattern is what has to catch it.
    monkeypatch.setenv("HOME", str(tmp_path))
    risk = classify("read_file", {"path": str(tmp_path / "proj" / "id_rsa")}, cwd=str(tmp_path))
    assert risk.level == "medium"


def test_read_file_ordinary_document_under_home_placeholder_stays_low(tmp_path, monkeypatch):
    # Ruling R-I1's third repro: `~/Documents/a.txt` matches neither the
    # directory denylist nor a credential filename pattern — PRD 9.1's "任务
    # 模式：只读工具直接放行" still has to hold for this one.
    monkeypatch.setenv("HOME", str(tmp_path))
    risk = classify(
        "read_file", {"path": str(tmp_path / "Documents" / "a.txt")}, cwd=str(tmp_path)
    )
    assert risk.level == "low"


def test_read_file_credential_filename_via_relative_path_resolves_against_cwd(
    tmp_path, monkeypatch
):
    # Ruling R-I1: "相对路径按 cwd 解析后再判" — a relative `path` must be
    # resolved against `cwd` BEFORE the filename check runs, not judged on
    # its literal unresolved text.
    monkeypatch.setenv("HOME", str(tmp_path))
    risk = classify("read_file", {"path": "id_rsa"}, cwd=str(tmp_path / "proj"))
    assert risk.level == "medium"


def test_search_files_credential_looking_root_is_unaffected_by_r_i1(tmp_path, monkeypatch):
    # R-I1 is scoped to `read_file` only (see `classify()`'s comment) —
    # `search_files`'s `path` names a directory ROOT being recursively
    # walked, not the single file being disclosed, so a directory whose own
    # NAME happens to match a credential pattern (here, a dir literally
    # called `id_rsa`) must not be escalated by this ruling — only `low` (no
    # other risk signal applies) proves the check truly didn't run for it.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    target = workspace / "id_rsa"
    target.mkdir(parents=True)
    risk = classify(
        "search_files", {"path": str(target), "pattern": "*"}, cwd=str(workspace)
    )
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


def test_terminal_cat_ssh_key_via_relative_path_with_home_cwd_is_high_risk(tmp_path, monkeypatch):
    # Round 1 fix, review finding #1 (critical): the DEFAULT Project's `cwd`
    # is `$HOME` — `cat .ssh/id_rsa` run from it targets the exact same file
    # as `cat ~/.ssh/id_rsa` (the test just above), with neither a `~` nor a
    # leading `/` in the command text for the old absolute-only check to
    # catch. Before this fix this classified `low` while the `~` form
    # classified `high` — a bare formatting difference silently deciding
    # whether G15's user gate fired at all.
    (tmp_path / ".ssh").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    risk = classify("terminal", {"command": "cat .ssh/id_rsa"}, cwd=str(tmp_path))
    assert risk.level == "high"


def test_terminal_head_aws_credentials_via_relative_path_with_home_cwd_is_high_risk(
    tmp_path, monkeypatch
):
    # Same repro as above, review finding #6's second example.
    (tmp_path / ".aws").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    risk = classify("terminal", {"command": "head -5 .aws/credentials"}, cwd=str(tmp_path))
    assert risk.level == "high"


def test_terminal_cat_uppercase_ssh_variant_is_high_risk(tmp_path, monkeypatch):
    # Round 2 review finding #1's exact repro: `cat ~/.SSH/id_rsa`/`cat
    # ~/.AWS/credentials` must not be a silent bypass of the lowercase form.
    monkeypatch.setenv("HOME", str(tmp_path))
    risk = classify("terminal", {"command": f"cat {tmp_path / '.SSH' / 'id_rsa'}"})
    assert risk.level == "high"


def test_terminal_relative_sensitive_path_with_no_cwd_stays_the_documented_edge_case(
    tmp_path, monkeypatch
):
    # Documents the one case `_terminal_token_sensitive_root`'s docstring
    # explicitly leaves unresolved: no `cwd` at all means there's nothing to
    # resolve a relative token against, so this legitimately can't be told
    # apart from any other plain, no-signal command and correctly falls
    # through to the R10 plain-command default (`low`) — NOT a regression of
    # findings #1/#6, which are about `cwd` being available (the real
    # Session/Project cwd, e.g. `$HOME`) and simply not threaded through; the
    # real `classify()` call site (`sessions/service.py::
    # _on_request_permission`) populates `cwd` from the session's real
    # Project path whenever that lookup succeeds — it only falls back to
    # `None` on a lookup failure unrelated to what the command text says —
    # so this bare-unit case is not the realistic shape those findings are
    # about; see the tests above for the reachable, previously-broken case.
    monkeypatch.setenv("HOME", str(tmp_path))
    risk = classify("terminal", {"command": "cat .ssh/id_rsa"})
    assert risk.level == "low"


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
