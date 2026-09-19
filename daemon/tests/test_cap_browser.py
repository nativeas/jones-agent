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

import signal
import sys
import threading
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


def test_shutdown_of_a_reattached_orphan_sends_a_real_sigterm_via_recovered_pid(tmp_path):
    """Review finding #10: after a simulated daemon restart, `ensure_started()`
    reattaches with `handle.process is None` (no `Popen` object) — `shutdown()`
    must still gracefully close it by recovering its real pid from Chrome's own
    `SingletonLock` symlink, not permanently no-op and leak it."""
    profile_dir = tmp_path / "profile"
    old_manager = BrowserManager(
        profile_dir, chrome_binary="fake-chrome-sentinel", spawn=_fake_spawn
    )
    old_handle = old_manager.ensure_started()
    real_process = old_handle.process
    assert real_process is not None

    new_manager = BrowserManager(
        profile_dir, chrome_binary="fake-chrome-sentinel", spawn=_fake_spawn
    )
    new_handle = new_manager.ensure_started()
    assert new_handle.process is None
    assert new_handle.pid == real_process.pid, "must recover the real pid from SingletonLock"

    new_manager.shutdown(timeout_s=5.0)

    real_process.wait(timeout=3)
    assert real_process.poll() is not None, "the orphan must actually receive SIGTERM/SIGKILL"


def test_shutdown_of_reattached_orphan_without_recoverable_pid_is_a_safe_noop(tmp_path):
    """The degraded case: no `SingletonLock` to read (e.g. an unusual Chrome
    variant, or a permissions issue) — `shutdown()` must not crash and must not
    touch a process it can't identify, just warn and clear its own handle."""
    profile_dir = tmp_path / "profile"
    old_manager = BrowserManager(
        profile_dir, chrome_binary="fake-chrome-sentinel", spawn=_fake_spawn
    )
    old_handle = old_manager.ensure_started()
    real_process = old_handle.process
    try:
        (profile_dir / "SingletonLock").unlink()

        new_manager = BrowserManager(
            profile_dir, chrome_binary="fake-chrome-sentinel", spawn=_fake_spawn
        )
        new_handle = new_manager.ensure_started()
        assert new_handle.process is None
        assert new_handle.pid == -1, "pid must not be recoverable without SingletonLock"

        new_manager.shutdown()  # must not raise
        assert real_process.poll() is None, "must never touch a process it can't identify"
    finally:
        real_process.send_signal(signal.SIGTERM)
        real_process.wait(timeout=3)


def test_launch_cleans_up_process_when_devtools_port_written_but_cdp_never_answers(tmp_path):
    """Review findings #3/#11: the "port file appeared, CDP never answered"
    failure path in `_launch` used to `raise` without cleanup, leaking the
    half-started process (which still holds the profile's single-instance
    lock) — unlike the sibling "port file never appeared" path just above it,
    which already cleaned up."""
    spawned: list = []

    def _no_cdp_spawn(argv, **kwargs):
        proc = _fake_spawn([*argv, "--fake-no-cdp-response"], **kwargs)
        spawned.append(proc)
        return proc

    manager = BrowserManager(
        tmp_path / "profile",
        chrome_binary="fake-chrome-sentinel",
        spawn=_no_cdp_spawn,
        launch_timeout_s=2.0,
    )
    with pytest.raises(BrowserLaunchError, match="CDP did not answer"):
        manager.ensure_started()

    assert spawned, "spawn must have been called"
    process = spawned[0]
    process.wait(timeout=3)
    assert process.poll() is not None, "the half-started Chrome must be cleaned up, not leaked"


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


def test_get_browser_manager_serializes_concurrent_callers_for_the_same_profile(tmp_path):
    """Round-3 review finding #12: `get_browser_manager`'s `_MANAGERS` cache used
    to be a plain check-then-set with no lock — two Sessions racing
    `browser_worker_config` (which dispatches here from `asyncio.to_thread`, i.e.
    real OS threads) could each observe the cache empty and install their own
    `BrowserManager` for the same profile_dir, silently defeating the "one
    Chrome per install" guarantee. `_MANAGERS_LOCK` now serializes this."""
    barrier = threading.Barrier(8)
    results: list[BrowserManager] = []

    def _worker() -> None:
        barrier.wait(timeout=5)
        results.append(get_browser_manager(tmp_path))

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(results) == 8
    assert len({id(m) for m in results}) == 1, (
        "every concurrent caller must get back the SAME manager instance"
    )


def test_ensure_started_serializes_concurrent_callers_launching_only_one_chrome(tmp_path):
    """Round-3 review finding #12: `browser_worker_config` runs
    `manager.ensure_started()` via `asyncio.to_thread` — two Sessions calling it
    at the same time really do call `ensure_started()` on the SAME manager from
    two different OS threads. Before `BrowserManager._lock` existed, both threads
    could see `is_alive()` == False at once (spawning even a fake Chrome
    subprocess and waiting for it to write DevToolsActivePort takes long enough
    to leave that window open) and both fall through to `_launch()` — deleting
    each other's DevToolsActivePort file and starting two real Chrome processes
    against the same `--user-data-dir`. This proves that no longer happens:
    exactly one Chrome gets spawned, and every concurrent caller ends up with a
    handle pointing at the SAME live port."""
    manager = _make_manager(tmp_path)
    spawn_calls: list[object] = []
    real_spawn = manager._spawn

    def _counting_spawn(argv, **kwargs):
        spawn_calls.append(argv)
        return real_spawn(argv, **kwargs)

    manager._spawn = _counting_spawn

    barrier = threading.Barrier(5)
    results: list[object] = []
    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            barrier.wait(timeout=5)
            results.append(manager.ensure_started())
        except BaseException as exc:  # noqa: BLE001 - surfaced via `errors` below, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    try:
        assert not errors, errors
        assert len(results) == 5
        ports = {handle.port for handle in results}
        assert len(ports) == 1, (
            f"expected every concurrent caller to land on the same live Chrome, got ports={ports}"
        )
        assert len(spawn_calls) == 1, (
            f"expected exactly one real Chrome spawn, got {len(spawn_calls)} — the lock is not "
            "serializing concurrent ensure_started() calls (review finding #12)"
        )
    finally:
        manager.shutdown()


async def test_browser_worker_config_duck_types_ctx_and_lazy_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_module, "_find_chrome_binary", lambda: "fake-chrome-sentinel")
    manager = get_browser_manager(tmp_path)
    manager._spawn = _fake_spawn
    manager._chrome_binary = "fake-chrome-sentinel"

    class _CtxWithMethod:
        def user_root(self) -> Path:
            return tmp_path

    class _CtxWithAttr:
        user_root = tmp_path

    class _FakePaths:
        @staticmethod
        def user_root() -> Path:
            return tmp_path

    class _CtxLikeRealDaemonContext:
        # Round-2 review finding #10: shaped like the real
        # `jones_daemon.context.DaemonContext` — a `.paths` attribute that is
        # itself a module-like object with a `user_root()` callable, and NO
        # `.user_root` of its own. The pre-fix `getattr(ctx, "user_root", ctx)`
        # duck-type fell through to `ctx` itself here (neither callable nor a
        # PathLike), which raised `TypeError` inside `Path(ctx)` instead of the
        # documented `BrowserLaunchError` contract.
        paths = _FakePaths

    try:
        cfg1 = await browser_worker_config(_CtxWithMethod())
        assert cfg1["env"]["BROWSER_CDP_URL"].startswith("http://127.0.0.1:")
        # Controller ruling R-J1 (round-2 review, findings #6/#8/#9): never
        # force browser.allow_private_urls — that flag also disables Hermes's
        # own SSRF/scheme checks for web_extract/vision/skills_hub, not just
        # the browser tools, and a same-origin redirect defeats the
        # navigate-time compensating control anyway.
        assert cfg1["config_yaml"] == {}

        # A bare Path ctx (what this branch's own probe/E2E tests pass) works too.
        cfg2 = await browser_worker_config(tmp_path)
        assert (
            cfg2["env"]["BROWSER_CDP_URL"] == cfg1["env"]["BROWSER_CDP_URL"]
        ), "reuses the same running Chrome"

        cfg3 = await browser_worker_config(_CtxWithAttr())
        assert cfg3["env"]["BROWSER_CDP_URL"] == cfg1["env"]["BROWSER_CDP_URL"]

        cfg4 = await browser_worker_config(_CtxLikeRealDaemonContext())
        assert cfg4["env"]["BROWSER_CDP_URL"] == cfg1["env"]["BROWSER_CDP_URL"]
    finally:
        manager.shutdown()
