"""Shared runtime context handed to every daemon module (docs/design/01-w2-interfaces.md §1).

Created by A (sessions/workers) because A is the first module that needs it; other
modules only import `DaemonContext`, they don't change its shape without updating
that doc first. `providers` and `config` are typed as `Protocol`s here (not concrete
classes) so B (#7) and C (#8/#9) can each land their own implementation without A
depending on their modules — until they do, `__main__.py` wires in the `Null*`
resolvers below, which return honest defaults / explicit `not_configured` errors
rather than fabricating behavior that doesn't exist yet (DEV.md 工程原则 #4).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Protocol, TypedDict

from jones_daemon.rpc.errors import PROVIDER_ERROR, RpcError
from jones_daemon.rpc.server import RpcServer


class ProviderBinding(TypedDict):
    """docs/design/01-w2-interfaces.md §3 — what B's resolver hands back for a worker."""

    provider: str
    model: str
    env: dict[str, str]
    hermes_config: dict[str, Any]


class ProviderResolver(Protocol):
    def resolve(self, model_pref: dict[str, Any] | None) -> ProviderBinding: ...

    def list_models(self, provider: str | None) -> list[dict[str, Any]]: ...


# `Permissions` isn't specified further than "what config.permissions() returns" in
# §4 — C owns its real shape (permissions.json merge rules, PRD 10.1). A only needs
# something it can pass through untouched, so a plain dict alias is honest: A makes
# no claims about its structure.
Permissions = dict[str, Any]


class ConfigResolver(Protocol):
    def settings(self, project_id: str | None) -> dict[str, Any]: ...

    def permissions(self, project_id: str | None) -> Permissions: ...

    def mcp_servers(self, project_id: str | None) -> list[dict[str, Any]]: ...


class NullProviderResolver:
    """Stand-in until B (#7) lands `providers/resolver.py`. Never fabricates a key or
    a model choice — every call fails closed with `provider_error/not_configured`."""

    def resolve(self, model_pref: dict[str, Any] | None) -> ProviderBinding:
        raise RpcError(
            PROVIDER_ERROR,
            "no provider configured (B/#7 not landed yet)",
            {"reason": "not_configured", "model_pref": model_pref},
        )

    def list_models(self, provider: str | None) -> list[dict[str, Any]]:
        return []


class NullConfigResolver:
    """Stand-in until C (#8/#9) lands `config/resolver.py`. Returns honest empty
    defaults, not invented settings."""

    def settings(self, project_id: str | None) -> dict[str, Any]:
        return {}

    def permissions(self, project_id: str | None) -> Permissions:
        return {}

    def mcp_servers(self, project_id: str | None) -> list[dict[str, Any]]:
        return []


@dataclass
class DaemonContext:
    # `db`/`paths` are typed against what `store/db.py` and `paths.py` actually are
    # today (a single-thread-owned `sqlite3.Connection`, a module of path
    # accessors) rather than the `Database`/`Paths` class names in
    # 01-w2-interfaces.md §1's pseudocode — those two files are shared/not A's to
    # redesign around a wrapper class that doesn't exist. Every DB call made
    # through `db` must go via `store.run_in_db_thread` (see store/db.py's module
    # docstring for why), never inline on the event loop thread.
    db: sqlite3.Connection
    paths: ModuleType
    server: RpcServer
    providers: ProviderResolver
    config: ConfigResolver
