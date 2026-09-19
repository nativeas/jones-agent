"""Shared plumbing for `daemon/tests/perf/` (docs/design/05-w6-interfaces.md §3.1,
Issue #25): each test records one measured metric against a PRD 11.1/11.2 threshold
via the `perf_record` fixture, and `pytest_sessionfinish` below writes every metric
collected in this run to `docs/acceptance/v1.0/perf-<date>.json` — a machine-readable
artifact, not a checkbox (05-w6-interfaces.md §0: "验收证据要可复现").

This directory intentionally participates in the normal `daemon/` test collection
(`testpaths = ["tests"]` in pyproject.toml, no separate marker/opt-in) — DEV.md 工程
原则 #3 "性能是需求" treats the PRD 11.1/11.2 numbers as acceptance items, not an
optional side channel, so a regression here fails `make check-daemon` the same way a
correctness regression would. Each test's own assertion is what actually gates
`make check`; this file only aggregates what they each already measured into the
JSON artifact.

One exception, isolated to the idle-CPU-wakeup test: PRD 11.2 specifies a 10s idle
observation window, but sitting idle for 10 real seconds on every `pytest -q` run
would materially slow down every contributor's local check loop for a single metric.
`JONES_PERF_IDLE_WINDOW_S` (default 2.0, used for the default/CI run) lets a
dedicated invocation opt into the full 10s window when producing the authoritative
`perf-<date>.json` artifact (see the report for the command used to generate the
committed one) — the JSON always records which window size actually ran, so the
artifact is never silently mislabeled.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

_REPORT_DIR = Path(__file__).resolve().parents[3] / "docs" / "acceptance" / "v1.0"

# This machine (recorded honestly below, not assumed): Apple M4 / 32GB / macOS 27 —
# NOT one of PRD 11.1's two reference machines (Apple M1/16GB, Intel i5 12th gen/
# 16GB). Numbers measured here are directionally useful (they exercise the same
# code paths and would catch a large regression) but are not a substitute for a
# reference-machine run before shipping — see the report's "性能数字" section.
_IS_REFERENCE_MACHINE = False


@dataclass
class _Metric:
    name: str
    value: float
    unit: str
    threshold: float
    threshold_source: str
    passed: bool
    detail: dict[str, Any] = field(default_factory=dict)


_collected: list[_Metric] = []


@pytest.fixture
def perf_record():
    """Returns `record(name, value, unit, threshold, threshold_source, detail=None)
    -> bool` (the bool is `value <= threshold`, so a test can both record and
    assert in one line: `assert perf_record(...)`). Recording happens before the
    caller asserts, so a threshold breach still lands in the JSON artifact for
    diagnosis instead of vanishing along with the failed test."""

    def _record(
        name: str,
        value: float,
        unit: str,
        threshold: float,
        threshold_source: str,
        *,
        detail: dict[str, Any] | None = None,
    ) -> bool:
        passed = value <= threshold
        _collected.append(
            _Metric(
                name=name,
                value=value,
                unit=unit,
                threshold=threshold,
                threshold_source=threshold_source,
                passed=passed,
                detail=detail or {},
            )
        )
        return passed

    return _record


@pytest.fixture
def daemon_home():
    """A JONES_HOME for a real spawned daemon, short enough for AF_UNIX's sockaddr
    path limit (~104 bytes on macOS). pytest's own `tmp_path` fixture nests under
    `/private/var/folders/.../pytest-of-<user>/pytest-<N>/<test-name><N>/` — long
    enough by itself, before appending `runtime/daemon.sock`, to blow that limit
    (measured: 129 bytes, `bind()` raising `OSError: AF_UNIX path too long`) —
    this worktree's own scratchpad path is not the cause (`JONES_HOME` here never
    touches it), pytest's default temp base already is. `/tmp` (not `/private/tmp`
    directly, though they're the same inode on macOS) keeps the prefix short."""
    path = Path(tempfile.mkdtemp(dir="/tmp", prefix="jones-perf-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _mac_chip_brand() -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return out.stdout.strip() or None
    except OSError:
        return None


def _mac_memory_gb() -> float | None:
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=2, check=False
        )
        return round(int(out.stdout.strip()) / (1024**3), 1)
    except (OSError, ValueError):
        return None


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:  # noqa: ARG001
    if not _collected:
        return
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    date_str = time.strftime("%Y-%m-%d", time.gmtime())
    out_path = _REPORT_DIR / f"perf-{date_str}.json"
    payload = {
        "date": date_str,
        "generated_by": "daemon/tests/perf (pytest)",
        "machine": {
            "platform": platform.platform(),
            "arch": platform.machine(),
            "chip": _mac_chip_brand(),
            "memory_gb": _mac_memory_gb(),
            "is_prd_reference_machine": _IS_REFERENCE_MACHINE,
            "prd_reference_machines": ["Apple M1 / 16GB", "Intel i5 12代 / 16GB"],
        },
        "idle_window_s": float(os.environ.get("JONES_PERF_IDLE_WINDOW_S", "2.0")),
        "metrics": [
            {
                "name": m.name,
                "value": m.value,
                "unit": m.unit,
                "threshold": m.threshold,
                "threshold_source": m.threshold_source,
                "passed": m.passed,
                "detail": m.detail,
            }
            for m in _collected
        ],
        "all_passed": all(m.passed for m in _collected),
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
