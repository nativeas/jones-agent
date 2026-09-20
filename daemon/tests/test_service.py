"""Tests for `jones_daemon.service` (Issue #6, design §6).

`launchctl` is never actually invoked here — every test injects a fake `runner`
recording the commands it was asked to run and returning a canned
`subprocess.CompletedProcess`, per design §6 "launchctl 交互用可注入的 runner mock".
Real end-to-end launchd verification (install → kill → auto-restart → uninstall) was
run once by hand on this machine; see the issue report, not this file.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys

import pytest

from jones_daemon import service

UID = os.getuid()
TARGET = f"gui/{UID}/{service.DEFAULT_LABEL}"
BOOTOUT = ["launchctl", "bootout", TARGET]
PRINT = ["launchctl", "print", TARGET]


class FakeRunner:
    """Records every command it was called with; returns a canned result per-call
    (or a default success) so tests can script exactly what `launchctl` "said"."""

    Results = dict[tuple[str, ...], "subprocess.CompletedProcess[str]"]

    def __init__(self, results: Results | None = None):
        self.calls: list[list[str]] = []
        self._results = results or {}
        self.default = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        return self._results.get(tuple(cmd), self.default)


def _cp(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# --- render_plist ------------------------------------------------------------


def test_render_plist_has_expected_shape(tmp_path):
    home = tmp_path / "jones-home"
    content = service.render_plist(
        program_arguments=["/usr/bin/python3", "-m", "jones_daemon"], jones_home=home
    )
    obj = plistlib.loads(content)

    assert obj["Label"] == service.DEFAULT_LABEL
    assert obj["ProgramArguments"] == ["/usr/bin/python3", "-m", "jones_daemon"]
    assert obj["KeepAlive"] == {"SuccessfulExit": False, "Crashed": True}
    assert obj["RunAtLoad"] is True
    assert obj["ThrottleInterval"] == 10
    assert obj["StandardOutPath"] == str(home / "logs" / "daemon.out.log")
    assert obj["StandardErrorPath"] == str(home / "logs" / "daemon.err.log")
    assert obj["EnvironmentVariables"] == {"JONES_HOME": str(home)}
    assert obj["ProcessType"] == "Interactive"


def test_render_plist_creates_the_log_directory(tmp_path):
    home = tmp_path / "jones-home"
    assert not (home / "logs").exists()
    service.render_plist(jones_home=home)
    assert (home / "logs").is_dir()


def test_render_plist_defaults_program_arguments_to_current_python(tmp_path):
    obj = plistlib.loads(service.render_plist(jones_home=tmp_path / "home"))
    assert obj["ProgramArguments"] == [sys.executable, "-m", "jones_daemon"]


# --- install -------------------------------------------------------------


def test_install_writes_plist_and_bootstraps(tmp_path):
    agents_dir = tmp_path / "LaunchAgents"
    home = tmp_path / "jones-home"
    runner = FakeRunner()

    result = service.install(jones_home=home, launch_agents_dir=agents_dir, runner=runner)

    dest = agents_dir / f"{service.DEFAULT_LABEL}.plist"
    assert result.plist_path == dest
    assert dest.exists()
    assert result.bootstrap_ok is True
    obj = plistlib.loads(dest.read_bytes())
    assert obj["Label"] == service.DEFAULT_LABEL

    assert runner.calls == [BOOTOUT, ["launchctl", "bootstrap", f"gui/{UID}", str(dest)]]


def test_install_ignores_bootout_failure_but_not_bootstrap_failure(tmp_path):
    # bootout "fails" (nothing was registered yet) — must not abort the install.
    runner = FakeRunner({tuple(BOOTOUT): _cp(1, stderr="not found")})
    result = service.install(
        jones_home=tmp_path / "home", launch_agents_dir=tmp_path / "LaunchAgents", runner=runner
    )
    assert result.bootstrap_ok is True


def test_install_raises_service_error_when_bootstrap_fails(tmp_path):
    dest = tmp_path / "LaunchAgents" / f"{service.DEFAULT_LABEL}.plist"
    bootstrap = ["launchctl", "bootstrap", f"gui/{UID}", str(dest)]
    runner = FakeRunner({tuple(bootstrap): _cp(1, stderr="boom")})

    with pytest.raises(service.ServiceError, match="boom"):
        service.install(
            jones_home=tmp_path / "home", launch_agents_dir=tmp_path / "LaunchAgents", runner=runner
        )
    # the plist is still written before bootstrap is attempted — a failed bootstrap
    # doesn't need to be re-diagnosable from a missing file.
    assert dest.exists()


def test_install_is_an_atomic_write(tmp_path):
    agents_dir = tmp_path / "LaunchAgents"
    service.install(jones_home=tmp_path / "home", launch_agents_dir=agents_dir, runner=FakeRunner())
    # no leftover .tmp file after a successful install
    assert list(agents_dir.glob("*.tmp")) == []


# --- uninstall -------------------------------------------------------------


def test_uninstall_removes_plist_and_boots_out(tmp_path):
    agents_dir = tmp_path / "LaunchAgents"
    runner = FakeRunner()
    service.install(jones_home=tmp_path / "home", launch_agents_dir=agents_dir, runner=runner)
    runner.calls.clear()

    result = service.uninstall(launch_agents_dir=agents_dir, runner=runner)

    assert result.plist_removed is True
    assert result.bootout_ok is True
    assert not (agents_dir / f"{service.DEFAULT_LABEL}.plist").exists()
    assert runner.calls == [BOOTOUT]


def test_uninstall_is_idempotent_when_nothing_was_installed(tmp_path):
    agents_dir = tmp_path / "LaunchAgents"
    runner = FakeRunner({tuple(BOOTOUT): _cp(1, stderr="not found")})

    result = service.uninstall(launch_agents_dir=agents_dir, runner=runner)

    assert result.plist_removed is False
    assert result.bootout_ok is False
    # never raises — "not installed" is a valid starting state for uninstall, not an error


# --- status -------------------------------------------------------------


def test_status_reports_not_loaded_when_launchctl_cannot_find_it(tmp_path):
    runner = FakeRunner({tuple(PRINT): _cp(3, stderr="Could not find service")})
    result = service.status(launch_agents_dir=tmp_path / "LaunchAgents", runner=runner)
    assert result.loaded is False
    assert result.plist_installed is False
    assert result.pid is None
    assert result.state is None


def test_status_parses_pid_and_state_from_launchctl_print(tmp_path):
    stdout = f"{TARGET} = {{\n\tactive count = 1\n\tstate = running\n\tpid = 4242\n}}\n"
    runner = FakeRunner({tuple(PRINT): _cp(0, stdout=stdout)})
    result = service.status(launch_agents_dir=tmp_path / "LaunchAgents", runner=runner)
    assert result.loaded is True
    assert result.state == "running"
    assert result.pid == 4242


def test_status_reflects_plist_installed_flag_independent_of_loaded(tmp_path):
    agents_dir = tmp_path / "LaunchAgents"
    agents_dir.mkdir(parents=True)
    (agents_dir / f"{service.DEFAULT_LABEL}.plist").write_bytes(b"<plist/>")
    runner = FakeRunner({tuple(PRINT): _cp(3, stderr="not loaded")})

    result = service.status(launch_agents_dir=agents_dir, runner=runner)

    assert result.plist_installed is True
    assert result.loaded is False


# --- CLI dispatch -------------------------------------------------------------


def test_dispatch_cli_rejects_unknown_subcommand(capsys):
    assert service.dispatch_cli(["bogus"]) == 2
    assert "usage" in capsys.readouterr().err


def test_dispatch_cli_requires_a_subcommand(capsys):
    assert service.dispatch_cli([]) == 2


def test_maybe_handle_cli_returns_none_for_non_service_argv():
    assert service.maybe_handle_cli([]) is None
    assert service.maybe_handle_cli(["--foo"]) is None


def test_maybe_handle_cli_dispatches_service_subcommands(monkeypatch, tmp_path):
    # status against a fresh, never-installed label — runs the real dispatch path
    # end to end (still via the fake default_runner substitution below) without
    # touching the user's actual ~/Library/LaunchAgents.
    monkeypatch.setattr(service, "_default_launch_agents_dir", lambda: tmp_path / "LaunchAgents")
    fake = FakeRunner({tuple(PRINT): _cp(3)})
    monkeypatch.setattr(service, "default_runner", fake)

    code = service.maybe_handle_cli(["service", "status"])

    assert code == 0
    # The substitution has to actually take effect: before `dispatch_cli` switched to
    # late-binding its `runner`, this monkeypatch was a no-op and the call shelled out
    # to the real `launchctl` (fine on macOS, `FileNotFoundError` on a Linux runner).
    assert fake.calls, "default_runner substitution never took effect"
    assert fake.calls[0][0] == "launchctl"


def test_dispatch_cli_parses_program_flag(monkeypatch, tmp_path):
    # dispatch_cli itself has no launch_agents_dir/jones_home override (those are
    # Python-API-only knobs — the CLI always targets the real per-user locations);
    # redirect the module-level default so this never touches the real
    # ~/Library/LaunchAgents, the same technique
    # test_maybe_handle_cli_dispatches_service_subcommands uses.
    monkeypatch.setattr(service, "_default_launch_agents_dir", lambda: tmp_path / "LaunchAgents")
    runner = FakeRunner()

    code = service.dispatch_cli(["install", "--program", "/opt/jones/daemon/run.sh"], runner=runner)

    assert code == 0
    dest = tmp_path / "LaunchAgents" / f"{service.DEFAULT_LABEL}.plist"
    assert dest.exists()
    assert plistlib.loads(dest.read_bytes())["ProgramArguments"] == ["/opt/jones/daemon/run.sh"]
    bootstrap_calls = [c for c in runner.calls if c[:2] == ["launchctl", "bootstrap"]]
    assert bootstrap_calls == [["launchctl", "bootstrap", f"gui/{UID}", str(dest)]]
