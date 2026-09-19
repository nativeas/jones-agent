"""`capabilities/browser.py` process-lifecycle tests against `fake_chrome.py`
(docs/design/00-foundation.md §9, 03-w4-interfaces.md §4, Issue #15).

Real subprocess, real SIGTERM/SIGKILL, real HTTP CDP-liveness probe — no real
Chrome binary needed (see fake_chrome.py's docstring for why this is the right
level of fakery, matching `fake_acp_agent.py`'s role for the ACP worker). The
Chrome-binary-required, real-login-persistence scenario from Issue #15's
acceptance checklist lives in `tests/integration/test_cap_browser_e2e.py`,
gated on `JONES_E2E=1` + a real Chrome install.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from jones_daemon.capabilities import browser as browser_module
from jones_daemon.capabilities.browser import (
    BrowserLaunchError,
    BrowserManager,
    _find_chrome_binary,
    _probe_cdp_alive,
    browser_worker_config,
    get_browser_manager,
)

_FAKE_CHROME = str(Path(__file__).parent / "fake_chrome.py")


def _fake_spawn(argv, **kwargs):
    """Replace the (fake, never-checked-for-existence) chrome_binary in argv[0]
    with `sys.executable fake_chrome.py`, keeping every real flag BrowserManager
    built (--user-data-dir=..., --remote-debugging-port=0, ...)."""
    import subprocess

    real_argv = [sys.executable, _FAKE_CHROME, *argv[1:]]
    return subprocess.Popen(real_argv, **kwargs)


def _make_manager(tmp_path, **kwargs) -> BrowserManager:
    return BrowserManager(
        tmp_path / "profile",
        chrome_binary="fake-chrome-sentinel",
        spawn=_fake_spawn,
        launch_timeout_s=kwargs.pop("launch_timeout_s", 5.0),
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _clear_manager_cache():
    # get_browser_manager caches by profile_dir; tests use fresh tmp_paths so
    # this mostly doesn't matter, but keep it hermetic between tests anyway.
    browser_module._MANAGERS.clear()
    yield
    browser_module._MANAGERS.clear()


def test_ensure_started_launches_and_is_idempotent(tmp_path):
    manager = _make_manager(tmp_path)
    assert not manager.is_alive()

    handle1 = manager.ensure_started()
    try:
        assert manager.is_alive()
        assert handle1.port > 0
        assert handle1.cdp_http_url == f"http://127.0.0.1:{handle1.port}"
        assert _probe_cdp_alive(handle1.port)

        handle2 = manager.ensure_started()
        assert handle2 is handle1, "second call must reuse the live process, not relaunch"
    finally:
        manager.shutdown()
    assert not manager.is_alive()


def test_shutdown_sigterm_then_process_exits(tmp_path):
    manager = _make_manager(tmp_path)
    handle = manager.ensure_started()
    process = handle.process
    assert process is not None

    t0 = time.monotonic()
    manager.shutdown(timeout_s=5.0)
    elapsed = time.monotonic() - t0

    assert process.poll() is not None, "process must have exited"
    assert process.returncode == 0, (
        "SIGTERM path exits cleanly (real Chrome does too, see report)"
    )
    assert elapsed < 4.0, (
        f"graceful SIGTERM path should not need the SIGKILL fallback (took {elapsed:.2f}s)"
    )


def test_shutdown_falls_back_to_sigkill_when_sigterm_ignored(tmp_path):
    manager = _make_manager(tmp_path)
    manager._spawn = lambda argv, **kw: _fake_spawn([*argv, "--fake-ignore-sigterm"], **kw)
    handle = manager.ensure_started()
    process = handle.process
    assert process is not None

    manager.shutdown(timeout_s=0.5)

    assert process.poll() is not None, "SIGKILL fallback must still terminate the process"
    assert not manager.is_alive()


def test_ensure_started_reattaches_to_a_live_orphan_after_manager_replaced(tmp_path):
    """Simulates a daemon restart: the OLD BrowserManager instance is discarded
    (as if the daemon process had been killed), but the Chrome subprocess itself
    is still running (Unix doesn't kill children when their parent dies) — the
    NEW manager instance must reattach via the still-valid DevToolsActivePort
    file + a liveness probe, never try to spawn a second Chrome against the same
    profile (which, against a real Chrome, hits the single-instance-lock failure
    mode documented in docs/spikes/04-browser-login-state.md)."""
    profile_dir = tmp_path / "profile"
    old_manager = BrowserManager(
        profile_dir, chrome_binary="fake-chrome-sentinel", spawn=_fake_spawn
    )
    old_handle = old_manager.ensure_started()
    try:
        spawn_calls = []
        new_manager = BrowserManager(
            profile_dir,
            chrome_binary="fake-chrome-sentinel",
            spawn=lambda *a, **kw: (spawn_calls.append(1), _fake_spawn(*a, **kw))[1],
        )
        new_handle = new_manager.ensure_started()

        assert new_handle.port == old_handle.port, "must reattach to the SAME live process"
        assert new_handle.process is None, "reattached handle does not own the process"
        assert not spawn_calls, "must not spawn a second Chrome while the first is still alive"
    finally:
        old_manager.shutdown()


def test_ensure_started_launches_fresh_when_stale_devtools_port_file_present(tmp_path):
    """The opposite crash scenario: Chrome died too (e.g. same SIGKILL that took
    the daemon down also took its child). The profile directory's
    DevToolsActivePort file is now stale (points at a dead port) — must be
    detected and a fresh Chrome launched, not an error."""
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True)
    (profile_dir / "DevToolsActivePort").write_text(
        "59999\n/devtools/browser/dead\n", encoding="utf-8"
    )

    manager = BrowserManager(profile_dir, chrome_binary="fake-chrome-sentinel", spawn=_fake_spawn)
    try:
        handle = manager.ensure_started()
        assert handle.port != 59999
        assert _probe_cdp_alive(handle.port)
    finally:
        manager.shutdown()


def test_launch_error_when_process_exits_immediately(tmp_path):
    import subprocess

    def _dying_spawn(argv, **kwargs):
        # A real Chrome that fails to start exits without ever writing
        # DevToolsActivePort — simulate with `false`-equivalent: python -c exit(1).
        return subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(1)"], **kwargs)

    manager = BrowserManager(
        tmp_path / "profile", chrome_binary="fake-chrome-sentinel", spawn=_dying_spawn
    )
    with pytest.raises(BrowserLaunchError, match="exited during startup"):
        manager.ensure_started()


def test_launch_error_when_devtools_port_never_appears(tmp_path):
    import subprocess

    def _silent_spawn(argv, **kwargs):
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)

    manager = BrowserManager(
        tmp_path / "profile",
        chrome_binary="fake-chrome-sentinel",
        spawn=_silent_spawn,
        launch_timeout_s=0.3,
    )
    try:
        with pytest.raises(BrowserLaunchError, match="did not write"):
            manager.ensure_started()
    finally:
        # best-effort: the dying-check inside _launch already kills it, but be
        # sure this test doesn't leak a sleeping child either way.
        for m in list(browser_module._MANAGERS.values()):
            m.shutdown()


def test_find_chrome_binary_honors_override(tmp_path, monkeypatch):
    fake_binary = tmp_path / "not-really-chrome"
    fake_binary.write_text("", encoding="utf-8")
    monkeypatch.setenv("JONES_CHROME_BINARY", str(fake_binary))
    assert _find_chrome_binary() == str(fake_binary)


def test_find_chrome_binary_raises_clear_error_when_nothing_found(monkeypatch):
    # Empty the hardcoded candidate list too — a dev/CI machine with a real Chrome
    # installed at one of those fixed paths would otherwise find it regardless of
    # PATH, defeating this test's "nothing found anywhere" scenario.
    monkeypatch.setattr(browser_module, "_CHROME_CANDIDATES", ())
    monkeypatch.delenv("JONES_CHROME_BINARY", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(BrowserLaunchError, match="No Chrome/Chromium binary found"):
        _find_chrome_binary()


def test_get_browser_manager_is_a_process_wide_singleton_per_profile(tmp_path):
    m1 = get_browser_manager(tmp_path)
    m2 = get_browser_manager(tmp_path)
    assert m1 is m2
    assert m1.profile_dir == tmp_path / "browser" / "profile"


def test_browser_worker_config_duck_types_ctx_and_lazy_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_module, "_find_chrome_binary", lambda: "fake-chrome-sentinel")
    manager = get_browser_manager(tmp_path)
    manager._spawn = _fake_spawn
    manager._chrome_binary = "fake-chrome-sentinel"

    class _CtxWithMethod:
        def user_root(self) -> Path:
            return tmp_path

    class _CtxWithAttr:
        user_root = tmp_path

    try:
        cfg1 = browser_worker_config(_CtxWithMethod())
        assert cfg1["env"]["BROWSER_CDP_URL"].startswith("http://127.0.0.1:")
        assert cfg1["config_yaml"] == {"browser": {"allow_private_urls": True}}

        # A bare Path ctx (what this branch's own probe/E2E tests pass) works too.
        cfg2 = browser_worker_config(tmp_path)
        assert (
            cfg2["env"]["BROWSER_CDP_URL"] == cfg1["env"]["BROWSER_CDP_URL"]
        ), "reuses the same running Chrome"

        cfg3 = browser_worker_config(_CtxWithAttr())
        assert cfg3["env"]["BROWSER_CDP_URL"] == cfg1["env"]["BROWSER_CDP_URL"]
    finally:
        manager.shutdown()
