"""Directory audit (Issue #23, 04-w5-interfaces.md §5: "写一个测试对照 PRD 10.2 的
用户级与项目级目录树, paths.py 每个访问器都有对应项, 多余/缺失即失败").

This doesn't hand-copy PRD 10.2's tree into Python (that would drift silently the
next time someone edits the PRD) — it asserts the *shape* every accessor in
`paths.py` must have: user-level accessors resolve under `user_root()`,
project-level accessors resolve under `<project>/.jones/`, and the exact set of
top-level entries each produces matches PRD 10.2 §"目录布局" literally, so an
accessor added for something not in that tree (or a tree entry nobody wrote an
accessor for) fails loudly.
"""

from __future__ import annotations

import inspect

from jones_daemon import paths

# PRD §10.2 "用户级" tree — every top-level entry under `~/.jones/`, mapped to the
# `paths.py` accessor that must produce it. `jones.db`/`secrets`/... are files or
# directories that live directly under `user_root()`.
_EXPECTED_USER_LEVEL: dict[str, str] = {
    "jones.db": "db_path",
    "config": "config_dir",
    "secrets": "secrets_dir",
    "agents": "agents_dir",
    "skills": "skills_dir",
    "projects": "projects_dir",
    "memory": "memory_global_dir",  # accessor resolves the full memory/global leaf
    "runtime": "runtime_dir",
    "runs": "runs_dir",
    "logs": "logs_dir",
    "cache": "cache_dir",
}

# Accessors that resolve *under* one of the above (runtime/'s two files, and the
# per-Project attachments leaf under projects/<id>/) — not separate top-level PRD
# entries themselves, so they're checked separately below rather than in the
# top-level set comparison.
_USER_LEVEL_LEAF_ACCESSORS = {
    "pid_file": ("runtime_dir", "daemon.pid"),
    "sock_file": ("runtime_dir", "daemon.sock"),
}

# PRD §10.2 "项目级" tree — every entry under `<项目目录>/.jones/`.
_EXPECTED_PROJECT_LEVEL: dict[str, str] = {
    "settings.json": "project_settings_path",
    "permissions.json": "project_permissions_path",
    "mcp.json": "project_mcp_path",
    "agents": "project_agents_dir",
    "skills": "project_skills_dir",
    "memory": "project_memory_dir",
}

# `project_root` itself (produces `.jones/` — the container, not a leaf inside
# it) and `project_attachments_dir` (produces `<user_root>/projects/<id>/`, the
# user-level `projects/<project-id>/` leaf from the table above, not something
# under `<project>/.jones/`) are real accessors this module exports that aren't
# entries *inside* the project-level tree — tracked separately so the "every
# public function is accounted for" sweep below doesn't flag them as unexpected.
_OTHER_KNOWN_ACCESSORS = {"user_root", "project_root", "project_attachments_dir"}


def _public_functions() -> dict[str, object]:
    return {
        name: fn
        for name, fn in inspect.getmembers(paths, inspect.isfunction)
        if not name.startswith("_") and fn.__module__ == paths.__name__
    }


def test_every_public_accessor_is_accounted_for_by_this_audit():
    """多余即失败: a new accessor added to `paths.py` for something not in PRD
    10.2's tree must fail here until this test (or the PRD) is updated —
    catching the drift at review time rather than silently accreting paths."""
    known = (
        set(_EXPECTED_USER_LEVEL.values())
        | set(_USER_LEVEL_LEAF_ACCESSORS)
        | set(_EXPECTED_PROJECT_LEVEL.values())
        | _OTHER_KNOWN_ACCESSORS
    )
    actual = set(_public_functions())
    assert actual == known, f"unaccounted accessors: {actual ^ known}"


def test_user_level_top_level_entries_match_prd_10_2(tmp_path, monkeypatch):
    """缺失即失败: every PRD 10.2 user-level tree entry has a working accessor
    that actually lands at `user_root()/<entry>` (a directory) or
    `user_root()/<entry>` (a file, `jones.db`) — and nothing else lands directly
    under `user_root()` outside this set once every accessor has been exercised."""
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))

    for entry, accessor_name in _EXPECTED_USER_LEVEL.items():
        result = getattr(paths, accessor_name)()
        if accessor_name == "memory_global_dir":
            # PRD 10.2 nests global memory one level under memory/ — the
            # accessor resolves the full "memory/global" leaf, not just the
            # top-level "memory" entry this loop otherwise checks.
            assert result == paths.user_root() / entry / "global"
            continue
        assert result == paths.user_root() / entry, (
            f"{accessor_name}() = {result}, expected {paths.user_root() / entry}"
        )

    for leaf_accessor, (parent_accessor, filename) in _USER_LEVEL_LEAF_ACCESSORS.items():
        result = getattr(paths, leaf_accessor)()
        assert result == getattr(paths, parent_accessor)() / filename

    on_disk = {p.name for p in paths.user_root().iterdir()}
    assert on_disk <= set(_EXPECTED_USER_LEVEL), f"unexpected entries on disk: {on_disk}"


def test_project_level_entries_match_prd_10_2(tmp_path, monkeypatch):
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))
    project_dir = tmp_path / "myproj"
    project_dir.mkdir()

    for entry, accessor_name in _EXPECTED_PROJECT_LEVEL.items():
        result = getattr(paths, accessor_name)(str(project_dir))
        assert result == paths.project_root(str(project_dir)) / entry, (
            f"{accessor_name}(...) = {result}, "
            f"expected {paths.project_root(str(project_dir)) / entry}"
        )

    on_disk = {p.name for p in paths.project_root(str(project_dir)).iterdir()}
    assert on_disk <= set(_EXPECTED_PROJECT_LEVEL), f"unexpected entries on disk: {on_disk}"


def test_project_attachments_dir_matches_the_user_level_projects_leaf(tmp_path, monkeypatch):
    """`<user_root>/projects/<project-id>/` (PRD 10.2's `projects/<project-id>/`
    attachments leaf) — a user-level entry keyed by Project id, not by path, so
    it's checked on its own rather than folded into the top-level sweep above."""
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))
    assert paths.project_attachments_dir("proj_x") == paths.projects_dir() / "proj_x"
    assert paths.project_attachments_dir("proj_x").is_dir()
    # create=False must not resurrect a deleted Project's attachments directory.
    paths.project_attachments_dir("proj_x").rmdir()
    assert not paths.project_attachments_dir("proj_y", create=False).exists()
