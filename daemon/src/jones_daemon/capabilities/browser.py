"""Jones-dedicated Chrome process lifecycle (Issue #15/#16, docs/design/00-foundation.md
§9 — rewritten by this branch from "复用 Playwright MCP" to "Hermes's own native
`browser_*` toolset, attached over CDP to a Chrome Jones itself launches and owns").

**What this module owns**: exactly one long-lived, headed Chrome process per Jones
install, launched with a Jones-exclusive `--user-data-dir` (never the user's
Default profile) and `--remote-debugging-port=0`. Hermes's own `browser_navigate` /
`browser_snapshot` / `browser_click` / ... tools (already in `_HERMES_CORE_TOOLS`,
see the report's "调研结论" for the source citations) attach to it via CDP
(`BROWSER_CDP_URL` env on the worker process) — this module never speaks the
browser automation protocol itself, it only starts/stops/discovers the Chrome
process. Per §9's "不允许两套同时开" (no dual browser stacks), nothing here spawns
or configures a Playwright/MCP browser server.

**Crash/restart model** (see report "判据实测" for the full empirical basis,
including a correction to this module's first draft): login state lives on disk
in the profile directory's cookie/session storage, not in process continuity —
but that disk write is NOT synchronous with `Set-Cookie`. Chromium's
SQLitePersistentCookieStore batches writes; an abrupt `SIGKILL` immediately after
login lost the just-set cookie in 7 of 8 repeated trials in this report (only
~1/8 survived — an early, too-small 2-trial manual check had wrongly suggested
this was safe; the automated repeat run caught the race). A graceful shutdown —
`SIGTERM` and letting Chrome exit on its own, which THIS module's `shutdown()`
always does — reliably flushed the cookie in every one of 5 repeated trials. So
`shutdown()` sending `SIGTERM` before ever falling back to `SIGKILL` is
load-bearing, not a nice-to-have. `ensure_started()` does the simple, robust
thing on every call regardless of which way the previous process went down: if a
Chrome process already answers CDP on the port recorded in this profile's
`DevToolsActivePort` file, reuse it (covers "daemon restarted, Chrome survived
as an orphan" — Unix does not kill children when a parent dies); otherwise
launch a fresh one against the *same* profile directory (covers "Chrome died
together with the daemon" — whatever login state had already reached disk is
still there, up to the SIGTERM-vs-SIGKILL caveat above). This also sidesteps
Chrome's single-instance lock entirely: we never attempt to open a second process
against a profile another live Chrome already holds (docs/spikes/04-browser-login-state.md
"往已运行的 profile 上补开调试端口会被单实例锁吞掉" — that failure mode is only
reachable if you launch *without* first checking for a live owner, which this
module never does).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from jones_daemon.logging import get_logger

logger = get_logger("capabilities.browser")

# How long ensure_started() will wait for a freshly-launched Chrome to write
# DevToolsActivePort before giving up (PRD 11.1 doesn't set a browser-specific
# cold-start budget; this is generous relative to the ~1-2s observed in this
# report's real trials, not a performance target).
DEFAULT_LAUNCH_TIMEOUT_S = 10.0
# Graceful-shutdown budget: SIGTERM, then poll for exit, then SIGKILL. Real Chrome
# in this report's trials exited within ~0.3s of SIGTERM; this is a safety margin,
# not a measured requirement.
DEFAULT_SHUTDOWN_TIMEOUT_S = 5.0
_PORT_POLL_INTERVAL_S = 0.05
_LIVENESS_PROBE_TIMEOUT_S = 1.0

_DEVTOOLS_ACTIVE_PORT_FILE = "DevToolsActivePort"

# Common install locations, checked in order; overridable with JONES_CHROME_BINARY
# for dev machines / CI images with a nonstandard install path (diagnostic honesty:
# an unfound binary is a clear BrowserLaunchError, never a silent no-op).
_CHROME_CANDIDATES: tuple[str, ...] = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
)


class BrowserLaunchError(Exception):
    """Chrome could not be started or did not become reachable over CDP in time.
    Callers (H's `_prepare_hermes_home` wiring) must surface this as an explicit
    error card, never a silent fallback (DEV.md 工程原则 #4, §9 "诚实失败")."""


def _find_chrome_binary() -> str:
    override = os.environ.get("JONES_CHROME_BINARY", "").strip()
    if override:
        if not Path(override).exists():
            raise BrowserLaunchError(f"JONES_CHROME_BINARY={override!r} does not exist")
        return override
    for candidate in _CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    found = shutil.which("google-chrome") or shutil.which("chromium")
    if found:
        return found
    raise BrowserLaunchError(
        "No Chrome/Chromium binary found (checked "
        f"{', '.join(_CHROME_CANDIDATES)}, PATH); set JONES_CHROME_BINARY to override."
    )


@dataclass
class BrowserHandle:
    """A live, CDP-reachable Chrome process this module either launched or
    reattached to."""

    pid: int
    profile_dir: Path
    port: int
    ws_path: str
    process: subprocess.Popen | None  # None when reattached to an orphan we didn't spawn

    @property
    def cdp_http_url(self) -> str:
        # What `BROWSER_CDP_URL` / `browser.cdp_url` expects (tools/browser_tool_cdp.py
        # ::_resolve_cdp_override resolves this via /json/version -> webSocketDebuggerUrl
        # itself; passing the bare http root is sufficient and is what this report's
        # probe verified end to end).
        return f"http://127.0.0.1:{self.port}"

    @property
    def cdp_ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}{self.ws_path}"


def _read_devtools_active_port(profile_dir: Path) -> tuple[int, str] | None:
    path = profile_dir / _DEVTOOLS_ACTIVE_PORT_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.splitlines()
    if len(lines) < 2 or not lines[0].strip().isdigit():
        return None
    return int(lines[0].strip()), lines[1].strip()


def _probe_cdp_alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=_LIVENESS_PROBE_TIMEOUT_S
        ) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


SpawnFn = Callable[..., subprocess.Popen]


class BrowserManager:
    """Owns the single Jones-dedicated Chrome process for one profile directory.
    Not thread-safe by itself; callers serialize through `get_browser_manager`'s
    module-level cache + the daemon's single-threaded asyncio event loop (RPC
    handlers never run concurrently against the same manager on separate OS
    threads today — see report "并发" note if that changes)."""

    def __init__(
        self,
        profile_dir: Path,
        *,
        chrome_binary: str | None = None,
        headless: bool = False,
        launch_timeout_s: float = DEFAULT_LAUNCH_TIMEOUT_S,
        spawn: SpawnFn | None = None,
    ) -> None:
        self._profile_dir = profile_dir
        self._chrome_binary = chrome_binary
        self._headless = headless
        self._launch_timeout_s = launch_timeout_s
        # Injectable for unit tests (no real Chrome needed) — must return a
        # subprocess.Popen-like object supporting .pid/.poll()/.wait()/.terminate()/.kill().
        self._spawn: SpawnFn = spawn or subprocess.Popen
        self._handle: BrowserHandle | None = None

    @property
    def profile_dir(self) -> Path:
        return self._profile_dir

    def is_alive(self) -> bool:
        if self._handle is None:
            return False
        if self._handle.process is not None and self._handle.process.poll() is not None:
            return False
        return _probe_cdp_alive(self._handle.port)

    def ensure_started(self) -> BrowserHandle:
        """Idempotent: reuse a live handle, reattach to a live orphan (profile
        survived a daemon crash — see module docstring), or launch fresh."""
        if self.is_alive():
            assert self._handle is not None
            return self._handle

        self._profile_dir.mkdir(parents=True, exist_ok=True)
        self._profile_dir.chmod(0o700)

        existing = _read_devtools_active_port(self._profile_dir)
        if existing is not None and _probe_cdp_alive(existing[0]):
            port, ws_path = existing
            logger.info(
                "browser: reattached to a live orphan Chrome",
                extra={"detail": {"profile_dir": str(self._profile_dir), "port": port}},
            )
            self._handle = BrowserHandle(
                pid=-1, profile_dir=self._profile_dir, port=port, ws_path=ws_path, process=None
            )
            return self._handle

        return self._launch()

    def _launch(self) -> BrowserHandle:
        active_port_file = self._profile_dir / _DEVTOOLS_ACTIVE_PORT_FILE
        # Stale file from a dead process would otherwise be indistinguishable from
        # a fresh one — delete it so the poll loop below unambiguously waits for
        # THIS launch's write, not a leftover.
        active_port_file.unlink(missing_ok=True)

        binary = self._chrome_binary or _find_chrome_binary()
        argv = [
            binary,
            f"--user-data-dir={self._profile_dir}",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if self._headless:
            # Production default is headed (§9: the user must be able to see this
            # window to log in). Tests set headless=True for CI machines with no
            # display — never flip this default in non-test code.
            argv.append("--headless=new")
        logger.info(
            "browser: launching Jones Chrome",
            extra={
                "detail": {
                    "binary": binary,
                    "profile_dir": str(self._profile_dir),
                    "headless": self._headless,
                }
            },
        )
        process = self._spawn(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL
        )

        deadline = time.monotonic() + self._launch_timeout_s
        port_info: tuple[int, str] | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise BrowserLaunchError(
                    f"Chrome exited during startup (returncode={process.returncode}); "
                    "see profile_dir for logs if any were captured"
                )
            port_info = _read_devtools_active_port(self._profile_dir)
            if port_info is not None:
                break
            time.sleep(_PORT_POLL_INTERVAL_S)
        if port_info is None:
            with _terminate_best_effort(process):
                pass
            raise BrowserLaunchError(
                f"Chrome did not write {_DEVTOOLS_ACTIVE_PORT_FILE} within "
                f"{self._launch_timeout_s}s (profile_dir={self._profile_dir})"
            )
        port, ws_path = port_info
        if not _probe_cdp_alive(port):
            raise BrowserLaunchError(
                f"Chrome wrote {_DEVTOOLS_ACTIVE_PORT_FILE} (port={port}) but CDP did not answer"
            )
        self._handle = BrowserHandle(
            pid=process.pid,
            profile_dir=self._profile_dir,
            port=port,
            ws_path=ws_path,
            process=process,
        )
        return self._handle

    def shutdown(self, *, timeout_s: float = DEFAULT_SHUTDOWN_TIMEOUT_S) -> None:
        """SIGTERM, wait, SIGKILL on timeout. No-op if this manager didn't spawn
        the process itself (a reattached orphan — see module docstring; killing a
        process this instance doesn't own would be a different daemon's browser
        the next time it starts, not a leak this instance is responsible for)."""
        handle = self._handle
        if handle is None or handle.process is None:
            self._handle = None
            return
        process = handle.process
        if process.poll() is not None:
            self._handle = None
            return
        logger.info("browser: shutting down Jones Chrome", extra={"detail": {"pid": handle.pid}})
        process.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self._handle = None
                return
            time.sleep(_PORT_POLL_INTERVAL_S)
        logger.warning(
            "browser: SIGTERM did not exit Chrome in time, sending SIGKILL",
            extra={"detail": {"pid": handle.pid, "timeout_s": timeout_s}},
        )
        process.kill()
        process.wait()
        self._handle = None


class _terminate_best_effort:
    """Context manager used only for the "Chrome never wrote DevToolsActivePort"
    failure path in `_launch` — cleans up the half-started process without
    letting a cleanup error mask the real `BrowserLaunchError`."""

    def __init__(self, process: subprocess.Popen) -> None:
        self._process = process

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc_info: object) -> bool:
        try:
            if self._process.poll() is None:
                self._process.kill()
                self._process.wait(timeout=2)
        except Exception:
            logger.warning("browser: cleanup of a failed launch itself failed", exc_info=True)
        return False  # never swallow the caller's exception


_MANAGERS: dict[Path, BrowserManager] = {}


def get_browser_manager(user_root: Path, **kwargs: object) -> BrowserManager:
    """One `BrowserManager` per Jones install (`<user_root>/browser/profile`,
    §9) — a process-wide cache so every session's `browser_worker_config` call
    reuses the same Chrome instead of each one trying to own its own."""
    profile_dir = Path(user_root) / "browser" / "profile"
    manager = _MANAGERS.get(profile_dir)
    if manager is None:
        manager = BrowserManager(profile_dir, **kwargs)  # type: ignore[arg-type]
        _MANAGERS[profile_dir] = manager
    return manager


def shutdown_all(*, timeout_s: float = DEFAULT_SHUTDOWN_TIMEOUT_S) -> None:
    """Daemon-exit hook: gracefully close every Chrome this process spawned."""
    for manager in list(_MANAGERS.values()):
        manager.shutdown(timeout_s=timeout_s)


def browser_worker_config(ctx: object, session: object = None) -> dict:
    """Env + config.yaml fragment for a worker's Hermes browser_* toolset to
    attach to the Jones Chrome over CDP (03-w4-interfaces.md §4, §1's shared-change
    grant on `workers/manager.py::_prepare_hermes_home` — H calls this once that
    branch lands; until then this report's own tests call it directly).

    `ctx` is duck-typed to tolerate whatever shape H's ServiceContext ends up
    being: a `.user_root` attribute (callable or plain `Path`), OR (in this
    branch's own tests, and any caller that already has the path) a bare `Path`.
    `session` is accepted for interface stability but unused: §9 is one
    Jones-wide Chrome, not one per session — see the report's "契约变更" section
    for why a later per-session-isolation requirement would need to revisit this
    signature, not silently branch on `session` today.

    Ensures the browser is actually running (lazy start) before returning, so a
    worker that gets this config can attach immediately; on launch failure this
    raises `BrowserLaunchError` — callers must turn that into an explicit error
    card (§9 "诚实失败"), never omit the browser toolset silently.
    """
    user_root = getattr(ctx, "user_root", ctx)
    if callable(user_root):
        user_root = user_root()
    manager = get_browser_manager(Path(user_root))
    handle = manager.ensure_started()
    return {
        "env": {"BROWSER_CDP_URL": handle.cdp_http_url},
        # tools/browser_tool_cloud.py::_is_local_backend: "A CDP override is never
        # trusted as local (that Chrome may live off-host)" — Jones's Chrome
        # genuinely is on the same host as the worker, so the SSRF guard on
        # private/LAN URLs must be explicitly lifted or every intranet/localhost
        # navigate is blocked (see report "调研结论" for the source citation and
        # the probe that hit this before this field was added).
        "config_yaml": {"browser": {"allow_private_urls": True}},
    }
