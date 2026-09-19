"""config/jsonfile.py: the read/write primitives `ConfigResolver` and
`config/methods.py` build on (docs/design/01-w2-interfaces.md §4, review round 1
finding #7 — atomic writes, and telling "missing" apart from "broken" on read).
"""

import pytest

from jones_daemon.config.jsonfile import read_json, read_json_result, write_json


def test_read_json_result_missing_file_is_ok_with_no_data(tmp_path):
    data, ok = read_json_result(tmp_path / "nope.json")
    assert data is None
    assert ok is True


def test_read_json_result_valid_object_is_ok(tmp_path):
    path = tmp_path / "f.json"
    write_json(path, {"a": 1})
    data, ok = read_json_result(path)
    assert data == {"a": 1}
    assert ok is True


def test_read_json_result_corrupt_json_is_not_ok(tmp_path):
    path = tmp_path / "f.json"
    path.write_text("{not valid json")
    data, ok = read_json_result(path)
    assert data is None
    assert ok is False


def test_read_json_result_non_object_json_is_not_ok(tmp_path):
    path = tmp_path / "f.json"
    path.write_text("[1, 2, 3]")
    data, ok = read_json_result(path)
    assert data is None
    assert ok is False


def test_read_json_falls_back_to_default_on_either_missing_or_corrupt(tmp_path):
    missing = tmp_path / "missing.json"
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not valid json")

    assert read_json(missing, {"x": 1}) == {"x": 1}
    assert read_json(corrupt, {"x": 1}) == {"x": 1}


def test_write_json_is_atomic_no_tmp_file_left_behind(tmp_path):
    path = tmp_path / "f.json"
    write_json(path, {"a": 1})

    assert path.read_text().strip() == '{\n  "a": 1\n}'
    leftovers = [p for p in tmp_path.iterdir() if p != path]
    assert leftovers == []  # no .tmp sibling survives a successful write


def test_write_json_never_leaves_a_partial_file_on_a_failed_replace(tmp_path, monkeypatch):
    path = tmp_path / "f.json"
    write_json(path, {"a": 1})  # existing, valid content

    def _boom(*args, **kwargs):
        raise OSError("simulated crash mid-replace")

    monkeypatch.setattr("jones_daemon.config.jsonfile.os.replace", _boom)

    with pytest.raises(OSError):
        write_json(path, {"a": 2})

    # The original file is untouched — a reader never sees a torn write — and the
    # temp file used to stage the new content doesn't leak either.
    assert path.read_text().strip() == '{\n  "a": 1\n}'
    leftovers = [p for p in tmp_path.iterdir() if p != path]
    assert leftovers == []
