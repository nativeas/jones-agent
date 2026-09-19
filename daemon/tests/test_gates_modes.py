"""Unit tests for `sessions/modes.py` (PRD 9.1/9.6, N13's mode-narrowing half)."""

from __future__ import annotations

from jones_daemon.sessions.modes import MODE_RANK, is_valid_child_mode


def test_rank_ordering_matches_prd_9_1():
    assert MODE_RANK["chat"] < MODE_RANK["task"] < MODE_RANK["auto"]


def test_task_parent_forbids_auto_child_N13():
    assert is_valid_child_mode("auto", "task") is False


def test_task_parent_allows_task_or_chat_child():
    assert is_valid_child_mode("task", "task") is True
    assert is_valid_child_mode("chat", "task") is True


def test_auto_parent_allows_any_child():
    assert is_valid_child_mode("chat", "auto") is True
    assert is_valid_child_mode("task", "auto") is True
    assert is_valid_child_mode("auto", "auto") is True


def test_chat_parent_only_allows_chat_child():
    assert is_valid_child_mode("chat", "chat") is True
    assert is_valid_child_mode("task", "chat") is False
    assert is_valid_child_mode("auto", "chat") is False


def test_unknown_mode_is_never_valid():
    assert is_valid_child_mode("bogus", "task") is False
    assert is_valid_child_mode("task", "bogus") is False
