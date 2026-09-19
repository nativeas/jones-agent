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
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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

# PRD 9.2 "空闲超时（默认 10 min）回收".
DEFAULT_IDLE_TIMEOUT_S = 600.0
_IDLE_SCAN_INTERVAL_S = 30.0

# Generous relative to PRD 11.1's "冷启动 worker ≤ 2s" *target* — this is a hard
# refusal ceiling for a startup that's actively stuck (hung self-check, worker never
# answers `initialize`), not the performance bar itself; see the report for measured
# numbers against the fake test agent.
DEFAULT_STARTUP_TIMEOUT_S = 20.0

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
        return self._user_root / "workers" / session_id / "hermes"

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
        """§8.1: prove `jones_gate` is actually loaded before this worker is handed
        to a real session. Sends a directive prompt asking the model to call the
        reserved, always-blocked probe tool, then inspects the `session/update`
        events collected during that prompt for a blocked (never a completed) call
        to it. See the PR report for this mechanism's real-Hermes reliability
        caveat — it is fully deterministic against the fake test agent used in
        every non-`JONES_E2E` test.
        """
        assert worker.client is not None and worker.acp_session_id is not None  # noqa: S101
        await worker.client.prompt(worker.acp_session_id, _PROBE_PROMPT)
        verdicts = {v for v in (_probe_event_verdict(u) for u in worker.startup_updates) if v}
        if "completed" in verdicts:
            raise WorkerStartupError(
                f"{PROBE_TOOL_NAME} ran to completion instead of being blocked — "
                "jones_gate is not enforcing (HERMES_SAFE_MODE? plugin failed to load?)"
            )
        if "blocked" not in verdicts:
            detail = (
                f" — {PROBE_TOOL_NAME} failed, but without jones_gate's own block "
                "marker; most likely Hermes reporting 'unknown tool' because the "
                "plugin (and therefore the probe tool registration) never loaded"
                if "failed_unverified" in verdicts
                else ""
            )
            raise WorkerStartupError(
                f"no verified-blocked {PROBE_TOOL_NAME} tool_call observed during "
                f"self-check — cannot confirm jones_gate is loaded{detail}"
            )

    async def _terminate(self, worker: Worker, *, reason: str) -> None:
        if worker.client is not None:
            await worker.client.close()
        if worker.process.returncode is None:
            worker.process.terminate()
            try:
                await asyncio.wait_for(worker.process.wait(), timeout=5.0)
            except TimeoutError:
                worker.process.kill()
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
