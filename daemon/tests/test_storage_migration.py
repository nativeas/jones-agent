"""G19 — 数据可迁移 (PRD 12.1, 04-w5-interfaces.md §5): `~/.jones/` copied whole to
another machine stays usable, except `secrets/vault.enc` (bound to the origin
machine's Keychain-derived data key) — which must fail *explicitly* as
`VaultKeyMismatchError`, never a crash or a silent empty-vault read.

`JONES_VAULT_KEY` (base64, 32 bytes) is the test-only stand-in for "a different
machine's Keychain" (secrets/vault.py's own module docstring: this env var exists
specifically so tests never touch a real Keychain) — machine A's key and machine
B's key are two different `JONES_VAULT_KEY` values, exactly modeling "the same
`vault.enc` bytes, a different data key" without needing two real macOS Keychains.
"""

from __future__ import annotations

import base64
import os
import secrets as _secrets
import shutil
from pathlib import Path

from jones_daemon import paths
from jones_daemon.agents.service import AgentService
from jones_daemon.projects.bootstrap import bootstrap_projects_and_agents
from jones_daemon.secrets.vault import Vault, VaultError, VaultKeyMismatchError, build_default_vault
from jones_daemon.sessions import queries
from jones_daemon.skills import service as skills_service
from jones_daemon.store import apply_pending, connect


def _random_vault_key() -> str:
    return base64.b64encode(_secrets.token_bytes(32)).decode("ascii")


def _write_skill(root: Path, name: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n\n# {name}\n", encoding="utf-8"
    )


def test_full_home_directory_survives_a_cross_machine_copy_except_the_vault(tmp_path, monkeypatch):
    # --- "machine A": start a daemon's worth of state -------------------------
    home_a = tmp_path / "machine-a-home"
    key_a = _random_vault_key()
    monkeypatch.setenv("JONES_HOME", str(home_a))
    monkeypatch.setenv("JONES_VAULT_KEY", key_a)

    conn = connect(paths.db_path())
    apply_pending(conn)
    bootstrap_projects_and_agents(conn)
    AgentService(conn).upsert({"name": "Researcher", "persona": "digs deep"})
    queries.create_session(
        conn, session_id="s1", project_id="proj_default", agent_id="agent_default",
        parent_id=None, is_main=False, mode="task", title="a session to migrate",
    )
    queries.create_turn_and_user_message(
        conn, turn_id="t1", message_id="m1", session_id="s1", text="hello from machine A",
        queued=False,
    )
    _write_skill(paths.skills_dir(), "weekly-report")

    vault_a = build_default_vault(paths.secrets_dir())
    vault_a.set("anthropic", "sk-ant-machine-a-secret-0000000000")

    conn.close()  # "停" — daemon shuts down, connection closed cleanly

    # --- copy the whole ~/.jones/ tree to "machine B" --------------------------
    home_b = tmp_path / "machine-b-home"
    shutil.copytree(home_a, home_b)

    # --- "machine B": start with a *different* vault key -----------------------
    monkeypatch.setenv("JONES_HOME", str(home_b))
    monkeypatch.setenv("JONES_VAULT_KEY", _random_vault_key())

    conn_b = connect(paths.db_path())
    try:
        # Session/Agent/Skill history is fully readable, untouched by the vault
        # mismatch below (PRD G19: "会话历史/Agent/Skill 完整可用").
        session = queries.get_session(conn_b, "s1")
        assert session is not None
        assert session["title"] == "a session to migrate"
        messages = queries.list_turn_messages(conn_b, session_id="s1", before_seq=None, limit=10)
        assert any(m["content"]["text"] == "hello from machine A" for m in messages)

        agents = AgentService(conn_b).list(project_id=None)
        assert any(a["name"] == "Researcher" for a in agents)

        skills = skills_service.list_skills(project_path=None)
        assert any(s["name"] == "weekly-report" for s in skills)

        # secrets/: the file exists (it was copied byte-for-byte) but decrypts
        # with the *old* machine's key baked in — machine B's different
        # JONES_VAULT_KEY must fail as an explicit, distinguishable error, not a
        # crash and not a silent "no keys configured".
        vault_b = build_default_vault(paths.secrets_dir())
        try:
            vault_b.get("anthropic")
            raise AssertionError("expected VaultKeyMismatchError, got no exception")
        except VaultKeyMismatchError:
            pass
    finally:
        conn_b.close()


def test_vault_key_mismatch_is_a_distinct_subclass_of_vault_error(tmp_path, monkeypatch):
    """Callers that only `except VaultError` (providers/methods.py, unchanged by
    this issue) keep working unmodified; callers that need to tell "wrong key,
    re-enter your keys" apart from "file corrupt" can `except
    VaultKeyMismatchError` specifically."""
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))
    path = tmp_path / "home" / "secrets" / "vault.enc"
    path.parent.mkdir(parents=True)

    key1 = base64.b64decode(_random_vault_key())
    key2 = base64.b64decode(_random_vault_key())
    Vault(path, data_key=key1).set("p", "secret-value")

    mismatched = Vault(path, data_key=key2)
    try:
        mismatched.get("p")
        raise AssertionError("expected VaultKeyMismatchError")
    except VaultKeyMismatchError as exc:
        assert isinstance(exc, VaultError)  # narrows, doesn't replace, the base contract


def test_a_genuinely_corrupt_vault_file_is_a_plain_vault_error_not_a_key_mismatch(tmp_path):
    path = tmp_path / "vault.enc"
    path.write_text("not even json")
    vault = Vault(path, data_key=os.urandom(32))
    try:
        vault.get("p")
        raise AssertionError("expected VaultError")
    except VaultKeyMismatchError:
        raise AssertionError("a malformed file must not be reported as a key mismatch") from None
    except VaultError:
        pass
