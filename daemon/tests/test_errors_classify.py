"""Pure unit tests for `errors/classify.py` (Issue #22, 04-w5-interfaces.md §4).

No DB, no asyncio, no fake ACP subprocess — `sessions/service.py::_terminate_run`
is exercised separately, end-to-end, in `test_errors_sessions_integration.py`."""

from __future__ import annotations

from jones_daemon.errors import classify
from jones_daemon.errors.classify import ErrorKind

# -- classify(): every reason-text bucket a real call site actually produces ----


def test_worker_startup_failed_classifies_as_worker_crash():
    assert (
        classify.classify(kind_hint="error", reason="worker startup failed: boom")
        == ErrorKind.WORKER_CRASH
    )


def test_acp_prompt_failed_connection_closed_classifies_as_worker_crash():
    assert (
        classify.classify(kind_hint="error", reason="ACP prompt failed: connection closed")
        == ErrorKind.WORKER_CRASH
    )


def test_own_watchdog_reason_classifies_as_worker_crash():
    # sessions/service.py::_on_worker_crash's own N07 force-termination wording.
    assert (
        classify.classify(
            kind_hint="error", reason="worker process exited unexpectedly (code 137)"
        )
        == ErrorKind.WORKER_CRASH
    )


def test_approval_timeout_reason_classifies_correctly():
    assert (
        classify.classify(kind_hint="error", reason="approval timed out (审批超时)")
        == ErrorKind.APPROVAL_TIMEOUT
    )


def test_network_markers_classify_as_network():
    for reason in [
        "ACP prompt failed: Connection refused",
        "ACP prompt failed: request timed out after 30s",
        "provider_error: Temporary failure in name resolution",
    ]:
        assert classify.classify(kind_hint="error", reason=reason) == ErrorKind.NETWORK, reason


def test_quota_markers_classify_as_provider_quota_not_generic_error():
    for reason in [
        "ACP prompt failed: 429 Too Many Requests",
        "ACP prompt failed: rate_limit_error: insufficient_quota",
    ]:
        got = classify.classify(kind_hint="error", reason=reason)
        assert got == ErrorKind.PROVIDER_QUOTA, reason


def test_auth_markers_classify_as_provider_auth():
    for reason in [
        "ACP prompt failed: 401 Unauthorized",
        "ACP prompt failed: invalid_api_key",
        "provider_error: no key configured",
    ]:
        got = classify.classify(kind_hint="error", reason=reason)
        assert got == ErrorKind.PROVIDER_AUTH, reason


def test_last_step_failed_classifies_generic_acp_failure_as_tool_exception():
    # No network/auth/quota/crash marker present — the *only* signal that this
    # was a tool exception (not some other model-layer failure) is that the last
    # recorded Step for the Run had status="failed".
    assert (
        classify.classify(
            kind_hint="error",
            reason="ACP prompt failed: tool execution failed: boom",
            last_step_status="failed",
        )
        == ErrorKind.TOOL_EXCEPTION
    )


def test_generic_acp_failure_without_a_failed_step_is_provider_error():
    assert (
        classify.classify(
            kind_hint="error",
            reason="ACP prompt failed: something unrecognized",
            last_step_status="completed",
        )
        == ErrorKind.PROVIDER_ERROR
    )


def test_unexpected_error_prefix_classifies_as_internal():
    assert (
        classify.classify(kind_hint="error", reason="unexpected error: KeyError('x')")
        == ErrorKind.INTERNAL
    )


def test_explicit_budget_hint_is_trusted_outright():
    assert (
        classify.classify(kind_hint="budget", reason="anything at all")
        == ErrorKind.BUDGET
    )


def test_provider_error_prefix_without_other_markers():
    reason = "provider_error: something unrecognized happened"
    assert classify.classify(kind_hint="error", reason=reason) == ErrorKind.PROVIDER_ERROR


def test_unknown_provider_classifies_as_provider_auth():
    # "unknown provider" comes from a misconfigured model_pref (typo'd vendor
    # name) — closer to "go fix your provider/key setup" than a generic
    # provider_error, so it shares provider_auth's action set (switch_model,
    # not a pointless same-input retry).
    assert (
        classify.classify(kind_hint="error", reason="provider_error: unknown provider 'foo'")
        == ErrorKind.PROVIDER_AUTH
    )


# -- terminated_kind_for(): outer run.terminated.kind -------------------------


def test_provider_quota_upgrades_outer_kind_to_budget():
    assert classify.terminated_kind_for("error", ErrorKind.PROVIDER_QUOTA) == "budget"


def test_budget_kind_stays_budget():
    assert classify.terminated_kind_for("budget", ErrorKind.BUDGET) == "budget"


def test_network_stays_error():
    assert classify.terminated_kind_for("error", ErrorKind.NETWORK) == "error"


def test_user_stop_passes_through_untouched():
    assert classify.terminated_kind_for("user", ErrorKind.INTERNAL) == "user"


# -- build_card() / build_user_card() shape ------------------------------------


def test_build_card_shape_matches_the_04_w5_contract():
    card = classify.build_card(ErrorKind.NETWORK, reason="Connection refused", step_seq=3)
    d = card.to_dict()
    assert set(d) == {"kind", "title", "message", "step_seq", "raw_excerpt", "actions", "retryable"}
    assert d["kind"] == "network"
    assert d["step_seq"] == 3
    assert d["retryable"] is True
    assert "retry" in d["actions"]


def test_provider_auth_and_quota_never_offer_a_bare_retry():
    # Retrying with the exact same (invalid/exhausted) key can't succeed —
    # offering "retry" there would be dishonest (DEV.md 工程原则 #4).
    for kind in (ErrorKind.PROVIDER_AUTH, ErrorKind.PROVIDER_QUOTA, ErrorKind.BUDGET):
        card = classify.build_card(kind, reason="x", step_seq=None)
        assert "retry" not in card.actions
        assert card.retryable is False
        assert "switch_model" in card.actions
        assert "abandon" in card.actions


def test_build_user_card_has_no_actions():
    card = classify.build_user_card("stopped by user")
    assert card.kind == "user"
    assert card.actions == ()
    assert card.retryable is False


def test_is_user_stop():
    assert classify.is_user_stop("user") is True
    assert classify.is_user_stop("error") is False
    assert classify.is_user_stop("budget") is False


def test_every_error_kind_has_a_title_and_is_covered_by_visual_bucket_set():
    # Every ErrorKind must build a card (no KeyError from an incomplete _TITLES/
    # _ACTIONS table) — and VISUALLY_DISTINCT_KINDS ⊆ ErrorKind, exactly the "7
    # types get a dedicated color/icon, the rest fall back to a generic style"
    # split the renderer implements (04-w5-interfaces.md §4: "7 类卡片").
    for kind in ErrorKind:
        card = classify.build_card(kind, reason="x", step_seq=None)
        assert card.title
    assert classify.VISUALLY_DISTINCT_KINDS <= set(ErrorKind)
    assert len(classify.VISUALLY_DISTINCT_KINDS) == 7


# -- redact_secrets(): G03/N02 "错误卡片...均不出现完整 Key" -------------------


def test_redact_secrets_masks_sk_prefixed_keys():
    out = classify.redact_secrets("auth failed for key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    assert "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in out
    assert out.endswith("***WXYZ") or "***WXYZ" in out


def test_redact_secrets_masks_labeled_key_value():
    out = classify.redact_secrets('Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz012345')
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in out


def test_redact_secrets_masks_env_style_assignment():
    out = classify.redact_secrets("JONES_ANTHROPIC_API_KEY=sk-ant-abcdefghijklmnopqrstuvwx")
    assert "sk-ant-abcdefghijklmnopqrstuvwx" not in out
    assert "JONES_ANTHROPIC_API_KEY=" in out  # the label itself is not secret


def test_redact_secrets_leaves_ordinary_text_alone():
    text = "the tool exited with code 1 while reading /tmp/foo.txt"
    assert classify.redact_secrets(text) == text


def test_redact_secrets_never_raises_on_empty_or_odd_input():
    assert classify.redact_secrets("") == ""
    assert classify.redact_secrets("sk-") == "sk-"  # too short to look like a real key


def test_build_card_raw_excerpt_is_bounded_and_redacted():
    long_reason = "provider_error: " + ("x" * 5000) + " sk-ant-api03-" + ("A" * 40)
    card = classify.build_card(ErrorKind.PROVIDER_ERROR, reason=long_reason, step_seq=None)
    assert len(card.raw_excerpt) <= 2048
    assert "sk-ant-api03-" + ("A" * 40) not in card.raw_excerpt
