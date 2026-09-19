"""G13 — PRD 12.1 发布门禁:

"Project 隔离：Project A 的记忆、Agent、Skill 在 Project B 中不可见（除标为
全局者）" —— 验证方式："双 Project 交叉验证"。

已有覆盖只证明了"项目级 ≠ 用户级"（`test_agents_service.py::
test_list_includes_user_level_plus_the_given_projects_own_agents`、
`test_skills_service.py::test_list_skills_project_tier_shadows_a_same_named_user_skill`），
不是这条门禁字面要求的"Project A ≠ Project B"（两个具体项目互相不可见）——按
05-w6-interfaces.md §2 补一个薄的新用例，双 Project 真实交叉验证，不复用假的。

记忆（长期记忆向量库）是 FR18（PRD 8.2 P1，"v1 尽力、可顺延"），v1 未实现，见
`docs/acceptance/v1.0/FR-checklist.md`；本用例覆盖 Agent 与 Skill 两项，记忆项
留空（无功能可验证，非缺口）。
"""

from __future__ import annotations

import sqlite3

import pytest

from jones_daemon import paths
from jones_daemon.agents.service import AgentService
from jones_daemon.projects.service import ProjectService
from jones_daemon.skills import service as skills_service
from jones_daemon.store import apply_pending, connect


@pytest.fixture
def conn(tmp_path, monkeypatch) -> sqlite3.Connection:
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))
    c = connect(paths.db_path())
    apply_pending(c)
    yield c
    c.close()


def _write_skill(root, name: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 项目专属\n---\n\n# {name}\n正文。\n",
        encoding="utf-8",
    )


def test_g13_project_scoped_agent_is_invisible_across_projects(conn, tmp_path):
    projects = ProjectService(conn)
    agents = AgentService(conn)
    dir_a = tmp_path / "project-a"
    dir_b = tmp_path / "project-b"
    dir_a.mkdir()
    dir_b.mkdir()
    project_a = projects.create(str(dir_a))
    project_b = projects.create(str(dir_b))

    agent_a = agents.upsert({"name": "OnlyInA", "project_id": project_a["id"]})

    ids_visible_in_b = {a["id"] for a in agents.list(project_b["id"])}
    ids_visible_in_a = {a["id"] for a in agents.list(project_a["id"])}

    assert agent_a["id"] not in ids_visible_in_b, (
        "Project A 的 Agent 在 Project B 中可见——违反 G13"
    )
    assert agent_a["id"] in ids_visible_in_a


def test_g13_project_scoped_skill_is_invisible_across_projects(tmp_path, monkeypatch):
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(skills_service, "BUNDLED_SKILLS_DIR", tmp_path / "empty-bundled")
    (tmp_path / "empty-bundled").mkdir()
    dir_a = tmp_path / "project-a"
    dir_b = tmp_path / "project-b"
    dir_a.mkdir()
    dir_b.mkdir()

    _write_skill(paths.project_skills_dir(str(dir_a)), "only-in-a")

    names_in_b = {s["name"] for s in skills_service.list_skills(project_path=str(dir_b))}
    names_in_a = {s["name"] for s in skills_service.list_skills(project_path=str(dir_a))}

    assert "only-in-a" not in names_in_b, (
        "Project A 的 Skill 在 Project B 中可见——违反 G13"
    )
    assert "only-in-a" in names_in_a
