"""Unit tests for the rule gate's hard-deny classifier (Issue #11 G05/N01):
`kernel/plugin/jones_gate/_hard_deny.py`. Pure-function tests, no process
spawning, no daemon plumbing — this module runs inside the worker process,
so the honest way to test it is calling it directly, same as it's called
from `_on_pre_tool_call`."""

from __future__ import annotations

import tempfile

from jones_daemon.kernel.plugin.jones_gate import _hard_deny


def test_rm_rf_outside_temp_is_denied():
    verdict = _hard_deny.classify_command("rm -rf /Users/alice/Documents")
    assert verdict.denied
    assert "rm" in verdict.reason


def test_rm_r_outside_temp_is_denied():
    verdict = _hard_deny.classify_command("rm -r /Users/alice/important_project")
    assert verdict.denied


def test_rm_rf_inside_temp_dir_is_not_hard_denied():
    tmp_target = f"{tempfile.gettempdir()}/scratch"
    verdict = _hard_deny.classify_command(f"rm -rf {tmp_target}")
    assert not verdict.denied


def test_rm_without_recursive_force_is_not_hard_denied():
    verdict = _hard_deny.classify_command("rm ./one_file.txt")
    assert not verdict.denied


def test_rm_rf_with_no_targets_is_denied():
    verdict = _hard_deny.classify_command("rm -rf")
    assert verdict.denied


def test_trash_command_is_denied():
    verdict = _hard_deny.classify_command("trash ~/Documents/report.pdf")
    assert verdict.denied


def test_shred_is_denied():
    verdict = _hard_deny.classify_command("shred -u secret.txt")
    assert verdict.denied


def test_mkfs_is_denied():
    verdict = _hard_deny.classify_command("mkfs.ext4 /dev/sda1")
    assert verdict.denied


def test_diskutil_erase_is_denied():
    verdict = _hard_deny.classify_command("diskutil eraseDisk APFS Untitled disk2")
    assert verdict.denied


def test_diskutil_list_is_not_denied():
    verdict = _hard_deny.classify_command("diskutil list")
    assert not verdict.denied


def test_git_push_force_to_main_is_denied():
    verdict = _hard_deny.classify_command("git push --force origin main")
    assert verdict.denied


def test_git_push_force_with_no_explicit_ref_is_denied():
    # Ambiguous target (pushes whatever branch is checked out) -> conservative deny.
    verdict = _hard_deny.classify_command("git push --force")
    assert verdict.denied


def test_git_push_force_to_feature_branch_is_not_hard_denied():
    verdict = _hard_deny.classify_command("git push --force origin feature/my-branch")
    assert not verdict.denied


def test_git_push_without_force_is_not_denied():
    verdict = _hard_deny.classify_command("git push origin main")
    assert not verdict.denied


def test_benign_command_is_not_denied():
    verdict = _hard_deny.classify_command("ls -la /tmp")
    assert not verdict.denied


def test_chained_command_hits_the_second_segment():
    verdict = _hard_deny.classify_command("echo hi && rm -rf ~/Documents")
    assert verdict.denied


def test_unparseable_command_is_not_hard_denied():
    # Unbalanced quote -> shlex can't parse it -> not hard-denied here (falls
    # through to the review gate, which marks unparseable commands high-risk
    # instead, see permissions/review.py).
    verdict = _hard_deny.classify_command("echo 'unterminated")
    assert not verdict.denied


def test_protected_user_root_path_write_is_denied():
    denied = _hard_deny.is_protected_path(
        "~/.jones/config/permissions.json", user_root="~/.jones", project_permissions_path=None
    )
    assert denied


def test_protected_project_permissions_path_is_denied():
    denied = _hard_deny.is_protected_path(
        "/repo/.jones/permissions.json",
        user_root="/does/not/matter",
        project_permissions_path="/repo/.jones/permissions.json",
    )
    assert denied


def test_unrelated_project_file_is_not_protected():
    denied = _hard_deny.is_protected_path(
        "/repo/src/main.py", user_root="/Users/alice/.jones", project_permissions_path=None
    )
    assert not denied


def test_command_touching_user_root_is_denied():
    denied = _hard_deny.command_touches_protected_path(
        "cat ~/.jones/secrets/vault.enc",
        user_root="~/.jones",
        project_permissions_path=None,
        cwd=None,
    )
    assert denied


def test_command_not_touching_user_root_is_not_denied():
    denied = _hard_deny.command_touches_protected_path(
        "ls -la /tmp", user_root="~/.jones", project_permissions_path=None, cwd=None
    )
    assert not denied


# Review finding #2 (2026-09-19): known shell-wrapper programs must be
# unwrapped so the checks above see the real command being executed, not
# just the wrapper invoking it.


def test_bash_c_wrapped_rm_rf_is_denied():
    verdict = _hard_deny.classify_command("bash -c 'rm -rf /Users/alice'")
    assert verdict.denied


def test_sh_c_wrapped_rm_rf_is_denied():
    verdict = _hard_deny.classify_command('sh -c "rm -rf /Users/alice"')
    assert verdict.denied


def test_zsh_c_wrapped_rm_rf_is_denied():
    verdict = _hard_deny.classify_command("zsh -c 'rm -rf /Users/alice'")
    assert verdict.denied


def test_nested_env_and_sh_c_wrapped_rm_rf_is_denied():
    verdict = _hard_deny.classify_command("env FOO=1 sh -c 'rm -rf /Users/alice'")
    assert verdict.denied


def test_timeout_wrapped_rm_rf_is_denied():
    verdict = _hard_deny.classify_command("timeout 10 rm -rf /Users/alice")
    assert verdict.denied


def test_nohup_wrapped_rm_rf_is_denied():
    verdict = _hard_deny.classify_command("nohup rm -rf /Users/alice")
    assert verdict.denied


def test_xargs_rm_rf_is_denied_for_lack_of_a_provable_target():
    # xargs' real targets come from stdin, invisible to static analysis — no
    # visible target argument means `_rm_verdict` can't prove it's safe, and
    # fails closed the same way a target-less `rm -rf` already does.
    verdict = _hard_deny.classify_command("xargs rm -rf")
    assert verdict.denied


def test_find_delete_is_denied():
    verdict = _hard_deny.classify_command("find . -delete")
    assert verdict.denied


def test_bash_c_wrapped_benign_command_is_not_denied():
    verdict = _hard_deny.classify_command("bash -c 'echo hello'")
    assert not verdict.denied


def test_bash_c_wrapped_command_touching_user_root_is_denied():
    denied = _hard_deny.command_touches_protected_path(
        "bash -c 'cat ~/.jones/secrets/vault.enc'",
        user_root="~/.jones", project_permissions_path=None, cwd=None,
    )
    assert denied
