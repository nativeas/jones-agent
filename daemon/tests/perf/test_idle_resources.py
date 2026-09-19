"""Idle memory (RSS) and idle CPU (no wakeups) — docs/design/05-w6-interfaces.md
§3.1, PRD 11.2: "守护进程常驻内存（空闲）≤ 150 MB" and "空闲 CPU ≈ 0：无任务时不轮询".

Uses `ps` (stdlib `subprocess`, no new dependency) rather than `psutil` — the
design doc explicitly allows either ("用 psutil/ps 采样"); `ps` needs nothing added
to `pyproject.toml` for a metric this narrow (two columns off one PID).

Both tests share one already-started, session-scoped daemon subprocess (module-
scoped fixture below) — spawning a fresh daemon per metric would also work but
costs an extra cold start each time for no measurement benefit, since neither
metric depends on how the daemon was started.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

_THRESHOLD_RSS_MB = 150.0
# "≈ 0": daemon 空闲不轮询 (00-foundation.md's own worded requirement, DEV.md 工程
# 原则 #3). A few ms of CPU time across the window is measurement/scheduler noise
# (process wakeup for the `ps` probe itself, GC, etc.), not a busy-loop — the
# threshold below is deliberately far under "1 core saturated for the window"
# while still well above what a single `ps` sample's own noise floor can produce.
_THRESHOLD_CPU_S = 0.15


@pytest.fixture(scope="module")
def idle_daemon():
    # Short, `/tmp`-rooted directory, not pytest's own `tmp_path`/`tmp_path_factory`
    # base — see `daemon_home` in conftest.py's docstring: that base is already
    # long enough (before appending `runtime/daemon.sock`) to blow AF_UNIX's
    # sockaddr path limit on macOS. Module-scoped fixtures can't depend on the
    # function-scoped `daemon_home` fixture (pytest scope mismatch), so this
    # inlines the same short-tmpdir approach with manual cleanup.
    home = Path(tempfile.mkdtemp(dir="/tmp", prefix="jones-perf-idle-"))
    env = os.environ.copy()
    env["JONES_HOME"] = str(home)
    proc = subprocess.Popen(
        [sys.executable, "-m", "jones_daemon"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    sock_path = home / "runtime" / "daemon.sock"
    deadline = time.monotonic() + 10.0
    connected = False
    while time.monotonic() < deadline:
        if sock_path.exists():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.2)
            try:
                probe.connect(str(sock_path))
                connected = True
                break
            except OSError:
                pass
            finally:
                probe.close()
        if proc.poll() is not None:
            break
        time.sleep(0.01)
    if not connected:
        stderr = proc.stderr.read() if proc.stderr else ""
        proc.kill()
        pytest.fail(
            f"idle_daemon fixture: daemon never became ready; stderr tail:\n{stderr[-2000:]}"
        )

    yield proc

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    shutil.rmtree(home, ignore_errors=True)


def _ps_rss_kb(pid: int) -> int:
    out = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, check=True
    )
    return int(out.stdout.strip())


_TIME_RE = re.compile(r"^(?:(\d+):)?(\d+):(\d+)(?:\.(\d+))?$")


def _ps_cpu_seconds(pid: int) -> float:
    """Parse `ps -o time=` (`[[HH:]MM:]SS[.ss]` cumulative CPU time) into seconds."""
    out = subprocess.run(
        ["ps", "-o", "time=", "-p", str(pid)], capture_output=True, text=True, check=True
    )
    raw = out.stdout.strip()
    match = _TIME_RE.match(raw)
    if not match:
        raise AssertionError(f"unparseable `ps -o time=` output: {raw!r}")
    hours, minutes, seconds, frac = match.groups()
    total = int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
    if frac:
        total += int(frac) / (10 ** len(frac))
    return float(total)


def test_idle_rss(idle_daemon, perf_record):
    time.sleep(0.3)  # let RSS settle past the immediate post-bootstrap transient
    rss_mb = _ps_rss_kb(idle_daemon.pid) / 1024
    passed = perf_record(
        "idle_rss_mb",
        rss_mb,
        "MB",
        _THRESHOLD_RSS_MB,
        "PRD 11.2 守护进程常驻内存（空闲）≤ 150MB",
    )
    assert passed, f"idle RSS {rss_mb:.1f}MB exceeds {_THRESHOLD_RSS_MB}MB"


def test_idle_cpu_no_wakeups(idle_daemon, perf_record):
    window_s = float(os.environ.get("JONES_PERF_IDLE_WINDOW_S", "2.0"))
    before = _ps_cpu_seconds(idle_daemon.pid)
    time.sleep(window_s)
    after = _ps_cpu_seconds(idle_daemon.pid)
    delta_s = after - before

    passed = perf_record(
        "idle_cpu_delta_s",
        delta_s,
        "s",
        _THRESHOLD_CPU_S,
        "PRD 11.2 空闲 CPU ≈ 0（无任务时不轮询）",
        detail={"window_s": window_s},
    )
    assert passed, (
        f"idle CPU consumed {delta_s:.3f}s of CPU time over a {window_s}s idle window "
        f"(threshold {_THRESHOLD_CPU_S}s) — daemon may be polling while idle"
    )
