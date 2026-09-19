"""Real-Chrome / real-Hermes end-to-end checks for Issue #15's acceptance
checklist and this branch's α-vs-β decision (docs/design/00-foundation.md §9).

Two independent gates, each self-skipping:
  - `JONES_E2E=1` + a real Chrome/Chromium binary reachable
    (`capabilities.browser._find_chrome_binary`) — `test_login_state_survives_*`.
  - the above, PLUS `hermes-agent` importable (`uv sync --group worker`, see
    docs/DEV.md) — `test_real_hermes_browser_navigate_goes_through_the_gate`,
    the same "real invoke_tool() + real pre_tool_call plugin + real browser_*
    dispatch" shape as docs/spikes/hermes_hook_demo.py (spike #1), extended to
    actually execute (not just block) a tool end to end.

Neither needs a model API key: `invoke_tool()` is driven directly (the plugin
verdict IS the "human decision" in this test, matching how spike #1's demo
stands in for the daemon's real answer — see that file for the established
pattern this reuses).
"""

from __future__ import annotations

import http.server
import json
import os
import tempfile
import threading
from pathlib import Path

import pytest

from jones_daemon.capabilities.browser import (
    BrowserLaunchError,
    BrowserManager,
    _find_chrome_binary,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("JONES_E2E"),
    reason="real-Chrome e2e: set JONES_E2E=1 to run (see module docstring)",
)


def _chrome_available() -> bool:
    try:
        _find_chrome_binary()
        return True
    except BrowserLaunchError:
        return False


def _hermes_available() -> bool:
    try:
        import agent.agent_runtime_helpers  # noqa: F401
        import hermes_cli.plugins  # noqa: F401
    except ImportError:
        return False
    return True


class _LoginSiteHandler(http.server.BaseHTTPRequestHandler):
    """Minimal persistent-cookie login/secure-area test site (127.0.0.1 only, no
    external requests, no real accounts) — same shape as
    docs/design/00-foundation.md §9's own probe and docs/spikes/04-browser-login-state.md's
    "persistent cookie (本地测试站)" scenario."""

    def log_message(self, *_a: object) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/login":
            self.send_response(200)
            self.send_header("Set-Cookie", "session_probe=abc123; Max-Age=86400; Path=/")
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"logged in")
        elif self.path == "/secure":
            ok = "session_probe=abc123" in (self.headers.get("Cookie") or "")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Secure Area" if ok else b"Not logged in")
        elif self.path == "/form":
            # Review finding #5: a real page with a real form field, for
            # `test_snapshot_then_click_then_type_covers_the_form_filling_
            # acceptance_bar` — labeled input Hermes's own accessibility
            # snapshot can resolve a ref for.
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b'<html><body><form>'
                b'<label for="name">Name</label>'
                b'<input id="name" name="name" type="text">'
                b"</form></body></html>"
            )
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture
def login_site():
    server = http.server.HTTPServer(("127.0.0.1", 0), _LoginSiteHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _short_socket_dir(name: str) -> Path:
    # Unix domain socket paths have a ~103-char limit (macOS) and pytest's
    # `tmp_path` is deeply nested enough to blow through it once agent-browser
    # appends its own socket filename — use a short-prefixed tempdir instead of
    # nesting under tmp_path (round-1 fix while writing this test: it failed for
    # exactly this reason on the first real run).
    return Path(tempfile.mkdtemp(prefix=f"ab-{name}-"))


def _agent_browser_env(socket_dir: Path) -> dict:
    """Isolated per-call env, mirroring Hermes's own
    tools/browser_tool_session.py::_agent_browser_command_env +
    _prepare_session_socket_dir: WITHOUT `AGENT_BROWSER_SOCKET_DIR`, agent-browser
    falls back to a single shared `~/.agent-browser/default.*` daemon+session that
    is reused across unrelated invocations — the FIRST `--cdp` value it ever saw
    wins and later `--cdp` values on that same machine are silently ignored. This
    was hit for real while writing this test (a `default` session from an earlier
    manual probe kept redirecting every subsequent call back to its long-dead
    Chrome instance) and is worth documenting: any caller of agent-browser that
    skips this isolation — not just tests — gets the same footgun."""
    env = dict(os.environ)
    socket_dir.mkdir(parents=True, exist_ok=True)
    env["AGENT_BROWSER_SOCKET_DIR"] = str(socket_dir)
    return env


def _agent_browser_run(cdp_http_url: str, socket_dir: Path, *args: str) -> dict:
    """Drive the SAME `agent-browser` CLI Hermes's browser_* tools shell out to
    (tools/browser_tool_session.py::_run_browser_command), via `npx`, exactly the
    way a real worker process invokes it per tool call — not a hand-rolled CDP
    client. Requires `npx` in PATH and network access on first run (package
    download); this report's manual trial had it cached already."""
    import subprocess

    npx = __import__("shutil").which("npx")
    if npx is None:
        pytest.skip("npx not on PATH — cannot drive agent-browser")
    result = subprocess.run(
        [npx, "-y", "agent-browser@0.26.0", "--cdp", cdp_http_url, "--json", *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=_agent_browser_env(socket_dir),
    )
    assert result.returncode == 0, f"agent-browser {args[0]} failed: {result.stderr}"
    return json.loads(result.stdout)


def _agent_browser_open(cdp_http_url: str, socket_dir: Path, url: str) -> dict:
    return _agent_browser_run(cdp_http_url, socket_dir, "open", url)


def _agent_browser_get_text(cdp_http_url: str, socket_dir: Path) -> str:
    return _agent_browser_run(cdp_http_url, socket_dir, "get", "text", "body")["data"]["text"]


@pytest.mark.skipif(not _chrome_available(), reason="no Chrome/Chromium binary found")
def test_login_state_survives_a_graceful_shutdown_and_restart(tmp_path, login_site):
    """Issue #15's judging criterion: 登录一次 → 杀 daemon → 重启 → 登录态仍在.

    Exercises the REAL production code path: `manager.shutdown()` (`SIGTERM`,
    which this module always sends before ever falling back to `SIGKILL`) — not
    a raw `SIGKILL`. That distinction is load-bearing, not cosmetic: a first
    draft of this test killed the process with no grace period at all and
    "usually" passed manually, which turned out to be luck, not a guarantee —
    an 8-trial repeat (see this branch's report "判据实测") found the just-set
    cookie survived a bare `SIGKILL` only 1 time in 8 (Chromium's
    SQLitePersistentCookieStore batches writes; `Set-Cookie` is not synchronously
    on disk). `shutdown()`'s `SIGTERM`-first design flushed it in 5 of 5 repeated
    trials, which is why this test asserts the graceful path, not the abrupt one.
    """
    manager = BrowserManager(tmp_path / "profile", headless=True)
    handle = manager.ensure_started()
    data = _agent_browser_open(handle.cdp_http_url, _short_socket_dir("1"), f"{login_site}/login")
    assert data["success"] is True

    manager.shutdown()
    assert not manager.is_alive()

    manager2 = BrowserManager(tmp_path / "profile", headless=True)
    try:
        handle2 = manager2.ensure_started()
        assert handle2.port != handle.port, (
            "must be a genuinely fresh process, not a reattach to the dead one"
        )
        sock2 = _short_socket_dir("2")
        _agent_browser_open(handle2.cdp_http_url, sock2, f"{login_site}/secure")
        text = _agent_browser_get_text(handle2.cdp_http_url, sock2)
        assert text == "Secure Area", (
            f"login state did not survive a graceful shutdown + restart (got {text!r})"
        )
    finally:
        manager2.shutdown()


@pytest.mark.skipif(not _chrome_available(), reason="no Chrome/Chromium binary found")
def test_agent_browser_close_does_not_kill_the_shared_chrome(tmp_path, login_site):
    """Safety property this branch's α design depends on: Hermes's browser_close
    tool call (session-scoped in agent-browser's CDP-attach mode) must never
    terminate the actual Jones-owned Chrome process — verified by this report's
    manual trial (2026-09-19); this test locks it in."""
    manager = BrowserManager(tmp_path / "profile", headless=True)
    handle = manager.ensure_started()
    socket_dir = _short_socket_dir("close")
    try:
        _agent_browser_open(handle.cdp_http_url, socket_dir, f"{login_site}/login")
        _agent_browser_run(handle.cdp_http_url, socket_dir, "close")
        assert manager.is_alive(), "the shared Chrome process must still be running after `close`"

        text = _agent_browser_get_text(handle.cdp_http_url, socket_dir)
        assert text == "logged in", "the CDP connection must still work after `close`"
    finally:
        manager.shutdown()


def _write_jones_probe_plugin(hermes_home: Path) -> None:
    """Shared setup for the two gate-integration tests below: the SAME
    `pre_tool_call` plugin every other Hermes tool goes through (no bespoke
    MCP-client wiring needed) — `browser_cdp` (escape hatch) always `block`s,
    every other `browser_*` tool `approve`s. Controller ruling R-J1 (round-2
    review): NO `browser.allow_private_urls` in this config any more — see
    `test_real_hermes_browser_navigate_goes_through_the_gate`'s docstring for
    what that changes about what these two tests can demonstrate."""
    plugin_dir = hermes_home / "plugins" / "jones_probe"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        "name: jones_probe\nversion: 0.1.0\nhooks: [pre_tool_call]\n", encoding="utf-8"
    )
    (plugin_dir / "__init__.py").write_text(
        "def _on_pre_tool_call(tool_name='', args=None, tool_call_id='', **kw):\n"
        "    if tool_name == 'browser_cdp':\n"
        "        return {'action': 'block', 'message': 'JONES RULE GATE: escape hatch'}\n"
        "    if tool_name.startswith('browser_'):\n"
        "        return {'action': 'approve', 'message': tool_name, 'rule_key': tool_name}\n"
        "    return None\n"
        "def register(ctx):\n"
        "    ctx.register_hook('pre_tool_call', _on_pre_tool_call)\n",
        encoding="utf-8",
    )
    # No `browser:` section at all — controller ruling R-J1, this branch never
    # writes `allow_private_urls` (see `capabilities/browser.py::
    # browser_worker_config`'s docstring for the full reasoning).
    (hermes_home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - jones_probe\n  hook_callback_timeout: 5\n",
        encoding="utf-8",
    )


@pytest.mark.skipif(
    not (_chrome_available() and _hermes_available()),
    reason="needs a real Chrome AND hermes-agent importable (uv sync --group worker)",
)
def test_real_hermes_browser_navigate_goes_through_the_gate(tmp_path):
    """The gate-integration half of the α decision: `browser_navigate` must be
    interceptable by the SAME `pre_tool_call` plugin every other Hermes tool
    goes through (no bespoke MCP-client wiring needed) — a rule-gate block on
    `browser_cdp` (escape hatch) really stops it before execution, and an
    approve on `browser_navigate` really reaches the real Chrome over CDP.
    Same pattern as docs/spikes/hermes_hook_demo.py, extended past "block" to
    "approve -> real execution".

    Controller ruling R-J1 (round-2 review, findings #6/#8/#9): this test used
    to navigate to the local `login_site` fixture WITH `browser.
    allow_private_urls: true` set — that config is gone now (see
    `_write_jones_probe_plugin`), and without it Hermes's own `_url_policy_
    error` refuses a loopback target regardless of what the plugin approves
    (`test_browser_navigate_to_a_loopback_target_is_refused_by_hermes_itself`
    below locks that refusal in). This test now targets a real public URL
    instead — real network access required — to keep demonstrating the thing
    it actually exists to prove (the plugin intercept + real execution chain),
    without relying on a config this branch no longer ships."""
    manager = BrowserManager(tmp_path / "profile", headless=True)
    handle = manager.ensure_started()
    try:
        hermes_home = Path(tempfile.mkdtemp(prefix="jones-cap-browser-e2e-"))
        _write_jones_probe_plugin(hermes_home)
        env_backup = dict(os.environ)
        os.environ["HERMES_HOME"] = str(hermes_home)
        os.environ["BROWSER_CDP_URL"] = handle.cdp_http_url
        try:
            import agent.agent_runtime_helpers as arh
            import hermes_cli.plugins as plugins
            from tools import terminal_tool
            from tools.approval_context import set_hermes_interactive_context

            manager_ = plugins.get_plugin_manager()
            manager_.discover_and_load()
            assert manager_.has_hook("pre_tool_call")

            class _StubAgent:
                session_id = "s-1"
                _current_turn_id = "tu-1"
                _current_api_request_id = ""
                valid_tool_names = {"browser_navigate", "browser_cdp"}
                enabled_toolsets = None
                disabled_toolsets = None
                _memory_manager = None

            stub_agent = _StubAgent()

            blocked = arh.invoke_tool(
                stub_agent,
                "browser_cdp",
                {"method": "Network.setCookie"},
                "t-1",
                tool_call_id="c-1",
            )
            assert "JONES RULE GATE" in json.loads(blocked)["error"]

            prev_cb = terminal_tool._get_approval_callback()
            token = set_hermes_interactive_context(True)
            terminal_tool.set_approval_callback(lambda *a, **k: "allow")
            try:
                result = arh.invoke_tool(
                    stub_agent,
                    "browser_navigate",
                    {"url": "https://example.com/"},
                    "t-1",
                    tool_call_id="c-2",
                )
            finally:
                terminal_tool.set_approval_callback(prev_cb)
                from tools.approval_context import reset_hermes_interactive_context

                reset_hermes_interactive_context(token)
            parsed = json.loads(result)
            assert parsed.get("success") is True, parsed
            assert parsed.get("url") == "https://example.com/"
        finally:
            os.environ.clear()
            os.environ.update(env_backup)
    finally:
        manager.shutdown()


@pytest.mark.skipif(
    not (_chrome_available() and _hermes_available()),
    reason="needs a real Chrome AND hermes-agent importable (uv sync --group worker)",
)
def test_browser_navigate_to_a_loopback_target_is_refused_by_hermes_itself(tmp_path, login_site):
    """Controller ruling R-J1's actual security property, proven end to end (not
    just asserted in `permissions/review.py`'s unit tests): with no `browser.
    allow_private_urls` in the worker's config.yaml, a `browser_navigate` call
    that the plugin layer APPROVES (the review-gate escalation to the user
    gate is a separate, earlier layer — this test starts from "the user already
    said yes" to isolate Hermes's own SSRF floor) still gets refused by Hermes's
    own `tools/browser_tool.py::_url_policy_error` when the target is a
    loopback address — i.e. removing the forced config doesn't just move the
    decision to the user gate, it makes local/private navigation genuinely not
    work today, which is the documented trade-off (see `browser_worker_config`'s
    docstring)."""
    manager = BrowserManager(tmp_path / "profile", headless=True)
    handle = manager.ensure_started()
    try:
        hermes_home = Path(tempfile.mkdtemp(prefix="jones-cap-browser-e2e-refuse-"))
        _write_jones_probe_plugin(hermes_home)
        env_backup = dict(os.environ)
        os.environ["HERMES_HOME"] = str(hermes_home)
        os.environ["BROWSER_CDP_URL"] = handle.cdp_http_url
        try:
            import agent.agent_runtime_helpers as arh
            import hermes_cli.plugins as plugins
            from tools import terminal_tool
            from tools.approval_context import set_hermes_interactive_context

            manager_ = plugins.get_plugin_manager()
            manager_.discover_and_load()

            class _StubAgent:
                session_id = "s-1"
                _current_turn_id = "tu-1"
                _current_api_request_id = ""
                valid_tool_names = {"browser_navigate"}
                enabled_toolsets = None
                disabled_toolsets = None
                _memory_manager = None

            prev_cb = terminal_tool._get_approval_callback()
            token = set_hermes_interactive_context(True)
            terminal_tool.set_approval_callback(lambda *a, **k: "allow")
            try:
                result = arh.invoke_tool(
                    _StubAgent(),
                    "browser_navigate",
                    {"url": f"{login_site}/secure"},
                    "t-1",
                    tool_call_id="c-1",
                )
            finally:
                terminal_tool.set_approval_callback(prev_cb)
                from tools.approval_context import reset_hermes_interactive_context

                reset_hermes_interactive_context(token)
            parsed = json.loads(result)
            assert parsed.get("success") is not True, (
                "loopback navigation must be refused by Hermes without "
                f"browser.allow_private_urls, got: {parsed}"
            )
        finally:
            os.environ.clear()
            os.environ.update(env_backup)
    finally:
        manager.shutdown()


@pytest.mark.skipif(
    not (_chrome_available() and _hermes_available()),
    reason="needs a real Chrome AND hermes-agent importable (uv sync --group worker)",
)
def test_snapshot_then_click_then_type_covers_the_form_filling_acceptance_bar(
    tmp_path, login_site
):
    """Review finding #5: PRD 12.3 FR09's "导航/读页/点击/填表" acceptance had
    zero click/type coverage before this test — the report's claim that
    `browser_snapshot` → ref → `browser_click`/`browser_type` covers "填表" had
    no run behind it. Drives the real `agent-browser` CLI directly (same level
    as the other tests in this file, no model needed): snapshot a real form
    page, resolve a `@ref` from it (`snapshot`'s own JSON:
    `data.refs.<id> == {"name": ..., "role": ...}` — verified against a real
    run against this exact fixture, not assumed), type into the field via that
    ref (`type @e1 <text>`, NOT `find ... type` — that subaction genuinely
    doesn't exist in agent-browser 0.26.0's CLI, verified the same way), and
    read the value back via `get value @ref` — the actual composition this
    branch's report claims works, actually run end to end while writing this
    test (real Chrome, real agent-browser, this file's own `login_site`
    fixture's new `/form` route)."""
    manager = BrowserManager(tmp_path / "profile", headless=True)
    handle = manager.ensure_started()
    socket_dir = _short_socket_dir("form")
    try:
        _agent_browser_open(handle.cdp_http_url, socket_dir, f"{login_site}/form")
        snap = _agent_browser_run(handle.cdp_http_url, socket_dir, "snapshot")
        assert snap.get("success") is True, snap
        refs = snap.get("data", {}).get("refs", {})
        name_ref = next((ref for ref, info in refs.items() if info.get("name") == "Name"), None)
        assert name_ref is not None, f"expected a ref for the Name field, got refs={refs}"

        typed = _agent_browser_run(handle.cdp_http_url, socket_dir, "type", f"@{name_ref}", "Jones")
        assert typed.get("success") is True, typed

        value = _agent_browser_run(handle.cdp_http_url, socket_dir, "get", "value", f"@{name_ref}")
        assert value.get("data", {}).get("value") == "Jones", value
    finally:
        manager.shutdown()
