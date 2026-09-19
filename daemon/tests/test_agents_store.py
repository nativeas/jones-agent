import shutil

import pytest

from jones_daemon.agents.store import AgentStore


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def test_write_then_read_user_level_agent(home):
    store = AgentStore()
    agent = {
        "id": "a1",
        "name": "Scout",
        "persona": "curious",
        "tone": "casual",
        "principles": "verify before claiming",
        "tool_allowlist": ["shell"],
        "skills": ["research"],
        "model_pref": {"provider": "anthropic"},
        "created_at": "t0",
        "updated_at": "t0",
    }
    store.write(agent, project_path=None)

    assert (home / "agents" / "a1" / "agent.yaml").is_file()
    loaded = store.read("a1", project_path=None)
    assert loaded == agent


def test_read_missing_agent_returns_none(home):
    store = AgentStore()
    assert store.read("nope", project_path=None) is None


def test_write_then_read_project_level_agent(home, tmp_path):
    store = AgentStore()
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    agent = {
        "id": "a2",
        "name": "Builder",
        "persona": None,
        "tone": None,
        "principles": None,
        "tool_allowlist": [],
        "skills": [],
        "model_pref": {},
        "created_at": "t0",
        "updated_at": "t0",
    }
    store.write(agent, project_path=str(project_dir))

    assert (project_dir / ".jones" / "agents" / "a2" / "agent.yaml").is_file()
    assert store.read("a2", project_path=None) is None  # not visible at user scope
    assert store.read("a2", project_path=str(project_dir)) == agent


def test_delete_removes_the_agent_directory(home):
    store = AgentStore()
    store.write(
        {
            "id": "a3",
            "name": "Temp",
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
    store.delete("a3", project_path=None)
    assert store.read("a3", project_path=None) is None


def test_list_ids_only_counts_directories_with_an_agent_yaml(home):
    store = AgentStore()
    (home / "agents" / "not-an-agent").mkdir(parents=True)  # no agent.yaml inside
    store.write(
        {
            "id": "real",
            "name": "Real",
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
    assert store.list_ids(project_path=None) == ["real"]


# -- review #5: read paths must not mkdir --------------------------------------


def test_list_ids_on_an_untouched_project_does_not_create_dot_jones(tmp_path):
    store = AgentStore()
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    assert store.list_ids(project_path=str(project_dir)) == []
    assert not (project_dir / ".jones").exists()


def test_read_on_an_untouched_project_does_not_create_dot_jones(tmp_path):
    store = AgentStore()
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    assert store.read("nope", project_path=str(project_dir)) is None
    assert not (project_dir / ".jones").exists()


def test_list_ids_does_not_resurrect_a_deleted_project_directory(tmp_path):
    store = AgentStore()
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    store.write(
        {
            "id": "a1",
            "name": "A",
            "persona": None,
            "tone": None,
            "principles": None,
            "tool_allowlist": [],
            "skills": [],
            "model_pref": {},
            "created_at": "t0",
            "updated_at": "t0",
        },
        project_path=str(project_dir),
    )
    assert (project_dir / ".jones" / "agents").exists()

    shutil.rmtree(project_dir)  # user deletes/unmounts the whole project directory
    assert not project_dir.exists()

    assert store.list_ids(project_path=str(project_dir)) == []
    assert not project_dir.exists()  # not recreated by the read
