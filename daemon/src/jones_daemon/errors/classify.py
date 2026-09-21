"""Error classification + `ErrorCard` construction (Issue #22, PRD FR14/9.3/5.5,
docs/design/04-w5-interfaces.md §4).

Pure, stdlib-only, no `jones_daemon` imports and no DB/asyncio dependency — this
module's whole job is "given the free-text `reason` a Run terminated with (plus a
little context `sessions/service.py::_terminate_run` already has to hand: the
caller's `kind` hint and the status of the last Step that ran), decide which of
the nine `ErrorKind`s this is and build the structured card the RPC contract
(`run.terminated`'s `card` field) and the renderer both need". Keeping it pure
means `test_errors_classify.py` can exercise every branch without a DB, an event
loop, or a fake ACP subprocess — see that file's tests for the exhaustive mapping.

## Where the nine kinds actually come from

`sessions/service.py`'s existing call sites into `_terminate_run` (all landed by
earlier branches — 02-w3-interfaces.md §2, §1.2 — this branch does not touch any
of them) pass a small, fixed set of `reason` string *prefixes*:

  - `"provider_error: ..."`      — `ctx.providers.resolve()` failed before a
                                    worker was even spawned (no key / unknown
                                    vendor / db-vault mismatch).
  - `"worker startup failed: ..."` — `WorkerManager.ensure_started()` raised
                                    `WorkerStartupError` (self-check failed).
  - `"ACP prompt failed: ..."`   — `worker.client.prompt()` raised `AcpError`/
                                    `AcpProtocolError` while a Turn was running
                                    (covers network drops, provider auth/quota
                                    errors surfaced through the model API, tool
                                    exceptions that end the Turn, and a worker
                                    dying mid-Turn — all funnel through this one
                                    prefix, so this is where most of the text-
                                    pattern matching below does its work).
  - `"unexpected error: ..."`    — the catch-all `except Exception` backstop.
  - `"approval timed out (审批超时)"` — PRD 9.4, already implemented.
  - `"stopped by user"`           — `kind="user"`, never reaches this module's
                                    `ErrorKind` machinery at all (see
                                    `is_user_stop`/`build_user_card` below).

This branch's own new code (`sessions/service.py::_on_worker_crash`'s N07 5s
watchdog) adds exactly one more: `"worker process exited unexpectedly ..."` —
deliberately worded so `_WORKER_CRASH_MARKERS` below matches it before anything
else, since that call site *knows* for certain it's a worker_crash (no text
sniffing needed, most reliable of every branch below).

R-N5 (controller ruling, 2026-09-20) adds `_handle_tool_call_start`'s
Step-count/时长上限 checks as callers too, but they don't need a `reason`
marker of their own — they pass `kind_hint="budget"` directly, which `classify()`
trusts outright and returns before any text matching runs at all (see that
function's docstring).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# ErrorKind
# ---------------------------------------------------------------------------


class ErrorKind(StrEnum):
    """Fixed enum per 04-w5-interfaces.md §4 — do not add members without
    updating that doc first (it's the contract, this is the implementation)."""

    NETWORK = "network"
    PROVIDER_AUTH = "provider_auth"
    PROVIDER_QUOTA = "provider_quota"
    PROVIDER_ERROR = "provider_error"
    TOOL_EXCEPTION = "tool_exception"
    WORKER_CRASH = "worker_crash"
    APPROVAL_TIMEOUT = "approval_timeout"
    BUDGET = "budget"
    INTERNAL = "internal"


# The renderer groups these nine into 7 visual styles (04-w5-interfaces.md §4:
# "7 类卡片...颜色+图标"：网络/配额/认证/工具/崩溃/超时/预算) — PROVIDER_ERROR and
# INTERNAL don't get a dedicated color/icon, they fall back to a generic style.
# Kept here (not just in the renderer) as the one place that documents which
# kinds are "the 7" vs. "the 2 generic fallbacks", so the two sides can't drift
# silently.
VISUALLY_DISTINCT_KINDS = frozenset(
    {
        ErrorKind.NETWORK,
        ErrorKind.PROVIDER_QUOTA,
        ErrorKind.PROVIDER_AUTH,
        ErrorKind.TOOL_EXCEPTION,
        ErrorKind.WORKER_CRASH,
        ErrorKind.APPROVAL_TIMEOUT,
        ErrorKind.BUDGET,
    }
)

# PRD 9.3's own termination-kind table classifies "远端 API Key 额度受限/被限流"
# under **预算终止**, not 错误终止 — so a Run whose classified ErrorKind is
# PROVIDER_QUOTA (or the hard-coded BUDGET kind) surfaces as
# `run.terminated.kind == "budget"`; everything else stays "error". See
# `terminated_kind_for()`.
_BUDGET_LIKE_KINDS = frozenset({ErrorKind.PROVIDER_QUOTA, ErrorKind.BUDGET})

_TITLES: dict[ErrorKind, str] = {
    ErrorKind.NETWORK: "网络错误",
    ErrorKind.PROVIDER_AUTH: "模型 Key 无效",
    ErrorKind.PROVIDER_QUOTA: "额度已用尽",
    ErrorKind.PROVIDER_ERROR: "模型服务出错",
    ErrorKind.TOOL_EXCEPTION: "工具执行出错",
    ErrorKind.WORKER_CRASH: "工作进程崩溃",
    ErrorKind.APPROVAL_TIMEOUT: "审批超时",
    ErrorKind.BUDGET: "已达预算上限",
    ErrorKind.INTERNAL: "内部错误",
}

# actions ⊆ {"retry", "switch_model", "abandon"} (04-w5-interfaces.md §4). Skips
# "retry" for kinds where retrying unchanged (same key, same quota) would just
# fail identically — offering it there would be dishonest ("重试" implying it
# might work when it structurally can't).
#
# Round-2 review fix (#1, critical): `switch_model` never appears here anymore.
# `session.retry`'s `model_override` only ever reaches `ctx.providers.resolve()`
# — a pre-flight check `_run_turn` runs before spawning/reusing a worker — and
# is then discarded; the worker subprocess itself is spawned by
# `WorkerManager._spawn_and_check` via `_worker_env(hermes_home)` with no
# `extra_env`, and `ensure_started` returns the SAME already-running worker for
# a session that has one, never restarting it. So the process that actually
# runs the next Turn keeps using the Agent's original provider/model no matter
# what the user picked — for `PROVIDER_AUTH`/`PROVIDER_QUOTA`/`BUDGET`,
# `switch_model` used to be the *only* action on the card, i.e. the one thing
# a user could click that daemon reports as "succeeded" while doing nothing to
# fix the actual failure (re-runs the same bad Key/quota). That is worse than
# offering no action — see 04-w5-interfaces.md §4.2 for the two options this
# was weighed against and why this repo picked "remove it" over "wire
# `ProviderBinding` into the spawn path", which is A/#10's territory, not
# N/#22's. The `model_override` RPC parameter itself is left in place
# (`session.retry` still accepts and threads it through `_run_turn`'s resolver
# pre-check) — only the UI affordance is withdrawn — so re-enabling this is a
# one-line change here once a future branch actually wires the resolved
# binding into worker spawn/restart.
_ACTIONS: dict[ErrorKind, tuple[str, ...]] = {
    ErrorKind.NETWORK: ("retry", "abandon"),
    ErrorKind.PROVIDER_AUTH: ("abandon",),
    ErrorKind.PROVIDER_QUOTA: ("abandon",),
    ErrorKind.PROVIDER_ERROR: ("retry", "abandon"),
    ErrorKind.TOOL_EXCEPTION: ("retry", "abandon"),
    ErrorKind.WORKER_CRASH: ("retry", "abandon"),
    ErrorKind.APPROVAL_TIMEOUT: ("retry", "abandon"),
    ErrorKind.BUDGET: ("abandon",),
    ErrorKind.INTERNAL: ("retry", "abandon"),
}

# Kinds whose ONLY action is now "abandon" (round-2 review #1) get an explicit
# hint appended to `card.message` — otherwise a user sees just a title +
# message + one "放弃" button with no indication of what to actually do about
# it. Written once here rather than duplicated per-kind in `_TITLES`/renderer.
_ONLY_ABANDON_HINT = "去设置页换 Key / 换默认模型后重发。"


def _actions_for(kind: ErrorKind) -> tuple[str, ...]:
    return _ACTIONS[kind]


# ---------------------------------------------------------------------------
# ErrorCard
# ---------------------------------------------------------------------------

_MAX_EXCERPT_CHARS = 2048  # "raw_excerpt(≤2KB, 已脱敏)" — chars, not bytes: every
# byte of the redacted/truncated text here is ASCII-range JSON/log punctuation
# or CJK produced by this module's own `_TITLES`/`reason` text, never arbitrary
# multi-byte-heavy binary data, so counting characters is a safe, simpler proxy
# for the "≤2KB" intent without risking cutting a multi-byte UTF-8 sequence in
# half (which counting *bytes* via a naive slice would).
_MAX_MESSAGE_CHARS = 240


@dataclass(frozen=True)
class ErrorCard:
    """Exactly the shape 04-w5-interfaces.md §4 specifies: `{kind, title,
    message, step_seq?, raw_excerpt(≤2KB, 已脱敏), actions, retryable}`, plus
    an optional `budget` detail (round-2 review #4).

    `budget`: PRD 9.3's 预算终止 row requires "显式卡片说明是哪个预算、用了多少、
    上限多少" — `{name, used, limit, unit}`. Two call sites can produce
    `kind="budget"`: a caller passing it explicitly — as of R-N5 (controller
    ruling, 2026-09-20), that's `sessions/service.py::_handle_tool_call_start`'s
    Step-count/duration-cap checks (11.2's two upper bounds, now actually
    wired — see 04-w5-interfaces.md §4.2), which DOES have real numbers to
    hand and passes them through here — and this module's own text-based
    upgrade of a rate-limit/quota `reason` string, which is still free text
    from a provider exception, not a structured API response with numbers to
    parse out reliably, and so still leaves this `None`. This field exists so
    a *future* caller that does have real numbers (e.g. once a provider error
    body is parsed instead of just its message) has somewhere to put them,
    and the renderer already knows how to show it when present; that is the
    "structural, not deferred" fix the round-2 review asked for, without
    fabricating numbers this module doesn't have."""

    kind: str  # ErrorKind.value, or "user" for the non-error pass-through card
    title: str
    message: str
    step_seq: int | None
    raw_excerpt: str
    actions: tuple[str, ...]
    retryable: bool
    budget: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "message": self.message,
            "step_seq": self.step_seq,
            "raw_excerpt": self.raw_excerpt,
            "actions": list(self.actions),
            "retryable": self.retryable,
            "budget": self.budget,
        }


# ---------------------------------------------------------------------------
# Secret redaction (G03/N02: "错误卡片...均不出现完整 Key")
# ---------------------------------------------------------------------------
#
# No existing reusable redaction utility was found to reuse (04-w5-interfaces.md
# §4 says "脱敏复用 B 的工具函数" — B/#7's `providers/methods.py::_key_hint` is
# private, module-local, and only computes a hint for a key you already have in
# hand; it isn't a "find and mask secrets in arbitrary text" function, and no
# such function exists anywhere in the tree as of this branch — see the PR
# report's "契约变更" section). `DaemonContext` also has no `vault` field, so
# `_terminate_run` has no way to look up "every configured key's exact value" to
# do an exact-match redaction even if it wanted to.
#
# Pattern-based redaction instead: match key-*shaped* substrings (vendor
# prefixes, `key=`/`token=`/`Bearer ` followed by a long opaque value, AWS-style
# access key ids) regardless of whether they belong to a currently-configured
# vendor. This is strictly more defensive than an exact-match list for this use
# case — it also catches a key from an unconfigured/unexpected vendor echoed
# back in a raw exception message, which an exact-match list built from "the
# providers table" could never do — at the cost of being a heuristic (it can
# both over- and under-match unusual strings; see the false-negative/positive
# notes in the test file). Mirrors the existing "只给末 4 位" convention
# (`providers/methods.py::_key_hint`) for what's left visible.


def _mask(value: str) -> str:
    if len(value) <= 4:
        return "***"
    return f"***{value[-4:]}"


# Vendor-prefixed keys (Anthropic `sk-ant-...`, OpenAI `sk-...`/`sk-proj-...`,
# DeepSeek/others also commonly `sk-...`).
_RE_SK_PREFIX = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
# AWS-style access key ids (used by some S3-compatible/media-gen backends).
_RE_AKIA = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
# Round-1 review fix (#2): Google AI Studio / Gemini keys (`AIzaSy...`) — a
# formally-supported vendor (`providers/catalog.py`) whose HTTP error text
# habitually echoes back the *whole request URL* including `?key=...`. Matched
# both standalone (this pattern) and via the broadened `_RE_LABELED` below
# (`key=AIza...`) as defense in depth.
_RE_GEMINI = re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b")
# `key=...` / `token=...` / `secret=...` / `Authorization: Bearer ...` style —
# keeps the label, masks only the value. Round-1 review fix (#2): the label
# alternation used to require a "key"/"token" *compound* word
# (`api_key`/`access_token`) and missed the bare `key=`/`token=` shape a lot of
# real provider error text actually uses (e.g. Gemini's `?key=...` query
# param, a bare `token=ghp_...` in a generic exception message) — despite the
# module docstring and 04-w5-interfaces.md §4.1 both claiming that shape was
# covered. `\bkey\b`/`\btoken\b` require a word boundary on both sides, so
# this still can't match inside `keyboard`/`tokenizer` (no boundary exists
# between two word characters).
_RE_LABELED = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|secret|authorization|bearer|key|token)\b"
    r"([\s:=\"']{1,5})([A-Za-z0-9_\-./+=]{12,})"
)
# `SOME_API_KEY=value` / `SOME_TOKEN=value` env-var-style assignments (matches
# the `JONES_<VENDOR>_API_KEY` env vars `providers/resolver.py` builds).
_RE_ENV_ASSIGNMENT = re.compile(r"\b([A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET)[A-Z0-9_]*)=(\S+)")


def redact_secrets(text: str) -> str:
    """Best-effort mask of key/token-shaped substrings in `text`. Never raises —
    called on the way into an error card, must never itself become the reason a
    termination fails to report (DEV.md 工程原则 #4)."""
    if not text:
        return text
    out = _RE_SK_PREFIX.sub(lambda m: _mask(m.group(0)), text)
    out = _RE_AKIA.sub(lambda m: _mask(m.group(0)), out)
    out = _RE_GEMINI.sub(lambda m: _mask(m.group(0)), out)
    out = _RE_LABELED.sub(lambda m: f"{m.group(1)}{m.group(2)}{_mask(m.group(3))}", out)
    out = _RE_ENV_ASSIGNMENT.sub(lambda m: f"{m.group(1)}={_mask(m.group(2))}", out)
    return out


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

_WORKER_CRASH_MARKERS = (
    "worker process exited unexpectedly",  # this branch's own N07 watchdog
    "worker stdout closed",  # kernel/acp_client.py's AcpProtocolError text
    "connection closed",  # same, the close()-time variant
    "worker startup failed",
)
_APPROVAL_TIMEOUT_MARKERS = ("approval timed out", "审批超时")
_NETWORK_MARKERS = (
    "connection refused",
    "connection reset",
    "timed out",
    "timeout",
    "name or service not known",
    "temporary failure in name resolution",
    "network is unreachable",
    "dns",
    "econnrefused",
    "econnreset",
)
_AUTH_MARKERS = (
    "401",
    "unauthorized",
    "invalid api key",
    "invalid_api_key",
    "authentication",
    "invalid x-api-key",
    "no key configured",
    "unknown provider",
)
# Round-1 review fix (#4): these used to be one undifferentiated tuple, so a
# transient 429/rate-limit (wait a few seconds, retry — the standard recovery)
# and a genuinely exhausted quota (retrying changes nothing) both produced the
# same "额度已用尽" title with `retry` stripped from `actions` — untrue for the
# rate-limit case, and in conflict with PRD 12.3 FR14's "重试 / 换模型 / 放弃三个
# 可用操作". Both still classify to the same `ErrorKind.PROVIDER_QUOTA` (PRD 9.3
# groups "远端 API Key 额度受限/被限流" together under 预算终止 — that part of the
# original classification was correct), but `build_card()` below uses these two
# separately to pick title/actions once it has the specific `reason` text.
_RATE_LIMIT_MARKERS = ("429", "rate limit", "rate_limit", "too many requests")
_QUOTA_EXHAUSTED_MARKERS = ("quota", "insufficient_quota")
_QUOTA_MARKERS = _RATE_LIMIT_MARKERS + _QUOTA_EXHAUSTED_MARKERS


def _match_any(haystack: str, markers: tuple[str, ...]) -> bool:
    return any(marker in haystack for marker in markers)


def classify(
    *,
    kind_hint: str,
    reason: str,
    last_step_status: str | None = None,
) -> ErrorKind:
    """Decide the `ErrorKind` for a terminating Run.

    `kind_hint` is the `kind` the caller passed to `_terminate_run` — today
    always `"error"` or `"budget"` (never `"user"`, see `is_user_stop`). An
    explicit `"budget"` hint is trusted outright — as of R-N5 (controller
    ruling, 2026-09-20), `sessions/service.py::_handle_tool_call_start`'s
    Step-count/duration-cap checks (11.2's two upper bounds) are exactly such a
    caller; `"error"`/anything else falls through to text classification of
    `reason`, using `last_step_status` (the last Step recorded for this Run, if
    any) to distinguish "the model/API layer failed" from "a specific tool call
    itself failed" within the shared `"ACP prompt failed: ..."` bucket.
    """
    if kind_hint == "budget":
        return ErrorKind.BUDGET
    lowered = reason.lower()
    if _match_any(lowered, _WORKER_CRASH_MARKERS):
        return ErrorKind.WORKER_CRASH
    if _match_any(lowered, _APPROVAL_TIMEOUT_MARKERS):
        return ErrorKind.APPROVAL_TIMEOUT
    if _match_any(lowered, _NETWORK_MARKERS):
        return ErrorKind.NETWORK
    if _match_any(lowered, _QUOTA_MARKERS):
        return ErrorKind.PROVIDER_QUOTA
    if _match_any(lowered, _AUTH_MARKERS):
        return ErrorKind.PROVIDER_AUTH
    if reason.startswith("unexpected error:"):
        return ErrorKind.INTERNAL
    if last_step_status == "failed":
        return ErrorKind.TOOL_EXCEPTION
    if reason.startswith("provider_error:"):
        return ErrorKind.PROVIDER_ERROR
    if reason.startswith("ACP prompt failed:"):
        return ErrorKind.PROVIDER_ERROR
    return ErrorKind.INTERNAL


def terminated_kind_for(kind_hint: str, error_kind: ErrorKind) -> str:
    """The outer `run.terminated.kind` (`"user"|"error"|"budget"`, 00-foundation
    §4.2) — distinct from the finer-grained `ErrorCard.kind`. PRD 9.3 classifies
    quota/rate-limit as a 预算终止, so `PROVIDER_QUOTA` upgrades the outer kind
    to `"budget"` even though the caller passed `kind="error"` (it has no way to
    know that ahead of classifying the text)."""
    if kind_hint == "user":
        return "user"
    return "budget" if error_kind in _BUDGET_LIKE_KINDS else "error"


def is_user_stop(kind_hint: str) -> bool:
    return kind_hint == "user"


def build_user_card(reason: str) -> ErrorCard:
    """`kind="user"` never goes through `ErrorKind` — PRD 9.3 doesn't call a
    user-initiated stop an "error", and none of retry/switch_model/abandon make
    sense for it (the user just asked to stop; there's no failure to retry)."""
    fallback = "用户手动停止，正在执行的动作已收尾。"
    return ErrorCard(
        kind="user",
        title="已停止",
        message=_truncate(redact_secrets(reason) or fallback, _MAX_MESSAGE_CHARS),
        step_seq=None,
        raw_excerpt="",
        actions=(),
        retryable=False,
    )


def build_card(
    kind: ErrorKind,
    *,
    reason: str,
    step_seq: int | None,
    budget: dict[str, Any] | None = None,
) -> ErrorCard:
    redacted = redact_secrets(reason)
    title = _TITLES[kind]
    actions = _actions_for(kind)
    # Round-1 review fix (#4): a rate-limited (429/"too many requests") Run
    # isn't "额度已用尽" and, unlike a truly exhausted quota, retrying it after a
    # short wait is the textbook recovery — so give it back `retry` and an
    # honest title. Only when the text names an actual limit hit and *not* an
    # explicit quota-exhaustion word (`_QUOTA_EXHAUSTED_MARKERS`) — "429 ...
    # insufficient_quota" (both present) still means "确实用尽了", and keeps the
    # conservative no-retry treatment.
    lowered = reason.lower()
    if (
        kind is ErrorKind.PROVIDER_QUOTA
        and _match_any(lowered, _RATE_LIMIT_MARKERS)
        and not _match_any(lowered, _QUOTA_EXHAUSTED_MARKERS)
    ):
        title = "请求过于频繁，请稍后再试"
        actions = ("retry", "abandon")
    message = _truncate(redacted, _MAX_MESSAGE_CHARS)
    # Round-2 review fix (#1): a card whose only action is "abandon" (see
    # `_ACTIONS`'s round-2 comment) needs to actually say what to do instead of
    # leaving the user staring at one button — append the hint rather than
    # replace `message`, so the original (redacted) error text is still there
    # for anyone who wants it.
    if actions == ("abandon",):
        message = f"{message} {_ONLY_ABANDON_HINT}" if message else _ONLY_ABANDON_HINT
    return ErrorCard(
        kind=kind.value,
        title=title,
        message=message,
        step_seq=step_seq,
        raw_excerpt=_truncate(redacted, _MAX_EXCERPT_CHARS),
        actions=actions,
        retryable="retry" in actions,
        # R-N5 (controller ruling, 2026-09-20; PRD 9.3 预算终止 row: "显式卡片
        # 说明是哪个预算、用了多少、上限多少") — only `_handle_tool_call_start`'s
        # Step-count/duration-cap call sites pass this today; every other
        # caller still has no structured numbers to give (see this dataclass's
        # own docstring above), so `budget` stays `None` for them exactly as
        # before this round.
        budget=budget,
    )
