"""WorkerManager — one Hermes ACP worker subprocess per Session (docs/design/
00-foundation.md §3, §8.1; 01-w2-interfaces.md §2).

Owns the parts of the worker lifecycle that are specific to *spawning a process
correctly and proving it's safe to use*: isolated `HERMES_HOME`, an env that can
never carry `HERMES_YOLO_MODE`/`HERMES_SAFE_MODE`, the `jones_gate` plugin files on
disk, and the fail-closed startup self-check (§8.1). It does not know anything
about Turns, Messages, or the DB — `SessionService` drives those through the
`on_session_update` / `on_request_permission` / `on_worker_crash` callbacks passed
in at construction.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
import shutil
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jones_daemon import paths
from jones_daemon.capabilities import mcp_config
from jones_daemon.kernel.acp_client import AcpClient, AcpError, AcpProtocolError
from jones_daemon.logging import get_logger

logger = get_logger("workers")

# Kept in sync by hand with kernel/plugin/jones_gate/__init__.py::PROBE_TOOL_NAME —
# that file must not import from jones_daemon (see its module docstring: it ships
# into the worker's own Python environment, not the daemon's).
PROBE_TOOL_NAME = "jones.__probe__"

# Kept in sync by hand with kernel/plugin/jones_gate/__init__.py's `_on_pre_tool_call`
# block message (same reason PROBE_TOOL_NAME is duplicated, not imported, above).
# `_probe_event_verdict` requires this exact string to appear in a "failed" probe
# event before calling it "blocked" — a `status: failed` alone doesn't prove
# jones_gate did the blocking; see that function's docstring (round-1 review fix).
_GATE_BLOCK_MARKER = "jones_gate startup self-check: this tool is reserved and never runs."

_JONES_GATE_SRC = Path(__file__).resolve().parent.parent / "kernel" / "plugin" / "jones_gate"

# Kept in sync by hand with kernel/plugin/jones_gate/_tools_snapshot.py's `_FILE_NAME`
# (same reason PROBE_TOOL_NAME/`_GATE_BLOCK_MARKER` are duplicated, not imported,
# above — that package ships into the worker's own Python environment). Issue #38
# primary self-check criterion — see `_wait_for_tools_snapshot`'s docstring.
_TOOLS_SNAPSHOT_FILE_NAME = "jones_tools.json"
_TOOLS_SNAPSHOT_POLL_INTERVAL_S = 0.05

# `_wait_for_tools_snapshot` needs its OWN timeout, strictly less than
# `self._startup_timeout_s`, so its fail-closed rejection carries a specific,
# actionable reason instead of the bare, empty-message `TimeoutError()` that
# results when it's only ever cancelled from OUTSIDE by `_spawn_and_check`'s
# outer `asyncio.wait_for` — see that method's docstring for the production
# symptom this was fixed for. Same reasoning as `_PROBE_BUDGET_S` below: the
# reserve just has to comfortably outlast this method's own cleanup so its
# `WorkerStartupError` propagates before the outer cutoff would instead.
_TOOLS_SNAPSHOT_TIMEOUT_RESERVE_S = 1.0

# PRD 9.2 "空闲超时（默认 10 min）回收".
DEFAULT_IDLE_TIMEOUT_S = 600.0
_IDLE_SCAN_INTERVAL_S = 30.0

# Generous relative to PRD 11.1's "冷启动 worker ≤ 2s" *target* — this is a hard
# refusal ceiling for a startup that's actively stuck (hung self-check, worker never
# answers `initialize`), not the performance bar itself; see the report for measured
# numbers against the fake test agent.
DEFAULT_STARTUP_TIMEOUT_S = 20.0

# Round-1 review findings #1/#6: the diagnostic-only second-layer probe (below)
# used to share `DEFAULT_STARTUP_TIMEOUT_S` with the OUTER `asyncio.wait_for`
# wrapping the whole self-check in `_spawn_and_check` — identical durations
# meant the probe's own inner timeout could never fire first (the outer's
# deadline was always registered earlier), so delivering an already-proven-safe
# worker sat waiting for a real model to finish an entire Turn (measured: 9-12s
# beyond the primary tools-snapshot check passing against a real DeepSeek
# worker, occasionally exceeding `DEFAULT_STARTUP_TIMEOUT_S` outright and
# rejecting a worker for no reason but a slow diagnostic). This budget is
# deliberately its own, separate, much smaller constant.
_PROBE_BUDGET_S = 3.0

_PROBE_PROMPT = (
    "[jones-daemon automated startup self-check — not a real user request] "
    f"Call the tool named exactly `{PROBE_TOOL_NAME}` right now, with no arguments, "
    "and nothing else. Do not explain or ask for confirmation first."
)

SessionUpdateHandler = Callable[[dict[str, Any]], Awaitable[None]]
RequestPermissionHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class WorkerStartupError(Exception):
    """Raised when a worker subprocess can't be proven safe to deliver to a real
    session — always fail-closed (00-foundation.md §8.1), never deliver a worker
    whose plugin-loaded state is merely assumed."""


@dataclass
class Worker:
    session_id: str
    process: asyncio.subprocess.Process
    hermes_home: Path
    client: AcpClient | None = None
    acp_session_id: str | None = None
    busy: bool = False
    last_active: float = field(default_factory=time.monotonic)
    # Trampolines so `_spawn_and_check` can capture self-check events before the
    # real SessionService-bound handlers are wired in (see `_spawn_and_check`).
    session_update_handler: SessionUpdateHandler = field(default=lambda params: _noop(params))
    request_permission_handler: RequestPermissionHandler = field(
        default=lambda params: _deny_during_startup(params)
    )
    startup_updates: list[dict[str, Any]] = field(default_factory=list)


async def _noop(_params: dict[str, Any]) -> None:
    return None


async def _deny_during_startup(_params: dict[str, Any]) -> dict[str, Any]:
    # A real permission request arriving before self-check has finished would be
    # unexpected (jones_gate's approve path only ever runs for the probe tool
    # during self-check, and the probe is always blocked, never approved) — deny
    # rather than silently swallow it (DEV.md 工程原则 #4).
    return {"outcome": {"outcome": "cancelled"}}


def _worker_env(hermes_home: Path, extra_env: dict[str, str] | None = None) -> dict[str, str]:
    """Env for the worker subprocess: inherits the daemon's own env (PATH, HOME,
    locale, ...) but MUST NOT inherit `HERMES_YOLO_MODE`/`HERMES_SAFE_MODE` even if
    the daemon's own process happens to have them set (00-foundation.md §7 "必须禁用
    的 Hermes 内置绕过路径"), and always gets its own isolated `HERMES_HOME`.
    `extra_env` (provider API keys, once B/#7 lands `ProviderBinding.env`) is applied
    before the final strip, not after, so it can never smuggle either variable back
    in — belt and suspenders around the one thing this function exists to guarantee.
    """
    env = dict(os.environ)
    env.update(extra_env or {})
    env.pop("HERMES_YOLO_MODE", None)
    env.pop("HERMES_SAFE_MODE", None)
    env["HERMES_HOME"] = str(hermes_home)
    return env


def _signal_worker_group(process: asyncio.subprocess.Process, sig: int) -> None:
    """G09 "无孤儿进程" (Issue #40 investigation): signal the worker's WHOLE
    process group — spawned with `start_new_session=True` above specifically
    so this is possible — not just its own PID. `Process.terminate()`/
    `.kill()` alone only ever reached the worker itself; any descendant still
    alive at that moment (Hermes's own terminal-tool child, mid-`killpg` in
    its OWN background thread when the daemon decides to tear the worker down
    at the same time) was never guaranteed to go with it. `os.getpgid` on a
    pid whose leader already exited but whose group still has live members
    keeps working (POSIX: the pgid stays valid as long as any member is
    alive) — this is what makes the SIGKILL escalation below still reach
    stragglers even if the worker itself died first. Windows never gets here
    with a real group (v1.0 is macOS-only, PRD 11 G18; `start_new_session`
    isn't set there) so this falls back to the single-PID call `Process.
    terminate`/`.kill()` already provided.
    """
    if sys.platform == "win32":
        (process.terminate if sig == signal.SIGTERM else process.kill)()
        return
    try:
        pgid = os.getpgid(process.pid)
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass  # already gone — nothing left to signal


def _prepare_hermes_home(
    hermes_home: Path,
    *,
    mcp_servers: list[dict[str, Any]] | None = None,
    skill_dirs: list[Path] | None = None,
) -> None:
    """Materialize an isolated HERMES_HOME: jones_gate plugin files + config.yaml
    enabling it. Never copies from a user's real `~/.hermes` profile (00-foundation.md
    §7: isolation is the point, not convenience) — this directory starts empty every
    time a worker is (re)spawned, notably including a fresh, empty `command_allowlist`.

    `mcp_servers` (Issue #17/FR13, 03-w4-interfaces.md §2): the project's resolved
    `ctx.config.mcp_servers(project_id)` list, converted to Hermes's `config.yaml`
    `mcp_servers:` shape by `capabilities/mcp_config.py` — see that module's
    docstring for the concrete per-server schema and the source verification for
    why `config.yaml` (read by `acp_adapter/entry.py`'s background MCP discovery
    AND by `acp_adapter/session.py::_make_agent`'s `enabled_toolsets` computation)
    is the right channel, not the ACP `session/new` `mcpServers` protocol field.

    `skill_dirs` (Issue #17's "skills 路径", real implementation is #18/K's —
    `capabilities/registry.py`'s module docstring and the PR report explain this
    is a symlink-based integration point wired here but not yet called with real
    data pending K's `skills.worker_skill_dirs(ctx, session)`): each path is
    symlinked into `<HERMES_HOME>/skills/<dirname>` (`tools/skills_tool.py`'s
    `SKILLS_DIR = HERMES_HOME / "skills"`, source-verified) — best-effort, a
    failed symlink is logged and skipped, never fatal to worker startup.
    """
    hermes_home.mkdir(parents=True, exist_ok=True)
    hermes_home.chmod(0o700)
    # Review round-2 finding #3: `HERMES_HOME` is persisted by `session_id`
    # (`_hermes_home_for` below), so a worker restart (crash recovery, a
    # config change) reuses the same directory a PREVIOUS worker generation's
    # `on_session_start` hook already wrote `jones_tools.json` into. Without
    # this, a `capability.list` call made before the new worker's own first
    # Turn would read the OLD generation's snapshot as if it were current —
    # reporting last generation's tools/MCP servers (or their absence) as
    # "actual", not "not yet known" (`registry.reconcile`'s honest `actual_
    # available=False` default only works if the file is actually gone).
    for stale in (hermes_home / "jones_tools.json", hermes_home / "jones_tools.json.tmp"):
        stale.unlink(missing_ok=True)
    plugin_dst = hermes_home / "plugins" / "jones_gate"
    if plugin_dst.exists():
        shutil.rmtree(plugin_dst)
    shutil.copytree(_JONES_GATE_SRC, plugin_dst)
    # Hand-written, not a YAML library call: daemon/pyproject.toml has no YAML
    # dependency to add just for this (DEV.md 工程原则 #6). The `mcp_servers`
    # value is emitted as JSON flow syntax — valid YAML 1.2 (a JSON document IS a
    # conforming YAML document), which is what makes hand-writing this safe for
    # arbitrary nested dicts/strings (proper escaping via `json.dumps`, no
    # hand-rolled indentation/quoting rules that would mishandle a server name or
    # header value containing `:`/quotes). Deliberately does NOT write
    # `approvals.mode` at all — Hermes's own default is `manual`, and omitting the
    # key is the only way to be sure this file never accidentally writes the one
    # value (`off`) that's forbidden (00-foundation.md §7/§8.1).
    hermes_servers = mcp_config.hermes_mcp_servers_dict(mcp_servers)
    (hermes_home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - jones_gate\n"
        "command_allowlist: []\n"
        f"mcp_servers: {json.dumps(hermes_servers, ensure_ascii=False)}\n",
        encoding="utf-8",
    )

    if skill_dirs:
        skills_root = hermes_home / "skills"
        skills_root.mkdir(parents=True, exist_ok=True)
        for path in skill_dirs:
            try:
                link = skills_root / path.name
                if link.exists() or link.is_symlink():
                    continue
                link.symlink_to(path)
            except OSError:
                logger.warning(
                    "failed to symlink skill dir into worker HERMES_HOME",
                    extra={"detail": {"path": str(path), "hermes_home": str(hermes_home)}},
                )


def _probe_event_verdict(update: dict[str, Any]) -> str | None:
    """Inspect one `session/update` payload for a tool_call/tool_call_update event
    about the probe tool. Returns "blocked" (failed *with jones_gate's own block
    marker present* — see below), "completed" (a hard failure — the plugin let it
    through), "failed_unverified" (failed, but without proof jones_gate did the
    blocking), or None (not a probe event).

    A `status: failed` event alone does NOT prove `jones_gate` blocked the call —
    it's indistinguishable from Hermes failing the call because the tool doesn't
    exist at all, which is exactly what happens when the plugin never loaded
    (`HERMES_SAFE_MODE`, or `_load_tools()`'s discovery failure being swallowed to
    a warning, 00-foundation.md §8.2) and the probe tool was therefore never
    registered in the model's tool schema in the first place — the one scenario
    this whole self-check exists to catch (00-foundation.md §8.1: "断言收到的是
    插件产生的拒绝结果而不是工具直接执行的结果"). `jones_gate`'s block message
    is a fixed, only-the-plugin-can-produce-it string
    (`kernel/plugin/jones_gate/__init__.py`'s `_on_pre_tool_call`) that, per §7,
    ends up in the tool call's result — so require it before calling this
    "blocked" (round-1 review fix)."""
    body = update.get("update") or {}
    if body.get("sessionUpdate") not in ("tool_call", "tool_call_update"):
        return None
    haystack = json.dumps(
        [body.get("title"), body.get("rawInput"), body.get("rawOutput")], default=str
    )
    if PROBE_TOOL_NAME not in haystack:
        return None
    status = body.get("status")
    if status == "failed":
        return "blocked" if _GATE_BLOCK_MARKER in haystack else "failed_unverified"
    if status == "completed":
        return "completed"
    return None


class WorkerManager:
    def __init__(
        self,
        *,
        user_root: Path,
        on_session_update: Callable[[str, dict[str, Any]], Awaitable[None]],
        on_request_permission: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
        on_worker_crash: Callable[[str, int | None], Awaitable[None]],
        worker_cmd: list[str] | None = None,
        idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
        startup_timeout_s: float = DEFAULT_STARTUP_TIMEOUT_S,
    ) -> None:
        self._user_root = user_root
        self._on_session_update = on_session_update
        self._on_request_permission = on_request_permission
        self._on_worker_crash = on_worker_crash
        self._worker_cmd = worker_cmd or [sys.executable, "-m", "acp_adapter.entry"]
        self._idle_timeout_s = idle_timeout_s
        self._startup_timeout_s = startup_timeout_s
        self._workers: dict[str, Worker] = {}
        self._start_locks: dict[str, asyncio.Lock] = {}
        self._reaper_task: asyncio.Task[None] | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()
        # Startup latency samples (seconds), for the performance line in the PR
        # report — not consumed by any behavior, just measured and kept.
        self.startup_latencies_s: list[float] = []

    async def start(self) -> None:
        self._reaper_task = asyncio.create_task(self._reap_idle_loop())

    async def stop(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper_task
        for session_id in list(self._workers):
            await self.stop_worker(session_id, reason="daemon_shutdown")
        for task in list(self._background_tasks):
            task.cancel()

    def get(self, session_id: str) -> Worker | None:
        return self._workers.get(session_id)

    def worker_count(self) -> int:
        """`daemon.status`'s `workers` count (02-w3-interfaces.md §2 集成收口 #2,
        `rpc/methods.py::register_daemon_status`) — a plain read-only accessor,
        added by G/#12 (this file's `_worker_env`/`_prepare_hermes_home` remain
        F/#11's exclusive touch points per 02-w3-interfaces.md §0; see the PR
        report's "契约变更" section for why this one extra accessor was necessary
        despite that)."""
        return len(self._workers)

    def mark_busy(self, session_id: str, busy: bool) -> None:
        worker = self._workers.get(session_id)
        if worker is not None:
            worker.busy = busy
            worker.last_active = time.monotonic()

    async def ensure_started(
        self, session_id: str, *, cwd: str, mcp_servers: list[dict[str, Any]] | None = None
    ) -> Worker:
        """`mcp_servers` (Issue #17/FR13, 03-w4-interfaces.md §2; review round-2
        finding #4): the project's already-resolved `ctx.config.mcp_servers(
        project_id)` list, or `None` for "no MCP config to offer" (every
        existing caller with nothing to configure — a completely ordinary,
        supported case). Resolving this is deliberately the CALLER's job, not
        this method's: `ConfigResolver.mcp_servers()` ultimately does a real
        sqlite read (`DefaultConfigResolver._project_path`), and `store/db.py`
        connections are `check_same_thread=True` — created on, and only usable
        from, the dedicated DB thread (`__main__.py`'s comment on `connect()`,
        `DefaultConfigResolver`'s own module docstring). `WorkerManager` runs
        entirely on the asyncio event loop thread; a PREVIOUS round of this
        branch had this method call `self._config.mcp_servers(project_id)`
        directly here, which reproducibly raised `sqlite3.ProgrammingError:
        SQLite objects created in a thread can only be used in that same
        thread` on every real (non-test-double) `ConfigResolver` — silently
        caught by `_spawn_and_check`'s `except Exception` below and logged as
        a mere warning, so FR13's MCP wiring never actually took effect on the
        production path even though every test passed (the test doubles never
        touch sqlite). The fix is structural, not a wider `try`: the resolved
        VALUE crosses into this method, already computed off-loop by the
        caller (`sessions/service.py::_run_turn`, via `store.run_in_db_thread`)
        — this method and `_spawn_and_check` below never call a `ConfigResolver`
        themselves again."""
        existing = self._workers.get(session_id)
        if existing is not None:
            return existing
        lock = self._start_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            existing = self._workers.get(session_id)
            if existing is not None:
                return existing
            worker = await self._spawn_and_check(session_id, cwd=cwd, mcp_servers=mcp_servers)
            self._workers[session_id] = worker
            watch_task = asyncio.create_task(self._watch_exit(worker))
            self._background_tasks.add(watch_task)
            watch_task.add_done_callback(self._background_tasks.discard)
            return worker

    async def stop_worker(self, session_id: str, *, reason: str) -> None:
        worker = self._workers.pop(session_id, None)
        if worker is None:
            return
        await self._terminate(worker, reason=reason)

    # -- internals ------------------------------------------------------------

    def _hermes_home_for(self, session_id: str) -> Path:
        # Round-2 review, Issue #23: this used to hand-join `self._user_root /
        # "workers" / session_id / "hermes"` — a top-level `workers/` directory
        # PRD 10.2's tree never documented and no `paths.py` accessor covered,
        # so `session.delete`'s real-delete pass (04-w5-interfaces.md §5 G20)
        # had nowhere it knew to look and left it on disk forever, credentials
        # and all. `paths.worker_home_dir` is the one place this path is
        # spelled out now — see its own docstring for why it lives under
        # `runtime/` instead.
        return paths.worker_home_dir(self._user_root, session_id) / "hermes"

    async def _spawn_and_check(
        self, session_id: str, *, cwd: str, mcp_servers: list[dict[str, Any]] | None = None
    ) -> Worker:
        t0 = time.monotonic()
        hermes_home = self._hermes_home_for(session_id)
        mcp_servers = mcp_servers or []
        try:
            _prepare_hermes_home(hermes_home, mcp_servers=mcp_servers)
        except OSError as exc:
            # Was previously uncaught here, escaping `_spawn_and_check` as a bare
            # OSError instead of `WorkerStartupError` — `SessionService._run_turn`'s
            # `except WorkerStartupError` (the intended path for "this worker
            # couldn't be delivered") never saw it, so the Run was left 'running'
            # forever with no `run.terminated` (contract §7 "诚实失败"; round-1
            # review fix — `_run_turn` now also has a catch-all backstop for
            # failures like this one, but the honest fix is for this to be the
            # error type callers already expect).
            raise WorkerStartupError(
                f"failed to prepare HERMES_HOME at {hermes_home}: {exc}"
            ) from exc
        env = _worker_env(hermes_home)
        # Double-check, not trust: §8.1 "启动自检" point 1 — the daemon just built
        # this env itself, but asserting here is what makes "impossible for these to
        # leak through" a checked fact instead of an assumption about `_worker_env`.
        assert "HERMES_YOLO_MODE" not in env  # noqa: S101 - fail-closed startup invariant, not a test
        assert "HERMES_SAFE_MODE" not in env  # noqa: S101

        try:
            process = await asyncio.create_subprocess_exec(
                *self._worker_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                # G09 "无孤儿进程" (Issue #40 investigation): the worker itself
                # spawns further children (Hermes's local terminal backend,
                # `tools/environments/local.py`, starts each shell command in
                # its OWN new session/process group precisely so a cancelled
                # command can't escape a plain kill of the worker PID). Without
                # this, the worker shares the DAEMON's own process group, so a
                # worker that's SIGKILLed while a descendant hasn't finished
                # exiting yet (Hermes's own kill-and-verify is a background
                # thread, not synchronous with `session/cancel`) leaves that
                # descendant orphaned instead of dying with it. Starting the
                # worker in its own session lets `_terminate` below reap the
                # whole group in one `killpg`, the same shape Hermes's own
                # `_kill_process_group_posix` already uses one level down.
                # POSIX only (v1.0 is macOS-only per PRD 11's G18; Windows is
                # v1.1, `subprocess.Popen` doesn't support this flag there).
                start_new_session=(sys.platform != "win32"),
            )
        except OSError as exc:
            raise WorkerStartupError(f"failed to spawn worker process: {exc}") from exc

        worker = Worker(session_id=session_id, process=process, hermes_home=hermes_home)

        def _route_update(params: dict[str, Any]) -> Awaitable[None]:
            return worker.session_update_handler(params)

        def _route_permission(params: dict[str, Any]) -> Awaitable[dict[str, Any]]:
            return worker.request_permission_handler(params)

        assert process.stdout is not None and process.stdin is not None  # noqa: S101 - PIPE was requested
        client = AcpClient(
            process.stdout, process.stdin,
            on_session_update=_route_update, on_request_permission=_route_permission,
        )
        worker.client = client
        worker.session_update_handler = self._collect_startup_update(worker)

        assert process.stderr is not None  # noqa: S101
        stderr_task = asyncio.create_task(self._pump_stderr(session_id, process.stderr))
        self._background_tasks.add(stderr_task)
        stderr_task.add_done_callback(self._background_tasks.discard)

        try:
            await client.initialize()
            new_session_result = await asyncio.wait_for(
                client.new_session(cwd), timeout=self._startup_timeout_s
            )
            worker.acp_session_id = new_session_result["sessionId"]
            await asyncio.wait_for(
                self._startup_self_check(worker), timeout=self._startup_timeout_s
            )
            # Issue #40 investigation finding: the self-check's second-layer probe
            # (`_probe_second_layer`) sends its diagnostic prompt on THIS SAME
            # session and, whenever the model doesn't answer within its own small
            # `_PROBE_BUDGET_S` budget, sends a real `session/cancel` for it — a
            # real, reproducible outcome against DeepSeek (its probe Turn routinely
            # runs past 3s). Real Hermes's own `acp_adapter/server.py::cancel()`
            # records that cancelled Turn's text as `state.interrupted_prompt_
            # text`, and `_rewrite_prompt_for_interrupt()` silently PREPENDS it to
            # the very next prompt sent on this same session ("User correction/
            # guidance after interrupt: <next prompt>") — source-verified against
            # the installed hermes-agent checkout by reproducing it: a real worker
            # whose probe needed cancelling received the caller's actual first
            # production prompt (e.g. this file's own real-Hermes E2E tests' file/
            # terminal instructions) silently glued onto the leftover probe text,
            # confusing the model into not doing what the caller actually asked.
            # A worker delivered to a caller must never carry this land mine — get
            # a FRESH session for production use now that self-check has already
            # proven the plugin loaded (jones_tools.json) on the old one; a session
            # that has never had `cancel()` called on it can't have this problem.
            production_session = await asyncio.wait_for(
                client.new_session(cwd), timeout=self._startup_timeout_s
            )
            worker.acp_session_id = production_session["sessionId"]
        except (TimeoutError, AcpError, AcpProtocolError, KeyError, WorkerStartupError) as exc:
            elapsed = time.monotonic() - t0
            logger.error(
                "worker startup self-check failed, refusing to deliver this worker",
                extra={
                    "detail": {
                        "session_id": session_id,
                        "elapsed_s": round(elapsed, 3),
                        "error": str(exc),
                    }
                },
            )
            await self._terminate(worker, reason="startup_check_failed")
            raise WorkerStartupError(f"worker startup self-check failed: {exc}") from exc

        worker.session_update_handler = functools.partial(self._on_session_update, session_id)
        worker.request_permission_handler = functools.partial(
            self._on_request_permission, session_id
        )
        worker.last_active = time.monotonic()
        elapsed = time.monotonic() - t0
        self.startup_latencies_s.append(elapsed)
        logger.info(
            "worker started and self-check passed",
            extra={"detail": {"session_id": session_id, "elapsed_s": round(elapsed, 3)}},
        )
        return worker

    def _collect_startup_update(self, worker: Worker) -> SessionUpdateHandler:
        async def _collect(params: dict[str, Any]) -> None:
            worker.startup_updates.append(params)

        return _collect

    async def _startup_self_check(self, worker: Worker) -> None:
        """§8.1 fail-closed startup self-check — restructured per Issue #38's
        controller ruling into two layers, because the original sole criterion
        (the probe prompt, now `_probe_second_layer` below) turned out to depend
        on a model *choosing* to cooperate: 100% reproducible failure against a
        real DeepSeek worker, which never calls the probe tool no matter how the
        prompt is worded, even though `jones_gate` was loaded correctly the whole
        time. A fail-closed invariant can't rest on that.

        1. PRIMARY, fail-closed (`_wait_for_tools_snapshot`): poll for
           `<HERMES_HOME>/jones_tools.json`, written by `jones_gate`'s own
           `on_session_start` hook. The file's mere existence is proof the
           plugin loaded, produced by the plugin itself with zero MODEL
           participation. This alone decides whether the worker is delivered;
           still catches the `HERMES_SAFE_MODE`-style "plugin silently never
           loaded at all" case this self-check exists for, since that hook
           then never runs either.
        2. SECOND layer (`_probe_second_layer`): the original probe mechanism,
           kept to additionally verify `jones_gate`'s *block* semantics are
           actually enforcing (not just "loaded"). An uncooperative model (or
           any other outcome short of the reserved probe tool actually
           running) is diagnostic-only and never blocks delivery — but a
           verdict of "completed" (the tool ran instead of being blocked) is
           POSITIVE evidence the gate isn't enforcing despite being loaded,
           and still fail-closed rejects this worker (round-1 review findings
           #2/#5 — see `_probe_second_layer`'s own docstring; this is the one
           way this "optional" layer can still raise `WorkerStartupError`).

        **Round-1 review findings #1/#3/#6/#7 — this method itself no longer
        only calls out to the two layers above and returns; it now also (a)
        bounds how long the second layer's diagnostic prompt can delay
        delivery, independent of the model actually finishing its Turn, and
        (b) confirms the worker process is still alive before ever handing it
        back — see the inline comments below this docstring for both; kept
        out of this docstring itself so the mechanics stay next to the code
        they describe instead of drifting out of sync with it again.**

        **Why these two run CONCURRENTLY, not sequentially (correction to
        00-foundation.md §8.1's original text and this Issue's own "顺带确认"
        note — both wrong about this one specific hook, verified against the
        installed hermes-agent==0.21.2 checkout)**: `on_session_start` does
        NOT fire at `session/new`/`AIAgent` construction. Source: `agent/
        conversation_loop.py`'s `invoke_hook("on_session_start", ...)` call
        sits inside the FIRST TURN's system-prompt-build step — i.e. it only
        fires as a side effect of the daemon actually sending a
        `session/prompt`, before the model is called but strictly after a
        prompt was sent. A real run against DeepSeek confirmed this
        empirically: sequentially awaiting the snapshot BEFORE sending the
        probe prompt hangs forever (`jones_tools.json` never appears, because
        nothing ever triggers the hook) — see the PR report. Sending A prompt
        is therefore a REQUIRED trigger for the primary check, not merely
        nice-to-have for the optional second layer — but this is still zero
        MODEL participation (ruling 1's actual requirement): the daemon
        itself unconditionally sends this one prompt regardless of what any
        model does with it, so a model refusing to call the probe tool still
        can't stop `jones_tools.json` from appearing. `_probe_second_layer`
        therefore runs as a background task, started before the primary wait
        and always awaited afterward (never fire-and-forget) — it must finish
        before this method returns, because `_spawn_and_check` swaps `worker.
        session_update_handler` from the startup collector to the real,
        production handler the moment this method returns; a still-in-flight
        probe response arriving after that swap would be misrouted to
        `SessionService` instead of collected here.
        """
        assert worker.client is not None and worker.acp_session_id is not None  # noqa: S101
        probe_task = asyncio.create_task(self._probe_second_layer(worker))
        try:
            await self._wait_for_tools_snapshot(worker)
        except BaseException:
            # Primary failed (fail-closed timeout) or this whole self-check is
            # being cancelled out from under us (the OUTER `wait_for` in
            # `_spawn_and_check` timing out) — this worker is being rejected/
            # torn down regardless of what the probe would show, so don't
            # leave its prompt running against a worker we're about to kill.
            probe_task.cancel()
            with contextlib.suppress(BaseException):
                await probe_task
            raise
        # Primary passed: `jones_gate` is proven loaded with zero model
        # participation. `probe_task` is still awaited here (round-1 review
        # finding #1's second option, not full decoupling — see its own
        # docstring for why: `AcpClient.prompt()` has no client-side deadline,
        # and a REAL second `session/prompt` for this same ACP session must
        # never be sent while this one is still in flight, which is exactly
        # what `_spawn_and_check` swapping `worker.session_update_handler` to
        # the production handler before this straggling prompt's `session/
        # update`s stop arriving would risk) — but `_probe_second_layer` now
        # bounds ITSELF to `_PROBE_BUDGET_S`, not `self._startup_timeout_s`,
        # so this can only add a small, fixed delay, never the whole Turn.
        # May raise `WorkerStartupError` itself (round-1 review findings #2/#5
        # — a "completed" verdict, below).
        await probe_task
        # Round-1 review findings #3/#7: `_probe_second_layer` deliberately
        # treats a dead ACP connection as "probe couldn't run" (diagnostic, not
        # fatal — see its own docstring), since a worker merely slow to answer
        # the OPTIONAL probe must never block delivery. But a worker whose
        # PROCESS has actually died during this self-check — even after
        # writing a valid tools snapshot first — must never be delivered or
        # registered. `process.wait()` normally resolves instantly here (the
        # same OS notification that closed stdout, which is what unblocked
        # `probe_task` above with an `AcpProtocolError`, already fired); the
        # short bound below only covers the rare race where the child-exit
        # notification hasn't landed in this event loop iteration yet.
        if worker.process.returncode is None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(worker.process.wait(), timeout=0.5)
        if worker.process.returncode is not None:
            raise WorkerStartupError(
                "worker process exited unexpectedly during startup self-check "
                f"(returncode={worker.process.returncode})"
            )

    async def _wait_for_tools_snapshot(self, worker: Worker) -> None:
        """PRIMARY self-check criterion (Issue #38 ruling 1) — see
        `_startup_self_check`'s docstring, including why this only ever
        resolves once the concurrent `_probe_second_layer` has actually sent
        its prompt. Polls for `<HERMES_HOME>/jones_tools.json` to appear.

        Bounded to its OWN budget (`self._startup_timeout_s` minus
        `_TOOLS_SNAPSHOT_TIMEOUT_RESERVE_S`), not merely the OUTER
        `asyncio.wait_for(..., self._startup_timeout_s)` in `_spawn_and_check`
        that used to be the only thing bounding this loop. Relying solely on
        that outer wait_for produced a fail-closed rejection with an EMPTY
        reason in practice: cancelling this coroutine from outside raises a
        bare `TimeoutError()` (no args), so the final `WorkerStartupError`
        message (`_spawn_and_check`'s `f"worker startup self-check failed:
        {exc}"`) came out as `worker startup self-check failed: ` — a real
        user-facing error card with no clue what went wrong. This method now
        owns its own timeout and raises a `WorkerStartupError` with a
        specific, actionable reason instead — strictly less than the outer
        bound so it always fires (and its message survives) before that blunt
        outer cutoff would instead; same reasoning as `_PROBE_BUDGET_S`
        above.

        `_prepare_hermes_home` already deleted any stale snapshot left by a
        PREVIOUS worker generation for this same `hermes_home` before this
        worker was even spawned (see that function's comment), so a leftover
        file from an old generation can't produce a false pass here.
        """
        snapshot_path = worker.hermes_home / _TOOLS_SNAPSHOT_FILE_NAME
        budget_s = max(0.1, self._startup_timeout_s - _TOOLS_SNAPSHOT_TIMEOUT_RESERVE_S)
        t0 = time.monotonic()

        async def _poll() -> None:
            while not snapshot_path.exists():
                await asyncio.sleep(_TOOLS_SNAPSHOT_POLL_INTERVAL_S)

        try:
            await asyncio.wait_for(_poll(), timeout=budget_s)
        except TimeoutError as exc:
            elapsed = time.monotonic() - t0
            raise WorkerStartupError(
                f"timed out after {elapsed:.1f}s waiting for {snapshot_path} to "
                "appear — jones_gate's on_session_start hook never wrote it, which "
                "means the plugin likely never loaded (HERMES_SAFE_MODE? the plugin "
                "directory failed to write under HERMES_HOME? the worker crashed "
                "before handling its first prompt?)"
            ) from exc

    async def _probe_second_layer(self, worker: Worker) -> None:
        """Optional SECOND self-check layer (Issue #38 ruling 2) — see
        `_startup_self_check`'s docstring, including why THIS is what actually
        triggers the primary layer's file to ever appear. Sends the same
        directive prompt the old sole self-check used, asking the model to
        call the reserved, always-blocked probe tool, and evaluates what the
        `session/update` events collected during that prompt show.

        Bounded to `_PROBE_BUDGET_S` (round-1 review findings #1/#6), NOT
        `self._startup_timeout_s` — a separate, much smaller, independent
        budget, so this one extra, optional prompt can never make delivering
        an already-proven-safe worker wait for an entire model Turn (measured:
        9-12s beyond the primary check passing, against a real DeepSeek
        worker). On that budget expiring, this sends a real `session/cancel`
        (round-1 review finding #6: the old code shared ITS inner timeout's
        duration with, and registered it after, the OUTER `wait_for` around
        the whole self-check in `_spawn_and_check` — so the inner one could
        never fire first, and even if it somehow had, it only abandoned the
        local await without ever telling the worker subprocess to actually
        stop the Turn) — then still evaluates whatever verdict the `session/
        update` events observed SO FAR show, since those are collected in
        real time as they arrive (`_collect_startup_update`), independent of
        whether `prompt()` itself ever resolves: a model that calls the
        reserved tool typically does so near the start of its Turn (the
        directive prompt asks for nothing else), well inside this budget,
        even when it goes on to generate filler text for much longer
        afterward.

        Raises `WorkerStartupError` — the one way this "optional" layer still
        fail-closed rejects a worker the primary layer already passed — only
        for a "completed" verdict (round-1 review findings #2/#5): the
        reserved probe tool actually RUNNING instead of being blocked is
        positive, vendor-agnostic evidence `jones_gate`'s block semantics
        aren't enforcing even though the plugin loaded (00-foundation.md
        §8.1's actual reason this second layer exists at all — "断言收到的是
        插件产生的拒绝结果而不是工具直接执行的结果"). Every other outcome
        (blocked, failed-but-unverified, no call observed at all, the
        connection itself failing) stays diagnostic-only — logged, never
        raised — since none of those is evidence the gate is broken, only
        that this one extra prompt was inconclusive.
        """
        assert worker.client is not None and worker.acp_session_id is not None  # noqa: S101
        detail: dict[str, Any] = {"session_id": worker.session_id}
        try:
            await asyncio.wait_for(
                worker.client.prompt(worker.acp_session_id, _PROBE_PROMPT),
                timeout=_PROBE_BUDGET_S,
            )
        except TimeoutError:
            with contextlib.suppress(AcpProtocolError):
                await worker.client.cancel(worker.acp_session_id)
            logger.info(
                "startup self-check second-layer probe did not finish within its "
                f"own {_PROBE_BUDGET_S:.1f}s budget (independent of the overall "
                "startup timeout) — sent session/cancel and evaluating whatever "
                "session/update events it produced so far",
                extra={"detail": detail},
            )
        except (AcpError, AcpProtocolError) as exc:
            logger.info(
                "startup self-check second-layer probe could not run (error on this "
                "one extra, optional prompt) — primary tools-snapshot check already "
                "passed, delivering this worker regardless",
                extra={"detail": {**detail, "error": str(exc)}},
            )
            return
        verdicts = {v for v in (_probe_event_verdict(u) for u in worker.startup_updates) if v}
        if "completed" in verdicts:
            raise WorkerStartupError(
                f"{PROBE_TOOL_NAME} ran to completion instead of being blocked — "
                "jones_gate is not enforcing (HERMES_SAFE_MODE? plugin failed to load?)"
            )
        if "blocked" in verdicts:
            logger.info(
                "startup self-check second-layer probe confirmed jones_gate's block "
                "semantics are enforcing",
                extra={"detail": detail},
            )
        elif "failed_unverified" in verdicts:
            logger.warning(
                f"startup self-check second-layer probe: {PROBE_TOOL_NAME} failed but "
                "without jones_gate's own block marker — inconclusive (primary "
                "tools-snapshot check already passed; delivering this worker "
                "regardless)",
                extra={"detail": detail},
            )
        else:
            # No probe tool_call event observed at all — the model simply
            # didn't cooperate (Issue #38: reproducibly true for DeepSeek).
            # Per the controller's ruling this must never block delivery.
            logger.info(
                f"startup self-check second-layer probe: model did not call "
                f"{PROBE_TOOL_NAME} (non-cooperative model — primary tools-snapshot "
                "check already passed, delivering this worker regardless)",
                extra={"detail": detail},
            )

    async def _terminate(self, worker: Worker, *, reason: str) -> None:
        if worker.client is not None:
            await worker.client.close()
        if worker.process.returncode is None:
            _signal_worker_group(worker.process, signal.SIGTERM)
            try:
                await asyncio.wait_for(worker.process.wait(), timeout=5.0)
            except TimeoutError:
                _signal_worker_group(worker.process, signal.SIGKILL)
                await worker.process.wait()
        logger.info(
            "worker stopped", extra={"detail": {"session_id": worker.session_id, "reason": reason}}
        )

    async def _watch_exit(self, worker: Worker) -> None:
        returncode = await worker.process.wait()
        if self._workers.get(worker.session_id) is not worker:
            return  # already removed by an intentional stop_worker()
        del self._workers[worker.session_id]
        logger.error(
            "worker process exited unexpectedly",
            extra={"detail": {"session_id": worker.session_id, "returncode": returncode}},
        )
        try:
            await self._on_worker_crash(worker.session_id, returncode)
        except Exception:  # noqa: BLE001 - a crash handler must never crash the reaper itself
            logger.error("on_worker_crash handler raised", exc_info=True)

    async def _pump_stderr(self, session_id: str, stream: asyncio.StreamReader) -> None:
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                logger.info(
                    "worker stderr",
                    extra={
                        "detail": {
                            "session_id": session_id,
                            "line": line.decode(errors="replace").rstrip(),
                        }
                    },
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - stderr pump is best-effort logging, never fatal
            logger.debug("stderr pump ended", exc_info=True)

    async def _reap_idle_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(_IDLE_SCAN_INTERVAL_S)
                now = time.monotonic()
                stale = [
                    sid
                    for sid, w in self._workers.items()
                    if not w.busy and (now - w.last_active) > self._idle_timeout_s
                ]
                for sid in stale:
                    logger.info("recycling idle worker", extra={"detail": {"session_id": sid}})
                    await self.stop_worker(sid, reason="idle_timeout")
        except asyncio.CancelledError:
            raise
