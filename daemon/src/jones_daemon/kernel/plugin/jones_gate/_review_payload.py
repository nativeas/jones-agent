"""Encodes the data the daemon's review gate needs into the one string Hermes
actually forwards to the daemon for a plugin-escalated approval.

Why this exists (Issue #11 / docs/spikes/01-hermes-hook.md "审计写入时序"):
`tools/approval.py::request_tool_approval()` — what our plugin's `"approve"`
verdict hands off to — only ever forwards a synthetic `command` (`"<tool_name>
(plugin approval rule)"`) and our own free-text `message`/`description` to
the ACP `session/request_permission` the daemon receives (verified against
the installed `hermes-agent` checkout: `acp_adapter/permissions.py::
_build_permission_tool_call` sets `raw_input={"command": ..., "description":
...}` — nothing about the real tool args survives). The daemon-side review
gate (`permissions/review.py::classify()`) needs the real tool name + args to
do anything beyond a name-only guess (e.g. "does this terminal command
network-exfiltrate"), and this plugin — running inside the worker, receiving
`pre_tool_call(tool_name, args, ...)` directly from Hermes — is the only
place that ever has them. So: serialize what `classify()` needs into the
`message` we pass to Hermes; the daemon's `_on_request_permission` decodes it
back out of `toolCall.rawInput.description` (see that function's docstring
for the other raw_input shape it also has to recognize — the ACP edit-
approval path for `write_file`/`patch`, which carries real structured args on
its own and never goes through this encoding at all, see `__init__.py`).

Self-contained (stdlib only) — see `__init__.py`'s module docstring.
"""

from __future__ import annotations

import json
from typing import Any

MARKER = "JONES_REVIEW_V1:"
# Bounds what rides inside a human-readable approval message/log line/ACP
# wire payload — generous for a shell command or a file path, not a blank
# check for a multi-megabyte tool argument (e.g. `write_file`'s `content`,
# though that tool doesn't route through this encoding at all — see module
# docstring — this bound exists for whatever tool DOES carry a large arg).
_MAX_ARGS_JSON_LEN = 4000


def encode(tool_name: str, args: dict[str, Any], *, mode: str) -> str:
    args_json = json.dumps(args, ensure_ascii=False, default=str)
    truncated = len(args_json) > _MAX_ARGS_JSON_LEN
    if truncated:
        args_json = args_json[:_MAX_ARGS_JSON_LEN]
    payload = {"tool": tool_name, "args_json": args_json, "args_truncated": truncated, "mode": mode}
    return MARKER + json.dumps(payload, ensure_ascii=False)


def decode(text: str) -> dict[str, Any] | None:
    """Inverse of `encode`; `None` if `text` doesn't carry this marker or
    isn't well-formed — callers (daemon-side) must treat that as "cannot
    classify" and fail closed to the user gate, never as "low risk"."""
    if not isinstance(text, str) or not text.startswith(MARKER):
        return None
    try:
        payload = json.loads(text[len(MARKER) :])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or "tool" not in payload:
        return None
    args: dict[str, Any] = {}
    if not payload.get("args_truncated"):
        try:
            decoded_args = json.loads(payload.get("args_json", "{}"))
            if isinstance(decoded_args, dict):
                args = decoded_args
        except json.JSONDecodeError:
            pass
    return {"tool": payload["tool"], "args": args, "mode": payload.get("mode"),
            "args_truncated": bool(payload.get("args_truncated"))}
