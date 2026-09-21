"""SessionService — Session/Turn/queue orchestration + the ACP event -> DB/broadcast
translation (docs/design/01-w2-interfaces.md §2, 00-foundation.md §4/§7/§8).

One instance is constructed in `__main__.py` and shared by every `sessions/methods.py`
RPC handler. It owns:
  - the Session/Turn/Message/Run/Step/queue state machine (PRD 9.2, 9.3),
  - translating `AcpClient` callbacks (`session/update`, `session/request_permission`)
    into DB writes + `RpcServer.broadcast` notifications — this is the "daemon 侧写"
    half of 00-foundation.md §7's "审计写入时序": the `jones_gate` plugin's `approve`
    branch never writes anything itself, it only ever returns immediately (see that
    file's module docstring) and the actual audit trail is written here, driven by
    ACP events the worker later reports back.

Known W2 scope gaps (see the PR report): per-mode tool suppression (chat mode should
refuse to run tools at all — PRD 9.1/N12) and Goal/budget-triggered termination (PRD
9.3's third termination kind — Goals are FR17, not #10) are not implemented here;
nothing in this file pretends otherwise.

W3/#12 (docs/design/02-w3-interfaces.md §2) closed two adjacent pieces of the W2
gap list above, scoped narrowly to what FR06 回放 actually needs:
  - `_handle_tool_call_update` now also writes a Step's full `rawOutput` to
    `replay/store.py` (`payload_ref`) — `result_summary` stays a truncated text
    summary for quick UI rendering, the payload file is the full-fidelity replay
    source.
  - `_run_turn` now calls `ctx.providers.resolve()` as a fail-fast pre-flight
    check *before* asking `WorkerManager` to spawn a worker — a Session whose
    Agent has no usable provider/Key terminates immediately with a
    `provider_error`-flavored `run.terminated{kind:"error"}` card instead of
    either silently trying to launch a worker that can't do anything useful, or
    (worse, on a machine without `hermes-agent`'s `worker` extra installed —
    every CI runner and most dev machines, see docs/DEV.md) failing with an
    opaque "worker startup failed" card that looks identical whether the real
    problem is "no API key" or "hermes-agent isn't installed". This does **not**
    wire the resolved `ProviderBinding` (env vars, `hermes_config`) into the
    worker's actual subprocess env/`config.yaml` — that plumbing lives in
    `WorkerManager.ensure_started`/`_worker_env`/`_prepare_hermes_home`, which
    02-w3-interfaces.md §0 assigns to F, not G; a worker started after this
    check passes still launches with no `model:`/`providers:` block, exactly as
    before. See the PR report's "契约变更" section.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from jones_daemon.config.jsonfile import read_json, write_json
from jones_daemon.context import DaemonContext
from jones_daemon.errors import classify
from jones_daemon.kernel.acp_client import AcpError, AcpProtocolError
from jones_daemon.kernel.ids import new_ulid
from jones_daemon.kernel.plugin.jones_gate import (
    TERMINAL_LIKE_TOOLS,
    _hard_deny,
    _review_payload,
    _rules,
    _transparency,
)
from jones_daemon.logging import get_logger
from jones_daemon.permissions import gate_config, review
from jones_daemon.projects.service import ProjectService
from jones_daemon.providers.resolver import ProviderNotConfiguredError
from jones_daemon.replay import retention as replay_retention
from jones_daemon.replay import store as replay_store
from jones_daemon.rpc.errors import (
    INVALID_PARAMS,
    INVALID_STATE,
    MCP_SERVER_DOWN,
    NOT_FOUND,
    RpcError,
)
from jones_daemon.sessions import queries
from jones_daemon.store import run_in_db_thread
from jones_daemon.workers.manager import WorkerManager, WorkerStartupError

logger = get_logger("sessions")

_VALID_MODES = {"chat", "task", "auto"}

# Fixed by 002_seed_defaults_and_queue_turn.sql — see that file's header for why
# these ids (not their row contents) are the stable contract surface until C
# (#8/#9) lands real Project/Agent.
DEFAULT_PROJECT_ID = "proj_default"
DEFAULT_AGENT_ID = "agent_default"

# Review finding #5 (2026-09-19): `acp_adapter/edit_approval.py::
# make_acp_edit_approval_requester`'s own default `timeout` (installed
# `hermes-agent` checkout), never overridden by `acp_adapter/server.py`'s
# call site — see `_on_request_permission`'s docstring for the full story.
_EDIT_APPROVAL_HERMES_TIMEOUT_SECONDS = 60.0

# Issue #22 / N07 (04-w5-interfaces.md §4): "运行中 Run 超过 5s 没有任何 ACP update
# 且 worker 进程已退出 -> 立即 worker_crash 终止". `_on_worker_crash`'s watchdog
# polls at `_WORKER_CRASH_POLL_INTERVAL_S` for up to `_WORKER_CRASH_GRACE_S`
# before force-terminating — module-level (not a constructor param) so tests can
# `monkeypatch.setattr(service, "_WORKER_CRASH_GRACE_S", ...)` for speed without
# widening `SessionService.__init__`'s signature.
_WORKER_CRASH_GRACE_S = 5.0
_WORKER_CRASH_POLL_INTERVAL_S = 0.1
# R-N8 (controller ruling, round-6, 2026-09-20; PRD 11.2 单个 Run 最大时长):
# how often `_run_budget_watchdog_loop` scans `_active_turns` for a Turn past
# its `max_run_duration_s` — not from `settings.json` (that config's job is
# "how long is too long", not "how often to check"); a Run running well past
# its 2-hour default budget being caught within 15s of tripping it is more
# than timely enough, and cheap: an O(active Turns) in-memory scan, no DB
# round trip unless one has actually exceeded its budget.
_BUDGET_WATCHDOG_INTERVAL_S = 15.0


def _extract_text(content: Any) -> str:
    if isinstance(content, dict):
        return content.get("text") or ""
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def _select_permission_option(
    options: list[dict[str, Any]], decision: str, remember: str | None
) -> str:
    wanted_kind = {
        ("allow", False): "allow_once",
        ("allow", True): "allow_always",
        ("deny", False): "reject_once",
        ("deny", True): "reject_always",
    }[(decision, bool(remember))]
    for opt in options:
        if opt.get("kind") == wanted_kind:
            return opt["optionId"]
    prefix = "allow_" if decision == "allow" else "reject_"
    for opt in options:
        if str(opt.get("kind", "")).startswith(prefix):
            return opt["optionId"]
    raise RpcError(
        INVALID_STATE,
        f"worker did not offer a {decision!r} permission option",
        {"options": options},
    )


def _extract_tool_call(params: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
    """Recover the real tool name + args (+ a mode hint, see below) from an
    ACP `session/request_permission` payload — Issue #11's review gate needs
    the first two, but they can arrive in either of TWO independent shapes
    (00-foundation.md §7's "两套审批逻辑打架" question; resolved on the plugin
    side in `kernel/plugin/jones_gate/__init__.py`'s module docstring — "the
    write_file/patch special case"):

    1. `acp_adapter/edit_approval.py`'s dedicated `write_file`/`patch` path:
       `rawInput = {"tool": <name>, "arguments": {...}}` — real structured
       args, no decoding needed. No mode hint travels this path (see below).
    2. `jones_gate`'s own generic escalation (every other tool, via
       `tools/approval.py::request_tool_approval`): `rawInput = {"command":
       "<tool_name> (plugin approval rule)", "description": <our encoded
       message>}` — verified against the installed `hermes-agent` checkout
       (`acp_adapter/permissions.py::_build_permission_tool_call`) that no
       real args survive this path on Hermes's side; `description` carries
       whatever `kernel/plugin/jones_gate/_review_payload.py::encode()`
       packed into it worker-side (the ONLY place that ever has the real
       args for tools going through this path).

    Returns `("", {}, None)` for anything that doesn't match either shape (a
    future Hermes protocol change, or a malformed/truncated payload) — an
    empty tool name can never match a `_READ_ONLY_LOW`/name-based rule in
    `permissions/review.py::classify()`, so the caller correctly falls back
    to a non-`low` risk rather than silently guessing "safe" (DEV.md 工程
    原则 #4: 诚实失败).

    The third element (review finding #13, 2026-09-19) is the session mode
    the RULE gate saw when it decided to escalate this call — shape 2's
    `_review_payload.encode()` already packs `mode` in alongside `tool`/
    `args` (it's the same `jones_gate.json` snapshot `kernel/plugin/
    jones_gate/__init__.py::_decide` read moments earlier); `None` when
    unavailable (shape 1, or a malformed/legacy payload) — `_on_request_
    permission`'s caller falls back to its OWN Turn-start snapshot in that
    case rather than re-reading the session's live (possibly since-changed)
    mode from the database, see that function's docstring for why.
    """
    raw_input = (params.get("toolCall") or {}).get("rawInput")
    if not isinstance(raw_input, dict):
        return "", {}, None
    if "tool" in raw_input and "arguments" in raw_input:
        tool = raw_input.get("tool")
        args = raw_input.get("arguments")
        return (
            tool if isinstance(tool, str) else "",
            args if isinstance(args, dict) else {},
            None,
        )
    description = raw_input.get("description")
    decoded = _review_payload.decode(description) if isinstance(description, str) else None
    if decoded is not None:
        mode = decoded.get("mode")
        return decoded["tool"], decoded["args"], mode if isinstance(mode, str) else None
    return "", {}, None


# Round 1 fix (2026-09-19, review finding #7): `newText`/`oldText` here are
# not a unified diff, they're the WHOLE file's contents (Hermes's own
# `write_file`/`patch` ACP adapter passes the full post-write text, not a
# diff — see `_extract_diff_content`'s docstring). Left unbounded, one large
# file write turns `_handle_tool_call_update`'s `json.dumps(to_dump, ...)`
# (line ~1232, runs SYNCHRONOUSLY on the ACP read loop thread — CPython's C
# json encoder holds the GIL for the entire call) into a multi-MB blocking
# call, exactly the cost 02-w3-interfaces.md §3's "回放 payload 写入异步、不
# 阻塞 ACP 读循环" mandate exists to avoid, and grows both `steps.
# result_summary` and the on-disk replay payload (`replay/store.py::
# write_payload`, itself uncapped) without limit — including the full
# plaintext of any `.env`/credentials file written this way, where before
# this branch only a short `rawOutput` summary ever reached either place.
# Capped at the same order of magnitude `result_summary`'s own truncation
# already uses a few lines below (4000 chars) — `"truncated": True` recorded
# alongside so replay/UI can say so rather than silently showing a partial
# file as if it were the whole thing.
_DIFF_TEXT_MAX_CHARS = 4000


def _truncate_diff_text(text: str) -> tuple[str, bool]:
    if len(text) <= _DIFF_TEXT_MAX_CHARS:
        return text, False
    return text[:_DIFF_TEXT_MAX_CHARS], True


def _extract_diff_content(content: Any) -> dict[str, Any] | None:
    """Find an ACP diff-kind `ToolCallContent` block (`{"type": "diff",
    "path": ..., "newText": ..., "oldText": ...}` — the wire shape of the
    installed `agent-client-protocol==0.9.0` package's `FileEditToolCallContent`/
    `Diff` schema, `newText`/`oldText` aliased from `new_text`/`old_text`;
    verified against `venv/lib/python3.11/site-packages/acp/schema.py`) in a
    `content` value — either the array ACP's `tool_call`/`tool_call_update`
    events carry, or the single dict some ACP shapes use — and return it as
    `{"path", "old_text", "new_text"}` (this codebase's own snake_case
    convention), or `None` if no such block is present.

    Issue #13 (FR07's "写入前后 diff 在 Step 中可见"): source-verified against
    the installed `hermes-agent` checkout that Hermes DOES send this shape
    for `write_file`/`patch` over ACP — `acp_adapter/tools.py::
    build_tool_start` (when the edit auto-approves, `acp_adapter/events.py`'s
    `make_tool_progress_cb`) and `acp_adapter/edit_approval.py::
    build_acp_edit_tool_call` (the `session/request_permission` request
    itself, when the edit needs a human decision) both call `acp.tool_diff_
    content(path=..., old_text=..., new_text=...)`. The fallback this
    contract's original wording anticipated ("不带 → 在 jones_gate 的
    post_tool_call 里算 diff 写到 payload") is therefore not needed in
    practice — see the PR report's "契约变更" section for the full
    evidence trail and why the actual hook points are `_handle_tool_call_
    start`/`_on_request_permission`, not `_handle_tool_call_update` alone as
    03-w4-interfaces.md §3 originally assumed."""
    blocks: list[Any]
    if isinstance(content, list):
        blocks = content
    elif isinstance(content, dict):
        blocks = [content]
    else:
        return None
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "diff":
            continue
        path = block.get("path")
        if not isinstance(path, str) or not path:
            continue
        new_text_raw = block.get("newText")
        new_text, new_truncated = _truncate_diff_text(
            new_text_raw if isinstance(new_text_raw, str) else ""
        )
        old_text_raw = block.get("oldText")
        old_truncated = False
        old_text: str | None
        if isinstance(old_text_raw, str):
            old_text, old_truncated = _truncate_diff_text(old_text_raw)
        else:
            old_text = None
        result: dict[str, Any] = {"path": path, "old_text": old_text, "new_text": new_text}
        if new_truncated or old_truncated:
            result["truncated"] = True
        return result
    return None


def _last_step_for_run(conn: Any, run_id: str) -> dict[str, Any] | None:
    """The most recently started Step for a Run — used only by `_terminate_run`'s
    classification (Issue #22): distinguishes "the model/API layer itself failed"
    from "a specific tool call failed and that's what ended the Turn" within the
    generic `"ACP prompt failed: ..."` reason bucket (see `errors/classify.py`'s
    module docstring). Raw SQL rather than a new `sessions/queries.py` function —
    04-w5-interfaces.md §1 doesn't grant this branch write access to that file,
    and this is read-only, single-statement, and used from exactly one place;
    `queries._d()` (module-private, same row->dict decoding every other query in
    that file uses) is reused rather than duplicated."""
    row = conn.execute(
        "SELECT * FROM steps WHERE run_id = ? ORDER BY seq DESC LIMIT 1", (run_id,)
    ).fetchone()
    return queries._d(row)  # noqa: SLF001 - see docstring


def _fetch_turn(conn: Any, turn_id: str) -> dict[str, Any] | None:
    """Read-only turn lookup by id — `sessions/queries.py` has no such function
    (only `latest_turn(session_id)`); needed by `retry()` (Issue #22) to load the
    turn being retried/abandoned. Same "raw SQL, not a queries.py addition"
    reasoning as `_last_step_for_run` above."""
    row = conn.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
    return queries._d(row)  # noqa: SLF001 - see docstring


def _fetch_turn_user_text(conn: Any, turn: dict[str, Any]) -> str | None:
    """The original user message text for `turn` (`retry()`'s "用户消息复用") —
    `turns.user_message_id` -> `messages.content_json`. `None` for a turn with no
    user message row (shouldn't happen for anything `create_turn_and_user_message`
    produced, i.e. every real Turn — DEV.md 工程原则 #4: `retry()` raises rather
    than guess if this ever comes back empty)."""
    message_id = turn.get("user_message_id")
    if not message_id:
        return None
    row = conn.execute(
        "SELECT content_json FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    if row is None:
        return None
    try:
        content = json.loads(row["content_json"])
    except json.JSONDecodeError:
        return None
    text = content.get("text") if isinstance(content, dict) else None
    return text if isinstance(text, str) else None


@dataclass(frozen=True)
class _TerminationIntent:
    """R-N20 (controller ruling, round-9, 2026-09-20): what an active
    termination path (`stop()`/`_terminate_run_for_exceeded_budget`/
    `_on_worker_crash`) pins onto `ctx_turn.termination_intent` BEFORE it
    sends its ACP cancel notification — the FULL description `_terminate_run`
    needs to record, not just the outer `kind`. Round-8's R-N16 narrowed this
    field to a bare `str` (see `_TurnContext.termination_intent`'s own
    comment) and made `_terminate_run` give it priority over its caller's own
    `kind` argument — that closed round-6's original "wrong kind" bug, but
    left `reason`/`card`/`step_seq` themselves still coming from whichever
    call actually reaches `_terminate_run`'s write first. When that's
    `_finalize_turn_success`'s own generic `kind="user", reason="stopped by
    user"` call racing ahead of the claiming path's own (separate-task)
    `_terminate_run` call, the persisted `kind` came out right (R-N16) but
    the `reason`/`card` a user actually sees were still the hard-coded "用户
    终止" ones — no budget numbers, wrong message — same bug, one field over.
    Carrying the whole record here closes that: `_terminate_run` now adopts
    every field of a pinned intent wholesale, never mixing it with its
    caller's own arguments."""

    kind: str
    reason: str
    card: classify.ErrorCard
    step_seq: int | None


@dataclass
class _TurnContext:
    turn_id: str
    run_id: str
    session_id: str
    assistant_message_id: str | None = None
    assistant_text: str = ""
    thinking_message_id: str | None = None
    thinking_text: str = ""
    tool_call_steps: dict[str, str] = field(default_factory=dict)
    step_seq: int = 0
    step_started_at: dict[str, float] = field(default_factory=dict)
    # step_id -> its own `seq` — `_write_step_payload` (FR06 回放, 02-w3-interfaces.md
    # §2) needs the seq *that step* was assigned at start time, not `step_seq`'s
    # current value (which may have advanced past it by the time a `tool_call_update`
    # for an earlier, still-in-flight call arrives).
    step_seq_by_id: dict[str, int] = field(default_factory=dict)
    # step_id -> {"path","old_text","new_text"} — Issue #13's "写入 diff 在
    # Step 中可见" (FR07). Populated by `_handle_tool_call_start` (an ACP
    # diff-kind `ToolCallContent` on the `tool_call` "started" event, present
    # only when Hermes auto-approved the edit — `acp_adapter/tools.py`'s
    # `build_tool_start`) and by `_on_request_permission` (the SAME diff
    # content, present instead on the `session/request_permission` request
    # itself, when the edit needs a human decision —
    # `acp_adapter/edit_approval.py::build_acp_edit_tool_call` — verified
    # against the installed `hermes-agent` checkout: neither path ever runs
    # for the same call, see `_extract_diff_content`'s docstring). Consumed
    # (popped) by `_handle_tool_call_update` when that step's completion
    # event arrives — kept off the DB until then because `write_file`/
    # `patch`'s own `tool_call_update` never repeats the diff content
    # (`acp_adapter/tools.py::_build_tool_complete_content` has no
    # write_file/patch special case), so this is the only place the
    # in-flight daemon process ever has it.
    step_diffs: dict[str, dict[str, Any]] = field(default_factory=dict)
    # R-N5 (controller ruling, 2026-09-20; PRD 11.2/9.3's "触顶 11.2 的单 Run
    # Step 数/时长上限" -> 预算终止): `started_at`/`max_steps_per_run`/
    # `max_run_duration_s` all start at a placeholder `_run_turn` overwrites
    # immediately -- right as it constructs this dataclass, satisfying "`_run_
    # turn` 记录开始时刻" (see that method's own comment). Deliberately NOT a
    # `field(default_factory=time.monotonic)` for `started_at`: a default
    # factory is resolved to a plain function reference once, at this class's
    # own module-import moment -- a test wanting a fake clock would silently
    # keep hitting whatever real callable got captured at import time, never
    # a later substitution. `_run_turn`'s own `ctx_turn.started_at = self.
    # _clock()` line reads `SessionService._clock` fresh at call time instead
    # -- R-N8 (round-6) replaced this field's original rationale here (a
    # `monkeypatch.setattr(service_module.time, "monotonic", ...)` a
    # duration-cap test used to need) with that constructor-injected callable
    # instead, once the duration check itself moved off `_handle_tool_call_
    # start` onto a resident poll (`_check_run_durations`) that needed its own
    # deterministic-clock test story — see `SessionService.__init__`'s own
    # comment on `self._clock` for why. `max_steps_per_run`/`max_run_duration_s` mirror
    # the module-level `_DEFAULT_MAX_STEPS_PER_RUN`/`_DEFAULT_MAX_RUN_
    # DURATION_S` constants right below this class for a related reason: a
    # tool_call event that somehow raced ahead of `_run_turn`'s settings read
    # (not reachable in practice -- no `await` sits between `_TurnContext(...)`
    # and `worker_manager.ensure_started()` first spawning the worker that
    # could emit one) would still enforce the real PRD limits, not an
    # accidental 0.
    # `None` (not `0.0`): `time.monotonic()` is an arbitrary-epoch clock (often
    # since boot), so a numeric placeholder would make every `_TurnContext`
    # built OUTSIDE `_run_turn` — several existing unit tests construct one
    # directly to call `_handle_tool_call_start`/`_handle_tool_call_update` in
    # isolation, see e.g. `test_sessions_service.py`'s tool_call tests — read
    # back as "started an enormous, already-over-budget number of seconds
    # ago" the instant `_handle_tool_call_start` ran its new 时长上限 check.
    # `None` makes that check an explicit, honest no-op for a Turn that never
    # went through `_run_turn`'s own `ctx_turn.started_at = time.monotonic()`
    # line, instead of a silent false positive.
    started_at: float | None = None
    max_steps_per_run: int = 200
    max_run_duration_s: float = 7200.0
    # Set once, synchronously (no `await` between computing `effective_kind`
    # and this assignment — see `_terminate_run`), the first time this Run
    # actually gets terminated (any of the three outer kinds:
    # "user"|"error"|"budget") — `None` for a Turn that completed normally.
    # `_advance_queue` reads this off the *just-finished* Turn's own
    # `_TurnContext` to decide "was this Run terminated" without needing a
    # second DB round trip on its own hot path (R-N4, PRD 9.3: "终止不清空
    # Session 队列；队列中的后续指令挂起"). Deliberately survives a rebroadcast
    # (`_rebroadcast_terminated_run`) unchanged — that function never touches
    # `ctx_turn`; it doesn't need to, since by the time a second `_terminate_run`
    # call can observe `existing["status"] == "terminated"`, the first call's
    # write (and this synchronous assignment, which always happens strictly
    # before that write starts) has already landed.
    terminated_kind: str | None = None
    # R-N11 (controller ruling, round-7, 2026-09-20): pinned BEFORE any active
    # termination path (`stop()`/`_terminate_run_for_exceeded_budget`/
    # `_on_worker_crash`) sends its ACP `session/cancel` notification (or, for
    # a crashed worker, before it would if it could) — `_finalize_turn_
    # success` reads this back when `prompt()` eventually returns NORMALLY
    # with `stopReason:"cancelled"`, because that same notification makes the
    # worker report a cleanly-cancelled Turn regardless of *why* it was
    # cancelled. Round-6's R-N7 made budget termination walk the exact same
    # cancel-then-terminate path `stop()` already used for a user-initiated
    # stop — round-6 review found the resulting race: `_terminate_run_for_
    # exceeded_budget` (a background watchdog task, not `_run_turn`'s own
    # task) sends `cancel()`, and the read loop can resolve `_run_turn`'s
    # in-flight `prompt()` future with `stopReason:"cancelled"` concurrently,
    # before that same call's own subsequent `_terminate_run(kind="budget")`
    # write lands — `_finalize_turn_success`'s old, unconditional "stopReason
    # cancelled -> kind=user" could then win that race and silently overwrite
    # a budget/error termination with a wrong `kind="user"` (and no budget
    # details). `None` means no active termination has claimed this Turn yet.
    #
    # R-N16 (controller ruling, round-8, 2026-09-20): round-7's own fix for
    # the above (`_finalize_turn_success` skipping its `_terminate_run` call
    # entirely whenever this was non-`None`, deferring to the claiming path's
    # own call) opened a WORSE race — see `_finalize_turn_success`'s own
    # comment. `_terminate_run` is now the one call `_finalize_turn_success`
    # always makes on a cancelled `stopReason`, and `_terminate_run` itself
    # reads this field back to decide WHAT KIND to record (taking priority
    # over its caller's own `kind` argument whenever this isn't `None`) —
    # this field's job narrowed to exactly that: deciding what gets recorded,
    # never whether `_terminate_run` gets called at all.
    #
    # R-N20 (controller ruling, round-9, 2026-09-20): narrowing this to just
    # `kind` (R-N16) fixed WHICH outer kind gets persisted, but left `reason`/
    # `card`/`step_seq` themselves still coming from whichever call actually
    # performs the write — when that was `_finalize_turn_success`'s own
    # generic call, the card/reason a user saw stayed the hard-coded "用户
    # 终止" ones even for a Run correctly recorded as `kind="budget"`. Now a
    # `_TerminationIntent` (the whole record `_terminate_run` needs, not a
    # bare string) — see that dataclass's own comment for the full story.
    termination_intent: _TerminationIntent | None = None


# R-N5 (controller ruling, 2026-09-20; PRD 11.2 "单个 Run 最大 Step 数 200"/
# "单个 Run 最大时长 2 h"): the same two numbers `config/resolver.py::
# DEFAULT_SETTINGS` now carries as `max_steps_per_run`/`max_run_duration_s` —
# duplicated here (not imported) as `_TurnContext`'s own field defaults and as
# the fallback `_run_turn` applies when a session's resolved `settings` dict is
# missing either key (honest `ConfigResolver` stand-ins like `context.py::
# NullConfigResolver` return `{}` rather than inventing defaults — see that
# class's docstring — so `_run_turn` can't assume the merged-settings dict
# already has these; PRD 11.2's own numbers are the one true fallback, matched
# exactly in both places rather than risking the two drifting apart silently).
_DEFAULT_MAX_STEPS_PER_RUN = 200
_DEFAULT_MAX_RUN_DURATION_S = 7200.0


@dataclass
class _PendingPermission:
    session_id: str
    params: dict[str, Any]
    future: asyncio.Future[dict[str, Any]]
    # Populated by `_on_request_permission` (Issue #11's review gate) so
    # `permission_pending()` (owned by A/#10, not touched by this branch —
    # see the PR report's "契约变更" note) has real data available to read
    # instead of the hardcoded "unclassified" it still returns today.
    risk: str = "unclassified"


class SessionService:
    def __init__(
        self,
        ctx: DaemonContext,
        *,
        worker_cmd: list[str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ctx = ctx
        # R-N8 (controller ruling, round-6, 2026-09-20): injectable so
        # `_run_budget_watchdog_loop`/`_check_run_durations` (and `_run_turn`'s
        # own `started_at` stamp, kept on the same clock for consistency) can be
        # driven deterministically by a test without monkeypatching the real
        # `time.monotonic` — that was tried for the tool_call-triggered duration
        # check this replaces and broke `workers/manager.py`'s own idle-worker
        # reaper, which also reads `time.monotonic()` (see `test_errors_
        # sessions_integration.py`'s superseded docstring for the reproduction).
        # Defaults to the real clock so every non-test construction is
        # unaffected.
        self._clock = clock
        self.worker_manager = WorkerManager(
            user_root=ctx.paths.user_root(),
            on_session_update=self._on_session_update,
            on_request_permission=self._on_request_permission,
            on_worker_crash=self._on_worker_crash,
            worker_cmd=worker_cmd,
        )
        self._active_turns: dict[str, _TurnContext] = {}
        self._turn_tasks: dict[str, asyncio.Task[None]] = {}
        self._pending_permissions: dict[str, _PendingPermission] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        # FR06 回放 (02-w3-interfaces.md §2): background Step-payload writes
        # (`_write_step_payload`) and the retention sweep loop
        # (`replay/retention.py`) are tracked here so `shutdown()` can wait for
        # in-flight writes and cancel the sweep cleanly, instead of leaving
        # dangling tasks nobody awaits.
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._retention_task: asyncio.Task[None] | None = None
        # R-N8 (controller ruling, round-6, 2026-09-20; PRD 11.2 单个 Run 最大
        # 时长): a resident poll over `_active_turns`, replacing R-N5's
        # tool_call-triggered check (`_handle_tool_call_start`, removed this
        # round) — that check never fired for a Turn with zero tool calls (pure
        # text generation), the exact gap R-N5's own report flagged as a known
        # scope gap. Same lifecycle shape as `_retention_task` right above:
        # started in `startup()`, cancelled+awaited in `shutdown()`.
        self._budget_watchdog_task: asyncio.Task[None] | None = None
        # `permission.decide(remember="session")`'s target (Issue #11,
        # 02-w3-interfaces.md §1.1's "remember"): in-memory only, gone on
        # restart (matches PRD 5.8/9.2 — nothing about a restart should
        # silently widen what's allowed). Read by `_refresh_gate_config`,
        # written by `_remember_allow`.
        self._session_remembered_rules: dict[str, list[dict[str, str]]] = {}
        # Review finding #13 (2026-09-19): the mode `_refresh_gate_config`
        # snapshotted into `jones_gate.json` for this session's currently
        # (or about to be) running Turn — `send()` writes it right where it
        # writes that file; `_on_request_permission` reads it back as a
        # fallback when a request's own payload carries no mode hint (the
        # edit_approval `{"tool","arguments"}` shape never does — see
        # `_extract_tool_call`'s docstring), so a mid-Run `set_mode()` can't
        # loosen a pending review-gate decision for an already-running Run
        # by racing a live DB read, the same "正在执行中的 Run 不受影响"
        # guarantee `_refresh_gate_config` already gives the rule gate.
        self._turn_mode_snapshot: dict[str, str] = {}
        # Issue #22 (04-w5-interfaces.md §4): `retry()`'s optional `model_override`
        # for the "换模型" card action — keyed by the NEW turn_id `retry()` gets
        # back from `send()`, consumed (popped) by `_run_turn`'s provider-
        # resolution line the one time that Turn actually starts running. Same
        # restart-loses-it lifetime as `_session_remembered_rules` above (in-
        # memory only; a retry queued behind other work and never reached before
        # a restart just falls back to the Agent's normal model_pref, which is
        # honest — PRD 5.8 never promises restart preserves in-flight intent).
        self._turn_model_override: dict[str, dict[str, Any]] = {}
        # Round-N2 review fix (#1, controller ruling R-N1): `_terminate_run`'s
        # idempotency guard used to treat "DB write already landed" and
        # "already broadcast" as the same fact — they aren't (see that
        # method's guard for the full race this closes). This tracks which
        # `run_id`s have actually had `run.terminated` broadcast, so a second
        # call for a Run whose write landed but whose broadcast never fired
        # (its caller's coroutine got cancelled in between) can tell the
        # difference and rebroadcast instead of silently no-op-ing. Same
        # restart-scoped lifetime as the maps above; never pruned — each entry
        # is one short run_id string for one terminated Run in this process's
        # lifetime, not worth cross-function bookkeeping in `_advance_queue`
        # (out of this branch's authorized change set, 04-w5-interfaces.md §1).
        self._terminated_broadcast_run_ids: set[str] = set()

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self._session_locks.setdefault(session_id, asyncio.Lock())

    # -- lifecycle --------------------------------------------------------------

    async def startup(self) -> None:
        interrupted = await run_in_db_thread(queries.interrupt_stale_runs, self.ctx.db)
        if interrupted:
            logger.warning(
                "marked stale Runs from a previous process as terminated",
                extra={"detail": {"count": interrupted}},
            )
        await self.worker_manager.start()
        await self.ensure_main_session()
        # FR06 回放保留策略 (02-w3-interfaces.md §2/§3: "清理在空闲时跑") — a plain
        # background loop, not tied to any request's critical path.
        self._retention_task = asyncio.create_task(replay_retention.run_sweep_loop(self.ctx))
        # R-N8: same shape, this branch's own background loop.
        self._budget_watchdog_task = asyncio.create_task(self._run_budget_watchdog_loop())

    async def shutdown(self) -> None:
        # Close off every still-pending permission wait before tearing workers
        # down — otherwise `AcpClient`'s background task answering it (kernel/
        # acp_client.py) is left awaiting a future nobody will ever resolve.
        self._resolve_pending_permissions(reason="daemon shutdown")
        await self.worker_manager.stop()
        # Issue #35 fix (root cause found via H/#17's repro — see
        # `tests/repro_issue_35.py` and `tests/test_issue_35_repro.py`; reported
        # against `kernel/acp_client.py`/`workers/manager.py` in 02-w3-
        # interfaces.md §1.2, root cause turned out to live here instead — see
        # this PR's report for the full writeup):
        #
        # `_advance_queue` (this file) pops `self._active_turns[session_id]` —
        # the bookkeeping `active_turn_session_ids()` reports — BEFORE its own
        # two remaining awaits (`run_in_db_thread(queries.list_queue_items,
        # ...)` and the `queue.changed` broadcast) actually run. A caller using
        # "no active turns" as its "safe to tear down now" signal (exactly what
        # a graceful shutdown needs to do) can therefore proceed to close
        # `ctx.db` (`__main__.py`'s `_run()`, right after this method returns)
        # while that trailing work is still in flight on `store/db.py`'s
        # single-worker `_DB_EXECUTOR`. If the event loop closes (`asyncio.
        # run()`'s teardown) before that queued DB callable's result is
        # delivered back, the awaiting `_run_turn` task is abandoned mid-flight
        # — `loop.run_in_executor`'s completion callback has nowhere left to
        # deliver the result to — which is exactly "Task was destroyed but it
        # is pending!" at interpreter exit, this bug's actual, reproducible
        # shape (not a true infinite hang every time, which is why it only
        # showed up "间歇" — intermittently, depending on exactly how much of
        # that trailing work had completed before shutdown reached this point).
        #
        # The fix: wait for every still-tracked `_turn_tasks` entry to actually
        # finish (bounded, same 5s/`return_exceptions=True` shape the
        # `_background_tasks` wait right below already uses, for the same
        # reason — a shutdown must still make forward progress even if one is
        # stuck) — AFTER `worker_manager.stop()` above, which is what unblocks
        # a task still genuinely mid-Turn (an unbounded `AcpClient.prompt()`
        # await starts erroring the moment its worker's stdout closes, see that
        # class's `_read_loop` finally block), not before.
        if self._turn_tasks:
            await asyncio.wait(list(self._turn_tasks.values()), timeout=5.0)
        if self._retention_task is not None:
            self._retention_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._retention_task
        if self._budget_watchdog_task is not None:
            self._budget_watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._budget_watchdog_task
        # Let already-scheduled Step-payload writes (`_write_step_payload`)
        # finish rather than abandoning them mid-write — bounded, not indefinite:
        # a shutdown must still make forward progress even if a write is stuck
        # (e.g. a full disk). `return_exceptions=True` because a background
        # write task already logs its own failures (see `_write_step_payload`);
        # this wait exists to give it time to do so, not to re-raise here.
        if self._background_tasks:
            await asyncio.wait(self._background_tasks, timeout=5.0)

    def _resolve_pending_permissions(self, *, session_id: str | None = None, reason: str) -> None:
        """Close off pending `session/request_permission` waits (all of them, or
        just one session's) with a `cancelled` outcome.

        A pending permission blocks the *worker's* own progress on our answer,
        not the other way around — if the worker's read/dispatch loop is itself
        stuck waiting on this Turn's tool call, nothing (`stop()`, a crash,
        shutdown) can make forward progress on that Turn until we answer it one
        way or another (00-foundation.md §8.1/§7). `kernel/acp_client.py`'s read
        loop no longer blocks on answering this itself (round-1 review fix — see
        this PR report's "第 1 轮修复记录"), but nothing else ever resolves this
        future for a session that got stopped or whose worker died mid-request
        without this.
        """
        for entry in list(self._pending_permissions.values()):
            if session_id is not None and entry.session_id != session_id:
                continue
            if not entry.future.done():
                entry.future.set_result({"outcome": {"outcome": "cancelled"}})

    async def ensure_main_session(self) -> str:
        existing = await run_in_db_thread(queries.get_main_session, self.ctx.db)
        if existing is not None:
            return existing["id"]
        session_id = new_ulid()
        await run_in_db_thread(
            queries.create_session,
            self.ctx.db,
            session_id=session_id,
            project_id=DEFAULT_PROJECT_ID,
            agent_id=DEFAULT_AGENT_ID,
            parent_id=None,
            is_main=True,
            mode="task",
            title="Main",
        )
        logger.info("created main session", extra={"detail": {"session_id": session_id}})
        return session_id

    async def _cwd_for_project(self, project_id: str) -> str:
        """02-w3-interfaces.md §2 集成收口 #1: C (#8/#9) has since landed real
        `projects` rows (including `proj_default`'s real, per-machine path via
        `ProjectService.ensure_default_project()` at daemon startup — see
        __main__.py) — use `ProjectService.get()` for every project, not just a
        hardcoded home-directory special case for the default one. `get()` itself
        already raises `RpcError(NOT_FOUND, ...)` for an unknown id, which is the
        right error here too (a Session referencing a deleted Project)."""
        project = await run_in_db_thread(ProjectService(self.ctx.db).get, project_id)
        return project["path"]

    # -- Session CRUD -------------------------------------------------------------

    async def create(
        self,
        *,
        project_id: str,
        agent_id: str,
        parent_id: str | None = None,
        mode: str = "task",
        title: str | None = None,
        system_dispatch: bool = False,
    ) -> dict[str, Any]:
        """`system_dispatch` (Issue #20 跨分支裁定, 2026-09-19): PRD 9.6/N13's
        "子会话模式不得比父会话宽" gate exists to stop an Agent from
        self-escalating its own derived sub-sessions — it was never meant to
        constrain a session a *system* component dispatches on the user's own
        prior, explicit authorization. Cron is exactly that: a cron's `mode`
        is configured by the user in the cron definition itself (`cron.upsert`),
        not chosen live by an Agent, and PRD 9.1 says a Cron-triggered Run
        defaults to auto mode — so `CronService` (the only caller allowed to
        pass `system_dispatch=True`, see `scheduler/service.py::
        _dispatch_body_claimed`) skips the parent-mode-narrowing check below.
        This does NOT touch the Agent tool-allowlist half of N13 (`子会话的
        工具白名单 ⊆ 父会话`) — that is enforced continuously by
        `permissions/gate_config.py` off the live parent chain, independent
        of this method, and stays in force regardless of `system_dispatch`.
        The RPC `session.create` handler (`sessions/methods.py`) never reads
        this kwarg from request params, so an external caller can never set
        it to True.
        """
        if mode not in _VALID_MODES:
            raise RpcError(INVALID_PARAMS, f"invalid mode: {mode!r}", {"mode": mode})
        if parent_id is not None:
            parent = await run_in_db_thread(queries.get_session, self.ctx.db, parent_id)
            if parent is None:
                raise RpcError(NOT_FOUND, "parent session not found", {"parent_id": parent_id})
            if parent["mode"] == "task" and mode == "auto" and not system_dispatch:
                # PRD 9.6 "权限不放大" / N13: a task-mode parent may not derive an
                # auto-mode child (tightening is fine, the reverse is not) — unless
                # this is a system dispatch (see the `system_dispatch` docstring
                # above), which carries its own user-configured authorization.
                raise RpcError(
                    INVALID_STATE,
                    "child session cannot be mode=auto when its parent is mode=task (PRD 9.6, N13)",
                    {"parent_mode": parent["mode"], "requested_mode": mode},
                )
        session_id = new_ulid()
        return await run_in_db_thread(
            queries.create_session,
            self.ctx.db,
            session_id=session_id,
            project_id=project_id,
            agent_id=agent_id,
            parent_id=parent_id,
            is_main=False,
            mode=mode,
            title=title,
        )

    async def get(self, session_id: str) -> dict[str, Any]:
        row = await run_in_db_thread(queries.get_session, self.ctx.db, session_id)
        if row is None:
            raise RpcError(NOT_FOUND, "session not found", {"id": session_id})
        turn = await run_in_db_thread(queries.latest_turn, self.ctx.db, session_id)
        return {**row, "latest_turn": turn}

    async def list(self, project_id: str | None = None) -> list[dict[str, Any]]:
        return await run_in_db_thread(queries.list_sessions, self.ctx.db, project_id=project_id)

    async def set_mode(self, session_id: str, mode: str) -> dict[str, Any]:
        if mode not in _VALID_MODES:
            raise RpcError(INVALID_PARAMS, f"invalid mode: {mode!r}", {"mode": mode})
        session = await run_in_db_thread(queries.get_session, self.ctx.db, session_id)
        if session is None:
            raise RpcError(NOT_FOUND, "session not found", {"id": session_id})
        if session["parent_id"]:
            parent = await run_in_db_thread(queries.get_session, self.ctx.db, session["parent_id"])
            if parent is not None and parent["mode"] == "task" and mode == "auto":
                raise RpcError(
                    INVALID_STATE,
                    "child session cannot be mode=auto when its parent is mode=task (PRD 9.6, N13)",
                    {"parent_mode": parent["mode"], "requested_mode": mode},
                )
        row = await run_in_db_thread(queries.set_session_mode, self.ctx.db, session_id, mode)
        if row is None:
            raise RpcError(NOT_FOUND, "session not found", {"id": session_id})
        return row

    # -- send / queue -------------------------------------------------------------

    async def send(
        self, session_id: str, text: str, attachments: list[Any] | None = None
    ) -> dict[str, Any]:
        session = await run_in_db_thread(queries.get_session, self.ctx.db, session_id)
        if session is None:
            raise RpcError(NOT_FOUND, "session not found", {"id": session_id})
        async with self._lock(session_id):
            running = session_id in self._active_turns or (
                session_id in self._turn_tasks and not self._turn_tasks[session_id].done()
            )
            turn_id = new_ulid()
            message_id = new_ulid()
            await run_in_db_thread(
                queries.create_turn_and_user_message,
                self.ctx.db,
                turn_id=turn_id,
                message_id=message_id,
                session_id=session_id,
                text=text,
                queued=running,
            )
            if running:
                await run_in_db_thread(
                    queries.enqueue,
                    self.ctx.db,
                    session_id=session_id,
                    turn_id=turn_id,
                    text=text,
                    attachments=attachments,
                )
                items = await run_in_db_thread(queries.list_queue_items, self.ctx.db, session_id)
                await self.ctx.server.broadcast(
                    session_id, "queue.changed", {"session_id": session_id, "items": items}
                )
                return {"turn_id": turn_id, "queued": True}
            # 模式检查一处调用 (02-w3-interfaces.md §1.1): rewrite this
            # session's rule-gate config before the Turn that's about to run
            # sees it — see `_refresh_gate_config`'s docstring for why this
            # is the one place that call belongs. The mode snapshot recorded
            # alongside it (review finding #13) MUST be written from this
            # exact same `session["mode"]` read, not a fresh one — the whole
            # point is that both gates agree on the mode this Turn started
            # with, not just that they're both "recent".
            self._turn_mode_snapshot[session_id] = session["mode"]
            await self._refresh_gate_config(session)
            self._start_turn(session_id, turn_id, text)
            return {"turn_id": turn_id, "queued": False}

    async def _refresh_gate_config(self, session: dict[str, Any]) -> None:
        """Rewrite `<HERMES_HOME>/jones_gate.json` to match this session's
        CURRENT mode/permissions/Agent tool whitelist, right before a Turn is
        about to start running for real (Issue #11, 02-w3-interfaces.md
        §1.1: "模式切换即时生效...作用于该 Session 后续的 Turn；正在执行中的
        Run 不受影响", PRD 9.1).

        Deliberately only called from `send()`'s immediate-start branch
        (never while a Turn is running or being enqueued behind one): the
        rule gate re-reads this file live via an mtime check on every
        `pre_tool_call` (`kernel/plugin/jones_gate/_config.py`), so writing a
        new mode/ruleset here while a Turn is mid-flight would retroactively
        change gating for that ALREADY-RUNNING Run — exactly what PRD 9.1
        says must not happen.

        Known scope gap (documented in the PR report, not silently
        skipped): a mode change that lands entirely while items are queued
        behind a running Turn only takes effect at the next `send()` call
        that starts a Turn immediately — not the instant `_advance_queue`
        (owned by G/#12/Run-replay, not this branch's function to touch)
        dequeues and starts a queued item. That queued Turn runs with
        whatever config the last immediate `send()` wrote.

        Safe to call before a worker for this session even exists yet:
        `permissions/gate_config.write()` creates `HERMES_HOME` if needed,
        and `workers/manager.py::_prepare_hermes_home` (which may run
        later, when/if the worker actually spawns) never deletes or
        overwrites this specific file — it only manages the
        `plugins/jones_gate/` subdirectory and `config.yaml`.
        """
        project_id = session["project_id"]
        try:
            cwd = await self._cwd_for_project(project_id)
        except Exception:  # noqa: BLE001 - `_cwd_for_project` is not this
            # branch's function to touch/narrow the failure modes of (its
            # only documented one is `RpcError` for a non-default project —
            # C/#8#9 scope gap — but `_run_turn`'s own catch-all backstop
            # proves other exceptions are also possible from callers of it,
            # see test_sessions_service.py's
            # test_unexpected_exception_in_run_turn_still_terminates_the_run).
            # `send()` itself must never crash over resolving an OPTIONAL
            # workspace root for the gate config: degrade `cwd` to unknown
            # and keep going — the workspace-relative parts of the gate
            # config (the hard-deny gate's protected project-permissions-
            # path check, the review gate's write-in/out-of-workspace
            # classification) fail closed on their own when `cwd`/
            # `project_permissions_path` are unknown (see
            # `permissions/gate_config.py` and `permissions/review.py`),
            # never fail open. `_run_turn`'s own subsequent, separate call
            # to `_cwd_for_project` (unmodified by this branch) still
            # surfaces the SAME underlying failure as a real `run.terminated`
            # a moment later, so nothing is silently lost — see that
            # function's own `except WorkerStartupError`/catch-all.
            cwd = None

        extra_rules = self._session_remembered_rules.get(session["id"])

        def _build_and_write() -> None:
            permissions_result = self.ctx.config.permissions(project_id)
            config = gate_config.build(
                conn=self.ctx.db, permissions_result=permissions_result, session=session,
                user_root=self.ctx.paths.user_root(), project_path=cwd, extra_rules=extra_rules,
            )
            hermes_home = gate_config.hermes_home_for(self.ctx.paths.user_root(), session["id"])
            gate_config.write(hermes_home, config)

        await run_in_db_thread(_build_and_write)

    async def stop(self, session_id: str) -> dict[str, Any]:
        session = await run_in_db_thread(queries.get_session, self.ctx.db, session_id)
        if session is None:
            raise RpcError(NOT_FOUND, "session not found", {"id": session_id})
        task = self._turn_tasks.get(session_id)
        if task is None or task.done():
            return {"stopped": False}
        # Issue #39 (controller ruling, 2026-09-21): pin the termination
        # intent unconditionally — as soon as there's an active Turn to pin
        # it on — not only when a worker happens to already be up. A user's
        # stop is an intent about the TURN, not about whether its worker has
        # finished `ensure_started()` yet. R-N11's "pin before cancel"
        # ordering (below) only ever closed ONE race (`_finalize_turn_
        # success` racing this same field's own write) — gating the PIN
        # itself on `worker is not None and worker.client is not None and
        # worker.acp_session_id is not None` left an earlier one open:
        # `send()` immediately followed by `stop()`, before `_run_turn`'s own
        # `ensure_started()` call has even returned, means all three of those
        # are still `None`/unset here — nothing got pinned, no cancel got
        # sent, the worker then finishes starting moments later, and
        # `_run_turn` prompts the agent as if `stop()` had never been called
        # — while `stop()` already told the caller `{"stopped": True}`
        # (exactly Issue #39's "意图不落、cancel 不发、agent 照跑完").
        ctx_turn = self._active_turns.get(session_id)
        if ctx_turn is not None:
            user_stop_reason = "stopped by user"
            ctx_turn.termination_intent = _TerminationIntent(
                kind="user",
                reason=user_stop_reason,
                card=classify.build_user_card(user_stop_reason),
                step_seq=ctx_turn.step_seq or None,
            )
        worker = self.worker_manager.get(session_id)
        if worker is not None and worker.client is not None and worker.acp_session_id is not None:
            try:
                # A notification, not a hard kill: PRD 9.3 "用户终止" requires the
                # in-flight tool call to finish cleanly. `cancel()` sets Hermes's
                # cancel_event; `_run_turn`'s `prompt()` call returns normally with
                # `stopReason="cancelled"` once the agent notices, which
                # `_finalize_turn_success` turns into `run.terminated{kind:"user"}`.
                await worker.client.cancel(worker.acp_session_id)
            except Exception as exc:  # noqa: BLE001 - R-N13 (controller ruling,
                # round-7, 2026-09-20): a worker that crashed just before this
                # notification can fail `AcpClient._send` with something other
                # than `(AcpProtocolError, AcpError)` too — e.g. a raw
                # `ConnectionResetError` writing to a pipe whose other end just
                # died (`AcpClient._closed` is only ever set inside `close()`,
                # which nothing calls automatically the instant the subprocess
                # exits). None of them should block finishing this termination,
                # same as the two named ACP exception types already didn't —
                # only `CancelledError` (a `BaseException`, not caught here)
                # must still propagate.
                logger.warning(
                    "session/cancel notification failed (worker likely already gone)",
                    extra={"detail": {"session_id": session_id, "error": str(exc)}},
                )
        # else: no worker to cancel yet (still inside `ensure_started()`, or
        # never started) — nothing lost. The intent pinned above is enough:
        # `_run_turn` checks `ctx_turn.termination_intent` itself, right after
        # its own `ensure_started()` call returns and before it ever calls
        # `prompt()` (Issue #39's fix, part 3) — the worker IS ready by then,
        # so that check sends the cancel this branch couldn't and goes
        # straight to `_terminate_run`, never prompting the agent at all.
        # If this Turn's in-flight tool call is sitting on a pending
        # `session/request_permission` (no one has answered it yet — the user
        # just clicked "stop" instead of allow/deny), `cancel()` alone can't
        # unblock it: the worker won't notice the cancellation until *we* answer
        # its pending request one way or another. Round-1 review fix — without
        # this, `stop()` during a pending approval never produces `run.terminated`
        # (see this PR report's "第 1 轮修复记录").
        self._resolve_pending_permissions(session_id=session_id, reason="stopped by user")
        return {"stopped": True}

    async def delete_guard(self, session_id: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Round-2 review, Issue #23: `store/maintenance.py::delete_session`'s own
        "is this session running" check only reads `runs.status='running'` from
        SQLite — but this class's own comment on `_advance_queue` below is
        explicit that "everything that decides 'is this session still running'
        must happen under `self._lock(session_id)`", because the authoritative
        answer lives in `_active_turns`/`_turn_tasks`, not the DB row. `send()`
        writes the `turns` row, `create_task()`s the Turn, and populates
        `_active_turns`/`_turn_tasks` all inside that same lock — the `runs` row
        itself isn't inserted until `_run_turn` reaches `queries.create_run`,
        well after the task starts and after `send()` has already released the
        lock. A `session.delete` racing that window sees zero running Runs in
        SQLite, "successfully" deletes the session's rows out from under a Turn
        that is very much in flight, and the abandoned Turn's own later
        `create_run` then hits a live `sessions`/`turns` foreign key that no
        longer resolves.

        This closes that window the only way that matches the class's own
        stated invariant: acquire `self._lock(session_id)` here too, so a
        `send()` already past its own lock acquisition is fully done updating
        `_active_turns`/`_turn_tasks` before this ever checks them, and a
        `send()` that hasn't started yet blocks until this either refuses or
        (on refusal) is done. Also stops this session's worker eagerly —
        without this, a deleted session's worker process (and the credentials
        under its `HERMES_HOME`, see `store/maintenance.py::delete_session`'s
        own round-2 fix) would otherwise sit alive for up to
        `workers/manager.py`'s `DEFAULT_IDLE_TIMEOUT_S` (600s) after the
        Session it belongs to no longer exists.

        `fn` is the actual delete call (e.g. `store/maintenance.py::
        delete_session` via `run_in_db_thread`, wrapped in `sessions/methods.py::
        _run_delete_honestly`) — this method only owns the "is it safe to even
        try" decision and the worker teardown, not the delete mechanics
        themselves (04-w5-interfaces.md §1: this branch's `sessions/service.py`
        access is for exactly this kind of guard, not a rewrite of `send()`'s
        own logic).

        Round-3 review (review item 2): `session.export {delete_after: true}`
        goes through this same guard now too (`sessions/methods.py::
        session_export` -> `_run_export_honestly`), not just `session.delete` —
        it can trigger the exact same `delete_session` call and was racing the
        same window and leaving the same live-worker `HERMES_HOME` purge bug
        when it called `maintenance.export_session` directly."""
        async with self._lock(session_id):
            running = session_id in self._active_turns or (
                session_id in self._turn_tasks and not self._turn_tasks[session_id].done()
            )
            if running:
                raise RpcError(
                    INVALID_STATE,
                    "session has a Turn actively running; stop it before deleting",
                    {"id": session_id},
                )
            await self.worker_manager.stop_worker(session_id, reason="session_deleted")
            return await fn(*args, **kwargs)
    async def retry(
        self,
        session_id: str,
        turn_id: str,
        *,
        action: str = "retry",
        model_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """RPC `session.retry` (Issue #22, 04-w5-interfaces.md §4) — the server
        side of the error card's three actions:

          - "重试" -> `action="retry"`, no `model_override`.
          - "换模型" -> `action="retry"` WITH `model_override` set. Not a
            separate action at this layer — "switch model, then retry" is
            exactly "retry, but resolve the provider differently for this one
            Turn", which is what `model_override` already means (see below);
            giving it a second RPC action would just be two names for one
            operation (this is called out as an interface decision, not left
            silent, in the report's "契约变更" section).
          - "放弃" -> `action="abandon"`: clears this session's pending queue
            (04-w5-interfaces.md §4: "清掉队列中该 Turn 的后续") and marks both
            the abandoned turn and every queue item's own turn `cancelled`
            (reusing `queries.cancel_turn` — the same status `queue_remove`
            already uses for a manually-dequeued item, not a new vocabulary
            value invented for this).

        "重新发起该 Turn（新 Turn，用户消息复用）": `action="retry"` does not
        replay the OLD Turn — it calls `send()` (unmodified, see this branch's
        exclusive-touch list) with the original user message text, which is
        exactly `session.send`'s own existing immediate-run-or-enqueue
        semantics (PRD 9.2 same-session serial execution applies here too: if
        another Turn is already running on this session, the retry queues
        behind it like any other `send()`, which is correct — there is no
        special "jump the queue" case for a retry).
        """
        if action not in ("retry", "abandon"):
            raise RpcError(INVALID_PARAMS, f"invalid action: {action!r}", {"action": action})
        session = await run_in_db_thread(queries.get_session, self.ctx.db, session_id)
        if session is None:
            raise RpcError(NOT_FOUND, "session not found", {"id": session_id})
        turn = await run_in_db_thread(_fetch_turn, self.ctx.db, turn_id)
        if turn is None or turn["session_id"] != session_id:
            raise RpcError(
                NOT_FOUND, "turn not found", {"turn_id": turn_id, "session_id": session_id}
            )
        if turn["status"] != "terminated":
            # Only a Turn whose Run actually ended in `run.terminated` (error or
            # budget — success never shows a card with these actions, and a
            # still-running Turn has nothing to retry/abandon yet) is a valid
            # target. Round-tripping this twice (e.g. abandon, then abandon
            # again) is refused rather than silently no-op'd — an honest signal
            # to a UI bug that sent a stale run_id/turn_id twice, not a state
            # worth pretending is fine (DEV.md 工程原则 #4).
            raise RpcError(
                INVALID_STATE,
                f"turn {turn_id} is not in a retryable state (status={turn['status']!r})",
                {"turn_id": turn_id, "status": turn["status"]},
            )

        if action == "abandon":
            async with self._lock(session_id):
                pending = await run_in_db_thread(queries.list_queue_items, self.ctx.db, session_id)
                for item in pending:
                    removed_turn_id = await run_in_db_thread(
                        queries.remove_queue_item,
                        self.ctx.db,
                        session_id=session_id,
                        item_id=item["id"],
                    )
                    if removed_turn_id is not None:
                        await run_in_db_thread(queries.cancel_turn, self.ctx.db, removed_turn_id)
                await run_in_db_thread(queries.cancel_turn, self.ctx.db, turn_id)
            items = await run_in_db_thread(queries.list_queue_items, self.ctx.db, session_id)
            # R-N4 (controller ruling, 2026-09-20; 04-w5-interfaces.md §4.3):
            # an empty queue is never "suspended" — explicit `False`/`None`
            # here so a renderer that had this session's queue marked
            # suspended (from an earlier `_advance_queue` broadcast) doesn't
            # keep showing "已暂停" against a queue this abandon just emptied.
            # R-N9 (round-6): the persisted counterpart of that same clear —
            # `session.get`/`session.queue` must stop reporting this session
            # suspended too, not just this one broadcast.
            await run_in_db_thread(
                queries.set_queue_suspended_reason, self.ctx.db, session_id, None
            )
            await self.ctx.server.broadcast(
                session_id,
                "queue.changed",
                {"session_id": session_id, "items": items, "suspended": False, "reason": None},
            )
            return {"turn_id": turn_id, "action": "abandon", "cleared_queue_items": len(pending)}

        if model_override is not None and (
            not isinstance(model_override, dict) or not model_override.get("provider")
        ):
            raise RpcError(
                INVALID_PARAMS,
                "model_override must be an object with at least a 'provider'",
                {"model_override": model_override},
            )
        text = await run_in_db_thread(_fetch_turn_user_text, self.ctx.db, turn)
        if text is None:
            raise RpcError(
                INVALID_STATE,
                "original user message not found for this turn",
                {"turn_id": turn_id},
            )
        result = await self.send(session_id, text)
        if model_override is not None:
            self._turn_model_override[result["turn_id"]] = model_override
        return {**result, "action": "retry", "retried_turn_id": turn_id}

    async def queue(self, session_id: str) -> dict[str, Any]:
        """R-N9 (controller ruling, round-6, 2026-09-20): used to return the
        bare `QueueItem[]` — now wraps it with the same `suspended`/`reason`
        shape `queue.changed` broadcasts (R-N4) and `session.get` already
        carries via its own row (`queries.set_queue_suspended_reason`'s own
        docstring has the full read/write map), so a caller that only ever
        polls `session.queue` (not `session.get`) can still reconstruct
        "已暂停" + "继续" after a reload. Breaking response-shape change to
        this one RPC's `SessionService` method — `queue_remove`/
        `queue_reorder` (untouched, still bare `QueueItem[]`) aren't part of
        this round's ruling and keep their existing shape."""
        items = await run_in_db_thread(queries.list_queue_items, self.ctx.db, session_id)
        session = await run_in_db_thread(queries.get_session, self.ctx.db, session_id)
        reason = session["queue_suspended_reason"] if session else None
        return {"items": items, "suspended": reason is not None, "reason": reason}

    async def queue_remove(self, session_id: str, item_id: str) -> list[dict[str, Any]]:
        async with self._lock(session_id):
            turn_id = await run_in_db_thread(
                queries.remove_queue_item, self.ctx.db, session_id=session_id, item_id=item_id
            )
            if turn_id is None:
                raise RpcError(NOT_FOUND, "queue item not found", {"item_id": item_id})
            await run_in_db_thread(queries.cancel_turn, self.ctx.db, turn_id)
        items = await run_in_db_thread(queries.list_queue_items, self.ctx.db, session_id)
        await self.ctx.server.broadcast(
            session_id, "queue.changed", {"session_id": session_id, "items": items}
        )
        return items

    async def queue_reorder(self, session_id: str, item_ids: list[str]) -> list[dict[str, Any]]:
        async with self._lock(session_id):
            await run_in_db_thread(
                queries.reorder_queue_items, self.ctx.db, session_id=session_id, item_ids=item_ids
            )
        items = await run_in_db_thread(queries.list_queue_items, self.ctx.db, session_id)
        await self.ctx.server.broadcast(
            session_id, "queue.changed", {"session_id": session_id, "items": items}
        )
        return items

    async def queue_resume(self, session_id: str) -> dict[str, Any]:
        """RPC `session.queue_resume` (R-N4, controller ruling 2026-09-20;
        04-w5-interfaces.md §4.3 / PRD 9.3): the queue panel's "继续" button —
        one of the two explicit-or-implicit user actions PRD 9.3 names for
        un-suspending a Session's queue after a termination (`_advance_queue`
        stopped auto-popping the next queued item for any of the three outer
        `kind`s — user/error/budget — see that method's own comment). The
        other is simply `session.send`-ing a new message (`send()` itself is
        unmodified — see `_advance_queue`'s comment for why that already
        works without touching it).

        Pops and starts the next pending item directly — the same mechanics
        `_advance_queue`'s own non-suspended branch uses, just triggered by
        an explicit user click instead of a Turn finishing. Refuses (rather
        than silently no-op'ing, matching `retry()`'s own established
        precedent for a stale/duplicate UI action) both when a Turn is
        already running on this session (nothing to resume INTO) and when
        there is nothing pending to resume (an honest signal that the "继续"
        button was shown/clicked when it shouldn't have been, not a
        state worth pretending succeeded)."""
        async with self._lock(session_id):
            running = session_id in self._active_turns or (
                session_id in self._turn_tasks and not self._turn_tasks[session_id].done()
            )
            if running:
                raise RpcError(
                    INVALID_STATE,
                    "a Turn is already running on this session",
                    {"id": session_id},
                )
            item = await run_in_db_thread(queries.pop_next_queue_item, self.ctx.db, session_id)
            if item is None:
                raise RpcError(
                    INVALID_STATE,
                    "this session has no pending queue items to resume",
                    {"id": session_id},
                )
            self._start_turn(session_id, item["turn_id"], item["text"])
            # R-N9 (controller ruling, round-6, 2026-09-20): the persisted
            # counterpart of this method's own `suspended:false` broadcast
            # below. Redundant with (but strictly earlier than) the clear
            # `_run_turn` itself will also do once its just-scheduled task
            # actually runs (`_start_turn` above only schedules it) — awaited
            # here so `session.get`/`session.queue` are already consistent
            # with this RPC's own response the instant it returns, not just
            # eventually once that task gets its turn on the loop.
            await run_in_db_thread(
                queries.set_queue_suspended_reason, self.ctx.db, session_id, None
            )
        items = await run_in_db_thread(queries.list_queue_items, self.ctx.db, session_id)
        await self.ctx.server.broadcast(
            session_id,
            "queue.changed",
            {"session_id": session_id, "items": items, "suspended": False, "reason": None},
        )
        return {"resumed": True, "items": items}

    async def turn_messages(
        self, session_id: str, *, before: int | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        return await run_in_db_thread(
            queries.list_turn_messages,
            self.ctx.db,
            session_id=session_id,
            before_seq=before,
            limit=limit,
        )

    async def run_list(self, session_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """回放视图"选一个 Run"（02-w3-interfaces.md §2) — `run.list`, a contract
        addition on top of 00-foundation.md §4.1 (see this file's module
        docstring / the PR report's "契约变更"): that table never had a way to
        discover a historical Run id besides the live `turn.started` notification."""
        return await run_in_db_thread(
            queries.list_runs_for_session, self.ctx.db, session_id, limit=limit
        )

    async def run_get(self, run_id: str) -> dict[str, Any]:
        """FR06 回放 (02-w3-interfaces.md §2): the `runs` row already carries every
        piece of "终止信息" this method needs to return — `terminated_kind`/
        `terminated_reason` (existing) plus `terminated_step_seq` (this issue,
        migration 005) — and `prompt_snapshot_ref` is the readable reference the
        client fetches via `run.payload` (never inlined here: it can be
        arbitrarily large, same reasoning as Step payloads)."""
        row = await run_in_db_thread(queries.get_run, self.ctx.db, run_id)
        if row is None:
            raise RpcError(NOT_FOUND, "run not found", {"run_id": run_id})
        return row

    async def run_steps(
        self, run_id: str, *, after_seq: int | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """FR06 回放分页 (02-w3-interfaces.md §2): `after_seq`/`limit` page forward
        through a Run's Steps in `seq` order — the same order the replay UI's
        前进/后退 stepping walks, so "next page" and "step forward past what's
        loaded" are the same request shape."""
        return await run_in_db_thread(
            queries.list_run_steps, self.ctx.db, run_id, after_seq=after_seq, limit=limit
        )

    async def run_payload(
        self, ref: str, *, offset: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        """FR06 回放, 02-w3-interfaces.md §2: `run.payload {ref}` — full-fidelity
        Step output / prompt snapshot, fetched on demand (never pushed proactively
        — "回放时按需加载", PRD 10.3). `>1MB` chunking is `offset`/`limit`, not a
        separate method — see `replay/store.py::CHUNK_THRESHOLD_BYTES`.

        `ref` must resolve to a real file under `<user_root>/runs/` — see
        `replay/store.py::_resolve_ref` for why that's checked even though every
        `ref` this method is ever called with should already have come from a
        `steps.payload_ref`/`runs.prompt_snapshot_ref` column (defense in depth,
        not a first line of defense)."""
        user_root = self.ctx.paths.user_root()
        try:
            size = await asyncio.to_thread(replay_store.payload_size, user_root, ref)
            data = await asyncio.to_thread(
                replay_store.read_payload, user_root, ref, offset=offset, limit=limit
            )
        except replay_store.PayloadRefError as exc:
            raise RpcError(NOT_FOUND, str(exc), {"ref": ref}) from exc
        return {
            "ref": ref,
            "offset": offset,
            "size": size,
            "data_base64": base64.b64encode(data).decode("ascii"),
            "eof": offset + len(data) >= size,
        }

    def active_turn_session_ids(self) -> list[str]:
        """`daemon.status`'s `sessions_active` (02-w3-interfaces.md §2 集成收口 #2,
        `rpc/methods.py::register_daemon_status`) — a plain accessor over
        `_active_turns`, the same dict `stop()`/`_advance_queue()` already treat
        as "this Session currently has a Turn running"."""
        return list(self._active_turns.keys())

    # -- permissions ----------------------------------------------------------------

    async def permission_pending(self, session_id: str | None = None) -> list[dict[str, Any]]:
        out = []
        for decision_id, entry in self._pending_permissions.items():
            if session_id is not None and entry.session_id != session_id:
                continue
            tool_call = entry.params.get("toolCall") or {}
            out.append(
                {
                    "request_id": decision_id,
                    "session_id": entry.session_id,
                    "gate": "user",
                    # Risk scoring is W3 FR05 (review gate) scope — W2 only ever
                    # produces user-gate requests (jones_gate's approve branch),
                    # so there is nothing here to classify yet.
                    "risk": "unclassified",
                    "tool_call": tool_call,
                    "options": entry.params.get("options"),
                }
            )
        return out

    async def permission_decide(
        self, request_id: str, decision: str, remember: str | None = None
    ) -> dict[str, Any]:
        if decision not in ("allow", "deny"):
            raise RpcError(
                INVALID_PARAMS, f"invalid decision: {decision!r}", {"decision": decision}
            )
        if remember is not None and remember not in ("session", "project"):
            raise RpcError(
                INVALID_PARAMS, f"invalid remember: {remember!r}", {"remember": remember}
            )
        entry = self._pending_permissions.get(request_id)
        if entry is None:
            raise RpcError(NOT_FOUND, "no pending permission request", {"request_id": request_id})
        option_id = _select_permission_option(entry.params.get("options") or [], decision, remember)
        row = await run_in_db_thread(
            queries.decide_permission, self.ctx.db, request_id, decision=decision, decided_by="user"
        )
        if not entry.future.done():
            entry.future.set_result({"outcome": {"outcome": "selected", "optionId": option_id}})
        assert row is not None  # noqa: S101 - just written by decide_permission above
        await self.ctx.server.broadcast(entry.session_id, "permission.decided", row)
        # PRD FR05/02-w3-interfaces.md §1.1 "remember": only ever narrows
        # (writes an `allow` for this one specific tool/command match — see
        # `_remember_allow`) and only on an explicit `allow` decision; a
        # remembered `deny` would be redundant with the rule/hard-deny gates
        # that would already block it next time, and "remember this denial"
        # isn't a rule shape 01-w2-interfaces.md §4.1's permissions.json
        # schema (or this file's in-memory session rules) has a use for.
        if remember is not None and decision == "allow":
            await self._remember_allow(entry, remember)
        return row

    async def _remember_allow(self, entry: _PendingPermission, remember: str) -> None:
        """Persist an `allow` narrowed to this one tool/command match
        (02-w3-interfaces.md §1.1: "只能是 allow 收窄到具体 match，不得触碰硬
        禁止" — hard-deny lives in `kernel/plugin/jones_gate/_hard_deny.py`'s
        code constants, which nothing written here can ever reach, let alone
        loosen). `remember="session"` -> in-memory, applied by
        `_refresh_gate_config` starting with this session's next
        immediately-started Turn (not the current one — see that method's
        docstring for why applying it retroactively to an in-flight Run
        isn't attempted). `remember="project"` -> appended into
        `<project>/.jones/permissions.json`; `config/resolver.py`'s existing
        merge (PRD 10.1) is what actually enforces "can't loosen a
        user-level deny" — writing here never bypasses it, the merge simply
        drops an entry that would.

        Round 5 (controller ruling R8, 2026-09-19, final): `match` is run
        through `_rules._normalize` (the exact same whitespace
        normalization the rule gate compares AGAINST — 02-w3-interfaces.md
        §1.1's "去首尾空白、连续空白折成一个空格") BEFORE it's written or
        compared against, and de-duplication against whatever's already
        there (`_session_remembered_rules`/the project's `permissions.json`)
        uses that same normalized form — not a raw-string `!=` — so two
        `remember`s of what's really the same command text (differing only
        in incidental whitespace a user might retype slightly differently)
        never pile up as two separate rules a future audit would have to
        puzzle over.
        """
        tool_name, args, _mode_hint = _extract_tool_call(entry.params)
        if not tool_name:
            logger.warning(
                "permission.decide remember=%r requested but the tool name could not be "
                "recovered from this request; not persisting anything",
                remember, extra={"detail": {"session_id": entry.session_id}},
            )
            return
        command = args.get("command") if tool_name == "terminal" else None
        raw_match = command if isinstance(command, str) and command else tool_name
        match = _rules._normalize(raw_match)
        rule = {"match": match, "action": "allow"}
        if remember == "session":
            existing = self._session_remembered_rules.setdefault(entry.session_id, [])
            existing[:] = [
                r for r in existing if _rules._normalize(str(r.get("match", ""))) != match
            ]
            existing.append(rule)
            return
        session = await run_in_db_thread(queries.get_session, self.ctx.db, entry.session_id)
        if session is None:
            return
        try:
            project_path = await self._cwd_for_project(session["project_id"])
        except RpcError:
            logger.warning(
                "permission.decide remember='project' requested but this session's project "
                "path can't be resolved yet; not persisting anything",
                extra={"detail": {"session_id": entry.session_id}},
            )
            return

        def _write() -> None:
            path = self.ctx.paths.project_permissions_path(project_path)
            data = read_json(path, {"rules": []})
            rules = [
                r
                for r in (data.get("rules") or [])
                if _rules._normalize(str(r.get("match", ""))) != match
            ]
            rules.append(rule)
            data["rules"] = rules
            write_json(path, data)

        await run_in_db_thread(_write)

    # -- turn execution ---------------------------------------------------------------

    def _start_turn(self, session_id: str, turn_id: str, text: str) -> None:
        task = asyncio.create_task(self._run_turn(session_id, turn_id, text))
        self._turn_tasks[session_id] = task

        def _cleanup(_t: asyncio.Task[None]) -> None:
            if self._turn_tasks.get(session_id) is task:
                del self._turn_tasks[session_id]

        task.add_done_callback(_cleanup)

    async def _run_turn(self, session_id: str, turn_id: str, text: str) -> None:
        run_id = new_ulid()
        await run_in_db_thread(
            queries.create_run, self.ctx.db, run_id=run_id, turn_id=turn_id, session_id=session_id
        )
        # R-N9 (controller ruling, round-6, 2026-09-20; PRD 9.3): a Turn about
        # to run is, by definition, not a suspended queue — clears whatever
        # `_terminate_run` last persisted here (see that method's own
        # comment), regardless of which of the three paths that can start a
        # Turn got here (send()'s immediate-run branch, `retry()`,
        # `queue_resume()`, or `_advance_queue`'s own auto-continue — all of
        # them end up in this one function). Covers "send 一条新消息" as an
        # implicit resume without this branch needing to touch `send()`
        # itself (out of this round's authorized touch set).
        await run_in_db_thread(queries.set_queue_suspended_reason, self.ctx.db, session_id, None)
        # FR06 回放, 02-w3-interfaces.md §2: `runs.prompt_snapshot_ref` — ACP
        # doesn't expose the fully assembled prompt actually sent to the model
        # (00-foundation.md §7), so this records, honestly, only what's available
        # at Run-start: the user's own message plus enough identifiers to look up
        # the rest (Agent/mode) — not a fabricated "here's the system prompt".
        # Best-effort: a failure to write this must not abort the Turn itself
        # (fail loud via the log, not fail the whole Run over a replay nicety).
        try:
            snapshot_ref = await asyncio.to_thread(
                replay_store.write_prompt_snapshot,
                self.ctx.paths.user_root(),
                run_id,
                {
                    "note": (
                        "Hermes ACP does not expose the fully assembled prompt sent to "
                        "the model (docs/design/00-foundation.md §7); recording what is "
                        "available."
                    ),
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "run_id": run_id,
                    "user_message": text,
                    "captured_at": queries.iso_now(),
                },
            )
            await run_in_db_thread(
                queries.set_prompt_snapshot_ref, self.ctx.db, run_id, snapshot_ref
            )
        except OSError:
            logger.error(
                "failed to write prompt snapshot for a Run",
                exc_info=True,
                extra={"detail": {"run_id": run_id}},
            )
        ctx_turn = _TurnContext(turn_id=turn_id, run_id=run_id, session_id=session_id)
        # R-N5 (controller ruling, 2026-09-20): "`_run_turn` 记录开始时刻" —
        # set explicitly here (see `_TurnContext.started_at`'s own comment for
        # why this is a plain call, not that field's default) rather than at
        # the settings-resolution point a few lines below, so the 时长上限
        # clock starts from when the Turn actually begins, not from whenever
        # the Session/settings DB round trip happens to finish. `self._clock()`
        # (R-N8, round-6) rather than a bare `time.monotonic()` call — the same
        # clock `_check_run_durations` compares against, injectable by tests
        # without monkeypatching the real `time` module (see `__init__`'s
        # comment on `self._clock`).
        ctx_turn.started_at = self._clock()
        self._active_turns[session_id] = ctx_turn
        self.worker_manager.mark_busy(session_id, True)
        # RPC v0 §4.2's `turn.started {session_id, turn_id, run_id}` — the only
        # signal the front end has that a queued Turn actually started running
        # (the queue-auto-advance path never calls `send()`, so it has nothing
        # else to watch for this).
        await self.ctx.server.broadcast(
            session_id, "turn.started",
            {"session_id": session_id, "turn_id": turn_id, "run_id": run_id},
        )
        try:
            try:
                session = await run_in_db_thread(queries.get_session, self.ctx.db, session_id)
                cwd = await self._cwd_for_project(session["project_id"])
                # R-N5 (controller ruling, 2026-09-20; PRD 11.2/9.3): resolve this
                # Turn's Step-count/duration budget once, here, from the same
                # merged `settings.json` `_on_request_permission` already reads
                # for `approval_timeout_minutes` — stashed on `ctx_turn` (not
                # re-read per tool call) so `_handle_tool_call_start`'s
                # per-tool-call check (DEV.md 工程原则 #3: 性能是需求, 不在高频
                # 路径做额外 DB round trip) is a plain attribute compare. A
                # resolver that doesn't carry these two keys (e.g. `context.py::
                # NullConfigResolver`, which honestly returns `{}` rather than
                # inventing settings) falls back to PRD 11.2's own numbers —
                # `_DEFAULT_MAX_STEPS_PER_RUN`/`_DEFAULT_MAX_RUN_DURATION_S`
                # right above `_TurnContext`, matching that class's own field
                # defaults exactly.
                turn_settings = await run_in_db_thread(
                    self.ctx.config.settings, session["project_id"]
                )
                ctx_turn.max_steps_per_run = int(
                    (turn_settings or {}).get("max_steps_per_run") or _DEFAULT_MAX_STEPS_PER_RUN
                )
                ctx_turn.max_run_duration_s = float(
                    (turn_settings or {}).get("max_run_duration_s") or _DEFAULT_MAX_RUN_DURATION_S
                )
                # 集成收口 (02-w3-interfaces.md §2 item 4's `pnpm e2e` needs this to
                # be a real, distinguishable failure mode, not just documentation):
                # fail fast on a Session whose Agent has no usable provider/Key
                # *before* ever spawning a worker subprocess — otherwise "no key
                # configured" and "hermes-agent isn't installed on this machine"
                # (see docs/DEV.md — the common case for every CI runner and most
                # dev checkouts) both surface as the same opaque "worker startup
                # failed" card. Does not wire the resolved `ProviderBinding` into
                # the worker itself — see this file's module docstring.
                # Issue #22 scope note (04-w5-interfaces.md §4's "model_override
                # 写入本 Turn 的 provider 解析", contract amendment recorded in
                # this branch's report "契约变更" section — `_run_turn` is now
                # one of N/#22's authorized touch points too (controller ruling
                # R-N4/R-N5, 2026-09-20 — it wasn't when this note was first
                # written), but there was no other place this read could happen
                # even before that): `retry()`'s optional per-Turn override, if
                # `send()` (called
                # from `retry()`) populated one for this exact turn_id, takes
                # priority over the Agent's own `model_pref`; popped (not just
                # read) so it can never leak into a later, unrelated Turn on the
                # same session reusing a stale dict entry.
                model_pref = self._turn_model_override.pop(turn_id, None) or await run_in_db_thread(
                    queries.get_agent_model_pref, self.ctx.db, session["agent_id"]
                )
                try:
                    await run_in_db_thread(self.ctx.providers.resolve, model_pref)
                except (RpcError, ProviderNotConfiguredError) as exc:
                    message = exc.message if isinstance(exc, RpcError) else str(exc)
                    await self._terminate_run(
                        ctx_turn, kind="error", reason=f"provider_error: {message}"
                    )
                    return
                # Review round-2 finding #4 / controller ruling R-H4: resolve
                # `ctx.config.mcp_servers(project_id)` HERE, off the event
                # loop, and pass the already-resolved value into
                # `ensure_started` — `WorkerManager` itself never touches a
                # `ConfigResolver` again (see `ensure_started`'s docstring for
                # why calling it directly on the event loop reproducibly threw
                # `sqlite3.ProgrammingError` against every real, non-test-double
                # `ConfigResolver` and made FR13's MCP wiring dead on the
                # production path despite every test passing).
                try:
                    mcp_servers = await run_in_db_thread(
                        self.ctx.config.mcp_servers, session["project_id"]
                    )
                except Exception as exc:  # noqa: BLE001 - a broken mcp.json must not
                    # block the worker from starting at all (DEV.md 工程原则 #4:
                    # 诚实失败 — reported via `daemon.error`, not silently
                    # swallowed to a log line the way a previous round of this
                    # branch did; R-H4 "不允许只 warning"). The session still
                    # gets a worker with zero MCP tools rather than failing to
                    # start entirely over an unrelated config file.
                    logger.warning(
                        "failed to resolve mcp_servers for project; starting worker "
                        "with no MCP servers configured",
                        extra={"detail": {"project_id": session["project_id"]}},
                        exc_info=True,
                    )
                    mcp_servers = []
                    await self.ctx.server.broadcast_all(
                        "daemon.error",
                        {
                            "code": MCP_SERVER_DOWN,
                            "message": f"session {session_id}: failed to resolve this "
                            "project's configured MCP servers; starting with none",
                            "detail": {
                                "session_id": session_id,
                                "project_id": session["project_id"],
                                "error": str(exc),
                            },
                        },
                    )
                try:
                    worker = await self.worker_manager.ensure_started(
                        session_id, cwd=cwd, mcp_servers=mcp_servers
                    )
                except WorkerStartupError as exc:
                    await self._terminate_run(
                        ctx_turn, kind="error", reason=f"worker startup failed: {exc}"
                    )
                    return
                assert worker.client is not None and worker.acp_session_id is not None  # noqa: S101
                # Issue #39 (controller ruling, 2026-09-21): `stop()` may have
                # pinned `ctx_turn.termination_intent` while THIS Turn's
                # worker was still inside `ensure_started()` above (see that
                # method's own comment — it pins unconditionally now, not
                # only when a worker already exists) — by the time control
                # reaches here the worker IS ready, but the user's stop
                # already happened and must not be silently lost by prompting
                # the agent as if nothing happened. Check once, right here,
                # before ever calling `prompt()`: a pinned intent means some
                # active-termination path already claimed this Turn — send
                # the cancel now (skipped by `stop()` itself because the
                # worker wasn't ready when it ran) and go straight to
                # `_terminate_run`, exactly like every other claiming path
                # (`stop()` once the worker's ready, `_terminate_run_for_
                # exceeded_budget`, `_on_worker_crash`) already does.
                if ctx_turn.termination_intent is not None:
                    try:
                        await worker.client.cancel(worker.acp_session_id)
                    except Exception as exc:  # noqa: BLE001 - R-N13: same broad
                        # catch `stop()`/`_terminate_run_for_exceeded_budget`
                        # already use — a worker that crashed moments earlier
                        # can fail `AcpClient._send` with more than
                        # `(AcpProtocolError, AcpError)`; termination must
                        # still proceed regardless.
                        logger.warning(
                            "session/cancel notification failed for a Turn "
                            "already claimed before its worker became ready "
                            "(worker likely already gone)",
                            extra={"detail": {"session_id": session_id, "error": str(exc)}},
                        )
                    # These `kind`/`reason` arguments are only the honest
                    # fallback for the (never actually reachable here) case
                    # `ctx_turn.termination_intent` became `None` again by the
                    # time `_terminate_run` runs — the check above already
                    # confirmed it's set, so `_terminate_run` always adopts
                    # that pinned record wholesale instead (R-N20).
                    await self._terminate_run(
                        ctx_turn,
                        kind=ctx_turn.termination_intent.kind,
                        reason=ctx_turn.termination_intent.reason,
                    )
                    return
                try:
                    response = await worker.client.prompt(worker.acp_session_id, text)
                except (AcpError, AcpProtocolError) as exc:
                    # The worker may have streamed part of an assistant reply via
                    # `message.delta` before crashing/disconnecting — that text only
                    # lives in `ctx_turn` until finalized, so without this the DB
                    # (and `turn.messages` replay, 00-foundation.md §5/PRD FR06)
                    # would show an empty assistant message even though the user
                    # already saw real text stream past.
                    await self._finalize_streamed_messages(ctx_turn)
                    await self._terminate_run(
                        ctx_turn, kind="error", reason=f"ACP prompt failed: {exc}"
                    )
                    return
                await self._finalize_turn_success(ctx_turn, response)
            except Exception as exc:  # noqa: BLE001 - contract §7: any worker/subprocess
                # failure must become `run.terminated` or `daemon.error`, never a
                # silently-dropped task (this coroutine is never awaited by anyone
                # once scheduled via `asyncio.create_task` in `_start_turn`) — this
                # is the fail-closed backstop for failure modes the two narrower
                # `except` blocks above don't name (e.g. `_prepare_hermes_home`'s
                # filesystem errors surfacing as something other than
                # `WorkerStartupError`, `_cwd_for_project`'s `RpcError`, a DB write
                # failure). DEV.md 工程原则 #4 "诚实失败".
                logger.error(
                    "_run_turn failed with an unhandled exception",
                    exc_info=True,
                    extra={"detail": {"session_id": session_id, "turn_id": turn_id}},
                )
                await self._finalize_streamed_messages(ctx_turn)
                await self._terminate_run(
                    ctx_turn, kind="error", reason=f"unexpected error: {exc}"
                )
        finally:
            await self._advance_queue(session_id)

    async def _finalize_streamed_messages(self, ctx_turn: _TurnContext) -> None:
        """Persist whatever assistant/thinking text was streamed via `message.delta`
        so far and broadcast `message.completed` for it. Safe to call more than
        once or with nothing streamed yet (both message ids stay `None`, so this is
        a no-op) — every termination path (success, user stop, worker error) must
        call this before `_terminate_run`/`mark_run_completed`, or the streamed
        text a user already saw never reaches the `messages` table (00-foundation.md
        §5's replay source of truth, PRD FR06)."""
        if ctx_turn.assistant_message_id:
            row = await run_in_db_thread(
                queries.finalize_message, self.ctx.db, ctx_turn.assistant_message_id,
                kind="text", text=ctx_turn.assistant_text,
            )
            await self.ctx.server.broadcast(ctx_turn.session_id, "message.completed", row)
            ctx_turn.assistant_message_id = None
        if ctx_turn.thinking_message_id:
            row = await run_in_db_thread(
                queries.finalize_message, self.ctx.db, ctx_turn.thinking_message_id,
                kind="thinking", text=ctx_turn.thinking_text,
            )
            await self.ctx.server.broadcast(ctx_turn.session_id, "message.completed", row)
            ctx_turn.thinking_message_id = None

    async def _finalize_turn_success(
        self, ctx_turn: _TurnContext, response: dict[str, Any]
    ) -> None:
        await self._finalize_streamed_messages(ctx_turn)
        if response.get("stopReason") == "cancelled":
            # R-N16 (controller ruling, round-8, 2026-09-20): round-7's R-N11
            # made this an early `return` WITHOUT calling `_terminate_run` at
            # all whenever `ctx_turn.termination_intent` was already
            # `"budget"`/`"error"`, reasoning that the claiming path's own
            # `_terminate_run` call (`_terminate_run_for_exceeded_budget`/
            # `_on_worker_crash`) would handle the write instead. That opened
            # a WORSE race than the one it closed: `_run_turn`'s own `finally`
            # block calls `_advance_queue` immediately after this function
            # returns, on the SAME task, no `await` in between — but the
            # claiming path's own `_terminate_run` call runs on a SEPARATE
            # background task (the budget watchdog) and might not have
            # reached its write yet. `_advance_queue` would then read
            # `ctx_turn.terminated_kind` as still `None` — indistinguishable
            # from a normal completion — and auto-advance the queue instead
            # of suspending it (PRD 9.3), even though the eventual write a
            # moment later would have landed the correct `kind="budget"`.
            #
            # `_terminate_run` is the one necessary, sufficient path to that
            # write (see its own docstring) — so this now ALWAYS calls it
            # when a Turn comes back cancelled, unconditionally, with no
            # branch that skips it. `kind="user"` here is only ever the
            # honest default for the case nothing else has claimed this Turn
            # (`termination_intent is None`) — `_terminate_run` itself now
            # gives `ctx_turn.termination_intent` priority over this `kind`
            # argument when it's the one actually performing the write, so a
            # `"budget"`/`"error"` intent still lands as the correct kind
            # regardless of whether this call or the claiming path's own call
            # is the one that gets there first; whichever one loses that race
            # just hits `_terminate_run`'s existing idempotency guard.
            await self._terminate_run(ctx_turn, kind="user", reason="stopped by user")
            return
        await run_in_db_thread(
            queries.mark_run_completed, self.ctx.db, ctx_turn.run_id, ctx_turn.turn_id
        )

    async def _terminate_run(
        self,
        ctx_turn: _TurnContext,
        *,
        kind: str,
        reason: str,
        budget: dict[str, Any] | None = None,
    ) -> None:
        """R-N16 (controller ruling, round-8, 2026-09-20): the one and only
        in-process exit point that writes `runs.terminated_kind` — every
        active termination path (`stop()`/`_terminate_run_for_exceeded_
        budget`/`_on_worker_crash`/`_run_turn`'s own exception handlers)
        reaches `terminated_kind` exclusively by calling this method; none of
        them ever writes it directly.

        R-N22 (controller ruling, round-9, 2026-09-20): `sessions/queries.py
        ::interrupt_stale_runs` (PRD 11.3 崩溃恢复) is a DIFFERENT, narrower
        path that also writes `runs.terminated_kind='error'` directly via raw
        SQL, deliberately bypassing this method entirely — that is not a gap
        this method's "sole exit point" claim overlooked. It runs at daemon
        *startup*, before this process's own event loop has any `ctx_turn`/
        `_active_turns` for the stale rows it's marking (they belong to the
        *previous* process, which is why they're stale) — there is no
        in-process `_TurnContext` to call this method with, and manufacturing
        a fake one would be worse than the raw SQL it would replace. The two
        paths' job is disjoint by construction: this method is the only exit
        point *within a running process*; cross-process restart recovery is
        the one path that runs only at startup, outside that process's own
        Run lifecycle altogether.
        """
        # `budget` (R-N5, controller ruling 2026-09-20): the `{name, used, limit,
        # unit}` PRD 9.3's 预算终止 row requires ("显式卡片说明是哪个预算、用了
        # 多少、上限多少") — only `_handle_tool_call_start`'s new Step-count/
        # duration-cap call sites pass this (they're the only callers with real
        # numbers to hand); every other `kind="budget"` path (a provider
        # quota/rate-limit `reason` string classified by `errors/classify.py`)
        # still has none to give, same as before this round — see `ErrorCard.
        # budget`'s own docstring in that module.
        #
        # FR06 回放 "终止记录" (02-w3-interfaces.md §2): `ctx_turn.step_seq` is the
        # seq of the last Step started on this Run (0 if none ever started) —
        # exactly "终止时的 step_seq", so replay can show which Step was in
        # flight (or that none had started yet) when the Run ended.
        step_seq = ctx_turn.step_seq or None

        # Idempotency guard (Issue #22): `_on_worker_crash`'s N07 watchdog can, in
        # a narrow race, force-terminate a Run in the same window the in-flight
        # `prompt()` call's own `except (AcpError, AcpProtocolError)` branch was
        # about to do the same thing on its own (see `_on_worker_crash`'s
        # docstring note) — rather than add cross-coroutine locking for a race
        # this narrow, make the second call a documented no-op for the DB
        # write: a Run already out of `'running'` status has already had that
        # write happen once, and writing over it again would be the actual
        # bug, not this check.
        #
        # Round-N2 review fix (#1, controller ruling R-N1): "already written"
        # and "already broadcast" are NOT the same fact, and treating them as
        # one (the old code below this comment used to `return` unconditionally
        # here) could drop `run.terminated` entirely. The two racing callers
        # for the same Run are always "the in-flight `prompt()` call's own
        # termination" and "`_on_worker_crash`'s N07 watchdog" — if the FIRST
        # one to reach `mark_run_terminated` gets `task.cancel()`-ed by the
        # watchdog while genuinely blocked inside that DB call (a real
        # `ThreadPoolExecutor` future on `store/db.py`'s single worker thread —
        # cancelling the awaiting asyncio Task can't stop a callable already
        # running on that thread; the UPDATE keeps going to completion and
        # commits), the cancelled coroutine gets `CancelledError` delivered
        # right at that await and never reaches its own broadcast a few lines
        # below. The watchdog's own subsequent `_terminate_run` call would then
        # read `status != 'running'` here — under the old code, that alone was
        # treated as "already reported, nothing to do", so `run.terminated`
        # broadcasts zero times and the UI is stuck on "运行中" forever
        # (recoverable only by restarting the daemon; exactly what N07 exists
        # to prevent).
        #
        # Tracking which `run_id`s have actually been broadcast (not just
        # written) separately closes this: a duplicate call for a `run_id`
        # already in that set is a true no-op (something really did broadcast
        # already); one that isn't rebuilds the `run.terminated` payload from
        # the persisted row and broadcasts it now, guaranteeing at least one
        # broadcast for this Run no matter which of the two racing callers'
        # writes landed first or which one got cancelled in between.
        #
        # Round-N2 review fix (#3, "新问题"): `runs.status` has THREE non-
        # 'running' values, not two — `mark_run_completed` (a normal, successful
        # end of Turn) also flips it to 'completed', and a successfully
        # completed Run is never added to `_terminated_broadcast_run_ids` (it
        # never broadcasts `run.terminated` at all). The rebroadcast branch
        # above therefore has to be scoped to 'terminated' specifically — a
        # bare `status != 'running'` check would treat every ordinary
        # completion that reaches this guard (e.g. `_on_worker_crash`'s N07
        # watchdog racing a Turn that finished successfully a moment earlier)
        # as "written but never broadcast" and rebuild a `run.terminated` payload
        # from a row whose `terminated_kind`/`terminated_reason` are NULL —
        # `classify.classify(kind_hint=None, reason="")` falls through to
        # `ErrorKind.INTERNAL`, faking an "internal error" termination card
        # onto a Run that actually finished fine.
        existing = await run_in_db_thread(queries.get_run, self.ctx.db, ctx_turn.run_id)
        if existing is not None and existing["status"] != "running":
            if existing["status"] == "terminated" and existing["terminated_kind"]:
                if ctx_turn.run_id in self._terminated_broadcast_run_ids:
                    logger.debug(
                        "_terminate_run called for a Run that's already terminated AND "
                        "broadcast; skipping the duplicate DB write/broadcast",
                        extra={
                            "detail": {"run_id": ctx_turn.run_id, "status": existing["status"]}
                        },
                    )
                    return
                logger.warning(
                    "_terminate_run called for a Run whose DB write already landed but "
                    "was never broadcast (the writer's own coroutine likely got "
                    "cancelled between its DB write and its broadcast — R-N1); "
                    "rebroadcasting from the persisted row instead of dropping "
                    "run.terminated",
                    extra={"detail": {"run_id": ctx_turn.run_id, "status": existing["status"]}},
                )
                await self._rebroadcast_terminated_run(existing)
                return
            # Not a rebroadcast candidate — either the Run completed normally
            # (`status == 'completed'`) or it's 'terminated' but somehow
            # missing `terminated_kind` (shouldn't happen; `mark_run_terminated`
            # always writes it, but this is not the row to guess a fake kind
            # for). Same no-op as the pre-R-N1 guard: the DB write already
            # happened once, writing over it again would be the actual bug.
            logger.debug(
                "_terminate_run called for a Run that's no longer running and isn't a "
                "terminated-but-unbroadcast rebroadcast candidate; skipping without a "
                "duplicate write",
                extra={"detail": {"run_id": ctx_turn.run_id, "status": existing["status"]}},
            )
            return

        # R-N16 (controller ruling, round-8, 2026-09-20): from here on this
        # call IS the one that actually performs the write (the idempotency
        # guard above didn't return) — `ctx_turn.termination_intent`, pinned
        # by whichever active-termination path (`stop()`/`_terminate_run_
        # for_exceeded_budget`/`_on_worker_crash`) actually claimed this Turn
        # BEFORE it sent its ACP cancel notification (see that field's own
        # comment), takes priority over the caller's own arguments for
        # deciding what gets recorded. This is what makes the outcome no
        # longer depend on which of two racing `_terminate_run` calls for the
        # same Turn gets here first: `_finalize_turn_success` now always
        # calls this method with `kind="user"` on a cancelled `stopReason`
        # (R-N16), so without this override, whichever call happened to win
        # that race would decide what gets recorded — round-6's original bug,
        # just moved to a different call site. `termination_intent is None`
        # (a Turn nothing has claimed — an honest, un-prompted user stop) is
        # the only case that falls back to the caller's own arguments.
        #
        # R-N20 (controller ruling, round-9, 2026-09-20): R-N16 only read
        # `kind` off the pinned intent, leaving `reason`/`card`/`step_seq`
        # still whatever the WINNING call happened to pass — when that was
        # `_finalize_turn_success`'s own generic `reason="stopped by user"`
        # call, the persisted `kind` came out right but the reason/card a
        # user actually sees stayed the hard-coded "用户终止" ones, no budget
        # numbers included, even for a Run correctly recorded as
        # `kind="budget"`. `ctx_turn.termination_intent` is now the whole
        # `_TerminationIntent` record (built by the claiming path itself,
        # before it sent its cancel notification — see that dataclass's own
        # comment) and, when present, is adopted wholesale: none of this
        # method's own `kind`/`reason`/`budget`/`step_seq` arguments are
        # consulted at all in that case.
        intent = ctx_turn.termination_intent
        if intent is not None:
            effective_kind = intent.kind
            reason = intent.reason
            card = intent.card
            step_seq = intent.step_seq
        # Issue #22 (04-w5-interfaces.md §4): "run.terminated 的 card 字段统一用
        # ErrorCard". kind="user" (`_finalize_turn_success`'s own call site,
        # untouched by this branch) never goes through `ErrorKind` — PRD 9.3
        # doesn't call a user-initiated stop an "error", and none of the three
        # card actions apply to it (see `classify.build_user_card`'s docstring).
        elif classify.is_user_stop(kind):
            card = classify.build_user_card(reason)
            effective_kind = "user"
        else:
            last_step = await run_in_db_thread(_last_step_for_run, self.ctx.db, ctx_turn.run_id)
            error_kind = classify.classify(
                kind_hint=kind,
                reason=reason,
                last_step_status=last_step["status"] if last_step else None,
            )
            card = classify.build_card(error_kind, reason=reason, step_seq=step_seq, budget=budget)
            # PRD 9.3 classifies "远端 API Key 额度受限/被限流" as a 预算终止,不是
            # an 错误终止 — this can upgrade the outer `kind` from the caller's
            # own "error" to "budget" purely from the reason text, since no
            # existing call site has enough context to know that ahead of time.
            effective_kind = classify.terminated_kind_for(kind, error_kind)

        # R-N4 (controller ruling, 2026-09-20; PRD 9.3 "终止不清空 Session 队列；
        # 队列中的后续指令挂起，等用户决定继续或清空", applying to all three outer
        # kinds): recorded on `ctx_turn` itself — not a local variable — so
        # `_advance_queue` can read it off the *same* `_TurnContext` object once
        # this Turn's `finally` block reaches it, without a second DB round
        # trip. Set synchronously, before the `await` that actually persists it
        # a few lines below: a caller of this method that gets `task.cancel()`-
        # ed by another coroutine (the N07 crash watchdog; this round's new
        # Step/时长-上限 checks in `_handle_tool_call_start`) can only be
        # interrupted at an `await` point, so this assignment — with zero
        # `await` between computing `effective_kind` and here — always lands
        # before any such cancellation could ever reach it. That is also why a
        # later `_rebroadcast_terminated_run` call (a *different* call site,
        # `existing["status"] == "terminated"` already) never needs to set this
        # itself: whichever call actually got the DB write to land already set
        # it first.
        ctx_turn.terminated_kind = effective_kind

        # R-N9 (controller ruling, round-6, 2026-09-20; PRD 9.3): persist the
        # same fact `ctx_turn.terminated_kind` above just recorded in memory —
        # `_advance_queue`'s `queue.changed` broadcast (R-N4) already computes
        # "suspended" from exactly this value, but that broadcast is fired
        # once and never replayed; a renderer that reloads or switches
        # sessions away and back had nothing durable to rebuild "已暂停" +
        # "继续" from (round-6 review finding). `queries.set_queue_suspended_
        # reason`'s own docstring has the full read/write map; the matching
        # clear lives in `_run_turn` (every Turn that starts, however it got
        # started, is definitionally not a suspended queue) and in `retry()`'s
        # `abandon` branch (which already broadcasts `suspended:false`).
        await run_in_db_thread(
            queries.set_queue_suspended_reason, self.ctx.db, ctx_turn.session_id, effective_kind
        )

        # Round-1 review fix (#6): `card.message`/`card.raw_excerpt` above are
        # built from a redacted copy of `reason`, but this function then used
        # to pass the raw `reason` on to both `mark_run_terminated` (persisted
        # verbatim into `runs.terminated_reason`, later included in
        # `session.export`) and the `run.terminated` broadcast — a provider
        # exception that echoes a full key back (exactly the case
        # `redact_secrets` exists for, G03/N02) leaked it through both of
        # those, unredacted, even though the card sitting right next to it in
        # the same payload was clean. Redact once, reuse everywhere outside
        # the card.
        safe_reason = classify.redact_secrets(reason)

        await run_in_db_thread(
            queries.mark_run_terminated,
            self.ctx.db,
            ctx_turn.run_id,
            ctx_turn.turn_id,
            kind=effective_kind,
            reason=safe_reason,
            terminated_step_seq=step_seq,
        )
        await self.ctx.server.broadcast(
            ctx_turn.session_id,
            "run.terminated",
            {
                "run_id": ctx_turn.run_id,
                # Added by this branch (04-w5-interfaces.md §4's `session.retry`
                # needs a turn_id to retry, and `run.terminated`'s payload — 00-
                # foundation.md §4.2, updated by this branch's report — never
                # carried one) — the renderer's termination card is the only
                # place a user can act on a specific failed Turn, so it needs
                # this to call `session.retry` at all.
                "turn_id": ctx_turn.turn_id,
                "kind": effective_kind,
                "reason": safe_reason,
                "card": card.to_dict(),
            },
        )
        # R-N1: mark this Run as actually broadcast, not just written — see the
        # idempotency guard above.
        self._terminated_broadcast_run_ids.add(ctx_turn.run_id)

    async def _rebroadcast_terminated_run(self, existing: dict[str, Any]) -> None:
        """R-N1 (controller ruling, round-N2): rebuild and broadcast
        `run.terminated` from an already-persisted `runs` row — used only by
        `_terminate_run`'s idempotency guard, for a Run whose DB write landed
        but whose broadcast never happened (the coroutine that wrote it got
        cancelled before reaching its own broadcast call).

        `existing["terminated_kind"]`/`["terminated_reason"]` are the *outer*
        `kind` (`"user"|"error"|"budget"`) and the already-redacted `reason`
        `_terminate_run` itself persisted — this reclassifies from them
        directly (this coroutine is a different call site than the one that
        actually wrote the row, so it never had the original caller's raw
        `kind`/`reason` to begin with; the race means it can't).

        One known, accepted loss from that: if the original write upgraded a
        `PROVIDER_QUOTA` classification to the outer `"budget"` kind
        (`classify.terminated_kind_for`), this rebuild's `card.kind` comes
        back as `"budget"` rather than the finer `"provider_quota"`
        (`classify.classify()` trusts an explicit `kind_hint="budget"`
        outright) — a slightly different title/icon than the original
        broadcast would have shown, for this one reconstructed broadcast only.
        Not worth widening `runs`' persisted shape to carry the pre-upgrade
        `ErrorKind` just for a race this narrow: the outer `kind`/`actions`/
        `retryable` a user actually acts on are unaffected (`BUDGET` and
        `PROVIDER_QUOTA` share the same `("abandon",)` action set).

        Defensive: the caller (`_terminate_run`'s idempotency guard) already
        only reaches this for `existing["status"] == "terminated"` rows with a
        non-empty `terminated_kind` (round-N2 review fix #3 — a `'completed'`
        row has neither, and faking a kind for it would broadcast a bogus
        `run.terminated` onto a Run that actually finished fine). This function
        asserts that precondition itself rather than trusting every future
        caller to keep re-checking it.
        """
        run_id = existing["id"]
        reason = existing["terminated_reason"] or ""
        step_seq = existing["terminated_step_seq"]
        outer_kind = existing["terminated_kind"]
        if not outer_kind:
            logger.error(
                "_rebroadcast_terminated_run called for a run with no "
                "terminated_kind — refusing to fake one; this indicates a bug "
                "in the caller's status check, not a real termination to report",
                extra={"detail": {"run_id": run_id, "status": existing["status"]}},
            )
            return
        if classify.is_user_stop(outer_kind):
            card = classify.build_user_card(reason)
        else:
            last_step = await run_in_db_thread(_last_step_for_run, self.ctx.db, run_id)
            error_kind = classify.classify(
                kind_hint=outer_kind,
                reason=reason,
                last_step_status=last_step["status"] if last_step else None,
            )
            card = classify.build_card(error_kind, reason=reason, step_seq=step_seq)
        await self.ctx.server.broadcast(
            existing["session_id"],
            "run.terminated",
            {
                "run_id": run_id,
                "turn_id": existing["turn_id"],
                "kind": outer_kind,
                "reason": reason,
                "card": card.to_dict(),
            },
        )
        self._terminated_broadcast_run_ids.add(run_id)

    async def _advance_queue(self, session_id: str) -> None:
        # Deliberately NOT called from `startup()` — this is what keeps normal
        # queue auto-advance (PRD 9.2) and "restart never auto-sends" (PRD 9.2/
        # G10/N04) from being the same code path: a restart marks Runs
        # "terminated" via `interrupt_stale_runs`, it never calls this, so queued
        # items simply stay `pending` until a user explicitly re-sends.
        #
        # Everything that decides "is this session still running" must happen
        # under `self._lock(session_id)` — the same lock `send()` takes to decide
        # immediate-run vs. enqueue. Without it, a `send()` racing in right as a
        # Turn finishes can read `_turn_tasks[session_id]` as "not done yet" (the
        # finishing task hasn't returned from this very function yet), enqueue
        # behind it, and then find this function already committed to "nothing
        # left to pop" and returned — nothing will ever call `_advance_queue`
        # again for that item, so it stays `pending` forever. Round-1 review fix;
        # see this PR report's "第 1 轮修复记录" for the reproduction.
        async with self._lock(session_id):
            finished_turn = self._active_turns.pop(session_id, None)
            self.worker_manager.mark_busy(session_id, False)
            # R-N4 (controller ruling, 2026-09-20; PRD 9.3 "终止不清空 Session
            # 队列；队列中的后续指令挂起，等用户决定继续或清空" — applying to all
            # three outer termination kinds, user/error/budget, not just
            # error/budget): `_terminate_run` records the outer `kind` onto
            # `ctx_turn.terminated_kind` the moment it actually terminates a
            # Run — see that method's own comment for why reading it here,
            # off the just-finished Turn's own `_TurnContext`, is safe with no
            # extra synchronization. `None` means this Turn reached here via
            # `_finalize_turn_success`'s normal `mark_run_completed` path
            # (never touches `terminated_kind`) — the only case that still
            # auto-advances.
            #
            # R-N10 (controller ruling, round-6, 2026-09-20): the gap that used
            # to be documented here — the N07 crash watchdog (`_on_worker_
            # crash`) calling `task.cancel()` BEFORE its own `_terminate_run`
            # call, letting a just-cancelled `_run_turn` task reach this
            # function's `terminated_kind` read before the watchdog got back
            # around to setting it — is closed: `_on_worker_crash` (now one of
            # this round's authorized touch points) reordered its own
            # finalize/terminate/cancel calls to match `_terminate_run_for_
            # exceeded_budget`'s (R-N7) shape, terminate-then-cancel. Every
            # path that can set `finished_turn.terminated_kind` now fully
            # `await`s `_terminate_run` (which sets it synchronously, before
            # any of its own awaits — see that method's own comment) before
            # ever cancelling this Turn's task, so this read can no longer
            # observe a termination that "hasn't set it yet".
            suspended_kind = finished_turn.terminated_kind if finished_turn is not None else None
            if suspended_kind is not None:
                # Suspended: leave every pending `queue_items` row exactly as
                # it is (still 'pending') — nothing gets popped. Still have to
                # do the SAME "is this session still running" bookkeeping the
                # non-suspended branch below does (this function's own header
                # comment: that decision, and everything that follows from
                # it, must happen under this lock) — just without the
                # pop+start half of it.
                current = self._turn_tasks.get(session_id)
                if current is asyncio.current_task():
                    del self._turn_tasks[session_id]
            else:
                item = await run_in_db_thread(queries.pop_next_queue_item, self.ctx.db, session_id)
                if item is not None:
                    # No explicit "mark this queued Turn running" write here: `_run_turn`
                    # (via `queries.create_run`) already flips `turns.status` to
                    # 'running' as soon as it starts, so a separate call here would
                    # just be the same write twice.
                    self._start_turn(session_id, item["turn_id"], item["text"])
                else:
                    # Nothing more queued: drop *this* (finishing) task's own
                    # registration now, while still holding the lock, instead of
                    # waiting for its `add_done_callback` cleanup to fire after this
                    # coroutine returns — a `send()` blocked on the same lock must see
                    # "not running" the instant we've committed to "nothing else is
                    # coming", not some fraction of a second later once the task
                    # object itself transitions to done().
                    current = self._turn_tasks.get(session_id)
                    if current is asyncio.current_task():
                        del self._turn_tasks[session_id]
        items = await run_in_db_thread(queries.list_queue_items, self.ctx.db, session_id)
        await self.ctx.server.broadcast(
            session_id,
            "queue.changed",
            {
                "session_id": session_id,
                "items": items,
                "suspended": suspended_kind is not None,
                "reason": suspended_kind,
            },
        )

    # -- ACP event handlers (bound into WorkerManager at construction) ------------------

    async def _on_session_update(self, session_id: str, params: dict[str, Any]) -> None:
        ctx_turn = self._active_turns.get(session_id)
        if ctx_turn is None:
            logger.debug(
                "session/update with no active turn, ignoring",
                extra={"detail": {"session_id": session_id}},
            )
            return
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            await self._handle_text_delta(ctx_turn, update, thinking=False)
        elif kind == "agent_thought_chunk":
            await self._handle_text_delta(ctx_turn, update, thinking=True)
        elif kind == "tool_call":
            await self._handle_tool_call_start(ctx_turn, update)
        elif kind == "tool_call_update":
            await self._handle_tool_call_update(ctx_turn, update)
        # plan / available_commands_update / current_mode_update / config_option_update
        # / session_info_update / usage_update: no RPC v0 notification or schema
        # column exists for these yet (see module docstring's scope-gap note) —
        # dropping them here is a deliberate, documented gap, not a silent one.

    async def _handle_text_delta(
        self, ctx_turn: _TurnContext, update: dict[str, Any], *, thinking: bool
    ) -> None:
        text = _extract_text(update.get("content"))
        if not text:
            return
        if thinking:
            if ctx_turn.thinking_message_id is None:
                ctx_turn.thinking_message_id = new_ulid()
                await run_in_db_thread(
                    queries.insert_assistant_message, self.ctx.db,
                    message_id=ctx_turn.thinking_message_id, session_id=ctx_turn.session_id,
                    turn_id=ctx_turn.turn_id, kind="thinking",
                )
            ctx_turn.thinking_text += text
            message_id = ctx_turn.thinking_message_id
        else:
            if ctx_turn.assistant_message_id is None:
                ctx_turn.assistant_message_id = new_ulid()
                await run_in_db_thread(
                    queries.insert_assistant_message, self.ctx.db,
                    message_id=ctx_turn.assistant_message_id, session_id=ctx_turn.session_id,
                    turn_id=ctx_turn.turn_id, kind="text",
                )
            ctx_turn.assistant_text += text
            message_id = ctx_turn.assistant_message_id
        await self.ctx.server.broadcast(
            ctx_turn.session_id,
            "message.delta",
            {
                "session_id": ctx_turn.session_id,
                "turn_id": ctx_turn.turn_id,
                "message_id": message_id,
                "delta": text,
            },
        )

    async def _handle_tool_call_start(
        self, ctx_turn: _TurnContext, update: dict[str, Any]
    ) -> None:
        # R-N5 (controller ruling, 2026-09-20; PRD 11.2 "单个 Run 最大 Step 数
        # 200"): this fires on every ACP `tool_call` "started" event — this
        # branch's only authorized touch point that sees each one as it
        # happens. Checked BEFORE incrementing `ctx_turn.step_seq`/inserting a
        # `steps` row for this call — a call that trips the cap never gets
        # counted or persisted as a Step; `runs.terminated_step_seq` (below,
        # via `_terminate_run`) stays at the last Step that actually ran.
        #
        # R-N8 (controller ruling, round-6, 2026-09-20): the 时长上限 half of
        # this check USED to live here too — removed. Tying a duration check
        # to `tool_call` events meant a Turn with zero tool calls (pure text
        # generation) had no checkpoint to catch a duration overrun on at all
        # (R-N5's own report flagged this as a known scope gap; round-6 review
        # said it can't stay one). `_check_run_durations` (driven by
        # `_run_budget_watchdog_loop`, started from `startup()`) now scans
        # `_active_turns` directly on a resident timer instead, independent of
        # any ACP event — see that method's own docstring.
        if ctx_turn.step_seq + 1 > ctx_turn.max_steps_per_run:
            await self._terminate_run_for_exceeded_budget(
                ctx_turn,
                name="单个 Run 最大 Step 数",
                used=ctx_turn.step_seq + 1,
                limit=ctx_turn.max_steps_per_run,
                unit="步",
                reason=f"exceeded max_steps_per_run ({ctx_turn.max_steps_per_run} 步)",
            )
            return
        tool_call_id = update.get("toolCallId")
        step_id = new_ulid()
        ctx_turn.step_seq += 1
        ctx_turn.step_seq_by_id[step_id] = ctx_turn.step_seq
        if tool_call_id:
            ctx_turn.tool_call_steps[tool_call_id] = step_id
            ctx_turn.step_started_at[step_id] = time.monotonic()
        # Issue #13 (FR07 diff-in-Step): an auto-approved write_file/patch's
        # "started" event carries the diff directly (see
        # `_extract_diff_content`'s docstring for why this, not the
        # completion event, is where it lives) — stash it now, merged into
        # the Step by `_handle_tool_call_update` once that call completes.
        # The needs-approval path's diff arrives separately, via
        # `_on_request_permission`'s own `tool_call.content` (a DIFFERENT
        # synthetic `toolCallId` Hermes's edit-approval channel invents —
        # see that method's docstring), stashed there instead; the two
        # paths are mutually exclusive per call (verified against the
        # installed checkout: `edit_approval.py`'s auto-approve check runs
        # BEFORE it would ever send a `session/request_permission` at all).
        diff = _extract_diff_content(update.get("content"))
        if diff is not None:
            ctx_turn.step_diffs[step_id] = diff
        # ACP's ToolCall schema carries a human-readable `title`, not a raw tool
        # name (see kernel/acp_client.py's docstring evidence) — `title` is the
        # closest honest value for the `tool` column until Hermes's wire schema
        # exposes one, not a made-up name.
        tool_label = update.get("title") or tool_call_id or "tool"
        status = update.get("status") or "pending"
        row = await run_in_db_thread(
            queries.insert_step, self.ctx.db, step_id=step_id, run_id=ctx_turn.run_id,
            seq=ctx_turn.step_seq, tool=tool_label, args=update.get("rawInput"), status=status,
        )
        await self.ctx.server.broadcast(ctx_turn.session_id, "step.started", row)

    async def _terminate_run_for_exceeded_budget(
        self,
        ctx_turn: _TurnContext,
        *,
        name: str,
        used: float,
        limit: float,
        unit: str,
        reason: str,
    ) -> None:
        """R-N5 (controller ruling, 2026-09-20): shared by `_handle_tool_call_
        start`'s Step-count check and `_check_run_durations`'s resident 时长
        上限 poll (R-N8, round-6 — the duration half of this used to live in
        `_handle_tool_call_start` too, see that method's own comment for why
        it moved off ACP events entirely) to actually stop the Run —
        `_terminate_run` alone only writes `runs`/broadcasts `run.terminated`,
        it never stops anything by itself.

        R-N7 (controller ruling, round-6, 2026-09-20): cancelling only
        `_run_turn`'s own LOCAL asyncio task used to be the whole story here
        — round-5 review, correctly: "取消 _run_turn 只是放弃等待
        prompt()，worker 里的 agent 照跑不误、继续调工具" — the worker
        process's own agent loop never learned anything happened and kept
        calling tools/burning the budget underneath a UI that already showed
        a terminated Run (exactly the "看起来已死、其实还在跑" PRD 9.3
        forbids). Fixed by walking the SAME path `stop()` (a user-initiated
        termination) already uses: a real ACP `session/cancel` notification
        to the worker FIRST (so Hermes actually stops the in-flight Turn),
        THEN `_terminate_run` (DB write + broadcast; also sets `ctx_turn.
        terminated_kind` — R-N4 needs that landed before `_advance_queue`
        reads it from this Turn's own `finally` block), and only THEN the
        local task cancel. A `cancel()` failure (worker already dead, or
        unreachable) is logged and does NOT block the rest of this — same
        broad `except Exception` shape `stop()` also uses (R-N13, controller
        ruling round-7, 2026-09-20 — widened from the original `(AcpProtocol
        Error, AcpError)` because a worker that crashed moments earlier can
        fail `AcpClient._send` with a raw `ConnectionResetError` instead,
        which isn't either of those two); there's nothing left to notify and
        the termination must still proceed regardless.

        R-N11 (controller ruling, round-7, 2026-09-20): pins `ctx_turn.
        termination_intent` BEFORE sending that cancel notification — see
        `_TurnContext.termination_intent`'s own comment for the race this
        closes (this function runs on a background watchdog task, not
        `_run_turn`'s own — the read loop resolving `_run_turn`'s in-flight
        `prompt()` future with `stopReason:"cancelled"` can race ahead of
        this same call's own `_terminate_run` below).

        R-N20 (controller ruling, round-9, 2026-09-20): the pinned value now
        carries the full budget card (reason + `{name, used, limit, unit}`),
        not just `kind="budget"` — if `_finalize_turn_success`'s own generic
        call wins that race, `_terminate_run` must still record and broadcast
        the real budget reason/card, not the hard-coded "用户终止" one.

        Cancelling `_run_turn`'s own task LAST (not first) is still what
        guarantees its `finally: await self._advance_queue(...)` runs and
        unsticks the session's queue — `_terminate_run`'s idempotency guard
        (R-N1) makes any further termination this Run's own `_run_turn` might
        still attempt on its own a harmless no-op, not a second real one.

        Called from two different tasks depending on which check tripped:
        `_handle_tool_call_start`'s Step-count check runs inline on the ACP
        read-loop's own task (`kernel/acp_client.py::AcpClient._read_loop`'s
        `await self._on_session_update(params)` chain — see N07's own
        docstring for why that matters: no self-cancellation risk, and the
        read loop can't even read `_run_turn`'s own in-flight `prompt()`
        response until this whole call returns, so there's no race between
        this awaiting `_terminate_run` and that response independently
        resolving `_run_turn`'s own success/error paths first).
        `_check_run_durations`'s resident poll (R-N8) instead runs on its own
        background task (`_run_budget_watchdog_loop`, unrelated to any one
        Turn's worker) — `worker.client.cancel(...)` still targets THIS
        Turn's own worker/session by id regardless of which task calls it, so
        the same ordering guarantee holds either way.
        """
        # R-N11 (controller ruling, round-7, 2026-09-20): pin this BEFORE
        # sending the cancel notification below — see this method's own
        # docstring and `_TurnContext.termination_intent`'s own comment.
        # R-N20 (controller ruling, round-9, 2026-09-20): the pinned value is
        # the full record `_terminate_run` would itself compute for
        # `kind="budget"` with this same `reason`/`budget` dict — built here,
        # synchronously, with the same `classify` calls `_terminate_run` uses
        # when nothing has claimed the Turn (no DB round trip needed: an
        # explicit `kind_hint="budget"` is trusted outright by `classify.
        # classify()`, see its own docstring, so `last_step_status` is moot).
        budget_detail = {"name": name, "used": used, "limit": limit, "unit": unit}
        step_seq = ctx_turn.step_seq or None
        budget_error_kind = classify.classify(kind_hint="budget", reason=reason)
        ctx_turn.termination_intent = _TerminationIntent(
            kind=classify.terminated_kind_for("budget", budget_error_kind),
            reason=reason,
            card=classify.build_card(
                budget_error_kind, reason=reason, step_seq=step_seq, budget=budget_detail
            ),
            step_seq=step_seq,
        )
        worker = self.worker_manager.get(ctx_turn.session_id)
        if worker is not None and worker.client is not None and worker.acp_session_id is not None:
            try:
                # Same notification `stop()` sends for a user-initiated
                # termination (PRD 9.3) — see this method's own docstring
                # (R-N7) for why this must happen before `_terminate_run`.
                await worker.client.cancel(worker.acp_session_id)
            except Exception as exc:  # noqa: BLE001 - R-N13: see this method's
                # own docstring — a worker that just crashed can fail
                # `AcpClient._send` with more than `(AcpProtocolError,
                # AcpError)`; none of them should block finishing this
                # termination, only `CancelledError` still propagates.
                logger.warning(
                    "session/cancel notification failed while terminating for "
                    "exceeded budget (worker likely already gone)",
                    extra={"detail": {"session_id": ctx_turn.session_id, "error": str(exc)}},
                )
        # These arguments are only the honest fallback for the (never
        # actually reachable here) case `ctx_turn.termination_intent` is
        # `None` by the time this runs — the pin above already set it, so
        # `_terminate_run` always adopts the record above wholesale instead
        # (R-N20). Kept identical to that record rather than left stale, in
        # case a future refactor ever calls this with `termination_intent`
        # unset.
        await self._terminate_run(
            ctx_turn, kind="budget", reason=reason, budget=budget_detail
        )
        task = self._turn_tasks.get(ctx_turn.session_id)
        if task is not None and not task.done():
            task.cancel()

    async def _run_budget_watchdog_loop(self) -> None:
        """R-N8 (controller ruling, round-6, 2026-09-20; PRD 11.2 单个 Run 最
        大时长): a resident poll, started from `startup()`/cancelled+awaited
        from `shutdown()` — same shape as `replay/retention.py::
        run_sweep_loop`'s own background loop, not N07's per-crash deadline
        poll (`_on_worker_crash`), which exists for one specific race, not a
        general pattern. Exists because tying the duration check to
        `tool_call` events (as it lived before this round, in `_handle_
        tool_call_start`) never fired for a Turn that calls no tools at all —
        pure text generation could run past `max_run_duration_s` forever with
        no checkpoint to catch it (R-N5's own report's documented scope gap;
        round-6 review: this can't stay a documented gap).

        R-N12 (controller ruling, round-7, 2026-09-20): a single bad iteration
        (`_check_run_durations` raising) used to kill this whole background
        loop forever — same shape fix as `replay/retention.py::run_sweep_
        loop` already uses: catch and log any `Exception` per iteration so
        the NEXT scan still runs; only `CancelledError` (shutdown) may
        propagate out of the loop itself."""
        try:
            while True:
                await asyncio.sleep(_BUDGET_WATCHDOG_INTERVAL_S)
                try:
                    await self._check_run_durations()
                except Exception:  # noqa: BLE001 - one bad scan must not kill the loop forever
                    logger.error("budget watchdog iteration failed", exc_info=True)
        except asyncio.CancelledError:
            raise

    async def _check_run_durations(self) -> None:
        """The actual check `_run_budget_watchdog_loop` drives — split out so
        a test can call it directly against an injected `self._clock` (see
        `__init__`'s comment on that) instead of waiting real wall-clock
        seconds for the loop's own `asyncio.sleep`. Snapshots `_active_turns.
        items()` into a list before iterating: `_terminate_run_for_exceeded_
        budget` awaits (DB writes, an ACP notification, `_terminate_run`'s own
        broadcast) that can let another coroutine — most notably this same
        Turn's own `_advance_queue`, once its task gets cancelled — mutate
        `_active_turns` while this loop is still walking it."""
        now = self._clock()
        for _session_id, ctx_turn in list(self._active_turns.items()):
            if ctx_turn.started_at is None:
                # This `_TurnContext` never went through `_run_turn`'s own
                # assignment (test scaffolding building one directly, see
                # that field's own comment) — no duration budget to enforce
                # against an unknown start time.
                continue
            elapsed_s = now - ctx_turn.started_at
            if elapsed_s > ctx_turn.max_run_duration_s:
                await self._terminate_run_for_exceeded_budget(
                    ctx_turn,
                    name="单个 Run 最大时长",
                    used=round(elapsed_s),
                    limit=int(ctx_turn.max_run_duration_s),
                    unit="秒",
                    reason=f"exceeded max_run_duration_s ({ctx_turn.max_run_duration_s:.0f}s)",
                )

    async def _handle_tool_call_update(
        self, ctx_turn: _TurnContext, update: dict[str, Any]
    ) -> None:
        tool_call_id = update.get("toolCallId")
        step_id = ctx_turn.tool_call_steps.get(tool_call_id) if tool_call_id else None
        if step_id is None:
            logger.warning(
                "tool_call_update for an unknown tool_call_id, ignoring",
                extra={"detail": {"session_id": ctx_turn.session_id, "tool_call_id": tool_call_id}},
            )
            return
        status = update.get("status")
        raw_output = update.get("rawOutput")
        duration_ms = None
        started = ctx_turn.step_started_at.get(step_id)
        if started is not None and status in ("completed", "failed"):
            duration_ms = int((time.monotonic() - started) * 1000)
        # Issue #13 (FR07 diff-in-Step): the diff `_handle_tool_call_start`/
        # `_on_request_permission` stashed for this step (see `_TurnContext.
        # step_diffs`'s docstring) — popped, not peeked, so a step can only
        # ever attach its diff to ONE completion event even if Hermes were
        # to send more than one `tool_call_update` for the same
        # `toolCallId` (defensive; not observed in practice).
        diff = ctx_turn.step_diffs.pop(step_id, None)
        result_summary = None
        dumped_output: str | None = None
        if raw_output is not None or diff is not None:
            # Serialize exactly once. Round-1 review fix: this used to be
            # dumped twice — once here (truncated to 4000 chars for
            # `result_summary`) and again, independently, inside
            # `_write_step_payload` — and the second dump ran on *this* event
            # loop thread, before ever reaching `asyncio.to_thread` (CPython's C
            # json encoder holds the GIL throughout, so moving it into a thread
            # later doesn't give the loop a chance to run anything else while it
            # dumps). For a large tool `rawOutput` that roughly doubled the CPU
            # the ACP read loop pays per `tool_call_update` — exactly the cost
            # 02-w3-interfaces.md §3's "回放 payload 写入异步、不阻塞 ACP 读循环"
            # is about. `dumped_output` (the one dump) feeds both the truncated
            # summary and the full-fidelity payload below.
            #
            # `diff` only ever exists for `write_file`/`patch` (see
            # `_extract_diff_content`'s docstring) — wrapping `raw_output`
            # under a `"raw_output"` key ONLY when a diff is also present
            # keeps every other tool's `result_summary`/payload shape
            # byte-for-byte unchanged from before this branch (still the raw
            # `json.dumps(raw_output, ...)`), so nothing downstream that
            # parses an existing tool's `result_summary` (e.g. G/#12's replay
            # UI) needs to change for this addition.
            if diff is not None:
                to_dump: Any = {"diff": diff}
                if raw_output is not None:
                    to_dump["raw_output"] = raw_output
            else:
                to_dump = raw_output
            dumped_output = json.dumps(to_dump, default=str, ensure_ascii=False)
            result_summary = dumped_output[:4000]
        row = await run_in_db_thread(
            queries.update_step, self.ctx.db, step_id,
            status=status, result_summary=result_summary, duration_ms=duration_ms,
        )
        if status in ("completed", "failed"):
            await self.ctx.server.broadcast(ctx_turn.session_id, "step.completed", row)
        if dumped_output is not None and status in ("completed", "failed"):
            # Scheduled, not awaited: `_on_session_update` runs inline on
            # `AcpClient._read_loop`'s await chain (kernel/acp_client.py awaits
            # `on_session_update` for every incoming line before reading the
            # next) — blocking here on disk I/O would stall the ACP read loop
            # itself (02-w3-interfaces.md §3 "回放 payload 写入异步、不阻塞 ACP
            # 读循环"). `_write_step_payload` logs and swallows its own failures
            # (DEV.md 工程原则 #4) since nothing awaits this task's result.
            seq = ctx_turn.step_seq_by_id.get(step_id, ctx_turn.step_seq)
            task = asyncio.create_task(
                self._write_step_payload(ctx_turn.run_id, step_id, seq, dumped_output)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def _write_step_payload(
        self, run_id: str, step_id: str, seq: int, dumped_output: str
    ) -> None:
        """FR06 回放 full-fidelity Step payload (02-w3-interfaces.md §2) —
        background write scheduled by `_handle_tool_call_update`, see its
        docstring comment for why this must never be awaited inline there.

        Takes the already-`json.dumps`-ed text (not the raw object) — see that
        same comment for why this must not re-serialize it a second time."""
        try:
            data = dumped_output.encode("utf-8")
            ref = await asyncio.to_thread(
                replay_store.write_payload, self.ctx.paths.user_root(), run_id, seq, data, "json"
            )
            await run_in_db_thread(
                queries.update_step, self.ctx.db, step_id,
                status=None, result_summary=None, duration_ms=None, payload_ref=ref,
            )
        except Exception:  # noqa: BLE001 - a background write must never vanish
            # silently (DEV.md 工程原则 #4) — nothing else awaits this task or
            # otherwise observes its outcome, so this `except` is the only place
            # a failure here can possibly be reported.
            logger.error(
                "failed to write full Step payload for replay",
                exc_info=True,
                extra={"detail": {"run_id": run_id, "step_id": step_id, "seq": seq}},
            )

    async def _decide_terminal_like_permission(
        self,
        *,
        session_id: str,
        decision_id: str,
        step_id: str | None,
        tool_args: dict[str, Any],
        mode: str,
        risk: review.Risk,
        params: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Controller ruling R10 (round 6, final, not overturnable): the
        daemon is now the ONLY place a terminal-class tool call (`kernel/
        plugin/jones_gate::TERMINAL_LIKE_TOOLS`) can ever be allowed to run
        without a human — the rule gate never returns `allow` for these any
        more (see that package's `_decide` docstring), only `block`/
        `approve`. Every such call that reaches here is therefore
        re-classified from scratch, in depth, before this function even
        considers auto-approving it:

          1. the SAME hard-deny scan the plugin already ran
             (`_hard_deny.classify_command`, re-read from the identical
             `jones_gate.json` snapshot the plugin used for this Turn — see
             `permissions/gate_config.py::read`'s docstring for why a fresh
             snapshot, not a re-read, would be the wrong thing here) — a
             defense-in-depth RECHECK, never a trust of whatever the plugin
             already decided. A hit here is `deny`, recorded and broadcast
             exactly like a real user rejection would be, never silently
             dropped.
          2. `transparency(command)` (`_transparency.classify`) must be
             `plain` — an `opaque` command can never auto-allow here
             regardless of anything else, mirroring the plugin's own R5
             invariant.
          3. only then does risk matter: `review.classify()` must also say
             `low` (round 6 makes that achievable again for a `terminal`
             call — see `permissions/review.py::_classify_terminal`'s
             docstring for why the old `medium` floor is gone).

        Auto-allow fires only when (2) and (3) both hold AND EITHER a
        `permissions.json`/`remember` rule's `match` normalizes to exactly
        this command text (`_rules.has_normalized_exact_allow` — the ONLY
        thing a terminal `allow` rule still means, now that the plugin
        can't act on it directly — see 02-w3-interfaces.md §1.1's "决策模型
        v6"), OR the session is in `auto` mode (already means "act without
        asking for anything the review gate doesn't flag" — no rule
        required). Every other combination returns `None`, falling through
        to the caller's normal pending/user-gate flow with the SAME
        `risk`/`decision_id` this function was handed — never
        re-classified twice.

        Only `terminal` has a real `command` argument today (`process_
        manage`/`execute_code` — Hermes's other two terminal-class tools,
        W4 scope — have no established arg shape here yet); for those,
        `tool_args.get("command")` is `None` and this function always
        returns `None` (defer to the caller), which is the conservative
        choice — `review.classify()` never classifies an unrecognized tool
        name `low` either (its own fail-closed catch-all), so they always
        land on the user gate regardless."""
        command = tool_args.get("command") if isinstance(tool_args, dict) else None
        if not isinstance(command, str) or not command:
            return None

        hermes_home = gate_config.hermes_home_for(self.ctx.paths.user_root(), session_id)
        snapshot = gate_config.read(hermes_home) or {}
        user_root = snapshot.get("user_root")
        project_permissions_path = snapshot.get("project_permissions_path")
        rules = snapshot.get("rules") if isinstance(snapshot.get("rules"), list) else []

        hard = _hard_deny.classify_command(
            command,
            user_root=user_root if isinstance(user_root, str) else None,
            project_permissions_path=(
                project_permissions_path if isinstance(project_permissions_path, str) else None
            ),
        )
        if hard.denied:
            await run_in_db_thread(
                queries.insert_permission_decision,
                self.ctx.db, decision_id=decision_id, step_id=step_id,
                gate="rule", risk=risk.level, request=params,
            )
            row = await run_in_db_thread(
                queries.decide_permission, self.ctx.db, decision_id,
                decision="deny", decided_by="rule",
            )
            await self.ctx.server.broadcast(session_id, "permission.decided", row)
            option_id = _select_permission_option(params.get("options") or [], "deny", None)
            return {"outcome": {"outcome": "selected", "optionId": option_id}}

        eligible = (
            _transparency.classify(command) == "plain"
            and risk.level == "low"
            and (_rules.has_normalized_exact_allow(rules, command) or mode == "auto")
        )
        if not eligible:
            return None

        await run_in_db_thread(
            queries.insert_permission_decision,
            self.ctx.db, decision_id=decision_id, step_id=step_id,
            gate="review", risk=risk.level, request=params,
        )
        row = await run_in_db_thread(
            queries.decide_permission, self.ctx.db, decision_id,
            decision="allow", decided_by="rule",
        )
        await self.ctx.server.broadcast(session_id, "permission.decided", row)
        option_id = _select_permission_option(params.get("options") or [], "allow", None)
        return {"outcome": {"outcome": "selected", "optionId": option_id}}

    async def _on_request_permission(
        self, session_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """The daemon-side half of FR05's ②③ gates (Issue #11,
        02-w3-interfaces.md §1.1). Every ACP `session/request_permission`
        the worker sends — regardless of which of Hermes's two independent
        approval paths produced it, see `_extract_tool_call`'s docstring —
        lands here. What happens next depends on the review gate's risk
        classification of the real tool call (recovered from whichever
        `rawInput` shape this request carries):

          - low risk AND the session is in `auto` OR `task` mode -> decide
            instantly (`decided_by="rule"` — v1's review gate is a
            deterministic rule, not a model, see `permissions/review.py`'s
            module docstring), no `permission.requested` broadcast, no
            pending-approval UI ever shown to the user. Review finding #4
            (2026-09-19): PRD 9.1's task-mode row is explicit —
            "允许；只读工具直接放行，改变外部世界的动作逐条走三道闸" — task
            mode gates every ACTION that changes something outside the
            session, not every tool call regardless of risk; a `chat`-mode
            session never reaches this function at all (`kernel/plugin/
            jones_gate` blocks every tool call before it can be escalated —
            N12), so there is no mode left for which a `low`-risk call
            (read-only, by `permissions/review.py::classify()`'s own
            definition of what earns that level) should still interrupt the
            user.
          - everything else (medium/high risk, in any mode that still
            reaches this function) -> the pre-existing W2 user-gate flow:
            write `permission_decisions(pending)`, broadcast
            `permission.requested` (now carrying the real classified risk
            instead of `"unclassified"`), and wait for `permission.decide` —
            bounded by `settings.approval_timeout_minutes` when configured
            (PRD 9.4: timeout can only ever resolve to `deny`, never an
            auto-allow) AND, for the `write_file`/`patch` edit-approval
            shape specifically, by Hermes's OWN hardcoded 60s timeout on
            that channel (see the `_EDIT_APPROVAL_HERMES_TIMEOUT_SECONDS`
            comment below — review finding #5).

        Round 6 (controller ruling R10, 2026-09-19, final, not overturnable)
        carves a THIRD, stricter branch out of the above for terminal-class
        tools (`kernel/plugin/jones_gate::TERMINAL_LIKE_TOOLS`): the rule
        gate no longer has an `allow` fast path for these at all (see that
        package's `_decide` docstring), so every one of them reaching this
        function is re-classified from scratch, in depth, by
        `_decide_terminal_like_permission` BEFORE either of the two
        branches above ever runs for it — see that method's own docstring
        for the exact eligibility rule. Its possible outcomes are `deny`
        (a hard-deny hit the plugin's own copy should already have caught —
        this is defense in depth, not trust of what the plugin decided),
        `allow` (only when provably safe AND either an exact matching
        `permissions.json`/`remember` rule exists or the session is in
        `auto` mode), or `None` — meaning "fall through to the pending/
        user-gate flow below exactly as if this branch didn't exist",
        reusing the SAME `risk`/`decision_id` already computed, never
        re-classifying twice.

        The MODE used for the branch above is deliberately not always a
        fresh database read (review finding #13, 2026-09-19): `_extract_
        tool_call`'s mode hint (decoded from the SAME `jones_gate.json`
        snapshot the rule gate read moments earlier, when this request's
        shape carries one) is preferred, falling back to this session's
        Turn-start snapshot (`_turn_mode_snapshot`, written by `send()`
        alongside `_refresh_gate_config` — see that dict's docstring in
        `__init__`) and only then to a live DB read (this function's own
        historical behavior, kept as the last resort for a request that
        somehow outlives having ever gone through either). Reading the
        live, possibly-since-changed `session["mode"]` unconditionally would
        let a mode change mid-Run (`set_mode()` has no "Run in flight" guard)
        retroactively LOOSEN a pending review-gate decision for an
        already-running Run — the DB read is a red herring for what actually
        matters here, since the rule gate that produced this request already
        made its own decision against the mode captured at Turn start.
        """
        decision_id = new_ulid()
        tool_call = params.get("toolCall") or {}
        tool_call_id = tool_call.get("toolCallId")
        ctx_turn = self._active_turns.get(session_id)
        step_id = ctx_turn.tool_call_steps.get(tool_call_id) if ctx_turn and tool_call_id else None

        session = await run_in_db_thread(queries.get_session, self.ctx.db, session_id)
        project_id = session["project_id"] if session is not None else None
        cwd = None
        if session is not None:
            try:
                cwd = await self._cwd_for_project(project_id)
            except Exception:  # noqa: BLE001 - see `_refresh_gate_config`'s
                # matching `except Exception` for why this degrades (never
                # raises) rather than narrowing to `RpcError`
                cwd = None

        tool_name, tool_args, mode_hint = _extract_tool_call(params)
        mode = (
            mode_hint
            or self._turn_mode_snapshot.get(session_id)
            or (session["mode"] if session is not None else "task")
        )
        risk = review.classify(tool_name, tool_args, cwd=cwd)

        # Issue #13 (FR07 diff-in-Step), needs-approval path: `write_file`/
        # `patch` going through `acp_adapter/edit_approval.py`'s own
        # channel carries the diff on THIS request's `tool_call.content`
        # (`_extract_diff_content`'s docstring), but that channel's
        # `toolCallId` is a synthetic `edit-approval-N` id
        # (`edit_approval.py::build_acp_edit_tool_call`) from a counter
        # completely independent of the real ACP tool_call_id
        # `_handle_tool_call_start` assigned moments earlier for the SAME
        # call — so `step_id` above (looked up by that synthetic id) is
        # always `None` for this shape. Hermes dispatches tool calls
        # strictly sequentially within one Turn in this codebase (v1 has no
        # parallel tool execution — `delegate_task` stays off per
        # 03-w4-interfaces.md §2), and `tool.started` always fires (and
        # therefore `ctx_turn.tool_call_steps` always gains its entry)
        # before the dispatcher can reach this edit-approval gate for that
        # same call (verified against the installed checkout:
        # `agent/tool_executor.py::_begin_tool_execution` fires `tool.
        # started` before `model_tools.py::_pre_dispatch_guards` — which
        # calls `maybe_require_edit_approval` — ever runs) — so the most
        # recently started, still-open step in this Turn IS this call's
        # real step. A concurrent multi-write Turn (not possible in v1,
        # documented above) would be the one scenario this heuristic could
        # mis-attribute a diff to the wrong sibling step; written down here
        # rather than silently assumed away.
        diff = _extract_diff_content(tool_call.get("content"))
        if diff is not None and ctx_turn is not None:
            target_step_id = step_id or next(
                reversed(ctx_turn.tool_call_steps.values()), None
            )
            if target_step_id is not None:
                ctx_turn.step_diffs[target_step_id] = diff

        if tool_name in TERMINAL_LIKE_TOOLS:
            decided = await self._decide_terminal_like_permission(
                session_id=session_id, decision_id=decision_id, step_id=step_id,
                tool_args=tool_args, mode=mode, risk=risk, params=params,
            )
            if decided is not None:
                return decided
        elif risk.level == "low" and mode in ("auto", "task"):
            await run_in_db_thread(
                queries.insert_permission_decision,
                self.ctx.db, decision_id=decision_id, step_id=step_id,
                gate="review", risk=risk.level, request=params,
            )
            row = await run_in_db_thread(
                queries.decide_permission, self.ctx.db, decision_id,
                decision="allow", decided_by="rule",
            )
            await self.ctx.server.broadcast(session_id, "permission.decided", row)
            option_id = _select_permission_option(params.get("options") or [], "allow", None)
            return {"outcome": {"outcome": "selected", "optionId": option_id}}

        await run_in_db_thread(
            queries.insert_permission_decision,
            self.ctx.db,
            decision_id=decision_id,
            step_id=step_id,
            gate="user",
            risk=risk.level,
            request=params,
        )
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_permissions[decision_id] = _PendingPermission(
            session_id=session_id, params=params, future=fut, risk=risk.level,
        )
        await self.ctx.server.broadcast(
            session_id, "permission.requested",
            {
                "request_id": decision_id, "session_id": session_id, "gate": "user",
                "risk": risk.level,
                # Round 5 (controller ruling R5, 2026-09-19, final): the
                # user-gate card needs the review gate's own reason text to
                # show something more useful than a bare risk level — for an
                # `opaque` terminal command this is where "该命令无法静态分析，
                # 请人工确认" (see `permissions/review.py::_classify_terminal`)
                # actually reaches the UI; `tool_call` (below, unchanged)
                # already carries the real command/args verbatim (via
                # `_extract_tool_call`'s two decodable `rawInput` shapes), so
                # nothing further is needed to satisfy R5's "原样展示命令".
                "reasons": list(risk.reasons),
                "tool_call": tool_call, "options": params.get("options"),
            },
        )
        timeout_minutes = None
        if session is not None:
            settings = await run_in_db_thread(self.ctx.config.settings, project_id)
            timeout_minutes = (settings or {}).get("approval_timeout_minutes")
        configured_timeout_s = (
            float(timeout_minutes) * 60.0 if timeout_minutes is not None else None
        )
        # Review finding #5 (2026-09-19): the `write_file`/`patch` edit-
        # approval shape (`_extract_tool_call`'s `{"tool","arguments"}`
        # branch) travels over `acp_adapter/edit_approval.py::
        # make_acp_edit_approval_requester`, called by the installed
        # `hermes-agent`'s `acp_adapter/server.py::_wire_turn_callbacks`
        # with its 60.0s DEFAULT timeout, never overridden at that call
        # site — verified against the installed checkout. That channel
        # auto-DENIES the edit on ITS OWN after 60s, independent of
        # anything Jones configures. If `settings.approval_timeout_minutes`
        # is unset (PRD 9.4's "默认不超时") or looser than 60s, this
        # function would otherwise keep the pending-approval UI up well
        # past the point where the edit already didn't happen — a later
        # `permission.decide(allow)` would write `decision=allow` for an
        # edit Hermes already gave up on 60s+ earlier (the user sees "我批
        # 了、系统说批了" while nothing was done — exactly what 9.3 forbids).
        # The effective wait for THIS shape is therefore capped at 60s,
        # whichever of the two timeouts is tighter always wins.
        is_edit_approval_shape = (
            isinstance((tool_call.get("rawInput") or {}), dict)
            and "tool" in (tool_call.get("rawInput") or {})
            and "arguments" in (tool_call.get("rawInput") or {})
        )
        effective_timeout_s = configured_timeout_s
        if is_edit_approval_shape:
            effective_timeout_s = (
                _EDIT_APPROVAL_HERMES_TIMEOUT_SECONDS
                if effective_timeout_s is None
                else min(effective_timeout_s, _EDIT_APPROVAL_HERMES_TIMEOUT_SECONDS)
            )
        try:
            if effective_timeout_s is None:
                return await fut
            # NOT `asyncio.wait_for(fut, ...)`: on timeout, `wait_for` CANCELS
            # the awaitable it was given — cancelling `fut` itself would make
            # it `done()` (cancelled counts as done) before we ever get a
            # chance to distinguish "timed out, nothing decided it" from "a
            # real decision already arrived", and the `return await fut`
            # below would then raise `CancelledError` instead of returning an
            # answer, permanently silencing the ACP response (round-1-
            # equivalent fix, this PR — caught by
            # tests/test_gates_sessions_integration.py's timeout tests, which
            # hung/failed against the `wait_for` version). `asyncio.wait`
            # only reports which set `fut` landed in; it never touches `fut`
            # itself, so a genuine timeout leaves it exactly as pending as it
            # was, safe to inspect/resolve below.
            done, _pending = await asyncio.wait({fut}, timeout=effective_timeout_s)
            if fut not in done:
                # PRD 9.4: "超时可配置，但只能配「拒绝」" — never auto-allow.
                # `fut` may already carry a real user/`_resolve_pending_
                # permissions` answer that raced in right as the deadline
                # hit; only decide-and-answer here if nothing beat us to it.
                if not fut.done():
                    option_id = _select_permission_option(
                        params.get("options") or [], "deny", None
                    )
                    fut.set_result({"outcome": {"outcome": "selected", "optionId": option_id}})
                    row = await run_in_db_thread(
                        queries.decide_permission, self.ctx.db, decision_id,
                        decision="deny", decided_by="timeout",
                    )
                    await self.ctx.server.broadcast(session_id, "permission.decided", row)
                    # PRD 9.4/9.3: a timed-out approval terminates the
                    # Run as an error card ("审批超时"), not merely the one
                    # denied tool call — best-effort: if this Turn already
                    # finished by the time we get here (a last-second
                    # `permission.decide` raced us), there is nothing left
                    # to terminate. The reason names which timeout actually
                    # bound this wait (review finding #5) rather than
                    # always blaming `settings.approval_timeout_minutes`.
                    reason = "approval timed out (审批超时)"
                    if is_edit_approval_shape and (
                        configured_timeout_s is None
                        or configured_timeout_s > _EDIT_APPROVAL_HERMES_TIMEOUT_SECONDS
                    ):
                        reason = (
                            "the edit-approval channel auto-denies after 60s on its own "
                            "(Hermes-side timeout, independent of Jones's "
                            "approval_timeout_minutes); acting on that outcome now "
                            "(审批超时)"
                        )
                    if ctx_turn is not None and self._active_turns.get(session_id) is ctx_turn:
                        await self._terminate_run(ctx_turn, kind="error", reason=reason)
            return await fut
        finally:
            self._pending_permissions.pop(decision_id, None)

    async def _on_worker_crash(self, session_id: str, returncode: int | None) -> None:
        # A crashed worker can never answer a `session/request_permission` it
        # already sent — close that wait off now rather than leaving `AcpClient`'s
        # background task for it (kernel/acp_client.py) awaiting a future no one
        # will ever resolve (round-1 review fix).
        self._resolve_pending_permissions(session_id=session_id, reason="worker crashed")
        ctx_turn = self._active_turns.get(session_id)
        if ctx_turn is None:
            logger.warning(
                "idle worker exited unexpectedly; a fresh one will spawn on the next session.send",
                extra={"detail": {"session_id": session_id, "returncode": returncode}},
            )
            return
        # R-N11 (controller ruling, round-7, 2026-09-20): pinned defensively,
        # same as `stop()`/`_terminate_run_for_exceeded_budget` — this path
        # never itself sends an ACP cancel (the worker is already dead), so
        # `_finalize_turn_success`'s `stopReason:"cancelled"` race doesn't
        # apply here in practice, but claiming the intent up front costs
        # nothing and keeps all termination paths consistent.
        #
        # R-N20 (controller ruling, round-9, 2026-09-20): the pinned value
        # carries the full worker_crash card, not just `kind="error"` — same
        # reason string this function's own `_terminate_run` call below uses.
        # No DB round trip needed to build it: `"worker process exited
        # unexpectedly"` is `errors/classify.py::_WORKER_CRASH_MARKERS`'s own
        # first marker, matched before `classify()` ever consults
        # `last_step_status`, so passing `None` for it here is exact, not an
        # approximation.
        worker_crash_reason = f"worker process exited unexpectedly (code {returncode})"
        worker_crash_step_seq = ctx_turn.step_seq or None
        worker_crash_error_kind = classify.classify(kind_hint="error", reason=worker_crash_reason)
        ctx_turn.termination_intent = _TerminationIntent(
            kind=classify.terminated_kind_for("error", worker_crash_error_kind),
            reason=worker_crash_reason,
            card=classify.build_card(
                worker_crash_error_kind, reason=worker_crash_reason, step_seq=worker_crash_step_seq
            ),
            step_seq=worker_crash_step_seq,
        )
        # In the common case, the in-flight `worker.client.prompt()` call in
        # `_run_turn` observes the closed stdio pipe on its own (AcpClient's read
        # loop fails every pending future the instant the process's stdout hits
        # EOF — kernel/acp_client.py's `_read_loop` `finally` block) and
        # terminates the Run through its own `except (AcpError,
        # AcpProtocolError)` branch, well under a second after `_watch_exit`
        # calls this function.
        #
        # N07 (Issue #22, 04-w5-interfaces.md §4): "运行中 Run 超过 5s 没有任何
        # ACP update 且 worker 进程已退出 -> 立即 worker_crash 终止" — this is the
        # belt-and-suspenders backstop for whenever that doesn't happen in time
        # (the known asyncio-shutdown hang noted in 02-w3-interfaces.md §1.2's
        # "已发现但不在本分支范围内修复的 bug", or any other race/hang in the read
        # loop): poll for up to `_WORKER_CRASH_GRACE_S` and force the termination
        # here if this *exact* `ctx_turn` is still the session's active turn by
        # then — PRD N07 is explicit that the UI must never show "running"
        # against a dead Run for more than 5s, so this can't just log and hope.
        logger.info(
            "worker crashed during an active turn; waiting up to "
            f"{_WORKER_CRASH_GRACE_S}s for the in-flight prompt() call to report it "
            "before force-terminating (N07)",
            extra={"detail": {"session_id": session_id, "returncode": returncode}},
        )
        deadline = time.monotonic() + _WORKER_CRASH_GRACE_S
        while time.monotonic() < deadline:
            if self._active_turns.get(session_id) is not ctx_turn:
                return  # the in-flight prompt() call's own error path got there first
            await asyncio.sleep(_WORKER_CRASH_POLL_INTERVAL_S)
        if self._active_turns.get(session_id) is not ctx_turn:
            return
        # Round-1 review fix (#3/#7): the deadline expiring only means "the
        # in-flight `prompt()` call's own error path hasn't reached
        # `_advance_queue`'s `self._active_turns.pop(session_id, ...)` yet" — it
        # does *not* mean that path is still inside `prompt()` itself. It can
        # just as well already be past `_terminate_run` (DB write done,
        # `run.terminated` broadcast done or in flight) and sitting in
        # `finally: await self._advance_queue(session_id)`, waiting on
        # `self._lock(session_id)` or blocked on the `pop_next_queue_item` DB
        # call — `_active_turns` isn't cleared until that function actually
        # acquires the lock. Blindly `task.cancel()`-ing in that window used to
        # be able to land the `CancelledError` *inside* `pop_next_queue_item`,
        # after the queued item had already been popped off `queue_items` but
        # before `_start_turn` ran — silently dropping a queued user message
        # with no way to recover it.
        #
        # Reading `runs.status` right before deciding whether to cancel closes
        # that: if the normal path already wrote 'terminated'/'completed', skip
        # the cancel (and the redundant force-terminate below) entirely and let
        # that path's own broadcast stand.
        #
        # Round-N2 review fix (#1, controller ruling R-N1): a since-corrected
        # comment used to sit here claiming this read having no `await` before
        # `task.cancel()` was *by itself* enough to rule out any race — wrong
        # conclusion, and the wrong half of it is exactly what let round-2's
        # own fix (see below) reintroduce a *different* bug this round's
        # review caught: round-2 moved the `self._turn_tasks.get(session_id)`
        # lookup several `await`s below this point (after this watchdog's own
        # `_finalize_streamed_messages`/`_terminate_run` calls), so if the
        # in-flight `prompt()` call made enough progress during those awaits to
        # reach `finally: await self._advance_queue(...)` and pop+start the
        # NEXT queued Turn, that late lookup would silently grab the NEW task
        # (`self._turn_tasks[session_id]` already overwritten) instead of the
        # actually-stuck one — cancelling a just-started, user-queued Turn with
        # no error, no log, and no `run.terminated` for it (`CancelledError`
        # isn't caught by `_run_turn`'s `except Exception`; this watchdog's own
        # `_terminate_run` call is for the *crashed* Run, not that one).
        #
        # The fix is to capture (and, if needed, cancel) the task right here —
        # immediately after this read, no `await` between them — which DOES
        # correctly guarantee `self._turn_tasks.get(session_id)` is still the
        # stuck task (nothing else can run on this single-threaded loop between
        # two statements with no `await` in between; that part of the old
        # reasoning was right). What it does NOT guarantee, and what the old
        # comment's "⇒ 无竞态" conclusion got wrong, is that cancelling this
        # task can't still land *inside* `mark_run_terminated`'s DB call —
        # cancelling the asyncio Task can't stop a callable already running on
        # `store/db.py`'s single DB worker thread (a real `ThreadPoolExecutor`
        # future); the write keeps going to completion and commits, but the
        # cancelled coroutine never reaches its own broadcast. That race is
        # real regardless of the await-gap here, and closing it is no longer
        # this function's job: `_terminate_run`'s own idempotency guard now
        # tracks "written" vs. "broadcast" separately (R-N1) and rebroadcasts
        # from the persisted row if this Run was already marked terminated but
        # never actually broadcast — so cancelling immediately, right here, is
        # safe again, without needing round-2's "finalize+terminate before
        # cancel" reordering to *win* that race outright.
        current_run = await run_in_db_thread(queries.get_run, self.ctx.db, ctx_turn.run_id)
        if current_run is not None and current_run["status"] != "running":
            logger.debug(
                "N07 watchdog deadline reached but the Run was already "
                "terminated by the in-flight prompt() call itself; not cancelling",
                extra={"detail": {"session_id": session_id, "run_id": ctx_turn.run_id}},
            )
            return
        logger.warning(
            "worker crash not observed by the in-flight prompt() call within "
            f"{_WORKER_CRASH_GRACE_S}s; force-terminating the Run (N07)",
            extra={"detail": {"session_id": session_id, "returncode": returncode}},
        )
        # Captured here — immediately after the `current_run` status read
        # above, no `await` in between — for the identity guarantee the long
        # comment block above this method already spells out: this is what
        # keeps a late queue-advance from swapping in a NEW task before this
        # function could grab it. Round-N10 (controller ruling, round-6,
        # 2026-09-20) only moved WHEN this is actually cancelled (below), not
        # where it's captured.
        task = self._turn_tasks.get(session_id)
        # Round-1 review fix (#7): every other termination path (`_run_turn`'s
        # own `except (AcpError, AcpProtocolError)` and its catch-all backstop)
        # calls this before `_terminate_run` — streamed `message.delta` text
        # only lives on `ctx_turn` until finalized, so skipping it here (as
        # this watchdog path used to) meant a Run force-terminated by N07 could
        # drop assistant text the user had already watched stream past,
        # forever, from the `messages` table (FR06 replay).
        #
        # R-N10 (controller ruling, round-6, 2026-09-20): finalize+terminate
        # now run BEFORE `task.cancel()` below, not after — round-N2 (R-N1)
        # had put the cancel first, specifically to win the race for which
        # coroutine's `mark_run_terminated` call lands first; R-N1's own
        # write/broadcast split (this method's idempotency guard, see
        # `_terminate_run`) made winning that race unnecessary — a second,
        # losing write just no-ops or rebroadcasts. What cancel-first left
        # open instead (documented as a known scope gap in `_advance_queue`'s
        # own comment, R-N4): if this watchdog cancelled the stuck task
        # before `_terminate_run` (below) had a chance to set `ctx_turn.
        # terminated_kind`, and the just-cancelled task reached its own
        # `finally: await self._advance_queue(...)` before this watchdog got
        # back around to calling `_terminate_run`, `_advance_queue` would read
        # `terminated_kind` as still `None` and auto-advance a worker_crash
        # termination instead of suspending the queue for it (PRD 9.3).
        # Finalizing and terminating first closes that — `ctx_turn.
        # terminated_kind` is set synchronously inside `_terminate_run` (see
        # that method's own comment) strictly before this function ever
        # reaches `task.cancel()` below, so `_advance_queue` can never observe
        # it unset for a worker_crash termination again. Mirrors the ordering
        # R-N7 (round-6) already established for `_terminate_run_for_
        # exceeded_budget` — this function now being one of this round's
        # authorized touch points is what makes reusing that order here
        # possible for the first time.
        await self._finalize_streamed_messages(ctx_turn)
        # `kind`/`reason` here are only the honest fallback for the (never
        # actually reachable here) case `ctx_turn.termination_intent` is
        # `None` by the time this runs — the pin above already set it, so
        # `_terminate_run` always adopts that record wholesale instead
        # (R-N20). Kept identical to it rather than left stale.
        await self._terminate_run(ctx_turn, kind="error", reason=worker_crash_reason)
        if task is not None and not task.done():
            # Cancelling (rather than awaiting) `_run_turn`'s own stuck task is
            # what still lets its `finally: await self._advance_queue(...)` run
            # and unstick the session's queue — `asyncio.CancelledError` isn't
            # caught by that function's `except Exception`, so this can never
            # turn into a second uncontrolled `_terminate_run` call from that
            # task. R-N10: this now happens AFTER the finalize/terminate above,
            # not before — see the comment above those two calls.
            task.cancel()
