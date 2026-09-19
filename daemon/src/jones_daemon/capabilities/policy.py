"""Daemon-side import path for the shared allowlist policy (controller ruling
R-H2). The real logic — `tool_allowed`/`BUILTIN_TOOLS`/`CONDITIONAL_BUILTIN_
TOOLS`/`TOOL_SEARCH_BRIDGE_NAMES` — lives in `kernel/plugin/jones_gate/
_policy.py`, not here: that package is `shutil.copytree`d whole into every
worker's isolated `HERMES_HOME/plugins/jones_gate/` and loaded by Hermes's own
plugin manager INSIDE the worker process (see that module's docstring for
why it must be zero-dependency stdlib), while `kernel/plugin/jones_gate/
__init__.py::_decide` — the actual enforcement gate — needs the exact same
function `capabilities/registry.py` uses to compute the transparency page's
`enabled` field. Putting the one true implementation where it already gets
shipped to the worker, and re-exporting it here for daemon-side callers, is
the same precedent `capabilities/registry.py` already set by importing
`kernel.plugin.jones_gate._rules` directly (the daemon's own venv can import
`jones_gate` as an ordinary package; only the WORKER's copy of it can never
import back out to `jones_daemon`).

This module exists so a daemon-side caller can write `from jones_daemon.
capabilities import policy` / `policy.tool_allowed(...)` instead of reaching
into `kernel.plugin.jones_gate` — a shorter, more discoverable path from
`capabilities/` for something the transparency page's own registry uses on
every `capability.list` call.
"""

from __future__ import annotations

from jones_daemon.kernel.plugin.jones_gate._policy import (
    BUILTIN_TOOLS as BUILTIN_TOOLS,
)
from jones_daemon.kernel.plugin.jones_gate._policy import (
    CONDITIONAL_BUILTIN_TOOLS as CONDITIONAL_BUILTIN_TOOLS,
)
from jones_daemon.kernel.plugin.jones_gate._policy import (
    MCP_PLACEHOLDER_PREFIX as MCP_PLACEHOLDER_PREFIX,
)
from jones_daemon.kernel.plugin.jones_gate._policy import (
    MCP_TOOL_NAME_PREFIX as MCP_TOOL_NAME_PREFIX,
)
from jones_daemon.kernel.plugin.jones_gate._policy import (
    TOOL_SEARCH_BRIDGE_NAMES as TOOL_SEARCH_BRIDGE_NAMES,
)
from jones_daemon.kernel.plugin.jones_gate._policy import (
    mcp_server_for as mcp_server_for,
)
from jones_daemon.kernel.plugin.jones_gate._policy import (
    tool_allowed as tool_allowed,
)
