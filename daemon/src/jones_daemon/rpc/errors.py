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

APP_ERROR_CODES = {
    NOT_FOUND: "not_found",
    INVALID_STATE: "invalid_state",
    PERMISSION_DENIED: "permission_denied",
    PROVIDER_ERROR: "provider_error",
    BUDGET_EXCEEDED: "budget_exceeded",
    KERNEL_ERROR: "kernel_error",
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
