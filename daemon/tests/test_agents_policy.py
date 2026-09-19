from jones_daemon.agents.policy import is_tool_allowlist_subset


def test_empty_parent_accepts_any_child():
    assert is_tool_allowlist_subset([], []) is True
    assert is_tool_allowlist_subset(["shell", "browser"], []) is True


def test_empty_child_against_non_empty_parent_is_rejected():
    # An empty child claims "unrestricted", which is broader than a restricted
    # (non-empty) parent — not a valid narrowing (PRD 9.6 / N13).
    assert is_tool_allowlist_subset([], ["shell"]) is False


def test_literal_subset_is_accepted():
    assert is_tool_allowlist_subset(["shell"], ["shell", "browser"]) is True
    assert is_tool_allowlist_subset(["shell", "browser"], ["shell", "browser"]) is True


def test_child_with_a_tool_outside_parent_is_rejected():
    assert is_tool_allowlist_subset(["shell", "sudo"], ["shell"]) is False
