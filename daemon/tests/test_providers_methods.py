import asyncio
import base64
import io
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from jones_daemon.logging import JsonLinesFormatter
from jones_daemon.providers.catalog import VENDORS
from jones_daemon.providers.methods import _clear_provider_key, _write_provider_key, register
from jones_daemon.rpc.errors import INVALID_PARAMS, NOT_FOUND, PROVIDER_ERROR, RpcError
from jones_daemon.rpc.server import RpcServer
from jones_daemon.secrets.vault import Vault, VaultError, VaultKeyMismatchError
from jones_daemon.store.db import connect, run_in_db_thread
from jones_daemon.store.migrator import apply_pending


class _FakeCtx:
    """Stand-in for the not-yet-built DaemonContext — see providers/methods.py's module
    docstring for why `.db` is the raw sqlite3.Connection."""

    def __init__(self, conn):
        self.db = conn


@pytest.fixture(autouse=True)
def vault_key_env(monkeypatch):
    monkeypatch.setenv("JONES_VAULT_KEY", base64.b64encode(os.urandom(32)).decode())


@pytest.fixture
async def conn(tmp_path):
    # Must be created (and later used) on the dedicated DB thread — same invariant as
    # __main__.py's own startup (store/db.py: check_same_thread=True binds the connection to
    # whichever thread called connect()), since the RPC handlers reach it via run_in_db_thread.
    c = await run_in_db_thread(connect, tmp_path / "jones.db")
    await run_in_db_thread(apply_pending, c)
    yield c
    await run_in_db_thread(c.close)


@pytest.fixture
async def server(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "jones-home"))
    # AF_UNIX paths are capped at ~104 bytes on macOS (see test_rpc.py's fixture for the same
    # workaround) — the socket lives under a short-lived dir directly under the system temp root.
    short_dir = Path(tempfile.mkdtemp(prefix="jn-"))
    srv = RpcServer(short_dir / "t.sock")
    register(srv, _FakeCtx(conn))
    await srv.start()
    yield srv
    await srv.stop()
    shutil.rmtree(short_dir, ignore_errors=True)


@pytest.fixture
def log_capture():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLinesFormatter())
    logger = logging.getLogger("jones_daemon")
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    yield stream
    logger.setLevel(previous_level)
    logger.removeHandler(handler)


async def _call(sock_path, method: str, params: dict | None = None, req_id: str = "1") -> dict:
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    try:
        request = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            request["params"] = params
        writer.write((json.dumps(request) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=2)
        return json.loads(line)
    finally:
        writer.close()


async def test_provider_list_returns_all_six_vendors_unconfigured(server):
    response = await _call(server.socket_path, "provider.list")
    assert "error" not in response
    result = {row["provider"]: row for row in response["result"]}
    assert set(result) == set(VENDORS)
    for vendor, row in result.items():
        assert row["has_key"] is False
        assert row["key_hint"] is None
        assert row["default_model"] == VENDORS[vendor].default_model


async def test_set_key_then_list_reflects_it(server):
    set_response = await _call(
        server.socket_path, "provider.set_key", {"provider": "anthropic", "key": "sk-ant-abcd1234"}
    )
    assert "error" not in set_response
    assert set_response["result"] == {"provider": "anthropic", "has_key": True, "key_hint": "1234"}

    list_response = await _call(server.socket_path, "provider.list")
    row = next(r for r in list_response["result"] if r["provider"] == "anthropic")
    assert row["has_key"] is True
    assert row["key_hint"] == "1234"


async def test_set_key_unknown_provider_returns_not_found(server):
    response = await _call(
        server.socket_path, "provider.set_key", {"provider": "bedrock", "key": "whatever"}
    )
    assert response["error"]["code"] == NOT_FOUND


async def test_set_key_empty_key_returns_invalid_params(server):
    response = await _call(
        server.socket_path, "provider.set_key", {"provider": "anthropic", "key": "   "}
    )
    assert response["error"]["code"] == INVALID_PARAMS


async def test_delete_key_clears_configured_state(server):
    await _call(
        server.socket_path, "provider.set_key", {"provider": "openai", "key": "sk-openai-xyz"}
    )
    delete_response = await _call(server.socket_path, "provider.delete_key", {"provider": "openai"})
    assert delete_response["result"] == {"provider": "openai", "has_key": False, "key_hint": None}

    list_response = await _call(server.socket_path, "provider.list")
    row = next(r for r in list_response["result"] if r["provider"] == "openai")
    assert row["has_key"] is False


async def test_delete_key_unknown_provider_returns_not_found(server):
    response = await _call(server.socket_path, "provider.delete_key", {"provider": "nope"})
    assert response["error"]["code"] == NOT_FOUND


# --- round 1 review: a vault that can't be decrypted must not permanently lock BYOK out --------


def _corrupt_the_vault(tmp_path: Path) -> None:
    vault_path = Path(os.environ["JONES_HOME"]) / "secrets" / "vault.enc"
    assert vault_path.exists(), "corrupt a vault that was never created — test setup bug"
    vault_path.write_text("not json at all")


async def test_set_key_on_unreadable_vault_returns_provider_error_not_internal(server, tmp_path):
    await _call(
        server.socket_path, "provider.set_key", {"provider": "anthropic", "key": "sk-ant-abcd1234"}
    )
    _corrupt_the_vault(tmp_path)

    response = await _call(
        server.socket_path, "provider.set_key", {"provider": "openai", "key": "sk-openai-zyx98765"}
    )
    assert response["error"]["code"] == PROVIDER_ERROR
    assert "force=true" in response["error"]["message"]

    # Without force, nothing changed — anthropic's db row is untouched, still has_key=1.
    list_response = await _call(server.socket_path, "provider.list")
    row = next(r for r in list_response["result"] if r["provider"] == "anthropic")
    assert row["has_key"] is True


async def test_set_key_force_true_discards_broken_vault_and_recovers(server, tmp_path):
    await _call(
        server.socket_path, "provider.set_key", {"provider": "anthropic", "key": "sk-ant-abcd1234"}
    )
    _corrupt_the_vault(tmp_path)

    response = await _call(
        server.socket_path,
        "provider.set_key",
        {"provider": "openai", "key": "sk-openai-zyx98765", "force": True},
    )
    assert "error" not in response
    assert response["result"] == {"provider": "openai", "has_key": True, "key_hint": "8765"}

    rows = {r["provider"]: r for r in (await _call(server.socket_path, "provider.list"))["result"]}
    assert rows["openai"]["has_key"] is True
    # anthropic's key lived only in the vault that was just discarded — has_key must drop too, or
    # `providers` would keep claiming a key that exists nowhere (the "db/vault out of sync" state
    # resolver.py refuses to guess through).
    assert rows["anthropic"]["has_key"] is False

    # The vault is genuinely usable again — a plain (non-force) set_key now succeeds.
    followup = await _call(
        server.socket_path, "provider.set_key", {"provider": "deepseek", "key": "ds-key-abcd"}
    )
    assert "error" not in followup


async def test_delete_key_on_unreadable_vault_returns_provider_error_not_internal(server, tmp_path):
    await _call(
        server.socket_path, "provider.set_key", {"provider": "anthropic", "key": "sk-ant-abcd1234"}
    )
    _corrupt_the_vault(tmp_path)

    response = await _call(server.socket_path, "provider.delete_key", {"provider": "anthropic"})
    assert response["error"]["code"] == PROVIDER_ERROR
    assert "force=true" in response["error"]["message"]

    # The `providers` row is the authoritative, externally-visible state and is cleared before the
    # vault write is even attempted — so has_key is already False despite the RPC reporting error.
    list_response = await _call(server.socket_path, "provider.list")
    row = next(r for r in list_response["result"] if r["provider"] == "anthropic")
    assert row["has_key"] is False


# --- round 2 review: `vault_key_mismatch` must be distinguishable from a merely-corrupt ---------
# --- vault (Issue #23/G19: "不同 key 解密失败必须是显式 vault_key_mismatch 错误") --------------


class _KeyMismatchVault(Vault):
    """Stands in for a real machine-migration scenario: `_read_entries()` raises
    `VaultKeyMismatchError` specifically (not the generic `VaultError` the
    `_corrupt_the_vault` fixture above produces), the way a genuinely wrong data
    key does (see `secrets/vault.py::Vault._read_entries`)."""

    def set(self, name, value):
        raise VaultKeyMismatchError("simulated: wrong data key for this vault file")

    def delete(self, name):
        raise VaultKeyMismatchError("simulated: wrong data key for this vault file")


async def test_write_provider_key_reports_vault_key_mismatch_distinctly(conn):
    with pytest.raises(RpcError) as excinfo:
        await run_in_db_thread(
            _write_provider_key,
            conn,
            _KeyMismatchVault(Path("/unused")),
            "anthropic",
            "sk-ant-doesnotmatter",
        )
    assert excinfo.value.code == PROVIDER_ERROR
    assert excinfo.value.detail["vault_key_mismatch"] is True


async def test_clear_provider_key_reports_vault_key_mismatch_distinctly(conn):
    def _seed(c):
        c.execute(
            "INSERT INTO providers (id, name, has_key, key_hint, default_model, "
            "created_at, updated_at) VALUES ('provider_anthropic', 'anthropic', 1, 'abcd', "
            "'x', '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z')"
        )
        c.commit()

    await run_in_db_thread(_seed, conn)

    with pytest.raises(RpcError) as excinfo:
        await run_in_db_thread(
            _clear_provider_key, conn, _KeyMismatchVault(Path("/unused")), "anthropic"
        )
    assert excinfo.value.code == PROVIDER_ERROR
    assert excinfo.value.detail["vault_key_mismatch"] is True


# --- round 2 review: force=True must not leave a dangling transaction on the shared connection -
# --- when vault.reset() itself fails too (not just the vault.set() that triggered force) -------


class _ResetAlsoFailsVault(Vault):
    """Stands in for the other of the two ways `vault.set()` raises `VaultError` (per
    `secrets/vault.py`): not a merely-unreadable ciphertext (`_read_entries()` failing, which
    `reset()` bypasses and recovers from — covered by the round 1 tests above), but the data key
    itself being unavailable (e.g. the Keychain backend can't be reached at all). Both `set()` and
    `reset()` go through `_key()` -> `_resolve_data_key()`, so both fail the same way — `force`
    cannot save this case, and round 2's bug was that trying anyway left the shared connection
    holding an uncommitted, unrelated-looking destructive UPDATE."""

    def set(self, name, value):
        raise VaultError("simulated: vault data key unavailable")

    def reset(self, entries):
        raise VaultError("simulated: vault data key unavailable")


class _NoOpDeleteVault(Vault):
    """A vault whose `delete()` always succeeds trivially — stands in for `_clear_provider_key`'s
    own vault argument in the "later, unrelated call" half of the round 2 repro below; what that
    call does to the vault is irrelevant, only its `conn.commit()` matters."""

    def delete(self, name):
        return False


async def test_write_provider_key_force_rolls_back_when_reset_also_fails(conn):
    # Seed two providers as already configured, exactly like a real vault holding real keys would
    # leave `providers` before a doomed force-reset attempt on a third.
    def _seed(c):
        now = "2024-01-01T00:00:00Z"
        for vendor in ("anthropic", "deepseek"):
            c.execute(
                "INSERT INTO providers (id, name, has_key, key_hint, default_model, "
                "created_at, updated_at) VALUES (?, ?, 1, 'abcd', 'x', ?, ?)",
                (f"provider_{vendor}", vendor, now, now),
            )
        c.commit()

    await run_in_db_thread(_seed, conn)

    with pytest.raises(RpcError) as excinfo:
        await run_in_db_thread(
            _write_provider_key,
            conn,
            _ResetAlsoFailsVault(Path("/unused")),
            "openai",
            "sk-openai-doesnotmatter",
            force=True,
        )
    assert excinfo.value.code == PROVIDER_ERROR

    def _rows(c):
        return {r["name"]: r["has_key"] for r in c.execute("SELECT name, has_key FROM providers")}

    # The bug itself: the destructive "clear every other provider" UPDATE must not be left
    # sitting in an implicitly-open transaction on this shared, long-lived connection.
    assert (await run_in_db_thread(lambda c: c.in_transaction, conn)) is False
    # Nothing was even partially applied by the failed force attempt.
    assert (await run_in_db_thread(_rows, conn)) == {"anthropic": 1, "deepseek": 1}

    # The bug's actual symptom, reproduced exactly: a later, completely unrelated write on this
    # same shared connection commits — before the fix, this is the call that silently persisted
    # the dangling UPDATE from above and wiped out anthropic's `has_key` too.
    await run_in_db_thread(_clear_provider_key, conn, _NoOpDeleteVault(Path("/unused")), "deepseek")
    assert (await run_in_db_thread(_rows, conn)) == {"anthropic": 1, "deepseek": 0}


async def test_model_list_for_known_provider(server):
    response = await _call(server.socket_path, "model.list", {"provider": "deepseek"})
    assert "error" not in response
    ids = {m["id"] for m in response["result"]}
    assert ids == set(VENDORS["deepseek"].models)


async def test_model_list_unknown_provider_returns_not_found(server):
    response = await _call(server.socket_path, "model.list", {"provider": "nope"})
    assert response["error"]["code"] == NOT_FOUND


async def test_model_list_without_provider_aggregates_catalog(server):
    # Round 1 review: this used to skip patching the Ollama probe (the `>=` assertion hid a real
    # failure, but every run still fired a real HTTP request at localhost:11434 — harmless but
    # adds latency and depends on what's running on the machine). Same fix as
    # test_list_models_none_aggregates_every_vendor in test_providers_resolver.py.
    with patch("jones_daemon.providers.resolver._ollama_live_models", return_value=[]):
        response = await _call(server.socket_path, "model.list")
    assert "error" not in response
    got_providers = {m["provider"] for m in response["result"]}
    assert got_providers >= set(VENDORS) - {"ollama"}  # ollama depends on a live local probe


# --- G03 / N02: the full key must never appear anywhere outside the vault ---------------------


async def test_full_key_never_appears_in_logs_or_rpc_responses(server, log_capture):
    secret = "sk-ant-do-not-leak-0123456789abcdef"

    set_response = await _call(
        server.socket_path, "provider.set_key", {"provider": "anthropic", "key": secret}
    )
    list_response = await _call(server.socket_path, "provider.list")
    delete_response = await _call(
        server.socket_path, "provider.delete_key", {"provider": "anthropic"}
    )

    haystack = "\n".join(
        [
            log_capture.getvalue(),
            json.dumps(set_response),
            json.dumps(list_response),
            json.dumps(delete_response),
        ]
    )
    assert secret not in haystack
    assert base64.b64encode(secret.encode()).decode() not in haystack
    # the hint (last 4 chars) legitimately appears — proves this isn't a vacuous assertion
    assert secret[-4:] in haystack


# --- wiring: provider.*/model.list must actually be reachable in the real daemon --------------


def test_main_actually_calls_providers_methods_register():
    # Regression for a review finding: this module's own `register()` being unit-tested (every
    # test above) says nothing about whether the real daemon entry point ever calls it — before
    # this test, `__main__.py` never did, so `provider.*`/`model.list` returned METHOD_NOT_FOUND
    # on every real `python -m jones_daemon` run despite full test coverage here. Assert the call
    # site exists in `__main__.py`'s source, the same way this repo's own G03/N02 tests grep for
    # what must (not) appear, so removing the wiring line fails CI instead of failing silently at
    # runtime.
    import inspect

    from jones_daemon import __main__ as daemon_main

    source = inspect.getsource(daemon_main)
    assert "providers_methods.register(" in source
