"""Daemon cold start: spawn → Unix socket accepts a connection (docs/design/
05-w6-interfaces.md §3.1 "冷启动（spawn → socket 可 accept）").

Compared against PRD 11.1's "冷启动（守护进程未运行）≤ 6 s" row. That PRD number is
for the *whole app* (Electron shell + daemon spawn + SQLite + queue snapshot
restore) — this test only measures the daemon's own slice of it (real `python -m
jones_daemon`, fresh JONES_HOME, no worker/session activity), so passing here is
necessary but not sufficient for G17; `apps/desktop/tests/perf/` covers the
Electron-visible half.

Polling uses a real, in-process Unix socket connect attempt (not a spawned probe
subprocess) — docs/spikes/02-packaging.md §1 "计时方法" found that spawning a
Python probe subprocess to poll folds the probe's own fork+exec cost into the
"cold start" number, big enough (tens of ms) to swamp a real signal at this scale.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

# PRD 11.1's number is for the whole app; give the daemon-only slice the same
# ceiling (deliberately not tighter — see module docstring) rather than inventing
# an unreviewed sub-budget.
_THRESHOLD_MS = 6000.0
_POLL_INTERVAL_S = 0.005
_POLL_TIMEOUT_S = 10.0


def test_daemon_cold_start(daemon_home, perf_record):
    env = os.environ.copy()
    env["JONES_HOME"] = str(daemon_home)
    sock_path = daemon_home / "runtime" / "daemon.sock"

    start = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, "-m", "jones_daemon"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        connected = False
        deadline = start + _POLL_TIMEOUT_S
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
                break  # exited early — the assertion below reports this honestly
            time.sleep(_POLL_INTERVAL_S)
        elapsed_ms = (time.monotonic() - start) * 1000

        if not connected:
            stderr = proc.stderr.read() if proc.stderr else ""
            raise AssertionError(
                f"daemon did not accept a connection within {_POLL_TIMEOUT_S}s "
                f"(exit={proc.poll()}); stderr tail:\n{stderr[-2000:]}"
            )

        passed = perf_record(
            "daemon_cold_start_ms",
            elapsed_ms,
            "ms",
            _THRESHOLD_MS,
            "PRD 11.1 冷启动（守护进程未运行）≤ 6s",
        )
        assert passed, f"daemon cold start {elapsed_ms:.1f}ms exceeds {_THRESHOLD_MS}ms"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
