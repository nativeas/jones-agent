"""`ProviderResolver` (docs/design/01-w2-interfaces.md §3) and the six-vendor → Hermes binding
logic.

`resolve()` and `list_models()` are synchronous per the documented Protocol (they're called by
worker-launch code, not RPC handlers) — but `resolve()` reads the shared SQLite connection, which
is `check_same_thread=True` and single-threaded by convention (store/db.py). **Callers must invoke
`resolve()` via `run_in_db_thread`** (e.g. `await run_in_db_thread(resolver.resolve, model_pref)`),
never directly on the asyncio event loop thread. `list_models()` touches no DB state (see its
docstring) and has no such constraint, but does a blocking local HTTP call for `provider="ollama"`
— callers on an event loop should still offload it (e.g. `asyncio.to_thread`).
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from typing import Protocol, TypedDict

from jones_daemon.providers.catalog import VENDOR_PRIORITY, VENDORS
from jones_daemon.secrets.vault import Vault

# Local-only, no user-facing override yet (Jones doesn't have an Ollama base_url setting in v1 —
# see the report's "assumptions" section). Matches Hermes's own OLLAMA_HOST-less default
# (hermes_cli/models_local.py `_ollama_host_from_env`).
_OLLAMA_NATIVE_ROOT = "http://localhost:11434"
_OLLAMA_PROBE_TIMEOUT_S = 1.5


class ProviderBinding(TypedDict):
    provider: str  # anthropic | openai | deepseek | qwen | gemini | ollama
    model: str
    env: dict[str, str]  # env vars for the worker subprocess; the key value appears here ONCE
    hermes_config: dict  # merged into the worker's HERMES_HOME/config.yaml


class ProviderResolver(Protocol):
    def resolve(self, model_pref: dict | None) -> ProviderBinding: ...

    def list_models(self, provider: str | None) -> list[dict]: ...


class ProviderNotConfiguredError(RuntimeError):
    """Raised by `resolve()`/`list_models()` for an unknown vendor or a vendor with no usable key.

    Mirrors the `not_configured` condition A's `NullProviderResolver` raises before this resolver
    is wired in (docs/design/01-w2-interfaces.md §1) — callers (WorkerManager) should treat both
    the same way.
    """


def _build_binding(vendor: str, model: str | None, key: str | None) -> ProviderBinding:
    spec = VENDORS[vendor]
    model_id = model or spec.default_model
    if not model_id:
        raise ProviderNotConfiguredError(
            f"provider '{vendor}' has no model specified and no default_model configured"
        )

    env: dict[str, str] = {}
    hermes_config: dict = {"model": {"default": model_id, "provider": spec.hermes_provider}}

    if spec.custom_provider:
        provider_entry: dict = {"api": spec.default_base_url}
        if key:
            # Hermes's custom-provider entries take a *reference* to an env var (`key_env`), not
            # an inline key — so the key value is written to `env` exactly once, never duplicated
            # into hermes_config (docs/design/01-w2-interfaces.md §3: "Key 只在这里出现一次").
            env_name = f"JONES_{vendor.upper()}_API_KEY"
            provider_entry["key_env"] = env_name
            env[env_name] = key
        hermes_config["providers"] = {spec.hermes_provider: provider_entry}
    else:
        if not key:
            raise ProviderNotConfiguredError(f"provider '{vendor}' has no key configured")
        assert spec.key_env, f"vendor spec for '{vendor}' must declare key_env (see catalog.py)"
        env[spec.key_env] = key

    return ProviderBinding(provider=vendor, model=model_id, env=env, hermes_config=hermes_config)


def _fetch_provider_row(conn: sqlite3.Connection, vendor: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT has_key, default_model FROM providers WHERE name = ?", (vendor,)
    ).fetchone()


def _ollama_live_models() -> list[dict]:
    """Live `/api/tags` probe (hermes_cli/models_local.py's own approach for the local Ollama
    catalog) — never raises: an unreachable local server just means an empty list, not an RPC
    failure (DEV.md 工程原则 #4 diagnoses the *caller's* failures loudly; a local dev tool simply
    not running isn't one — the empty list is itself the honest answer).
    """
    try:
        with urllib.request.urlopen(
            f"{_OLLAMA_NATIVE_ROOT}/api/tags", timeout=_OLLAMA_PROBE_TIMEOUT_S
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return []
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return []
    out = []
    for entry in models:
        name = entry.get("name") if isinstance(entry, dict) else None
        if isinstance(name, str) and name.strip():
            out.append({"id": name.strip(), "provider": "ollama"})
    return out


class DaemonProviderResolver:
    """The real `ProviderResolver` (docs/design/01-w2-interfaces.md §1's `NullProviderResolver`
    stand-in, now implemented). Reads key state from the shared `providers` table and key material
    from `vault`.
    """

    def __init__(self, conn: sqlite3.Connection, vault: Vault) -> None:
        self._conn = conn
        self._vault = vault

    def resolve(self, model_pref: dict | None) -> ProviderBinding:
        vendor, model = self._select_vendor(model_pref)
        row = _fetch_provider_row(self._conn, vendor)
        has_key = bool(row is not None and row["has_key"])
        if not has_key and not VENDORS[vendor].custom_provider:
            raise ProviderNotConfiguredError(f"provider '{vendor}' has no key configured")

        key = self._vault.get(vendor)
        if has_key and not key:
            # The providers table and the vault disagree — never proceed on a guess.
            raise ProviderNotConfiguredError(
                f"provider '{vendor}' is marked configured but has no key in the vault "
                "(db/vault out of sync)"
            )

        default_model = model or (row["default_model"] if row is not None else None)
        return _build_binding(vendor, default_model, key)

    def _select_vendor(self, model_pref: dict | None) -> tuple[str, str | None]:
        if model_pref:
            vendor = str(model_pref.get("provider") or "").strip()
            if vendor not in VENDORS:
                raise ProviderNotConfiguredError(f"unknown provider '{vendor}'")
            model = str(model_pref.get("model") or "").strip() or None
            return vendor, model

        configured = {
            row["name"]
            for row in self._conn.execute(
                "SELECT name FROM providers WHERE has_key = 1"
            ).fetchall()
        }
        for vendor in VENDOR_PRIORITY:
            if vendor in configured:
                return vendor, None
        raise ProviderNotConfiguredError(
            "no provider has a key configured; set one via provider.set_key"
        )

    def list_models(self, provider: str | None) -> list[dict]:
        """Pure catalog lookup (plus a live probe for Ollama) — deliberately does not touch
        `self._conn`: which models a vendor *could* serve doesn't depend on whether a key is
        currently configured for it (the settings page needs to show the catalog before a key is
        even entered).
        """
        if provider is not None and provider not in VENDORS:
            raise ProviderNotConfiguredError(f"unknown provider '{provider}'")
        vendors = (provider,) if provider else tuple(VENDORS)
        out: list[dict] = []
        for vendor in vendors:
            if vendor == "ollama":
                out.extend(_ollama_live_models())
            else:
                out.extend({"id": m, "provider": vendor} for m in VENDORS[vendor].models)
        return out


def build_default_resolver(conn: sqlite3.Connection, vault: Vault) -> DaemonProviderResolver:
    return DaemonProviderResolver(conn, vault)
