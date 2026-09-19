"""`replay/store.py` — payload write/read/purge and ref-path safety (Issue #12,
docs/design/02-w3-interfaces.md §2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from jones_daemon.replay import store


def test_write_then_read_payload_roundtrips(tmp_path: Path) -> None:
    ref = store.write_payload(tmp_path, "run-1", 3, b'{"ok": true}', "json")
    assert ref == "run-1/3.json"
    assert store.read_payload(tmp_path, ref) == b'{"ok": true}'
    assert (tmp_path / "runs" / "run-1" / "3.json").is_file()


def test_write_prompt_snapshot_roundtrips(tmp_path: Path) -> None:
    ref = store.write_prompt_snapshot(tmp_path, "run-2", {"user_message": "hi"})
    assert ref == "run-2/prompt_snapshot.json"
    data = store.read_payload(tmp_path, ref)
    assert b'"user_message"' in data
    assert b'"hi"' in data


def test_read_payload_with_offset_and_limit(tmp_path: Path) -> None:
    ref = store.write_payload(tmp_path, "run-3", 1, b"0123456789", "txt")
    assert store.read_payload(tmp_path, ref, offset=2, limit=3) == b"234"
    assert store.read_payload(tmp_path, ref, offset=8) == b"89"


def test_payload_size(tmp_path: Path) -> None:
    ref = store.write_payload(tmp_path, "run-4", 1, b"0123456789", "txt")
    assert store.payload_size(tmp_path, ref) == 10


def test_read_payload_unknown_ref_raises(tmp_path: Path) -> None:
    with pytest.raises(store.PayloadRefError):
        store.read_payload(tmp_path, "run-does-not-exist/1.json")


def test_read_payload_rejects_path_traversal(tmp_path: Path) -> None:
    # A secret file that genuinely exists just outside runs/ — proves the
    # traversal check isn't merely "the file doesn't exist" (PRD N10/10.4: this
    # module must never become a path to reading files outside runs/).
    secret = tmp_path / "secrets" / "vault.enc"
    secret.parent.mkdir(parents=True)
    secret.write_bytes(b"top secret")
    with pytest.raises(store.PayloadRefError):
        store.read_payload(tmp_path, "../secrets/vault.enc")


def test_read_payload_rejects_an_absolute_path_ref(tmp_path: Path) -> None:
    secret = tmp_path / "secrets" / "vault.enc"
    secret.parent.mkdir(parents=True)
    secret.write_bytes(b"top secret")
    with pytest.raises(store.PayloadRefError):
        store.read_payload(tmp_path, str(secret))


def test_purge_run_deletes_the_whole_directory(tmp_path: Path) -> None:
    store.write_payload(tmp_path, "run-5", 1, b"a", "txt")
    store.write_payload(tmp_path, "run-5", 2, b"b", "txt")
    store.write_prompt_snapshot(tmp_path, "run-5", {"note": "x"})
    assert (tmp_path / "runs" / "run-5").is_dir()

    store.purge_run(tmp_path, "run-5")

    assert not (tmp_path / "runs" / "run-5").exists()


def test_purge_run_on_a_run_with_no_payloads_is_a_silent_no_op(tmp_path: Path) -> None:
    # Never written -> never had a directory. Must not raise (retention.py sweeps
    # every expired Run unconditionally, including ones with no payload at all
    # once a Step exists but its payload_ref was never set).
    store.purge_run(tmp_path, "run-never-existed")
