"""Regression guard for Issue #35's shutdown-hang risk class (see `repro_issue_
35.py`'s module docstring for the full sequence and the PR report for the
investigation writeup — this is a defensive regression test, not proof the
exact intermittent hang 02-w3-interfaces.md §1.2 reports is fully understood;
see the report's "评审关注点").

Runs the repro script as a real subprocess with a wall-clock timeout: a hang
is a deterministic pytest FAILURE (via `subprocess.run(..., timeout=...)`
raising `TimeoutExpired`), never an actually-frozen test run."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPRO = str(Path(__file__).parent / "repro_issue_35.py")


def test_shutdown_completes_after_a_permission_round_trip_and_a_second_turn(tmp_path):
    env = dict(os.environ)
    env["FAKE_ACP_MODE"] = "normal"
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, test-only
        [sys.executable, _REPRO, str(tmp_path)],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert result.returncode == 0, (
        f"repro process failed (stdout={result.stdout!r}, stderr={result.stderr!r})"
    )
    assert "REPRO_OK" in result.stdout
    # A dangling task at interpreter shutdown ("Task was destroyed but it is
    # pending!"/"Task exception was never retrieved") is the exact symptom
    # this Issue reports — even when the process still exits 0 (see the PR
    # report: it doesn't always manifest as a literal hang).
    assert "Task was destroyed" not in result.stderr
    assert "was never retrieved" not in result.stderr
