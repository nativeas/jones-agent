"""`sessions/methods.py::_run_delete_honestly` — the `session.delete`/`run.delete`
RPC-layer translation of `store/maintenance.py::PartialDeleteError` into
`daemon.error` + either a success or a structured `RpcError` (Issue #23 round-1
review, 04-w5-interfaces.md §6 "诚实失败"). Exercised directly against the helper
rather than through a full `DaemonContext`/`RpcServer` — everything this
function touches beyond `store/maintenance.py` (already covered by
`test_storage_maintenance.py`) is `server.broadcast_all`, which only needs a
fake with that one method.
"""

from __future__ import annotations

import pytest

from jones_daemon.rpc.errors import INVALID_STATE, RpcError
from jones_daemon.sessions.methods import _run_delete_honestly
from jones_daemon.store.maintenance import PartialDeleteError


class _FakeServer:
    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, dict]] = []

    async def broadcast_all(self, method, params):
        self.broadcasts.append((method, params))


async def test_returns_deleted_true_when_the_underlying_call_simply_succeeds():
    server = _FakeServer()

    def _ok():
        return None

    result = await _run_delete_honestly(server, _ok)

    assert result == {"deleted": True}
    assert server.broadcasts == []


async def test_partial_purge_failure_broadcasts_daemon_error_and_raises_invalid_state():
    server = _FakeServer()
    detail = {"session_id": "s1", "stage": "purge_run", "fully_deleted": False}

    def _partial():
        raise PartialDeleteError("purge failed partway", detail=detail)

    with pytest.raises(RpcError) as exc_info:
        await _run_delete_honestly(server, _partial)

    assert exc_info.value.code == INVALID_STATE
    assert exc_info.value.detail == detail

    assert len(server.broadcasts) == 1
    method, params = server.broadcasts[0]
    assert method == "daemon.error"
    assert params["code"] == "delete_partially_failed"
    assert params["detail"] == detail


async def test_checkpoint_busy_after_full_delete_broadcasts_daemon_error_but_reports_success():
    # The critical distinction round-1 review flagged: rows + payload are
    # already fully gone at this point (`fully_deleted: True`) — reporting this
    # to the RPC caller as a failure would be the *opposite* mistake (a
    # completed delete reported as failed), even though the anomaly still needs
    # to reach `daemon.error` for anyone watching WAL/disk health.
    server = _FakeServer()
    detail = {"session_id": "s1", "stage": "checkpoint", "fully_deleted": True}

    def _checkpoint_busy():
        raise PartialDeleteError("checkpoint still busy", detail=detail)

    result = await _run_delete_honestly(server, _checkpoint_busy)

    assert result == {"deleted": True}
    assert len(server.broadcasts) == 1
    method, params = server.broadcasts[0]
    assert method == "daemon.error"
    assert params["detail"] == detail


async def test_an_unrelated_exception_is_not_caught_here_and_never_broadcasts():
    # Only `PartialDeleteError` gets this treatment — anything else (e.g. the
    # `RpcError`s `delete_session` raises for its own refusal rules) must pass
    # through untouched, the same as before this fix, and must not be
    # misreported as a partial-delete daemon.error.
    server = _FakeServer()

    def _boom():
        raise RpcError(INVALID_STATE, "cannot delete the main session")

    with pytest.raises(RpcError, match="main session"):
        await _run_delete_honestly(server, _boom)

    assert server.broadcasts == []
