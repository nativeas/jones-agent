"""RPC `skill.list` (00-foundation.md §4.1 row added by this branch, issue #18/#19).

Fixture mirrors test_projects_methods.py's real-socket roundtrip pattern.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from jones_daemon import paths
from jones_daemon.projects.service import ProjectService
from jones_daemon.rpc.server import RpcServer
from jones_daemon.skills.methods import register
from jones_daemon.store import apply_pending, connect, run_in_db_thread


@pytest.fixture
async def server_and_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))

    def _open() -> object:
        c = connect(paths.db_path())
        apply_pending(c)
        return c

    conn = await run_in_db_thread(_open)
    ctx = SimpleNamespace(db=conn)

    short_dir = Path(tempfile.mkdtemp(prefix="jn-"))
    srv = RpcServer(short_dir / "t.sock")
    register(srv, ctx)
    await srv.start()
    yield srv, tmp_path, conn
    await srv.stop()
    await run_in_db_thread(conn.close)
    shutil.rmtree(short_dir, ignore_errors=True)


async def _call(sock_path, method, params) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    try:
        envelope = {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}
        writer.write((json.dumps(envelope) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=2)
        return json.loads(line)
    finally:
        writer.close()


def _write_skill(root: Path, name: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 测试 skill\n---\n\n# {name}\n", encoding="utf-8"
    )


async def test_skill_list_without_project_id_sees_only_user_and_builtin_tiers(server_and_dir):
    server, tmp_path, _conn = server_and_dir
    _write_skill(paths.skills_dir(), "weekly-report")

    res = await _call(server.socket_path, "skill.list", {})

    assert "error" not in res
    names = {s["name"] for s in res["result"]["skills"]}
    assert names == {"weekly-report"}
    assert res["result"]["skills"][0]["tier"] == "user"


async def test_skill_list_with_project_id_includes_the_project_tier(server_and_dir):
    server, tmp_path, conn = server_and_dir
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    project_id = await run_in_db_thread(ProjectService(conn).create, str(project_dir))
    _write_skill(paths.project_skills_dir(str(project_dir)), "deploy")

    res = await _call(server.socket_path, "skill.list", {"project_id": project_id["id"]})

    names = {s["name"] for s in res["result"]["skills"]}
    assert names == {"deploy"}
    assert res["result"]["skills"][0]["tier"] == "project"


async def test_skill_list_with_unknown_project_id_errors_instead_of_silently_ignoring_it(
    server_and_dir,
):
    server, _tmp_path, _conn = server_and_dir

    res = await _call(server.socket_path, "skill.list", {"project_id": "does-not-exist"})

    assert "error" in res  # NOT_FOUND propagates rather than degrading to "no project tier"
