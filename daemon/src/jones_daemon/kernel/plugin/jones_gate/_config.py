"""`<HERMES_HOME>/jones_gate.json` loader — mtime-cached, stdlib only (see
`__init__.py`'s module docstring for why this package has zero `jones_daemon`
import).

02-w3-interfaces.md §1.1: "插件每次 pre_tool_call 读一次 jones_gate.json（几
KB，mtime 缓存），零协议扩展；模式切换即时生效". The daemon (see
`permissions/gate_config.py`, which owns the writer half of this contract)
rewrites this file whenever a Turn is about to start running, so re-reading
it on every `pre_tool_call` (guarded by an mtime check so an unchanged file
costs one `stat()`, not a re-parse) is what makes a mode switch or a
`remember` visible to a worker that's already running.

Shape (written by `permissions/gate_config.py::build`, kept in sync by hand —
see that module's docstring):
    {
      "mode": "chat" | "task" | "auto",
      "user_root": "<abs path to ~/.jones>",
      "project_permissions_path": "<abs path>" | null,
      "rules": [{"match": str, "action": "allow" | "deny"}, ...],
      "rules_degraded": bool,  # true: permissions.json existed but failed to
                                # parse somewhere -> `rules` may be silently
                                # missing a deny; an "allow" match must NOT
                                # be trusted for direct passthrough while
                                # this is true (see __init__.py)
      "tool_allowlist": [str, ...]   # empty = unrestricted
    }
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

FAIL_CLOSED = object()  # sentinel: config missing/unreadable/malformed -> caller must fail closed

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}  # path -> (mtime, parsed)


def _gate_config_path() -> Path | None:
    home = os.environ.get("HERMES_HOME")
    if not home:
        return None
    return Path(home) / "jones_gate.json"


def load() -> dict[str, Any] | object:
    """Return the parsed config, `FAIL_CLOSED` if it's missing/unreadable/not
    a JSON object, or a cached parse when the file's mtime hasn't changed
    since the last read. Never raises — every failure mode is a value the
    caller fails closed on (DEV.md 工程原则 #4)."""
    path = _gate_config_path()
    if path is None:
        return FAIL_CLOSED
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return FAIL_CLOSED
    key = str(path)
    with _lock:
        cached = _cache.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return FAIL_CLOSED
    if not isinstance(parsed, dict):
        return FAIL_CLOSED
    with _lock:
        _cache[key] = (mtime, parsed)
    return parsed
