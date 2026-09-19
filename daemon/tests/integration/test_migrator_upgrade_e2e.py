"""End-to-end upgrade test (docs/design/05-w6-interfaces.md §3.3, PRD 11.3 "升级不
丢数据"; Issue #25): run a daemon built from an OLDER git revision against a fresh
JONES_HOME (creating real data — bootstrap default Project/Agent, the main
Session), then open that same JONES_HOME with the CURRENT build and assert
everything survived, complete.

"上一版" is the `v0.9-pre` tag, created at this branch's own merge-base with `main`
per 05-w6-interfaces.md §3.3: "用上一发布版本（当前 main 的某个 tag 作为「上一
版」，若无则 `git tag v0.9-pre` 于 W5 收尾提交）". No prior release tag existed on
`main` when this branch was cut (checked: `git tag --list`, empty), so this is the
fallback case.

**round-1 review fix (评审 #1/#4)**: this tag is local-only — `git ls-remote --tags
origin` returns zero tags, and there is no repo convention or CI step that pushes
one. A file collected by plain `uv run pytest -q`/`make check-daemon` that hard-fails
(the original `_run(["git", "rev-parse", _OLD_TAG])`, an `AssertionError` on a
missing ref, not a skip) whenever that tag is absent breaks CI on every PR
(`actions/checkout@v4` doesn't fetch tags by default even if one were pushed) and
`make check-daemon` on every fresh clone. This is now gated like the repo's other
real-subprocess/real-checkout e2e tests (`JONES_E2E=1`, see
`daemon/tests/integration/test_cap_browser_e2e.py`) and the fixture skips (not
raises) when the tag genuinely isn't there — belt-and-suspenders, since the primary
guard is that `JONES_E2E` is unset in CI and on a plain clone.

The "old" build runs from a real, separate `git worktree` of that tag with its own
`uv sync` — not a `PYTHONPATH` trick pointed at this checkout's venv — so it
genuinely exercises whatever that revision's own dependency/migration set was, not
an assumption that it matches HEAD's. In this repo's current history `v0.9-pre` and
HEAD happen to carry the same migrations (W6 adds no schema changes — 05-w6-
interfaces.md §0 "不加功能"), so this test does not exercise a real schema version
delta; see the report's "没做什么" section for what a genuine version-delta upgrade
test would need (a repo with an actual shipped previous release) and
`test_migrator.py::test_apply_pending_refuses_a_database_newer_than_this_build_knows`
for the version-delta *rejection* path, which is exercised directly against the
migrator with synthetic migration files instead.

Slow (a real `git worktree add` + `uv sync` in a fresh venv) and now `JONES_E2E`-
gated like the repo's other real-subprocess e2e tests — it needs no API key or real
Hermes checkout, just git + uv, but neither should run by default on every
`pytest -q`/`make check-daemon` given the tag's local-only nature above. Run with
`JONES_E2E=1 uv run pytest tests/integration/test_migrator_upgrade_e2e.py -v`.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

_OLD_TAG = "v0.9-pre"
_REPO_ROOT = Path(__file__).resolve().parents[3]  # .../daemon/tests/integration/.. -> repo root

pytestmark = pytest.mark.skipif(
    not os.environ.get("JONES_E2E"),
    reason="upgrade e2e needs a local-only v0.9-pre tag (never pushed, see module "
    "docstring): set JONES_E2E=1 to run (see docs/DEV.md)",
)


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


@pytest.fixture(scope="module")
def old_build_daemon_dir():
    """A real `git worktree` checked out at `v0.9-pre`, with its own `uv sync`'d
    venv — module-scoped so the (one-time) `uv sync` cost is paid once per test
    run, not once per test."""
    tag_check = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "rev-parse", _OLD_TAG],
        capture_output=True,
        text=True,
    )
    if tag_check.returncode != 0:
        pytest.skip(
            f"{_OLD_TAG} tag not found locally (it is never pushed, see module "
            "docstring) — nothing to build the 'old' daemon from"
        )
    worktree_dir = Path(tempfile.mkdtemp(prefix="jones-v09-worktree-"))
    shutil.rmtree(worktree_dir)  # `git worktree add` requires the target not exist
    _run(["git", "-C", str(_REPO_ROOT), "worktree", "add", "--detach", str(worktree_dir), _OLD_TAG])
    try:
        old_daemon_dir = worktree_dir / "daemon"
        env = os.environ.copy()
        env["UV_FROZEN"] = "1"
        _run(["uv", "sync"], cwd=old_daemon_dir, env=env)
        yield old_daemon_dir
    finally:
        _run(["git", "-C", str(_REPO_ROOT), "worktree", "remove", "--force", str(worktree_dir)])


def _wait_for_socket(sock_path: Path, proc: subprocess.Popen, timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if sock_path.exists():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.2)
            try:
                probe.connect(str(sock_path))
                return
            except OSError:
                pass
            finally:
                probe.close()
        if proc.poll() is not None:
            stderr = proc.stderr.read() if proc.stderr else ""
            raise AssertionError(
                f"daemon exited early (code={proc.poll()}); stderr:\n{stderr[-3000:]}"
            )
        time.sleep(0.02)
    raise AssertionError(
        f"daemon did not accept a connection within {timeout_s}s on {sock_path}"
    )


def _send(sock_path: Path, payload: dict) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5.0)
    s.connect(str(sock_path))
    try:
        s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
    finally:
        s.close()


def _call(sock_path: Path, req_id: str, method: str, params: dict | None = None) -> dict:
    payload: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        payload["params"] = params
    result = _send(sock_path, payload)
    assert "error" not in result, result
    return result["result"]


def _start_daemon(daemon_dir: Path, jones_home: Path, *, use_uv_run: bool) -> subprocess.Popen:
    env = os.environ.copy()
    env["JONES_HOME"] = str(jones_home)
    if use_uv_run:
        env["UV_FROZEN"] = "1"
        cmd = ["uv", "run", "python", "-m", "jones_daemon"]
    else:
        cmd = [sys.executable, "-m", "jones_daemon"]
    return subprocess.Popen(
        cmd, cwd=daemon_dir, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
    )


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def test_current_build_opens_a_database_created_by_the_previous_build(old_build_daemon_dir):
    home = Path(tempfile.mkdtemp(dir="/tmp", prefix="jones-upgrade-"))
    try:
        sock_path = home / "runtime" / "daemon.sock"

        # 1. Old build creates real data: main session + default Agent/Project
        #    (bootstrap), plus a second explicit session.
        old_proc = _start_daemon(old_build_daemon_dir, home, use_uv_run=True)
        try:
            _wait_for_socket(sock_path, old_proc)
            projects = _call(sock_path, "1", "project.list")
            project_id = projects[0]["id"]

            # `agent_default` is project-scoped (`project_id = proj_default`, not
            # NULL/user-level — verified against a real bootstrap: `agent.list`
            # filters strictly `project_id IS NULL` with no `project_id` param,
            # see agents/service.py::list), so the project_id filter is required
            # here, not optional.
            agents = _call(sock_path, "2", "agent.list", {"project_id": project_id})
            assert agents, f"expected at least the default agent, got {agents}"
            agent_id = agents[0]["id"]

            created = _call(
                sock_path,
                "3",
                "session.create",
                {"project_id": project_id, "agent_id": agent_id, "title": "pre-upgrade session"},
            )
            created_session_id = created["id"]

            sessions_before = _call(sock_path, "4", "session.list")
            session_ids_before = {s["id"] for s in sessions_before}
            assert created_session_id in session_ids_before
            main_session_ids_before = {s["id"] for s in sessions_before if s["is_main"]}
            assert len(main_session_ids_before) == 1
        finally:
            _stop(old_proc)

        # 2. Current build opens the SAME JONES_HOME — this is the actual upgrade:
        #    apply_pending() must run (idempotently, if there's nothing new to
        #    apply) without complaint, and every row from step 1 must still be
        #    there afterwards.
        new_proc = _start_daemon(_REPO_ROOT / "daemon", home, use_uv_run=False)
        try:
            _wait_for_socket(sock_path, new_proc)
            sessions_after = _call(sock_path, "5", "session.list")
            session_ids_after = {s["id"] for s in sessions_after}
            assert session_ids_after == session_ids_before, (
                "session set changed across the upgrade: "
                f"before={session_ids_before} after={session_ids_after}"
            )
            main_session_ids_after = {s["id"] for s in sessions_after if s["is_main"]}
            assert main_session_ids_after == main_session_ids_before

            agents_after = _call(sock_path, "6", "agent.list", {"project_id": project_id})
            assert agent_id in {a["id"] for a in agents_after}

            projects_after = _call(sock_path, "7", "project.list")
            assert project_id in {p["id"] for p in projects_after}
        finally:
            _stop(new_proc)
    finally:
        shutil.rmtree(home, ignore_errors=True)
