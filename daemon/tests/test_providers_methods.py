import asyncio
import base64
import io
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from jones_daemon.logging import JsonLinesFormatter
from jones_daemon.providers.catalog import VENDORS
from jones_daemon.providers.methods import register
from jones_daemon.rpc.errors import INVALID_PARAMS, NOT_FOUND
from jones_daemon.rpc.server import RpcServer
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


async def test_model_list_for_known_provider(server):
    response = await _call(server.socket_path, "model.list", {"provider": "deepseek"})
    assert "error" not in response
    ids = {m["id"] for m in response["result"]}
    assert ids == set(VENDORS["deepseek"].models)


async def test_model_list_unknown_provider_returns_not_found(server):
    response = await _call(server.socket_path, "model.list", {"provider": "nope"})
    assert response["error"]["code"] == NOT_FOUND


async def test_model_list_without_provider_aggregates_catalog(server):
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
