"""Shared helper for `daemon/tests/acceptance/` (Issue #24, docs/design/
05-w6-interfaces.md §2): "能自动化的写成 `daemon/tests/acceptance/test_gNN_*.py`
... 已有测试若已覆盖某条，写一个薄的 acceptance 用例引用或复用它，不重复造。"

Most G01-G21/N01-N18 items already have real, fixture-heavy unit/integration
coverage elsewhere in `daemon/tests/` (written during W1-W5 against the private
fixtures of whichever module owns that behaviour). Re-deriving the same
assertions here against copies of those fixtures would be exactly the
duplication the design doc says not to do, and would silently drift from the
real implementation the moment the original test's fixture shape changes.

Instead, each acceptance test in this package re-runs a curated list of
existing `tests/...::test_...` node IDs as a real subprocess `pytest`
invocation, under its own PRD-cited, G/N-numbered test name. This makes the
acceptance suite a genuine, currently-passing proof (not a changelog) of each
gate, while keeping the actual assertions in one place. If a referenced test
is renamed, deleted, or starts failing, this fails loudly instead of quietly
going green.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

DAEMON_ROOT = Path(__file__).resolve().parents[2]  # daemon/


def run_existing(*node_ids: str, env: dict[str, str] | None = None, timeout: float = 180) -> None:
    """Run the given pytest node IDs (relative to `daemon/`, e.g.
    `tests/test_gates_hard_deny.py::test_rm_rf_is_denied`) as a subprocess and
    fail this acceptance test with pytest's own output if any of them fail,
    error, or fail to collect (a renamed/deleted target must fail this test,
    not silently stop proving anything)."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", *node_ids],
        cwd=DAEMON_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=full_env,
    )
    assert result.returncode == 0, (
        f"reused acceptance coverage did not pass: {node_ids!r}\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
