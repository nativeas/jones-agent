"""ConfigResolver merge behavior (docs/design/01-w2-interfaces.md §4, issue #8/#9).

The permission-tightening rule is the load-bearing contract here — PRD 10.1 /
G14 / N11: a project-level `permissions.json` can only *tighten* what the
user level allows, never loosen it, and a rule that tries to loosen is
dropped (with a warning), not applied and not a hard error.
"""

import sqlite3

import pytest

from jones_daemon import paths
from jones_daemon.config.jsonfile import write_json
from jones_daemon.config.resolver import DEFAULT_SETTINGS, DefaultConfigResolver
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


@pytest.fixture
def project(conn, tmp_path):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    return ProjectService(conn).create(str(workdir))


def _rule(match: str, action: str) -> dict:
    return {"rules": [{"match": match, "action": action}]}


# -- settings() ---------------------------------------------------------------


def test_settings_returns_built_in_defaults_with_nothing_configured(conn):
    resolver = DefaultConfigResolver(conn)
    assert resolver.settings(None) == DEFAULT_SETTINGS


def test_settings_user_level_overrides_defaults(conn):
    write_json(paths.config_dir() / "settings.json", {"concurrency_limit": 8})
    resolver = DefaultConfigResolver(conn)
    settings = resolver.settings(None)
    assert settings["concurrency_limit"] == 8
    assert settings["default_mode"] == DEFAULT_SETTINGS["default_mode"]  # untouched key


def test_settings_project_level_overrides_user_level(conn, project):
    write_json(paths.config_dir() / "settings.json", {"default_mode": "task"})
    write_json(paths.project_settings_path(project["path"]), {"default_mode": "auto"})
    resolver = DefaultConfigResolver(conn)

    assert resolver.settings(None)["default_mode"] == "task"
    assert resolver.settings(project["id"])["default_mode"] == "auto"


def test_settings_unknown_project_id_raises_not_found(conn):
    resolver = DefaultConfigResolver(conn)
    with pytest.raises(RpcError):
        resolver.settings("no-such-project")


# -- permissions(): tightening-only merge (PRD 10.1 / G14 / N11) --------------


def test_permissions_project_may_add_a_new_deny_rule(conn, project):
    write_json(paths.project_permissions_path(project["path"]), _rule("git push", "deny"))
    resolver = DefaultConfigResolver(conn)

    result = resolver.permissions(project["id"])

    assert {"match": "git push", "action": "deny"} in [
        {"match": r.match, "action": r.action} for r in result.rules
    ]
    assert result.warnings == ()


def test_permissions_project_cannot_loosen_a_user_level_deny(conn, project):
    write_json(paths.config_dir() / "permissions.json", _rule("rm", "deny"))
    write_json(paths.project_permissions_path(project["path"]), _rule("rm", "allow"))
    resolver = DefaultConfigResolver(conn)

    result = resolver.permissions(project["id"])

    rule = next(r for r in result.rules if r.match == "rm")
    assert rule.action == "deny"  # user-level deny wins; project's "allow" is dropped
    assert len(result.warnings) == 1
    assert "rm" in result.warnings[0]


def test_permissions_project_reaffirming_a_user_deny_is_not_a_warning(conn, project):
    write_json(paths.config_dir() / "permissions.json", _rule("rm", "deny"))
    write_json(paths.project_permissions_path(project["path"]), _rule("rm", "deny"))
    resolver = DefaultConfigResolver(conn)

    result = resolver.permissions(project["id"])

    assert result.warnings == ()


def test_permissions_with_no_project_id_returns_only_user_rules(conn):
    write_json(paths.config_dir() / "permissions.json", _rule("rm", "deny"))
    resolver = DefaultConfigResolver(conn)

    result = resolver.permissions(None)

    assert [r.match for r in result.rules] == ["rm"]
    assert result.warnings == ()


# -- mcp_servers(): plain override-by-name merge (not a permission) -----------


def test_mcp_servers_project_entry_overrides_user_entry_of_the_same_name(conn, project):
    user_servers = {"servers": [{"name": "search", "url": "user-url"}]}
    write_json(paths.config_dir() / "mcp.json", user_servers)
    write_json(
        paths.project_mcp_path(project["path"]),
        {"servers": [{"name": "search", "url": "project-url"}, {"name": "extra", "url": "x"}]},
    )
    resolver = DefaultConfigResolver(conn)

    servers = {s["name"]: s for s in resolver.mcp_servers(project["id"])}

    assert servers["search"]["url"] == "project-url"
    assert "extra" in servers
    # User-level view is unaffected by the project override.
    assert resolver.mcp_servers(None) == user_servers["servers"]
