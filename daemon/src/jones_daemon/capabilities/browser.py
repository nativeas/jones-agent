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

import asyncio
import os
import shutil
import signal
import subprocess
import threading
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


_SINGLETON_LOCK_FILE = "SingletonLock"


def _read_singleton_lock_pid(profile_dir: Path) -> int | None:
    """Review finding #10: recover the real pid of a Chrome we reattached to
    (never spawned ourselves, so we have no `Popen` handle for it) from
    Chrome's own single-instance guard. `<profile_dir>/SingletonLock` is a
    symlink Chrome creates whose target is `<hostname>-<pid>` of whichever
    process holds the profile — reading it is the only way to get a pid back
    for an orphan, and without one `shutdown()` could only ever no-op for it
    (permanently, for the rest of this machine's life: once reattached, always
    reattached — see `shutdown()`'s docstring for why that used to mean the
    §9.1 login-state guarantee silently stopped applying after the first
    daemon restart)."""
    target = None
    try:
        target = os.readlink(profile_dir / _SINGLETON_LOCK_FILE)
    except OSError:
        return None
    # Target is "<hostname>-<pid>"; hostname itself may contain "-", so split
    # from the right.
    _, _, pid_str = target.rpartition("-")
    if not pid_str.isdigit():
        return None
    return int(pid_str)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, just not ours to signal-probe further than this — treat as
        # alive (matches os.kill's own semantics: EPERM means the pid exists).
        return True
    return True


SpawnFn = Callable[..., subprocess.Popen]


class BrowserManager:
    """Owns the single Jones-dedicated Chrome process for one profile directory.

    **Round-2 review finding #12**: the previous version of this docstring
    claimed callers "serialize through the daemon's single-threaded asyncio
    event loop (RPC handlers never run concurrently ... on separate OS
    threads today)" — that was already false the moment `browser_worker_config`
    became `async def` (controller ruling R-J4) and started running its blocking
    work via `asyncio.to_thread(manager.ensure_started)`: two sessions racing
    `browser_worker_config` concurrently now genuinely dispatch to
    `ensure_started()` on separate OS threads, at the same time the
    single-threaded-event-loop claim was describing as impossible. `self._lock`
    (below) is what actually makes this safe now — `ensure_started()` holds it
    for its entire body, so two concurrent callers serialize into "one launches,
    the other reuses the live handle" instead of racing `_launch()` (which would
    otherwise: possibly construct two `BrowserManager`s for the same profile via
    `get_browser_manager`'s check-then-set — see `_MANAGERS_LOCK` there; delete
    each other's `DevToolsActivePort` file via `_launch()`'s
    `unlink(missing_ok=True)`; or spawn two real Chrome processes against the
    same `--user-data-dir` and collide on Chrome's own single-instance lock)."""

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
        # Round-2 review finding #12: serializes `ensure_started()` across OS
        # threads (see class docstring above for why this is now load-bearing,
        # not defensive).
        self._lock = threading.Lock()

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
        survived a daemon crash — see module docstring), or launch fresh.

        Round-2 review finding #12: holds `self._lock` for the whole body —
        `browser_worker_config`'s `asyncio.to_thread(manager.ensure_started)`
        means two Sessions can now call this on the SAME manager from two
        different OS threads at once; without the lock both could pass the
        `is_alive()` check as False concurrently and both fall through to
        `_launch()` (see class docstring for the failure modes that opens up)."""
        with self._lock:
            if self.is_alive():
                assert self._handle is not None
                return self._handle

            self._profile_dir.mkdir(parents=True, exist_ok=True)
            self._profile_dir.chmod(0o700)

            existing = _read_devtools_active_port(self._profile_dir)
            if existing is not None and _probe_cdp_alive(existing[0]):
                port, ws_path = existing
                # Review finding #10: recover a real pid so `shutdown()` can still
                # gracefully SIGTERM this Chrome later — without it, this instance
                # would have no way to ever signal a process it didn't spawn.
                recovered_pid = _read_singleton_lock_pid(self._profile_dir)
                if recovered_pid is None:
                    logger.warning(
                        "browser: reattached to a live orphan Chrome but could not recover "
                        "its pid from SingletonLock — shutdown() will not be able to signal "
                        "it (review finding #10)",
                        extra={"detail": {"profile_dir": str(self._profile_dir)}},
                    )
                logger.info(
                    "browser: reattached to a live orphan Chrome",
                    extra={
                        "detail": {
                            "profile_dir": str(self._profile_dir),
                            "port": port,
                            "recovered_pid": recovered_pid,
                        }
                    },
                )
                self._handle = BrowserHandle(
                    pid=recovered_pid if recovered_pid is not None else -1,
                    profile_dir=self._profile_dir, port=port, ws_path=ws_path, process=None,
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
            # Review findings #3/#11: unlike the "port file never appeared" path
            # just above, this one used to `raise` without cleanup — the process
            # was still alive and holding the profile's single-instance lock, so
            # the NEXT `ensure_started()` would find a dead port, cold-start a
            # second Chrome against the same `--user-data-dir`, and hit exactly
            # the single-instance-lock failure mode docs/spikes/
            # 04-browser-login-state.md documents (real Chrome gets forwarded to
            # the stuck first instance and exits immediately).
            with _terminate_best_effort(process):
                pass
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
        """SIGTERM, wait, SIGKILL on timeout.

        Review finding #10: a reattached orphan (`handle.process is None` — see
        module docstring) is NOT skipped any more. Jones's profile directory is
        a single-instance-locked exclusive dir (this module never launches a
        second Chrome against one another live Chrome already holds — see
        `ensure_started()`), so whatever Chrome is holding it IS this Jones
        install's browser by construction; the original "killing a process we
        don't own would be a different daemon's browser" concern doesn't apply
        here. If `ensure_started()` recovered a real pid for it (from Chrome's
        `SingletonLock`), signal that pid directly instead of skipping — this is
        what keeps the §9.1 "graceful shutdown, not SIGKILL" login-state
        guarantee applying after a daemon restart, not just for the process
        this instance itself spawned. Only a genuinely unrecoverable pid (no
        `SingletonLock`, or unparseable) still no-ops, with a warning."""
        handle = self._handle
        if handle is None:
            return
        if handle.process is None:
            if handle.pid <= 0:
                logger.warning(
                    "browser: no pid recovered for this reattached orphan Chrome — "
                    "cannot shut it down gracefully, leaving it running",
                    extra={"detail": {"profile_dir": str(handle.profile_dir)}},
                )
                self._handle = None
                return
            self._shutdown_by_pid(handle.pid, timeout_s=timeout_s)
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

    def _shutdown_by_pid(self, pid: int, *, timeout_s: float) -> None:
        """Same SIGTERM→wait→SIGKILL sequence as the Popen path above, but
        signaled directly by pid (review finding #10) since a reattached
        orphan has no `Popen` object to call `.send_signal()`/`.wait()` on."""
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return  # already gone
        logger.info(
            "browser: shutting down reattached Jones Chrome", extra={"detail": {"pid": pid}}
        )
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                return
            time.sleep(_PORT_POLL_INTERVAL_S)
        logger.warning(
            "browser: SIGTERM did not exit reattached Chrome in time, sending SIGKILL",
            extra={"detail": {"pid": pid, "timeout_s": timeout_s}},
        )
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


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
# Round-2 review finding #12: guards `_MANAGERS`'s check-then-set below against
# two Sessions calling `browser_worker_config` concurrently (it dispatches to
# this function from `asyncio.to_thread`, i.e. real separate OS threads) —
# without it, two threads could both observe `_MANAGERS.get(profile_dir) is
# None` and each construct + install its own `BrowserManager` for the same
# profile_dir, defeating the "one Chrome per install" cache entirely.
_MANAGERS_LOCK = threading.Lock()


def get_browser_manager(user_root: Path, **kwargs: object) -> BrowserManager:
    """One `BrowserManager` per Jones install (`<user_root>/browser/profile`,
    §9) — a process-wide cache so every session's `browser_worker_config` call
    reuses the same Chrome instead of each one trying to own its own."""
    profile_dir = Path(user_root) / "browser" / "profile"
    with _MANAGERS_LOCK:
        manager = _MANAGERS.get(profile_dir)
        if manager is None:
            manager = BrowserManager(profile_dir, **kwargs)  # type: ignore[arg-type]
            _MANAGERS[profile_dir] = manager
        return manager


def shutdown_all(*, timeout_s: float = DEFAULT_SHUTDOWN_TIMEOUT_S) -> None:
    """Daemon-exit hook: gracefully close every Chrome this process spawned."""
    for manager in list(_MANAGERS.values()):
        manager.shutdown(timeout_s=timeout_s)


async def browser_worker_config(ctx: object, session: object = None) -> dict:
    """Env + config.yaml fragment for a worker's Hermes browser_* toolset to
    attach to the Jones Chrome over CDP (03-w4-interfaces.md §4, §1's shared-change
    grant on `workers/manager.py::_prepare_hermes_home` — H calls this once that
    branch lands; until then this report's own tests call it directly).

    `ctx` is duck-typed to tolerate whatever shape H's ServiceContext ends up
    being: a `.paths.user_root()` accessor (what the real `DaemonContext`
    actually has, see below), a `.user_root` attribute (callable or plain
    `Path`), OR (in this branch's own tests, and any caller that already has
    the path) a bare `Path`. `session` is accepted for interface stability but
    unused: §9 is one
    Jones-wide Chrome, not one per session — see the report's "契约变更" section
    for why a later per-session-isolation requirement would need to revisit this
    signature, not silently branch on `session` today.

    Ensures the browser is actually running (lazy start) before returning, so a
    worker that gets this config can attach immediately; on launch failure this
    raises `BrowserLaunchError` — callers must turn that into an explicit error
    card (§9 "诚实失败"), never omit the browser toolset silently, and — per §6
    ("浏览器起不来 → 明确错误卡片", not "会话起不来") — that error card must be
    scoped to the browser toolset for this session, not fail the worker spawn
    / session start entirely: a Chrome-less machine should still get a working,
    browser-less session.

    **Controller ruling R-J1 (round-2 review, 2026-09-19) — `browser.
    allow_private_urls` is NEVER set here, on purpose, even though that means
    `browser_navigate` to `file://`/localhost/private-net targets does not
    actually work today**: `tools/browser_tool_cloud.py::_is_local_backend()`
    treats a CDP override as "never local" unconditionally (its own docstring:
    "A CDP override is never trusted as local"), and
    `tools/browser_tool.py::_url_policy_error`/`_post_redirect_block` only
    exempt a navigation target from the private-address/scheme check for
    `local` backends, the hybrid-cloud "local sidecar" case (not applicable
    here — this branch never configures a cloud provider), or
    `browser.allow_private_urls`. There is no narrower flag in this
    hermes-agent version that frees only the CDP *attach* (the daemon's own
    `BROWSER_CDP_URL` env, read by `_get_cdp_override_raw()` — completely
    unrelated to `allow_private_urls`, attach already works with this field
    absent) without also freeing every *navigation target* from the SSRF/
    scheme check — round-1's finding #6/#8 showed exactly how far that reaches
    (a same-origin 302 redirect, or a bare `file://` URL, both auto-allowed in
    auto/task mode with zero user visibility). Round-2 finding #9 additionally
    showed the same global flag also lifts `web_extract`/vision/skills_hub's
    SSRF checks, not just the browser's. Leaving this field unset means: (a)
    public http(s) navigation works exactly as before; (b) `file://`/
    localhost/private-net navigation is refused BY HERMES ITSELF (`_url_policy_
    error` returns its own block, before this module's `permissions/
    review.py::_classify_browser_navigate` compensating control even gets a
    chance to matter) — stricter than "ask the user", not merely equivalent to
    it, until either hermes-agent ships a CDP-attach-scoped variant of this
    flag or a Jones-side patch to the vendored dependency adds one (out of
    this branch's scope: hermes-agent is not a directory this repo owns).
    `review.py`'s escalation-to-high for these URLs stays in place regardless,
    as defense in depth for the day either of those lands.

    **Review finding #12 — three things any caller MUST account for, not just
    "call it"** (round-3 review: the round-2 fix only did item 1 below — making
    this function `async` — and that alone was the NEW trigger for the race,
    not a fix for it; item 3 is round-3's actual fix, `BrowserManager._lock` /
    `_MANAGERS_LOCK`, see those docstrings):
    1. This function is now `async def` (controller ruling R-J4) precisely so
       callers never have to remember the off-thread wrapper themselves — the
       actual blocking work (`subprocess.Popen` + `urllib.request.urlopen`
       liveness polling, up to `launch_timeout_s`; ~0.76s measured cold start,
       10s worst case) runs via `asyncio.to_thread` inside this function.
       Calling it with a bare `.ensure_started()`-equivalent synchronous API
       from the daemon's event loop would stall every RPC and `session/update`
       broadcast in the daemon for the duration (§3/§6's broadcast latency
       budget) — this function's signature now makes that mistake impossible
       to make by accident.
    2. It eagerly launches a headed Chrome window on ITS OWN first call, with
       no regard for whether the calling Agent's tool whitelist even includes
       any `browser_*` tool. **Calling contract (controller ruling R-J4)**: a
       caller MUST NOT invoke this unconditionally at worker-spawn time for
       every session — it must be invoked lazily, the first time a session
       that actually has a `browser_*` tool enabled is about to use one. The
       daemon already has exactly one natural hook for "about to use a
       browser_* tool" that exists independently of whether H's registry
       (#17) has landed: `sessions/service.py::_on_request_permission` sees
       every `browser_*` tool call (via the rule/review gate) before it
       decides whether to auto-allow it — the recommended call site is there,
       on the first `browser_*`-tool `request_permission`/rule-gate pass for a
       session, not at `workers/manager.py`'s worker-spawn path. (H/#17 owns
       the actual wiring per `03-w4-interfaces.md` §1 — this paragraph is the
       contract this module commits to, not a claim that the wiring exists
       yet; see the report's "没做什么".) A machine with no Chrome installed
       must surface `BrowserLaunchError` as a scoped `hidden_reason=
       browser_unavailable` + error card for the browser toolset only (§6),
       never fail the whole worker/session.
    3. Making this function `async` (item 1) means two Sessions racing
       `browser_worker_config` now genuinely dispatch to `get_browser_manager`/
       `manager.ensure_started()` on two different OS threads at the same
       time — `asyncio.to_thread` uses a real thread pool, not cooperative
       scheduling. `get_browser_manager` and `BrowserManager.ensure_started()`
       now hold locks (`_MANAGERS_LOCK`, `self._lock`) across their whole
       bodies specifically to serialize that, so this function itself needs no
       locking of its own — it just awaits into already-safe code.
    """
    user_root = getattr(ctx, "user_root", ctx)
    if user_root is ctx:
        # Review finding #10 (round-2): the real `DaemonContext`
        # (`daemon/src/jones_daemon/context.py`) has no `user_root` attribute
        # at all — the real accessor is `ctx.paths.user_root()`
        # (`jones_daemon.paths` module, see `sessions/service.py:1244`'s
        # `self.ctx.paths.user_root()` for the call this codebase already
        # makes elsewhere). Fall back to that shape before falling back to
        # treating `ctx` itself as the path (what this branch's own tests and
        # any caller that already has a bare `Path` pass).
        paths_module = getattr(ctx, "paths", None)
        candidate = getattr(paths_module, "user_root", None)
        if callable(candidate):
            user_root = candidate
    if callable(user_root):
        user_root = user_root()
    manager = get_browser_manager(Path(user_root))
    handle = await asyncio.to_thread(manager.ensure_started)
    return {
        "env": {"BROWSER_CDP_URL": handle.cdp_http_url},
        # Deliberately NO "browser": {"allow_private_urls": True} here — see
        # this function's docstring, controller ruling R-J1. Public http(s)
        # navigation needs no extra config_yaml at all; private/loopback/
        # non-http(s) targets are refused by Hermes itself.
        "config_yaml": {},
    }
