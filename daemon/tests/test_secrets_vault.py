import base64
import os

import pytest

from jones_daemon import paths
from jones_daemon.secrets.vault import Vault, VaultError, build_default_vault

_TEST_KEY = os.urandom(32)


def _vault(tmp_path, **kwargs) -> Vault:
    return Vault(tmp_path / "vault.enc", data_key=_TEST_KEY, **kwargs)


def test_get_on_missing_file_returns_none(tmp_path):
    assert _vault(tmp_path).get("anthropic") is None


def test_set_then_get_roundtrips(tmp_path):
    v = _vault(tmp_path)
    v.set("anthropic", "sk-ant-super-secret-value")
    assert v.get("anthropic") == "sk-ant-super-secret-value"


def test_set_persists_across_vault_instances(tmp_path):
    path = tmp_path / "vault.enc"
    Vault(path, data_key=_TEST_KEY).set("openai", "sk-openai-abc123")
    assert Vault(path, data_key=_TEST_KEY).get("openai") == "sk-openai-abc123"


def test_multiple_entries_are_independent(tmp_path):
    v = _vault(tmp_path)
    v.set("anthropic", "key-a")
    v.set("openai", "key-b")
    assert v.get("anthropic") == "key-a"
    assert v.get("openai") == "key-b"
    assert set(v.names()) == {"anthropic", "openai"}


def test_updating_an_entry_does_not_disturb_others(tmp_path):
    v = _vault(tmp_path)
    v.set("anthropic", "key-a")
    v.set("openai", "key-b")
    v.set("anthropic", "key-a-rotated")
    assert v.get("anthropic") == "key-a-rotated"
    assert v.get("openai") == "key-b"


def test_delete_removes_entry_and_reports_existence(tmp_path):
    v = _vault(tmp_path)
    v.set("anthropic", "key-a")
    assert v.delete("anthropic") is True
    assert v.get("anthropic") is None
    assert v.delete("anthropic") is False  # already gone


def test_delete_of_unknown_name_does_not_write_a_file(tmp_path):
    path = tmp_path / "vault.enc"
    v = Vault(path, data_key=_TEST_KEY)
    assert v.delete("nope") is False
    assert not path.exists()


def test_vault_file_is_written_atomically_and_owner_only(tmp_path):
    import stat

    path = tmp_path / "vault.enc"
    Vault(path, data_key=_TEST_KEY).set("anthropic", "key-a")
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # no leftover temp file from the atomic write
    assert list(tmp_path.glob(".*.tmp-*")) == []


def test_wrong_data_key_fails_to_decrypt(tmp_path):
    path = tmp_path / "vault.enc"
    Vault(path, data_key=_TEST_KEY).set("anthropic", "key-a")
    wrong_key_vault = Vault(path, data_key=os.urandom(32))
    with pytest.raises(VaultError, match="decrypt"):
        wrong_key_vault.get("anthropic")


def test_ciphertext_on_disk_never_contains_the_plaintext_secret(tmp_path):
    path = tmp_path / "vault.enc"
    secret = "sk-ant-do-not-leak-this-0123456789"
    Vault(path, data_key=_TEST_KEY).set("anthropic", secret)
    raw = path.read_bytes()
    assert secret.encode() not in raw
    assert base64.b64encode(secret.encode()) not in raw


def test_corrupt_envelope_raises_vault_error_not_silently(tmp_path):
    path = tmp_path / "vault.enc"
    path.write_text("not json at all")
    with pytest.raises(VaultError):
        Vault(path, data_key=_TEST_KEY).get("anthropic")


# --- data key resolution (JONES_VAULT_KEY test gate) ------------------------------------------


def test_env_var_gate_supplies_the_data_key(tmp_path, monkeypatch):
    monkeypatch.delenv("JONES_VAULT_KEY", raising=False)
    monkeypatch.setenv("JONES_VAULT_KEY", base64.b64encode(os.urandom(32)).decode())
    v = build_default_vault(tmp_path)
    v.set("anthropic", "key-a")
    assert v.get("anthropic") == "key-a"


def test_env_var_gate_rejects_non_base64(tmp_path, monkeypatch):
    monkeypatch.setenv("JONES_VAULT_KEY", "not-valid-base64!!!")
    v = build_default_vault(tmp_path)
    with pytest.raises(VaultError, match="base64"):
        v.set("anthropic", "key-a")


def test_env_var_gate_rejects_wrong_length_key(tmp_path, monkeypatch):
    monkeypatch.setenv("JONES_VAULT_KEY", base64.b64encode(b"too-short").decode())
    v = build_default_vault(tmp_path)
    with pytest.raises(VaultError, match="32 bytes"):
        v.set("anthropic", "key-a")


def test_data_key_is_resolved_lazily_not_at_construction(tmp_path, monkeypatch):
    # Constructing a Vault must never require JONES_VAULT_KEY / touch the Keychain by itself —
    # only an actual get/set/delete/names call does (paths.py's "create on first access" pattern).
    monkeypatch.delenv("JONES_VAULT_KEY", raising=False)
    Vault(tmp_path / "vault.enc")  # must not raise


def test_build_default_vault_lands_at_the_documented_path(tmp_path, monkeypatch):
    # docs/design/01-w2-interfaces.md §3: "文件 <user_root>/secrets/vault.enc"
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JONES_VAULT_KEY", base64.b64encode(os.urandom(32)).decode())
    v = build_default_vault(paths.secrets_dir())
    v.set("anthropic", "key-a")
    assert (paths.user_root() / "secrets" / "vault.enc").exists()
