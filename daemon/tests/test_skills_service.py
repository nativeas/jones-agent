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


def test_list_skills_does_not_recreate_a_deleted_or_unmounted_project_dir(tmp_path, monkeypatch):
    """评审第 1 轮 #2: a read-only `skill.list` scan must not have the side
    effect of resurrecting `<project_path>/.jones` (and `.jones/skills`) for a
    project directory the user has since deleted or unmounted — same contract
    `paths.py`'s `project_root`/`project_agents_dir` already document, which
    `project_skills_dir` previously didn't follow (no `create` parameter at
    all). `project_path` here never exists on disk at any point, mirroring an
    unmounted volume."""
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))
    gone_project = tmp_path / "does-not-exist" / "myproject"
    assert not gone_project.exists()

    result = service.list_skills(project_path=str(gone_project))

    assert result == []
    assert not gone_project.exists()  # not even the project root got created
    assert not (gone_project / ".jones").exists()
    assert not (gone_project / ".jones" / "skills").exists()


def test_list_skills_prunes_excluded_dirs_instead_of_walking_into_them(tmp_path, monkeypatch):
    """评审第 1 轮 #3: `_iter_skill_md_files` must prune `_EXCLUDED_DIR_NAMES`
    during the walk (`os.walk` + in-place `dirnames` pruning), not just filter
    the results of an unpruned `rglob` — a `SKILL.md` nested inside an
    excluded dir (here: `node_modules`) must not be listed, same as before,
    but this also asserts the excluded subtree is genuinely never opened."""
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))
    _write_skill(paths.skills_dir(), "real-skill")
    _write_skill(paths.skills_dir() / "node_modules" / "some-pkg", "fake-skill")

    result = service.list_skills(project_path=None)

    assert [e["name"] for e in result] == ["real-skill"]

    visited: list[Path] = []
    orig_walk = service.os.walk

    def spying_walk(top, *args, **kwargs):
        for dirpath, dirnames, filenames in orig_walk(top, *args, **kwargs):
            visited.append(Path(dirpath))
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(service.os, "walk", spying_walk)
    list(service._iter_skill_md_files(paths.skills_dir()))
    assert not any(p.name == "node_modules" or "node_modules" in p.parts for p in visited)


def test_worker_skill_dirs_orders_project_before_user_before_builtin(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("JONES_HOME", str(home))
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    # 评审第 1 轮 #2: worker_skill_dirs() now resolves the project tier with
    # create=False (read-only — must not resurrect a deleted/unmounted
    # project's `.jones/skills`), so a project directory whose skills dir was
    # never populated genuinely has no project tier to return. Create it here
    # (the way a user actually placing a project-level skill would) so this
    # test still exercises the ordering it's named for.
    _write_skill(paths.project_skills_dir(str(project_dir)), "proj-skill")

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


def test_worker_skill_dirs_omits_and_does_not_recreate_an_unpopulated_project_dir(
    tmp_path, monkeypatch
):
    """评审第 1 轮 #2: same read-only contract as `list_skills` above, checked
    through `worker_skill_dirs` this time — the function H writes straight
    into worker `config.yaml`. A Project whose `.jones/skills` was never
    created (e.g. it has no project-level skills, or its path is a deleted/
    unmounted mount point) must neither appear in the returned list nor get
    its directory created as a side effect of just asking."""
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))
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

    dirs = service.worker_skill_dirs(ctx, {"project_id": "p1"})

    assert dirs == [paths.skills_dir(), service.BUNDLED_SKILLS_DIR]
    assert not (project_dir / ".jones").exists()
