import shutil
import sqlite3
from pathlib import Path

import pytest

from jones_daemon import paths
from jones_daemon.agents.service import AgentService
from jones_daemon.projects.service import DEFAULT_PROJECT_ID, ProjectService
from jones_daemon.rpc.errors import RpcError
from jones_daemon.store import apply_pending, connect
from jones_daemon.store.migrator import MIGRATIONS_DIR


@pytest.fixture
def conn(tmp_path, monkeypatch) -> sqlite3.Connection:
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))
    c = connect(paths.db_path())  # creates the user root dir (0700) first
    apply_pending(c)
    yield c
    c.close()


def test_create_anchors_a_project_on_an_existing_directory(conn, tmp_path):
    service = ProjectService(conn)
    workdir = tmp_path / "myproj"
    workdir.mkdir()

    project = service.create(str(workdir))

    assert project["path"] == str(workdir.resolve())
    assert project["name"] == "myproj"
    assert (workdir / ".jones").is_dir()  # FR02: 选目录即建 Project, inits <path>/.jones/


def test_create_rejects_a_path_that_is_not_a_directory(conn, tmp_path):
    service = ProjectService(conn)
    not_a_dir = tmp_path / "nope.txt"
    not_a_dir.write_text("x")

    with pytest.raises(RpcError):
        service.create(str(not_a_dir))


def test_create_is_idempotent_on_the_same_path(conn, tmp_path):
    service = ProjectService(conn)
    workdir = tmp_path / "myproj"
    workdir.mkdir()

    before = len(service.list())  # migration 004 already seeds proj_default
    first = service.create(str(workdir))
    second = service.create(str(workdir))

    assert first["id"] == second["id"]
    assert len(service.list()) == before + 1


def test_list_and_get_roundtrip(conn, tmp_path):
    service = ProjectService(conn)
    workdir = tmp_path / "a"
    workdir.mkdir()
    created = service.create(str(workdir))

    assert service.get(created["id"]) == created
    assert created in service.list()


def test_get_missing_project_raises_not_found(conn):
    service = ProjectService(conn)
    with pytest.raises(RpcError):
        service.get("does-not-exist")


def test_delete_removes_the_row_and_attachments_dir_but_not_dot_jones(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))
    service = ProjectService(conn)
    workdir = tmp_path / "myproj"
    workdir.mkdir()
    project = service.create(str(workdir))

    from jones_daemon import paths

    attachments = paths.projects_dir() / project["id"]
    attachments.mkdir(parents=True)
    (attachments / "file.txt").write_text("data")

    service.delete(project["id"])

    with pytest.raises(RpcError):
        service.get(project["id"])
    assert not attachments.exists()
    assert (workdir / ".jones").is_dir()  # user data — never deleted


def test_delete_refuses_when_sessions_still_reference_the_project(conn, tmp_path):
    service = ProjectService(conn)
    workdir = tmp_path / "myproj"
    workdir.mkdir()
    project = service.create(str(workdir))

    conn.execute(
        "INSERT INTO agents(id, name, created_at, updated_at) VALUES ('a1','a1','now','now')"
    )
    conn.execute(
        "INSERT INTO sessions(id, project_id, agent_id, is_main, mode, status, "
        "created_at, updated_at) VALUES ('s1', ?, 'a1', 0, 'chat', 'active', 'now', 'now')",
        (project["id"],),
    )
    conn.commit()

    with pytest.raises(RpcError):
        service.delete(project["id"])


def test_delete_refuses_when_a_project_scoped_agent_still_references_the_project(conn, tmp_path):
    # Reproduces review round 1, finding #6: `agents.project_id` is a
    # `REFERENCES projects(id)` FK too (001_init.sql), not just `sessions`; an
    # unchecked DELETE used to surface as a raw sqlite3.IntegrityError instead of
    # this application-level RpcError.
    projects = ProjectService(conn)
    workdir = tmp_path / "myproj"
    workdir.mkdir()
    project = projects.create(str(workdir))

    AgentService(conn).upsert({"name": "ProjAgent", "project_id": project["id"]})

    with pytest.raises(RpcError):
        projects.delete(project["id"])


def test_delete_refuses_the_default_project(conn):
    # Migration 004 (this branch's own) already seeds `proj_default` — no manual
    # insert needed, and inserting one would collide with its UNIQUE id.
    service = ProjectService(conn)

    with pytest.raises(RpcError):
        service.delete(DEFAULT_PROJECT_ID)


def test_ensure_default_project_corrects_the_placeholder_path_to_the_real_home(conn, tmp_path):
    # 002 / 004 seed proj_default with a placeholder path (a static SQL migration
    # can't know the per-machine home dir) — this is the runtime fix. Assert the
    # semantics (not a real directory yet), not one migration's literal sentinel.
    service = ProjectService(conn)
    seeded = service.get(DEFAULT_PROJECT_ID)["path"]
    assert seeded.startswith("__") and not Path(seeded).exists()

    home = tmp_path / "realhome"
    home.mkdir()

    result = service.ensure_default_project(str(home))

    assert result["path"] == str(home.resolve())
    assert (home / ".jones").is_dir()


def test_ensure_default_project_is_a_noop_when_migration_004_has_not_run(tmp_path, monkeypatch):
    # Simulate a schema at v001 only (as if 004 — this branch's own migration —
    # hadn't been applied yet), by pointing the migrator at a dir with just a copy
    # of the real 001_init.sql.
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    shutil.copy(MIGRATIONS_DIR / "001_init.sql", migrations_dir / "001_init.sql")

    c = connect(paths.db_path())
    try:
        apply_pending(c, migrations_dir=migrations_dir)
        service = ProjectService(c)
        assert service.ensure_default_project(str(tmp_path)) is None
    finally:
        c.close()
