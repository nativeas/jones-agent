"""Unit tests for the rule gate's hard-deny classifier (Issue #11 G05/N01):
`kernel/plugin/jones_gate/_hard_deny.py`. Pure-function tests, no process
spawning, no daemon plumbing — this module runs inside the worker process,
so the honest way to test it is calling it directly, same as it's called
from `_on_pre_tool_call`.

Round 4 (2026-09-19, controller ruling R2): rewritten for the new flat
token-stream scanner — see `_hard_deny.py`'s module docstring for the
"why" (rounds 1-3 patched a segment-based classifier one shell construct at
a time; round 4 replaces the premise instead)."""

from __future__ import annotations

import pytest

from jones_daemon.kernel.plugin.jones_gate import _hard_deny


def _denied(command: str, **kwargs) -> bool:
    return _hard_deny.classify_command(command, **kwargs).denied


# -- individual hard-deny patterns -------------------------------------------


def test_rm_rf_is_denied():
    assert _denied("rm -rf /Users/alice/Documents")


def test_rm_r_is_denied():
    assert _denied("rm -r /Users/alice/important_project")


def test_rm_rf_of_a_temp_dir_is_still_denied():
    # Round 4 deliberately drops the old cwd/temp-directory carve-out (see
    # `_hard_deny.py`'s module docstring's "the tradeoff, made explicit"
    # section): proving a target is "safely temporary" is exactly the kind
    # of per-invocation understanding this rewrite stops attempting.
    assert _denied("rm -rf /tmp/scratch")


def test_rm_without_recursive_force_is_not_hard_denied():
    assert not _denied("rm ./one_file.txt")


def test_rm_rf_with_no_targets_is_denied():
    assert _denied("rm -rf")


def test_trash_command_is_denied():
    assert _denied("trash ~/Documents/report.pdf")


def test_shred_is_denied():
    assert _denied("shred -u secret.txt")


def test_mkfs_is_denied():
    assert _denied("mkfs.ext4 /dev/sda1")


def test_diskutil_erase_is_denied():
    assert _denied("diskutil eraseDisk APFS Untitled disk2")


def test_diskutil_list_is_not_denied():
    assert not _denied("diskutil list")


def test_git_push_force_to_main_is_denied():
    assert _denied("git push --force origin main")


def test_git_push_force_with_no_explicit_ref_is_denied():
    # Ambiguous target (pushes whatever branch is checked out) -> conservative deny.
    assert _denied("git push --force")


def test_git_push_force_to_feature_branch_is_not_hard_denied():
    assert not _denied("git push --force origin feature/my-branch")


def test_git_push_without_force_is_not_denied():
    assert not _denied("git push origin main")


def test_find_delete_is_denied():
    assert _denied("find . -delete")


def test_benign_command_is_not_denied():
    assert not _denied("ls -la /tmp")


def test_unparseable_command_is_not_hard_denied():
    # Unbalanced quote -> the tokenizer can't parse it -> not hard-denied
    # here (falls through to the review gate, which marks unparseable
    # commands high-risk instead, see permissions/review.py).
    assert not _denied("echo 'unterminated")


# -- flat token-stream scanning: no command-boundary understanding needed ---
#
# Round 1-3 each unwrapped one more wrapper program (`sh -c`, `env`, `nohup`,
# `timeout`, `xargs`, combined `-lc`/`-xc` forms, `&`/newline joiners...) by
# hand. The round-4 scanner needs none of that plumbing for anything except
# a shell interpreter's own `-c` payload (which is the one case where the
# tokenizer legitimately hides the real tokens inside a single quoted
# string) — every other wrapper's argv sits directly in the flat stream.


def test_chained_command_hits_the_second_segment():
    assert _denied("echo hi && rm -rf ~/Documents")


def test_semicolon_joined_rm_rf_is_denied():
    assert _denied("npm test; rm -rf /Users/alice")


def test_ampersand_joined_rm_rf_is_denied():
    assert _denied("npm test & rm -rf /Users/alice")


def test_newline_joined_rm_rf_is_denied():
    assert _denied("npm test\nrm -rf /Users/alice")


def test_pipe_joined_rm_rf_is_denied():
    assert _denied("echo go | xargs -I{} rm -rf {}")


def test_env_wrapped_rm_rf_is_denied():
    assert _denied("env FOO=1 rm -rf /Users/alice")


def test_timeout_wrapped_rm_rf_is_denied():
    assert _denied("timeout 10 rm -rf /Users/alice")


def test_nohup_wrapped_rm_rf_is_denied():
    assert _denied("nohup rm -rf /Users/alice")


def test_xargs_rm_rf_is_denied():
    # No wrapper-specific handling needed at all: `rm`/`-rf` sit directly in
    # the flat token stream right after `xargs`.
    assert _denied("xargs rm -rf")


def test_find_exec_rm_rf_is_denied():
    assert _denied("find . -exec rm -rf {} \\;")


# -- the one real recursion: a shell interpreter's `-c` payload -------------


@pytest.mark.parametrize(
    "command",
    [
        "bash -c 'rm -rf /Users/alice'",
        'sh -c "rm -rf /Users/alice"',
        "zsh -c 'rm -rf /Users/alice'",
        "bash -lc 'rm -rf /Users/alice'",
        "bash -ic 'rm -rf ~'",
        "sh -xc 'rm -rf ~'",
        "bash -ec 'rm -rf ~'",
    ],
)
def test_shell_dash_c_wrapped_rm_rf_is_denied(command):
    assert _denied(command)


def test_nested_shell_c_wrapped_rm_rf_is_denied():
    assert _denied("bash -c \"sh -c 'rm -rf /Users/alice'\"")


def test_newline_inside_a_shell_c_payload_is_denied():
    assert _denied('bash -c "echo hi\nrm -rf /Users/alice"')


def test_shell_dash_c_wrapped_benign_command_is_not_denied():
    assert not _denied("bash -c 'echo hello'")


def test_bash_l_without_c_is_not_treated_as_a_dash_c_wrapper():
    # `-l` alone never takes a script payload — must not be misread as a
    # `-c` cluster just because it's a combined-looking short option.
    assert not _denied("bash -l 'echo hello'")


# -- protected `~/.jones` path (requires a write/delete verb, round 4) ------


def test_write_verb_touching_user_root_is_denied():
    assert _denied("rm ~/.jones/secrets/vault.enc", user_root="~/.jones")


def test_write_verb_touching_resolved_user_root_is_denied():
    assert _denied(
        "cp /tmp/x /Users/alice/.jones/config.json", user_root="/Users/alice/.jones"
    )


def test_write_verb_touching_project_permissions_path_is_denied():
    assert _denied(
        "rm /repo/.jones/permissions.json",
        project_permissions_path="/repo/.jones/permissions.json",
    )


def test_plain_read_of_user_root_is_not_hard_denied():
    # Round 4 (controller ruling R2): unlike rounds 1-3's
    # `command_touches_protected_path` (any touch at all, read or write, was
    # hard-denied), the new rule requires a write/delete-verb token in the
    # SAME stream. A bare, non-compound `cat ~/.jones/x` isn't hard-denied
    # here any more — it also isn't silently allowed: it has no operator
    # character at all, so it's not `compound` either, but with no matching
    # `permissions.json` allow rule it still escalates to the daemon review
    # gate (see `_rules.py::decide`) rather than executing unreviewed.
    assert not _denied("cat ~/.jones/secrets/vault.enc", user_root="~/.jones")


def test_command_not_touching_user_root_is_not_denied():
    assert not _denied("ls -la /tmp", user_root="~/.jones")


# -- is_protected_path: write_file/patch's plain path argument, unaffected --


def test_protected_user_root_path_write_is_denied():
    assert _hard_deny.is_protected_path(
        "~/.jones/config/permissions.json", user_root="~/.jones", project_permissions_path=None
    )


def test_protected_project_permissions_path_is_denied():
    assert _hard_deny.is_protected_path(
        "/repo/.jones/permissions.json",
        user_root="/does/not/matter",
        project_permissions_path="/repo/.jones/permissions.json",
    )


def test_unrelated_project_file_is_not_protected():
    assert not _hard_deny.is_protected_path(
        "/repo/src/main.py", user_root="/Users/alice/.jones", project_permissions_path=None
    )


# -- adversarial table (controller ruling R3): every bypass string the      -
# first three rounds' review findings gave, now denied by the rewritten     -
# scanner without any construct-specific handling --------------------------


@pytest.mark.parametrize(
    "command",
    [
        "bash -lc 'rm -rf /Users/alice'",
        "sh -xc 'rm -rf /Users/alice'",
        "npm test&rm -rf /Users/alice",
        "npm test ;rm -rf /Users/alice",
        "npm test\nrm -rf /Users/alice",
        "npm test $(rm -rf /Users/alice)",
        "xargs rm -rf",
        "find . -delete",
    ],
)
def test_adversarial_table_hard_deny_strings_are_denied(command):
    assert _denied(command)


# -- benign strings that must NOT be over-denied (controller ruling R3) -----


@pytest.mark.parametrize(
    "command",
    ["ls -la", "git status", "npm test", "cat file", "grep -r foo ."],
)
def test_adversarial_table_benign_strings_are_not_denied(command):
    # `grep -r foo .` in particular: `-r` here belongs to `grep`, not `rm` —
    # `_rm_denied` only ever fires when the token stream actually contains
    # an `rm` token, so this must stay clean.
    assert not _denied(command)
