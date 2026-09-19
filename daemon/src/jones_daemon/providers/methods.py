"""RPC methods `provider.list` / `provider.set_key` / `provider.delete_key` / `model.list`
(docs/design/00-foundation.md §4.1, docs/design/01-w2-interfaces.md §3).

`register(server, ctx)` follows the module convention every W2 branch uses (01-w2-interfaces.md
§0): `ctx` is the shared `DaemonContext` (§1), not yet built at the time this module was written
(branch A owns `context.py`). The one field this module needs is `ctx.db` — read here as the raw
`sqlite3.Connection` `store.connect()` already returns (store/db.py has no wrapper class today;
"Database" in the §1 dataclass sketch is described as "store/db.py 现有连接封装", i.e. this same
connection plus the `run_in_db_thread` discipline, not a new type). If `context.py` lands with a
`Database` wrapper exposing the connection under a different attribute, this is the one line that
needs updating — see the report's "契约变更" section.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from typing import Any

from jones_daemon import paths
from jones_daemon.providers.catalog import VENDORS
from jones_daemon.providers.resolver import DaemonProviderResolver, ProviderNotConfiguredError
from jones_daemon.rpc.errors import INVALID_PARAMS, NOT_FOUND, PROVIDER_ERROR, RpcError
from jones_daemon.rpc.server import Connection, RpcServer
from jones_daemon.secrets.vault import Vault, VaultError, build_default_vault
from jones_daemon.store import run_in_db_thread

_MAX_KEY_LENGTH = 4096


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _require_vendor(provider: Any) -> str:
    name = str(provider or "").strip()
    if name not in VENDORS:
        raise RpcError(
            NOT_FOUND,
            f"unknown provider: {provider!r}",
            {"known_providers": sorted(VENDORS)},
        )
    return name


def _require_key(raw: Any) -> str:
    # Structural validation only (non-empty, no control characters, bounded length) — "Key 校验
    # 失败有明确提示" for malformed input. Live validation (does the provider actually accept this
    # key) needs a network call per vendor and is out of W2 scope (Issue #7: six-vendor live Turn
    # verification is W5).
    if not isinstance(raw, str):
        raise RpcError(INVALID_PARAMS, "key must be a string")
    key = raw.strip()
    if not key:
        raise RpcError(INVALID_PARAMS, "key must not be empty")
    if len(key) > _MAX_KEY_LENGTH:
        raise RpcError(INVALID_PARAMS, f"key exceeds {_MAX_KEY_LENGTH} characters")
    if any(ord(c) < 0x20 for c in key):
        raise RpcError(INVALID_PARAMS, "key must not contain control characters")
    return key


def _key_hint(key: str) -> str:
    return key[-4:] if len(key) >= 4 else key


def _query_provider_rows(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    rows = conn.execute("SELECT name, has_key, key_hint, default_model FROM providers").fetchall()
    return {row["name"]: row for row in rows}


def _write_provider_key(
    conn: sqlite3.Connection, vault: Vault, vendor: str, key: str, *, force: bool = False
) -> str:
    """Vault first, then the `providers` row — so a crash between the two steps can only ever
    leave `has_key=0` with an orphaned (harmless) vault entry, never `has_key=1` pointing at a
    key that was never actually written (see resolver.py's db/vault-out-of-sync check, which
    exists as the second line of defense for the same reason).

    Round 1 review: a `vault.enc` that `_read_entries()` can no longer read (corrupt file, or a
    Keychain data key that no longer matches) used to make `vault.set()` raise `VaultError` on
    every future `provider.set_key` call, forever — the *only* way in for BYOK (PRD §46) locked
    shut with no recovery. Without `force`, that failure is now reported as a clear
    `PROVIDER_ERROR` instead of a bare `INTERNAL_ERROR`; with `force=True`, the caller has already
    been told this discards the old vault, so `vault.reset()` bypasses the read that just failed
    and starts a fresh one containing only `vendor`'s key.
    """
    try:
        vault.set(vendor, key)
    except VaultError as exc:
        if not force:
            raise RpcError(
                PROVIDER_ERROR,
                f"the credential vault could not be read ({exc}); it may be corrupted or its "
                "encryption key no longer matches. Resubmit provider.set_key with force=true to "
                "discard it and start a fresh vault — every previously stored provider key will "
                "need to be re-entered",
                {"vendor": vendor, "vault_unreadable": True},
            ) from exc
        # The fresh vault holds only `vendor`'s key now — every other provider's `has_key` must
        # drop to 0 too, or `providers` would keep claiming keys exist that no longer do anywhere
        # (exactly the "db/vault out of sync" state resolver.py refuses to guess through).
        conn.execute(
            "UPDATE providers SET has_key = 0, key_hint = NULL, updated_at = ? WHERE name != ?",
            (_now_iso(), vendor),
        )
        vault.reset({vendor: key})
    hint = _key_hint(key)
    now = _now_iso()
    existing = conn.execute("SELECT id FROM providers WHERE name = ?", (vendor,)).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO providers "
            "(id, name, has_key, key_hint, default_model, created_at, updated_at) "
            "VALUES (?, ?, 1, ?, ?, ?, ?)",
            (f"provider_{vendor}", vendor, hint, VENDORS[vendor].default_model, now, now),
        )
    else:
        conn.execute(
            "UPDATE providers SET has_key = 1, key_hint = ?, updated_at = ? WHERE name = ?",
            (hint, now, vendor),
        )
    conn.commit()
    return hint


def _clear_provider_key(conn: sqlite3.Connection, vault: Vault, vendor: str) -> None:
    """`providers` row first, then the vault — the opposite order from `_write_provider_key`,
    deliberately: a crash here can only leave a stray vault entry behind a `has_key=0` row
    (harmless — resolver.py never reaches the vault when `has_key` is false), never the reverse.

    Round 1 review: if the vault can't be read at all, `vault.delete()` used to raise a bare
    `VaultError` that reached the RPC layer as `INTERNAL_ERROR` with no actionable message — even
    though the `providers` row above had already committed `has_key=0`, i.e. the entire
    externally-visible contract of `delete_key` already held. Report it as `PROVIDER_ERROR` with a
    human message instead (there's nothing to force-reset here: unlike `set_key`, the recovery
    path is "set any provider's key with force=true", which this same corrupt vault already
    supports).
    """
    conn.execute(
        "UPDATE providers SET has_key = 0, key_hint = NULL, updated_at = ? WHERE name = ?",
        (_now_iso(), vendor),
    )
    conn.commit()
    try:
        vault.delete(vendor)
    except VaultError as exc:
        raise RpcError(
            PROVIDER_ERROR,
            f"provider marked as not configured, but its stored key could not be removed from "
            f"the credential vault ({exc}); the leftover entry is harmless on its own — set a new "
            "key for any provider with force=true to discard the vault entirely",
            {"vendor": vendor, "vault_unreadable": True},
        ) from exc


def _make_provider_list(conn: sqlite3.Connection):
    async def handler(params: dict[str, Any], _conn: Connection) -> list[dict]:
        rows = await run_in_db_thread(_query_provider_rows, conn)
        out = []
        for vendor, spec in VENDORS.items():
            row = rows.get(vendor)
            out.append(
                {
                    "provider": vendor,
                    "has_key": bool(row["has_key"]) if row else False,
                    "key_hint": row["key_hint"] if row else None,
                    "default_model": (row["default_model"] if row else None) or spec.default_model,
                }
            )
        return out

    return handler


def _make_provider_set_key(conn: sqlite3.Connection, vault: Vault):
    async def handler(params: dict[str, Any], _conn: Connection) -> dict:
        vendor = _require_vendor(params.get("provider"))
        key = _require_key(params.get("key"))
        force = bool(params.get("force"))
        hint = await run_in_db_thread(_write_provider_key, conn, vault, vendor, key, force=force)
        return {"provider": vendor, "has_key": True, "key_hint": hint}

    return handler


def _make_provider_delete_key(conn: sqlite3.Connection, vault: Vault):
    async def handler(params: dict[str, Any], _conn: Connection) -> dict:
        vendor = _require_vendor(params.get("provider"))
        await run_in_db_thread(_clear_provider_key, conn, vault, vendor)
        return {"provider": vendor, "has_key": False, "key_hint": None}

    return handler


def _make_model_list(resolver: DaemonProviderResolver):
    async def handler(params: dict[str, Any], _conn: Connection) -> list[dict]:
        provider = params.get("provider")
        if provider is not None:
            _require_vendor(provider)
        try:
            # list_models() never touches the shared sqlite connection (see its docstring), so
            # asyncio.to_thread — not run_in_db_thread — is the right offload: no reason to
            # serialize this behind the DB's single dedicated thread.
            return await asyncio.to_thread(resolver.list_models, provider)
        except ProviderNotConfiguredError as exc:
            raise RpcError(NOT_FOUND, str(exc)) from exc

    return handler


def register(server: RpcServer, ctx: Any) -> None:
    conn: sqlite3.Connection = ctx.db
    vault = build_default_vault(paths.secrets_dir())
    resolver = DaemonProviderResolver(conn, vault)

    server.register("provider.list", _make_provider_list(conn))
    server.register("provider.set_key", _make_provider_set_key(conn, vault))
    server.register("provider.delete_key", _make_provider_delete_key(conn, vault))
    server.register("model.list", _make_model_list(resolver))
