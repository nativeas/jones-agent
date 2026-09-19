"""G10 端到端 (04-w5-interfaces.md §5: "重启不重放再验一次...这里做端到端：真实起停
daemon") — a real `python -m jones_daemon` subprocess, started and stopped twice,
not the in-process `sessions/service.py` unit coverage A already has (PRD 9.2/9.4,
G10: "队列中有 N 条未发送指令时 kill 守护进程再启动，均为「待发送」").

Simplification, stated honestly (see the branch report's "契约变更"/"没做什么"):
the 3 pending `queue_items` are seeded by writing directly to `jones.db` between
the two real daemon runs, not by driving a live `session.send()` through to its
"already running, so queue instead" branch — that branch is only reachable once a
Turn is actually running, which needs a real Hermes worker (not available in this
environment per docs/DEV.md, and every other test in this suite injects a fake ACP
agent instead). What this test *does* exercise for real: two genuine process
start/stops of the actual daemon entry point, and that a restart never turns
`queue_items` sitting in the DB into a running Run.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets as _secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


def _random_vault_key() -> str:
    return base64.b64encode(_secrets.token_bytes(32)).decode("ascii")


def _daemon_env(jones_home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["JONES_HOME"] = str(jones_home)
    env["JONES_VAULT_KEY"] = _random_vault_key()  # never touch the real Keychain in CI
    return env


def _start_daemon(jones_home: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "jones_daemon"],
        env=_daemon_env(jones_home),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


async def _wait_for(predicate, *, timeout: float, interval: float = 0.05) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


async def _rpc(sock_path: Path, method: str, params: dict | None = None) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    try:
        request = {"jsonrpc": "2.0", "id": "1", "method": method, "params": params or {}}
        writer.write((json.dumps(request) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=5)
        return json.loads(line)
    finally:
        # `wait_closed()` (not just `close()`) matters here, not just tidiness:
        # a client connection left half-open when the daemon receives SIGTERM
        # made `RpcServer.stop()`'s `await self._server.wait_closed()` hang
        # indefinitely in manual repro (this asyncio version's `Server.wait_closed()`
        # waits for existing connections too, not just the listening socket) —
        # see the branch report's "评审关注点" for the isolated repro and why this
        # is a real `rpc/server.py`-side finding (A/W2's file), not fixed here.
        writer.close()
        await writer.wait_closed()


def _stop_daemon(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


@pytest.fixture
def jones_home():
    # Short-lived dir directly under the system temp root: an AF_UNIX socket path
    # under pytest's nested tmp_path can exceed macOS's ~104-byte sun_path limit
    # (same reasoning as test_single_instance.py's fixture).
    home = Path(tempfile.mkdtemp(prefix="jn-e2e-"))
    yield home
    shutil.rmtree(home, ignore_errors=True)


async def test_restart_does_not_replay_pending_queue_items(jones_home):
    sock_path = jones_home / "runtime" / "daemon.sock"

    # --- first real start: let it bootstrap the default Project/Agent + schema --
    proc1 = _start_daemon(jones_home)
    try:
        await _wait_for(lambda: sock_path.exists(), timeout=20)
        ping = await _rpc(sock_path, "daemon.ping")
        assert "error" not in ping, ping
    finally:
        _stop_daemon(proc1)
    await _wait_for(lambda: not sock_path.exists(), timeout=10)

    # --- while stopped: seed a Session with 3 pending queue_items directly -------
    # (see module docstring for why this is seeded rather than driven through a
    # live session.send()).
    from jones_daemon.sessions import queries
    from jones_daemon.store import apply_pending, connect

    conn = connect(jones_home / "jones.db")
    try:
        apply_pending(conn)  # no-op: already at the latest version from proc1's run
        queries.create_session(
            conn, session_id="s-restart", project_id="proj_default",
            agent_id="agent_default", parent_id=None, is_main=False, mode="task",
            title="restart test",
        )
        for i in range(3):
            turn_id = f"q-turn-{i}"
            queries.create_turn_and_user_message(
                conn, turn_id=turn_id, message_id=f"q-msg-{i}", session_id="s-restart",
                text=f"queued instruction {i}", queued=True,
            )
            queries.enqueue(
                conn, session_id="s-restart", turn_id=turn_id, text=f"queued instruction {i}",
                attachments=None,
            )
    finally:
        conn.close()

    # --- second real start: this is the restart under test -----------------------
    proc2 = _start_daemon(jones_home)
    try:
        await _wait_for(lambda: sock_path.exists(), timeout=20)

        queue_response = await _rpc(sock_path, "session.queue", {"id": "s-restart"})
        assert "error" not in queue_response, queue_response
        items = queue_response["result"]
        assert len(items) == 3
        assert all(item["state"] == "pending" for item in items)

        # N04/G10: nothing got auto-sent — no Run exists for this Session.
        runs_response = await _rpc(sock_path, "run.list", {"session_id": "s-restart"})
        assert "error" not in runs_response, runs_response
        assert runs_response["result"] == []

        status = await _rpc(sock_path, "daemon.status")
        assert "error" not in status, status
        assert status["result"]["sessions_active"] == 0
    finally:
        _stop_daemon(proc2)
        if proc2.stdout is not None:
            proc2.stdout.close()
    if proc1.stdout is not None:
        proc1.stdout.close()
