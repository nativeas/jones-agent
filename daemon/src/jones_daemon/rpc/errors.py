"""JSON-RPC 2.0 error codes (standard + Jones application codes, design §4.3)."""

from __future__ import annotations

from typing import Any

# Standard JSON-RPC 2.0 codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Jones application codes (docs/design/00-foundation.md §4.3).
NOT_FOUND = 1001
INVALID_STATE = 1002
PERMISSION_DENIED = 1003
PROVIDER_ERROR = 1004
BUDGET_EXCEEDED = 1005
KERNEL_ERROR = 1006
TOO_MANY_REQUESTS = 1007
# Issue #17/#19 daemon 侧 (03-w4-interfaces.md §2, G21; controller ruling R-H3).
# R-H3's own text names these "1007 capability_drift、1008 mcp_server_down" —
# 1007 was already `TOO_MANY_REQUESTS` in this table before this branch
# existed (docs/design/00-foundation.md §4.3, shipped and tested since W2/A's
# #10). Reusing it would silently change what an existing `daemon.error{code:
# 1007}` means to every current consumer — the premise (that 1007 was free)
# was wrong, not the ruling's intent; taking the next two free codes instead
# is the "改前提不打补丁" fix (DEV.md 工程原则 #2), not a disagreement with
# R-H3 — see the PR report's "契约变更" section.
MCP_SERVER_DOWN = 1008
CAPABILITY_DRIFT = 1009

APP_ERROR_CODES = {
    NOT_FOUND: "not_found",
    INVALID_STATE: "invalid_state",
    PERMISSION_DENIED: "permission_denied",
    PROVIDER_ERROR: "provider_error",
    BUDGET_EXCEEDED: "budget_exceeded",
    KERNEL_ERROR: "kernel_error",
    TOO_MANY_REQUESTS: "too_many_requests",
    MCP_SERVER_DOWN: "mcp_server_down",
    CAPABILITY_DRIFT: "capability_drift",
}


class RpcError(Exception):
    """Raised by a method handler to produce a JSON-RPC error response.

    `message` must be human-readable; `detail` carries structured context
    (DEV.md 工程原则 #4: 诚实失败, never silently swallow).
    """

    def __init__(self, code: int, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def to_error_object(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail is not None:
            error["data"] = self.detail
        return error
