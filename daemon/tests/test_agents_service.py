import sqlite3
import time

import pytest

from jones_daemon import paths
from jones_daemon.agents.service import DEFAULT_AGENT_ID, AgentService
from jones_daemon.projects.service import ProjectService
from jones_daemon.rpc.errors import RpcError
from jones_daemon.store import apply_pending, connect


@pytest.fixture
def conn(tmp_path, monkeypatch) -> sqlite3.Connection:
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))
    c = connect(paths.db_path())
    apply_pending(c)
    yield c
    c.close()


def test_migration_004_seeds_agent_default_with_expected_defaults(conn):
    service = AgentService(conn)
    agent = service.get(DEFAULT_AGENT_ID)
    assert agent["project_id"] is None
    assert agent["tool_allowlist"] == []  # 空 = 全部工具经闸
    assert agent["skills"] == []
    assert agent["model_pref"] == {}


def test_upsert_creates_a_new_user_level_agent_and_writes_its_file(conn):
    service = AgentService(conn)
    created = service.upsert({"name": "Scout", "persona": "curious", "tool_allowlist": ["shell"]})

    assert created["name"] == "Scout"
    assert created["project_id"] is None
    assert created["tool_allowlist"] == ["shell"]

    from_file = service._store.read(created["id"], project_path=None)
    assert from_file["name"] == "Scout"


def test_upsert_updates_in_place_preserving_unspecified_fields(conn):
    service = AgentService(conn)
    created = service.upsert({"name": "Scout", "persona": "curious", "tone": "dry"})

    # now_iso() has millisecond resolution — without this, two upserts issued back
    # to back can land in the same millisecond and make the updated_at != check
    # below flaky (observed: identical timestamps on a fast run).
    time.sleep(0.002)
    updated = service.upsert({"id": created["id"], "tone": "warm"})

    assert updated["name"] == "Scout"  # unspecified — preserved
    assert updated["persona"] == "curious"  # unspecified — preserved
    assert updated["tone"] == "warm"  # explicitly changed
    assert updated["created_at"] == created["created_at"]
    assert updated["updated_at"] != created["updated_at"]


def test_upsert_requires_a_name_for_a_new_agent(conn):
    service = AgentService(conn)
    with pytest.raises(RpcError):
        service.upsert({"persona": "no name given"})


def test_upsert_rejects_moving_an_existing_agent_to_a_different_project_id(conn, tmp_path):
    service = AgentService(conn)
    projects = ProjectService(conn)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    project = projects.create(str(workdir))

    created = service.upsert({"name": "Scout"})  # user-level

    with pytest.raises(RpcError):
        service.upsert({"id": created["id"], "project_id": project["id"]})


def test_upsert_for_a_project_scoped_agent_writes_under_the_project_dot_jones(conn, tmp_path):
    service = AgentService(conn)
    projects = ProjectService(conn)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    project = projects.create(str(workdir))

    created = service.upsert({"name": "Builder", "project_id": project["id"]})

    assert created["project_id"] == project["id"]
    assert (workdir / ".jones" / "agents" / created["id"] / "agent.yaml").is_file()


def test_list_includes_user_level_plus_the_given_projects_own_agents(conn, tmp_path):
    service = AgentService(conn)
    projects = ProjectService(conn)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    project = projects.create(str(workdir))

    user_agent = service.upsert({"name": "UserAgent"})
    project_agent = service.upsert({"name": "ProjAgent", "project_id": project["id"]})

    ids_for_project = {a["id"] for a in service.list(project["id"])}
    assert user_agent["id"] in ids_for_project
    assert project_agent["id"] in ids_for_project

    ids_user_scope = {a["id"] for a in service.list(None)}
    assert user_agent["id"] in ids_user_scope
    assert project_agent["id"] not in ids_user_scope  # project-scoped, not global


def test_delete_refuses_the_default_agent(conn):
    service = AgentService(conn)
    with pytest.raises(RpcError):
        service.delete(DEFAULT_AGENT_ID)


def test_delete_refuses_when_a_session_is_bound_to_the_agent(conn, tmp_path):
    service = AgentService(conn)
    projects = ProjectService(conn)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    project = projects.create(str(workdir))
    agent = service.upsert({"name": "Scout"})

    conn.execute(
        "INSERT INTO sessions(id, project_id, agent_id, is_main, mode, status, "
        "created_at, updated_at) VALUES ('s1', ?, ?, 0, 'chat', 'active', 'now', 'now')",
        (project["id"], agent["id"]),
    )
    conn.commit()

    with pytest.raises(RpcError):
        service.delete(agent["id"])


def test_delete_removes_row_and_file(conn):
    service = AgentService(conn)
    agent = service.upsert({"name": "Temp"})

    service.delete(agent["id"])

    with pytest.raises(RpcError):
        service.get(agent["id"])
    assert service._store.read(agent["id"], project_path=None) is None


def test_sync_from_files_indexes_a_hand_written_agent_yaml(conn):
    service = AgentService(conn)
    service._store.write(
        {
            "id": "handwritten",
            "name": "Handwritten",
            "persona": None,
            "tone": None,
            "principles": None,
            "tool_allowlist": [],
            "skills": [],
            "model_pref": {},
            "created_at": "t0",
            "updated_at": "t0",
        },
        project_path=None,
    )

    synced = service.sync_from_files()

    assert synced >= 1
    assert service.get("handwritten")["name"] == "Handwritten"


def test_ensure_default_agent_file_materializes_the_file_once(conn):
    service = AgentService(conn)
    assert service._store.read(DEFAULT_AGENT_ID, project_path=None) is None

    service.ensure_default_agent_file()
    assert service._store.read(DEFAULT_AGENT_ID, project_path=None) is not None

    # Hand-edit the file, then confirm a second call never overwrites it — the
    # file, once present, is authoritative (design §4: 文件为事实源).
    service._store.write(
        {**service.get(DEFAULT_AGENT_ID), "name": "Edited By User"}, project_path=None
    )
    service.ensure_default_agent_file()
    assert service._store.read(DEFAULT_AGENT_ID, project_path=None)["name"] == "Edited By User"
