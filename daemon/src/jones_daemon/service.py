"""launchd user-level service management for the daemon (Issue #6, design §6).

Provides both a Python API (`install` / `uninstall` / `status`, each accepting an
injectable `runner` so tests never shell out to real `launchctl`) and the CLI surface
wired up by `__main__.py`: `python -m jones_daemon service install|uninstall|status`.

Registration target: the user GUI domain (`gui/<uid>`), matching PRD 11.3 "守护进程
注册为系统用户级服务" — a LaunchAgent, not a LaunchDaemon (which would run as root
outside any login session and doesn't fit a per-user BYOK desktop app).
"""

from __future__ import annotations

import os
import plistlib
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jones_daemon import paths

# Matches the label `apps/desktop/src/main/daemonLifecycle.ts` uses for
# `launchctl kickstart -k gui/<uid>/<label>` (design §6) — the two must agree or
# Electron's crash-recovery kickstart targets a service that was never installed
# under that name.
DEFAULT_LABEL = "ai.jones.daemon"

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class ServiceError(RuntimeError):
    """A launchctl/plist operation failed. Always carries the command and the
    underlying error (DEV.md 工程原则 #4: 诚实失败 — never swallowed)."""


def default_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(list(cmd), capture_output=True, text=True, check=False)  # noqa: S603
    except OSError as exc:
        raise ServiceError(f"failed to execute {' '.join(cmd)}: {exc}") from exc


def _default_launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _default_program_arguments() -> list[str]:
    # `sys.executable` here is whatever venv's python is running `service install`
    # itself (e.g. `<daemon>/.venv/bin/python` under `uv run`) — an absolute path
    # launchd can exec directly without needing `uv`, a shell, or $PATH resolution
    # inside the LaunchAgent's minimal environment. Packaged installs override this
    # via `--program` with the bundled standalone executable's path (packaging/
    # standalone; not wired up by this issue — see report).
    return [sys.executable, "-m", "jones_daemon"]


def _target(label: str) -> tuple[str, str]:
    """Return (`gui/<uid>` domain, `gui/<uid>/<label>` service target)."""
    domain = f"gui/{os.getuid()}"
    return domain, f"{domain}/{label}"


@dataclass
class InstallResult:
    plist_path: Path
    program_arguments: list[str]
    bootstrap_ok: bool
    bootstrap_output: str


@dataclass
class UninstallResult:
    plist_removed: bool
    bootout_ok: bool
    bootout_output: str


@dataclass
class StatusResult:
    label: str
    plist_installed: bool
    loaded: bool
    pid: int | None
    state: str | None
    raw: str


def render_plist(
    *,
    program_arguments: Sequence[str] | None = None,
    jones_home: Path | None = None,
    label: str = DEFAULT_LABEL,
) -> bytes:
    """Build the LaunchAgent plist as bytes (XML). Uses `plistlib` (stdlib) rather
    than hand-formatted XML so nothing here can produce a malformed/unescaped plist.

    `jones_home` is taken as an explicit parameter, not read from `paths.user_root()`
    (which honors the *current process's* `JONES_HOME` env var) — the daemon this
    plist launches runs as a fresh process under launchd with its own environment,
    so the log/home paths baked into the plist must not silently depend on whatever
    env var happened to be set in the process generating it (that mismatch is exactly
    what makes `jones_home` and `EnvironmentVariables.JONES_HOME` explicit below).
    """
    home = Path(jones_home) if jones_home is not None else paths.user_root()
    args = (
        list(program_arguments) if program_arguments is not None else _default_program_arguments()
    )
    log_dir = home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    plist_obj: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": args,
        # Crash-only restart, not "always respawn on exit 0" — a clean exit (e.g. a
        # future `daemon stop` command) must not be treated as a crash to recover from.
        "KeepAlive": {"SuccessfulExit": False, "Crashed": True},
        "RunAtLoad": True,
        # launchd's own throttle: at least 10s between restarts of a crash-looping job.
        "ThrottleInterval": 10,
        "StandardOutPath": str(log_dir / "daemon.out.log"),
        "StandardErrorPath": str(log_dir / "daemon.err.log"),
        "EnvironmentVariables": {"JONES_HOME": str(home)},
        # See packaging/launchd/ai.jones.daemon.plist for why Interactive (not the
        # Adaptive/Background alternatives) is the correct ProcessType here.
        "ProcessType": "Interactive",
        "ExitTimeOut": 5,
    }
    return plistlib.dumps(plist_obj)


def install(
    *,
    program_arguments: Sequence[str] | None = None,
    jones_home: Path | None = None,
    launch_agents_dir: Path | None = None,
    label: str = DEFAULT_LABEL,
    runner: Runner = default_runner,
) -> InstallResult:
    agents_dir = (
        launch_agents_dir if launch_agents_dir is not None else _default_launch_agents_dir()
    )
    agents_dir.mkdir(parents=True, exist_ok=True)
    dest = agents_dir / f"{label}.plist"
    resolved_args = (
        list(program_arguments) if program_arguments is not None else _default_program_arguments()
    )
    content = render_plist(program_arguments=resolved_args, jones_home=jones_home, label=label)

    # Atomic write (same rule PRD 11.3 states for config files: 先写临时文件再 rename)
    # so a crash mid-write never leaves launchd bootstrapping a truncated plist.
    tmp = dest.with_suffix(".plist.tmp")
    tmp.write_bytes(content)
    tmp.replace(dest)

    domain, target = _target(label)

    # Best-effort: clears a stale prior registration (e.g. reinstalling after
    # changing program arguments) so `bootstrap` below always loads the plist just
    # written, not a launchd-cached copy of an older one. Failure here is the
    # *expected* outcome on a first install (nothing was registered yet) and is
    # never treated as an error — only the `bootstrap` result below is.
    runner(["launchctl", "bootout", target])

    result = runner(["launchctl", "bootstrap", domain, str(dest)])
    if result.returncode != 0:
        raise ServiceError(
            "launchctl bootstrap failed "
            f"(exit {result.returncode}): {(result.stderr or result.stdout).strip()}"
        )
    return InstallResult(
        plist_path=dest,
        program_arguments=resolved_args,
        bootstrap_ok=True,
        bootstrap_output=result.stdout,
    )


def uninstall(
    *,
    launch_agents_dir: Path | None = None,
    label: str = DEFAULT_LABEL,
    runner: Runner = default_runner,
) -> UninstallResult:
    agents_dir = (
        launch_agents_dir if launch_agents_dir is not None else _default_launch_agents_dir()
    )
    dest = agents_dir / f"{label}.plist"
    _domain, target = _target(label)

    result = runner(["launchctl", "bootout", target])
    plist_removed = dest.exists()
    if plist_removed:
        dest.unlink()

    return UninstallResult(
        plist_removed=plist_removed,
        bootout_ok=result.returncode == 0,
        bootout_output=(result.stderr or result.stdout).strip(),
    )


_STATE_RE = re.compile(r"^\s*state\s*=\s*(\S+)", re.MULTILINE)
_PID_RE = re.compile(r"^\s*pid\s*=\s*(\d+)", re.MULTILINE)


def status(
    *,
    launch_agents_dir: Path | None = None,
    label: str = DEFAULT_LABEL,
    runner: Runner = default_runner,
) -> StatusResult:
    agents_dir = (
        launch_agents_dir if launch_agents_dir is not None else _default_launch_agents_dir()
    )
    dest = agents_dir / f"{label}.plist"
    _domain, target = _target(label)

    result = runner(["launchctl", "print", target])
    loaded = result.returncode == 0
    state = None
    pid = None
    if loaded:
        state_match = _STATE_RE.search(result.stdout)
        pid_match = _PID_RE.search(result.stdout)
        state = state_match.group(1) if state_match else None
        pid = int(pid_match.group(1)) if pid_match else None

    return StatusResult(
        label=label,
        plist_installed=dest.exists(),
        loaded=loaded,
        pid=pid,
        state=state,
        raw=result.stdout if loaded else result.stderr,
    )


def _print_install_result(r: InstallResult) -> None:
    print(f"installed: {r.plist_path}")
    print(f"program: {' '.join(r.program_arguments)}")
    print("launchctl bootstrap: ok")


def _print_uninstall_result(r: UninstallResult) -> None:
    print(f"plist removed: {r.plist_removed}")
    if r.bootout_ok:
        print("launchctl bootout: ok")
    elif r.bootout_output:
        print(f"launchctl bootout: not loaded ({r.bootout_output})")
    else:
        print("launchctl bootout: not loaded")


def _print_status_result(r: StatusResult) -> None:
    print(f"label: {r.label}")
    print(f"plist installed: {r.plist_installed}")
    print(f"loaded: {r.loaded}")
    if r.loaded:
        print(f"state: {r.state}")
        print(f"pid: {r.pid}")


def dispatch_cli(argv: list[str], *, runner: Runner | None = None) -> int:
    """Handle `service install|uninstall|status [--program "<cmd>"]`. Returns the
    process exit code; never calls `sys.exit` itself so it stays a plain, testable
    function (`maybe_handle_cli` below is what wires it into a real exit).

    `runner` defaults to `default_runner` by LATE binding (`None` sentinel, resolved
    below) rather than a default argument value: a default argument is evaluated once
    at def time, so `monkeypatch.setattr(service, "default_runner", ...)` — which every
    test here and `maybe_handle_cli`'s own callers rely on — would silently keep
    shelling out to the real `launchctl`. That is exactly what made the CI daemon job
    (ubuntu, no `launchctl` binary) fail while the same test passed on macOS."""
    if runner is None:
        runner = default_runner
    if not argv or argv[0] not in {"install", "uninstall", "status"}:
        print(
            'usage: python -m jones_daemon service install|uninstall|status [--program "<cmd>"]',
            file=sys.stderr,
        )
        return 2

    sub = argv[0]
    rest = argv[1:]
    program_arguments: list[str] | None = None
    i = 0
    while i < len(rest):
        if rest[i] == "--program" and i + 1 < len(rest):
            program_arguments = shlex.split(rest[i + 1])
            i += 2
        else:
            print(f"unknown argument: {rest[i]}", file=sys.stderr)
            return 2

    try:
        if sub == "install":
            _print_install_result(install(program_arguments=program_arguments, runner=runner))
        elif sub == "uninstall":
            _print_uninstall_result(uninstall(runner=runner))
        else:
            _print_status_result(status(runner=runner))
    except ServiceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def maybe_handle_cli(argv: list[str]) -> int | None:
    """`argv` is `sys.argv[1:]`. Returns an exit code if this was a `service ...`
    invocation (the caller must `sys.exit` with it and skip starting the daemon
    event loop), or `None` if it wasn't (the caller should proceed normally)."""
    if not argv or argv[0] != "service":
        return None
    return dispatch_cli(argv[1:])
