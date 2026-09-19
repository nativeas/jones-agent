"""`cron.list` / `cron.upsert` / `cron.delete` / `cron.run_now` RPC wiring
(Issue #20, 00-foundation.md §4.1, docs/design/04-w5-interfaces.md §2).

Thin — `scheduler/methods.py` is a params-in/service-call/result-out translation
layer; the actual behavior is already covered by `test_scheduler_service.py`. These
tests only check the wiring itself: params are validated and passed through, and
`register()` returns the `CronService` `__main__.py` needs for its lifecycle hooks.
"""

from __future__ import annotations

from typing import Any

import pytest

from jones_daemon.context import DaemonContext, NullConfigResolver
from jones_daemon.projects.bootstrap import bootstrap_projects_and_agents
from jones_daemon.rpc.errors import INVALID_PARAMS, RpcError
from jones_daemon.rpc.server import RpcServer
from jones_daemon.scheduler import methods as scheduler_methods
from jones_daemon.scheduler.service import CronService
from jones_daemon.sessions.service import DEFAULT_AGENT_ID, DEFAULT_PROJECT_ID
from jones_daemon.store import apply_pending, connect, run_in_db_thread


class _StubProviders:
    def resolve(self, model_pref: dict[str, Any] | None) -> Any:
        return {"provider": "anthropic", "model": "x", "env": {}, "hermes_config": {}}

    def list_models(self, provider: str | None) -> list[dict[str, Any]]:
        return []


class _StubSessionService:
    """`scheduler/methods.py` never calls the session service directly (only
    `CronService` does), so this only needs to exist — `cron.upsert`/`list`/
    `delete` never dispatch anything."""


async def _make_server_and_service(tmp_path, monkeypatch) -> tuple[RpcServer, CronService]:
    monkeypatch.setenv("JONES_HOME", str(tmp_path))

    def _open() -> Any:
        conn = connect(tmp_path / "jones.db")
        apply_pending(conn)
        bootstrap_projects_and_agents(conn)
        return conn

    conn = await run_in_db_thread(_open)
    from jones_daemon import paths

    server = RpcServer(tmp_path / "daemon.sock")
    ctx = DaemonContext(
        db=conn, paths=paths, server=server,
        providers=_StubProviders(), config=NullConfigResolver(),
    )
    service = scheduler_methods.register(server, ctx, _StubSessionService())
    return server, service


def _handler(server: RpcServer, method: str):
    return server._methods[method]


async def test_register_returns_the_cron_service(tmp_path, monkeypatch):
    _server, service = await _make_server_and_service(tmp_path, monkeypatch)
    assert isinstance(service, CronService)


async def test_cron_upsert_then_list_round_trip(tmp_path, monkeypatch):
    server, _service = await _make_server_and_service(tmp_path, monkeypatch)
    upsert = _handler(server, "cron.upsert")
    created = await upsert(
        {
            "project_id": DEFAULT_PROJECT_ID, "agent_id": DEFAULT_AGENT_ID, "name": "nightly",
            "expr": "0 3 * * *", "prompt": "clean up", "mode": "auto",
        },
        conn=None,
    )
    assert created["name"] == "nightly"
    assert created["expr"] == "0 3 * * *"

    listed = await _handler(server, "cron.list")({}, conn=None)
    assert [c["id"] for c in listed] == [created["id"]]


async def test_cron_upsert_missing_required_field_is_invalid_params(tmp_path, monkeypatch):
    server, _service = await _make_server_and_service(tmp_path, monkeypatch)
    upsert = _handler(server, "cron.upsert")
    with pytest.raises(RpcError) as excinfo:
        await upsert({"project_id": DEFAULT_PROJECT_ID, "agent_id": DEFAULT_AGENT_ID}, conn=None)
    assert excinfo.value.code == INVALID_PARAMS


async def test_cron_upsert_non_bool_enabled_is_invalid_params(tmp_path, monkeypatch):
    server, _service = await _make_server_and_service(tmp_path, monkeypatch)
    upsert = _handler(server, "cron.upsert")
    with pytest.raises(RpcError) as excinfo:
        await upsert(
            {
                "project_id": DEFAULT_PROJECT_ID, "agent_id": DEFAULT_AGENT_ID, "name": "x",
                "expr": "* * * * *", "prompt": "x", "enabled": "yes",
            },
            conn=None,
        )
    assert excinfo.value.code == INVALID_PARAMS


async def test_cron_delete_then_list_is_empty(tmp_path, monkeypatch):
    server, _service = await _make_server_and_service(tmp_path, monkeypatch)
    created = await _handler(server, "cron.upsert")(
        {
            "project_id": DEFAULT_PROJECT_ID, "agent_id": DEFAULT_AGENT_ID, "name": "x",
            "expr": "* * * * *", "prompt": "x",
        },
        conn=None,
    )
    deleted = await _handler(server, "cron.delete")({"id": created["id"]}, conn=None)
    assert deleted["id"] == created["id"]
    listed = await _handler(server, "cron.list")({}, conn=None)
    assert listed == []


async def test_cron_delete_missing_id_is_invalid_params(tmp_path, monkeypatch):
    server, _service = await _make_server_and_service(tmp_path, monkeypatch)
    with pytest.raises(RpcError) as excinfo:
        await _handler(server, "cron.delete")({}, conn=None)
    assert excinfo.value.code == INVALID_PARAMS


async def test_cron_run_now_unknown_id_raises_not_found(tmp_path, monkeypatch):
    server, _service = await _make_server_and_service(tmp_path, monkeypatch)
    with pytest.raises(RpcError):
        await _handler(server, "cron.run_now")({"id": "nope"}, conn=None)
