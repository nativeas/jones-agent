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
    # 007_default_agent_research_skill.sql (J, controller ruling R-J5, #16): the
    # default Agent's skill set now includes Hermes's bundled
    # `research/grounded-citations` skill on top of 004's original `[]` seed —
    # this fixture applies every migration, 006 included, so this is the real
    # post-migration state, not 004's alone (the test's name predates 006).
    assert agent["skills"] == ["research/grounded-citations"]
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


# -- review #1: proj_default's path collides with the user-level agents dir ---


def test_sync_from_files_does_not_reassign_user_level_agents_when_proj_default_collides_with_home(
    tmp_path, monkeypatch
):
    """Reproduces review round 1, finding #1: in the real default shape (JONES_HOME
    unset), `user_root()` is `Path.home() / ".jones"` and `proj_default.path` is
    `Path.home()` — so `paths.project_agents_dir(proj_default.path)` and
    `paths.agents_dir()` resolve to the *same* directory. `sync_from_files`'s
    per-project pass must not re-upsert user-level agents (including
    `agent_default`) under `proj_default`'s `project_id`. Reproduced here without
    touching the real $HOME by pointing JONES_HOME at `<home>/.jones` and seeding
    `proj_default` at `<home>` — the exact shape `paths.user_root()` produces by
    default.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("JONES_HOME", str(home / ".jones"))
    c = connect(paths.db_path())
    apply_pending(c)
    try:
        ProjectService(c).ensure_default_project(str(home))
        assert (
            paths.project_agents_dir(home, create=False).resolve()
            == paths.agents_dir(create=False).resolve()
        )  # sanity: this test actually reproduces the collision

        service = AgentService(c)
        service.ensure_default_agent_file()
        created = service.upsert({"name": "MyUserAgent"})

        service.sync_from_files()

        assert service.get(DEFAULT_AGENT_ID)["project_id"] is None
        assert service.get(created["id"])["project_id"] is None
        user_level_ids = {a["id"] for a in service.list(None)}
        assert {DEFAULT_AGENT_ID, created["id"]} <= user_level_ids
    finally:
        c.close()


# -- review #4: startup sync isolates per-agent / per-project failures --------


def test_sync_from_files_skips_an_agent_yaml_missing_the_required_name_field(conn, tmp_path):
    service = AgentService(conn)
    agents_dir = paths.agents_dir()
    (agents_dir / "broken").mkdir()
    (agents_dir / "broken" / "agent.yaml").write_text("name: null\npersona: x\n")

    synced = service.sync_from_files()  # must not raise sqlite3.IntegrityError

    assert synced == 0
    assert conn.execute("SELECT 1 FROM agents WHERE id = 'broken'").fetchone() is None


def test_sync_from_files_skips_an_agent_yaml_with_invalid_syntax(conn, tmp_path):
    service = AgentService(conn)
    agents_dir = paths.agents_dir()
    (agents_dir / "badyaml").mkdir()
    (agents_dir / "badyaml" / "agent.yaml").write_text("name: [unterminated\n")

    synced = service.sync_from_files()  # must not raise yaml.YAMLError

    assert synced == 0


def test_sync_from_files_skips_an_agent_yaml_whose_top_level_is_not_a_mapping(conn, tmp_path):
    service = AgentService(conn)
    agents_dir = paths.agents_dir()
    (agents_dir / "listtop").mkdir()
    (agents_dir / "listtop" / "agent.yaml").write_text("- not\n- a\n- mapping\n")

    synced = service.sync_from_files()  # must not raise AttributeError

    assert synced == 0


def test_sync_from_files_still_syncs_good_agents_alongside_a_broken_one(conn, tmp_path):
    service = AgentService(conn)
    agents_dir = paths.agents_dir()
    (agents_dir / "broken").mkdir()
    (agents_dir / "broken" / "agent.yaml").write_text("name: null\n")
    service._store.write(
        {
            "id": "good",
            "name": "Good",
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

    assert synced == 1
    assert service.get("good")["name"] == "Good"


def test_sync_from_files_skips_a_project_whose_agents_dir_is_unreadable(
    conn, tmp_path, monkeypatch
):
    projects = ProjectService(conn)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    project = projects.create(str(workdir))

    service = AgentService(conn)
    service._store.write(
        {
            "id": "good",
            "name": "Good",
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

    original_list_ids = service._store.list_ids

    def _list_ids_maybe_boom(*, project_path):
        if project_path == project["path"]:
            raise PermissionError("simulated: unreadable project directory")
        return original_list_ids(project_path=project_path)

    monkeypatch.setattr(service._store, "list_ids", _list_ids_maybe_boom)

    synced = service.sync_from_files()  # must not raise PermissionError

    assert synced == 1  # the user-level "good" agent still synced
    assert service.get("good")["name"] == "Good"
