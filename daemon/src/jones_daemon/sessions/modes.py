"""Session work-mode semantics (PRD 9.1; Issue #11, docs/design/02-w3-interfaces.md
§1.1's "模式" bullet). Single source of truth for the chat/task/auto ordering
used by N13's mode-narrowing rule ("子会话模式不得比父宽").

`MODE_RANK` orders modes from *most* restrictive (chat: no tool calls at all)
to *least* (auto: rule-gate-allowed actions run without a per-action user
gate) — narrower always means "lower rank or equal".

This module is intentionally NOT imported by `sessions/service.py::create()`/
`set_mode()` — those already carry their own inline equivalent of
`is_valid_child_mode` (written before this file existed, under #10/A's
ownership of that file; 02-w3-interfaces.md §0 only grants this branch a few
named functions there, `create`/`set_mode` are not among them). This module
exists so that check has one well-tested, reusable definition instead of only
the inline copy — `tests/test_gates_modes.py` exercises it directly against
every PRD 9.1/N13 mode pair. Follow-up suggestion in the PR report: have
`create`/`set_mode` import this instead of re-deriving the same rule inline.
"""

from __future__ import annotations

MODE_RANK: dict[str, int] = {"chat": 0, "task": 1, "auto": 2}


def is_valid_child_mode(child_mode: str, parent_mode: str) -> bool:
    """Whether `child_mode` is an allowed session mode for a session whose
    parent is `parent_mode` (PRD 9.6: "子会话的工具白名单 ⊆ 父会话...父会话是
    任务模式，子会话不能是自动模式（反向可以收紧）"; N13). Unknown mode strings
    are never valid (fail closed — this function is used to REJECT invalid
    input, so an unrecognized mode should never quietly pass)."""
    if child_mode not in MODE_RANK or parent_mode not in MODE_RANK:
        return False
    return MODE_RANK[child_mode] <= MODE_RANK[parent_mode]
