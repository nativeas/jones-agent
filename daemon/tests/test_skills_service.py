"""daemon/src/jones_daemon/skills/service.py — 03-w4-interfaces.md §5, issue #18/#19."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from jones_daemon import paths
from jones_daemon.skills import service


def _write_skill(root: Path, name: str, *, description: str = "示例 skill") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n正文。\n",
        encoding="utf-8",
    )
    return skill_dir


def test_list_skills_finds_a_skill_in_the_user_tier(tmp_path, monkeypatch):
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))
    _write_skill(paths.skills_dir(), "weekly-report", description="整理周报")

    result = service.list_skills(project_path=None)

    assert result == [
        {
            "name": "weekly-report",
            "description": "整理周报",
            "tier": "user",
            "source_path": str(paths.skills_dir() / "weekly-report" / "SKILL.md"),
            "valid": True,
            "error": None,
        }
    ]


def test_list_skills_marks_a_malformed_skill_invalid_instead_of_dropping_it(tmp_path, monkeypatch):
    """DEV.md 工程原则 #4 / 03-w4-interfaces.md §6: "Skill 格式错 → 列出并标
    invalid，不静默跳过"."""
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))
    bad_dir = paths.skills_dir() / "no-frontmatter"
    bad_dir.mkdir(parents=True)
    (bad_dir / "SKILL.md").write_text("# 没有 frontmatter 的 skill\n", encoding="utf-8")

    result = service.list_skills(project_path=None)

    assert len(result) == 1
    entry = result[0]
    assert entry["name"] == "no-frontmatter"  # falls back to dir name
    assert entry["valid"] is False
    assert entry["error"] is not None


def test_list_skills_project_tier_shadows_a_same_named_user_skill(tmp_path, monkeypatch):
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()

    _write_skill(paths.skills_dir(), "deploy", description="用户级版本")
    _write_skill(paths.project_skills_dir(str(project_dir)), "deploy", description="项目级版本")

    result = service.list_skills(project_path=str(project_dir))

    assert len(result) == 1  # not two — the project-tier one shadows the user-tier one
    assert result[0]["tier"] == "project"
    assert result[0]["description"] == "项目级版本"


def test_list_skills_default_project_path_does_not_double_list_the_user_tier(tmp_path, monkeypatch):
    """`projects/bootstrap.py` seeds the default Project's path at `Path.home()`,
    which makes `project_skills_dir(home)` literally the same directory as
    `paths.skills_dir()` — scanning both must not double-list every skill (see
    the longer comment in the matching `worker_skill_dirs` test for why the
    env var here is `<fake_home>/.jones`, not `<fake_home>` itself)."""
    fake_home = tmp_path / "home"
    monkeypatch.setenv("JONES_HOME", str(fake_home / ".jones"))
    _write_skill(paths.skills_dir(), "research")

    result = service.list_skills(project_path=str(fake_home))

    assert len(result) == 1


def test_list_skills_empty_when_no_tier_has_anything(tmp_path, monkeypatch):
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))
    assert service.list_skills(project_path=None) == []


def test_worker_skill_dirs_orders_project_before_user_before_builtin(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("JONES_HOME", str(home))
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, path TEXT, name TEXT, "
                 "settings_json TEXT, created_at TEXT, updated_at TEXT)")
    conn.execute(
        "INSERT INTO projects VALUES ('p1', ?, 'demo', '{}', 't', 't')", (str(project_dir),)
    )
    conn.row_factory = sqlite3.Row
    ctx = SimpleNamespace(db=conn)
    session = {"project_id": "p1"}

    dirs = service.worker_skill_dirs(ctx, session)

    assert dirs == [
        paths.project_skills_dir(str(project_dir)),
        paths.skills_dir(),
        service.BUNDLED_SKILLS_DIR,
    ]


def test_worker_skill_dirs_dedups_the_default_projects_dir_against_the_user_dir(
    tmp_path, monkeypatch
):
    # `paths.user_root()` (no JONES_HOME override) is `Path.home() / ".jones"`,
    # and `projects/bootstrap.py` seeds the default Project's path at
    # `Path.home()` itself — so `project_skills_dir(Path.home())` (=
    # `Path.home()/".jones"/"skills"`) and `paths.skills_dir()` (=
    # `user_root()/"skills"`) land on the exact same directory. Reproduced
    # here under `JONES_HOME` (so this test never touches the real home) by
    # keeping that same "`<home>/.jones`" relationship between the override
    # and the fake "home" whose path the fake proj_default row carries.
    fake_home = tmp_path / "home"
    monkeypatch.setenv("JONES_HOME", str(fake_home / ".jones"))

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, path TEXT, name TEXT, "
                 "settings_json TEXT, created_at TEXT, updated_at TEXT)")
    conn.execute(
        "INSERT INTO projects VALUES ('proj_default', ?, 'default', '{}', 't', 't')",
        (str(fake_home),),
    )
    conn.row_factory = sqlite3.Row
    ctx = SimpleNamespace(db=conn)
    session = {"project_id": "proj_default"}

    dirs = service.worker_skill_dirs(ctx, session)

    # project dir collapsed into user dir
    assert dirs == [paths.skills_dir(), service.BUNDLED_SKILLS_DIR]


def test_worker_skill_dirs_without_a_project_id_skips_the_project_tier(tmp_path, monkeypatch):
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ctx = SimpleNamespace(db=conn)

    dirs = service.worker_skill_dirs(ctx, {"project_id": None})

    assert dirs == [paths.skills_dir(), service.BUNDLED_SKILLS_DIR]


def test_worker_skill_dirs_propagates_not_found_for_a_deleted_project(
    tmp_path, monkeypatch
):
    """DEV.md 工程原则 #4 (诚实失败): a Session referencing a Project that no
    longer exists must fail loudly, not quietly fall back to "no project
    tier" — see `worker_skill_dirs`'s docstring for why."""
    from jones_daemon.rpc.errors import RpcError

    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, path TEXT, name TEXT, "
                 "settings_json TEXT, created_at TEXT, updated_at TEXT)")
    conn.row_factory = sqlite3.Row
    ctx = SimpleNamespace(db=conn)

    with pytest.raises(RpcError):
        service.worker_skill_dirs(ctx, {"project_id": "does-not-exist"})
