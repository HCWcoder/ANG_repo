"""Editable test song IDs are canonical and saved without changing bad inputs."""

import json

import pytest

from anghami_session import test_settings
from anghami_session.errors import SessionError
from anghami_session.play_record import TEST_SONG_ID


@pytest.mark.parametrize("value, expected", [
    (1, "1"), ("1", "1"), (1263607749, "1263607749"),
    ("1280677978", "1280677978"), (2**63 - 1, str(2**63 - 1)),
    (str(2**63 - 1), str(2**63 - 1)),
])
def test_song_id_validation_returns_canonical_decimal_string(value, expected):
    assert test_settings.validate_test_song_id(value) == expected


@pytest.mark.parametrize("value", [
    None, True, False, 0, -1, 1.0, [], {}, "", "0", "00", "01", "+1", "-1",
    " 1", "1 ", "1\n", "1\t", "1\0", "1.0", "1e3", "١٢٣", "１２３",
    2**63, str(2**63), "9" * 1000, "synthetic-secret-invalid-song-id",
])
def test_invalid_song_id_is_rejected_without_echoing_input(value):
    with pytest.raises(SessionError) as error:
        test_settings.validate_test_song_id(value)
    assert "synthetic-secret-invalid-song-id" not in str(error.value)


def test_missing_setting_uses_default_without_creating_file(tmp_path):
    path = tmp_path / "test-settings.json"
    assert test_settings.read_test_song_id(path) == TEST_SONG_ID
    assert not path.exists()


def test_setting_persists_and_reloads_canonical_id(tmp_path):
    path = tmp_path / "test-settings.json"
    test_settings.write_test_song_id(1280677978, path)
    assert json.loads(path.read_text(encoding="utf-8")) == {"test_song_id": "1280677978"}
    assert test_settings.read_test_song_id(path) == "1280677978"
    test_settings.write_test_song_id("1263607749", path)
    assert test_settings.read_test_song_id(path) == "1263607749"
    assert sorted(item.name for item in tmp_path.iterdir()) == ["test-settings.json"]


@pytest.mark.parametrize("value", [0, "01", True, "synthetic-secret-invalid-song-id"])
def test_invalid_edit_preserves_previous_setting_and_leaves_no_temp_file(tmp_path, value):
    path = tmp_path / "test-settings.json"
    test_settings.write_test_song_id("1280677978", path)
    before = path.read_bytes()
    with pytest.raises(SessionError):
        test_settings.write_test_song_id(value, path)
    assert path.read_bytes() == before
    assert test_settings.read_test_song_id(path) == "1280677978"
    assert sorted(item.name for item in tmp_path.iterdir()) == ["test-settings.json"]


@pytest.mark.parametrize("raw", [
    b"{", b"null", b"[]", b'"text"', b"{}", b'{"song_id":"1280677978"}',
    b'{"test_song_id":"1280677978","unknown":"synthetic-secret"}',
    b'{"test_song_id":null}', b'{"test_song_id":true}', b'{"test_song_id":"01"}',
    b'{"test_song_id":"0"}', b'{"test_song_id":"9223372036854775808"}', b"\xff\xfe\xfd",
])
def test_malformed_existing_setting_fails_closed_and_is_not_rewritten(tmp_path, raw):
    path = tmp_path / "test-settings.json"
    path.write_bytes(raw)
    with pytest.raises(SessionError) as error:
        test_settings.read_test_song_id(path)
    assert "synthetic-secret" not in str(error.value)
    assert path.read_bytes() == raw


def test_directory_at_settings_path_is_a_failure_not_default(tmp_path):
    path = tmp_path / "test-settings.json"
    path.mkdir()
    with pytest.raises(SessionError):
        test_settings.read_test_song_id(path)


def test_oversized_setting_fails_closed_without_changing_file(tmp_path):
    path = tmp_path / "test-settings.json"
    raw = b'{"test_song_id":"1280677978"}' + b" " * 4096
    path.write_bytes(raw)
    with pytest.raises(SessionError):
        test_settings.read_test_song_id(path)
    assert path.read_bytes() == raw


@pytest.mark.parametrize("failed_operation", ["fsync", "replace"])
def test_write_io_failure_preserves_previous_file_and_removes_temporary(tmp_path, monkeypatch, failed_operation):
    path = tmp_path / "test-settings.json"
    test_settings.write_test_song_id("1263607749", path)
    before = path.read_bytes()

    def fail(*_, **__):
        raise OSError("synthetic-secret-io-error")

    monkeypatch.setattr(test_settings.os, failed_operation, fail)
    with pytest.raises(SessionError) as error:
        test_settings.write_test_song_id("1280677978", path)
    assert "synthetic-secret-io-error" not in str(error.value)
    assert path.read_bytes() == before
    assert sorted(item.name for item in tmp_path.iterdir()) == ["test-settings.json"]
