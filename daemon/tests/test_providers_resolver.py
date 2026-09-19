import json
import os
from unittest.mock import patch

import pytest

from jones_daemon.providers.catalog import VENDOR_PRIORITY, VENDORS
from jones_daemon.providers.resolver import (
    DaemonProviderResolver,
    ProviderNotConfiguredError,
    _ollama_live_models,
)
from jones_daemon.secrets.vault import Vault
from jones_daemon.store.db import connect
from jones_daemon.store.migrator import apply_pending

_TEST_KEY = os.urandom(32)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "jones.db")
    apply_pending(c)
    yield c
    c.close()


@pytest.fixture
def vault(tmp_path):
    return Vault(tmp_path / "vault.enc", data_key=_TEST_KEY)


def _configure(conn, vault, vendor: str, key: str) -> None:
    now = "2026-09-19T00:00:00Z"
    conn.execute(
        "INSERT INTO providers "
        "(id, name, has_key, key_hint, default_model, created_at, updated_at) "
        "VALUES (?, ?, 1, ?, ?, ?, ?)",
        (f"provider_{vendor}", vendor, key[-4:], VENDORS[vendor].default_model, now, now),
    )
    conn.commit()
    vault.set(vendor, key)


def test_every_catalog_vendor_resolves_when_configured(conn, vault):
    resolver = DaemonProviderResolver(conn, vault)
    for vendor in VENDORS:
        if vendor == "ollama":
            continue  # keyless — covered separately
        key = f"secret-for-{vendor}"
        _configure(conn, vault, vendor, key)
        binding = resolver.resolve({"provider": vendor, "model": "explicit-model"})
        assert binding["provider"] == vendor
        assert binding["model"] == "explicit-model"
        assert binding["hermes_config"]["model"] == {
            "default": "explicit-model",
            "provider": VENDORS[vendor].hermes_provider,
        }
        spec = VENDORS[vendor]
        assert binding["env"] == {spec.key_env: key}
        # the key value appears exactly once in the whole binding (env), never in hermes_config
        assert key not in json.dumps(binding["hermes_config"])


def test_resolve_falls_back_to_provider_default_model(conn, vault):
    _configure(conn, vault, "anthropic", "sk-ant-abc")
    resolver = DaemonProviderResolver(conn, vault)
    binding = resolver.resolve({"provider": "anthropic"})
    assert binding["model"] == VENDORS["anthropic"].default_model


def test_resolve_unconfigured_provider_raises(conn, vault):
    resolver = DaemonProviderResolver(conn, vault)
    with pytest.raises(ProviderNotConfiguredError, match="anthropic"):
        resolver.resolve({"provider": "anthropic"})


def test_resolve_unknown_provider_raises(conn, vault):
    resolver = DaemonProviderResolver(conn, vault)
    with pytest.raises(ProviderNotConfiguredError, match="unknown provider"):
        resolver.resolve({"provider": "not-a-real-vendor"})


def test_resolve_db_vault_out_of_sync_raises_not_silently(conn, vault):
    # has_key=1 in the table but nothing was ever written to the vault (e.g. a crash between the
    # two writes, or manual DB tampering) — resolve() must fail loudly, never launch a worker
    # with an empty key.
    now = "2026-09-19T00:00:00Z"
    conn.execute(
        "INSERT INTO providers "
        "(id, name, has_key, key_hint, default_model, created_at, updated_at) "
        "VALUES ('provider_anthropic', 'anthropic', 1, 'abcd', 'claude-opus-4-6', ?, ?)",
        (now, now),
    )
    conn.commit()
    resolver = DaemonProviderResolver(conn, vault)
    with pytest.raises(ProviderNotConfiguredError, match="out of sync"):
        resolver.resolve({"provider": "anthropic"})


def test_resolve_none_picks_default_by_vendor_priority(conn, vault):
    _configure(conn, vault, "openai", "sk-openai-1")
    _configure(conn, vault, "anthropic", "sk-ant-1")
    resolver = DaemonProviderResolver(conn, vault)
    # both configured; anthropic is first in VENDOR_PRIORITY
    assert resolver.resolve(None)["provider"] == "anthropic"


def test_resolve_none_skips_unconfigured_and_uses_next_priority(conn, vault):
    assert VENDOR_PRIORITY[0] == "anthropic"
    _configure(conn, vault, "deepseek", "sk-deepseek-1")
    resolver = DaemonProviderResolver(conn, vault)
    assert resolver.resolve(None)["provider"] == "deepseek"


def test_resolve_none_raises_when_nothing_configured(conn, vault):
    resolver = DaemonProviderResolver(conn, vault)
    with pytest.raises(ProviderNotConfiguredError, match="no provider has a key configured"):
        resolver.resolve(None)


# --- ollama: custom provider, keyless by default --------------------------------------------


def test_ollama_resolves_without_a_key(conn, vault):
    resolver = DaemonProviderResolver(conn, vault)
    binding = resolver.resolve({"provider": "ollama", "model": "llama3.3"})
    assert binding["env"] == {}
    assert binding["hermes_config"]["providers"]["ollama"] == {
        "api": VENDORS["ollama"].default_base_url
    }
    assert "key_env" not in binding["hermes_config"]["providers"]["ollama"]


def test_ollama_with_optional_key_references_it_via_key_env_not_inline(conn, vault):
    _configure(conn, vault, "ollama", "proxy-bearer-token")
    resolver = DaemonProviderResolver(conn, vault)
    binding = resolver.resolve({"provider": "ollama", "model": "llama3.3"})
    provider_entry = binding["hermes_config"]["providers"]["ollama"]
    env_name = provider_entry["key_env"]
    assert binding["env"] == {env_name: "proxy-bearer-token"}
    assert "proxy-bearer-token" not in json.dumps(binding["hermes_config"])


def test_ollama_without_a_model_raises_clearly(conn, vault):
    resolver = DaemonProviderResolver(conn, vault)
    with pytest.raises(ProviderNotConfiguredError, match="no model specified"):
        resolver.resolve({"provider": "ollama"})


# --- list_models --------------------------------------------------------------------------


def test_list_models_for_a_known_provider_returns_only_its_catalog(conn, vault):
    resolver = DaemonProviderResolver(conn, vault)
    models = resolver.list_models("deepseek")
    assert {m["id"] for m in models} == set(VENDORS["deepseek"].models)
    assert all(m["provider"] == "deepseek" for m in models)


def test_list_models_unknown_provider_raises(conn, vault):
    resolver = DaemonProviderResolver(conn, vault)
    with pytest.raises(ProviderNotConfiguredError, match="unknown provider"):
        resolver.list_models("not-a-real-vendor")


def test_list_models_none_aggregates_every_vendor(conn, vault):
    with patch("jones_daemon.providers.resolver._ollama_live_models", return_value=[]):
        resolver = DaemonProviderResolver(conn, vault)
        models = resolver.list_models(None)
    got_providers = {m["provider"] for m in models}
    assert got_providers == set(VENDORS) - {"ollama"}  # ollama probe returned nothing here
    for vendor in VENDORS:
        if vendor == "ollama":
            continue
        assert len([m for m in models if m["provider"] == vendor]) == len(VENDORS[vendor].models)


def test_list_models_does_not_query_the_db(conn, vault):
    # Catalog lookup must not depend on whether a key is configured — the settings page needs
    # the model list before a key is even entered. Poison the connection to prove it's unused.
    conn.close()
    resolver = DaemonProviderResolver(conn, vault)
    resolver.list_models("anthropic")  # must not raise sqlite3.ProgrammingError


def test_ollama_live_probe_returns_empty_list_when_unreachable():
    # No local Ollama server is expected to be running in the test sandbox; the probe must
    # degrade to an empty list rather than raising.
    assert _ollama_live_models() == []
