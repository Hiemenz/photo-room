"""
Tests for damaged and unreadable files.

A card with a read error, or an interrupted copy, produces truncated or
empty files. None of them may stop a batch or take down the editor.
"""

from pathlib import Path

import pytest

import editor
from editor import describe_error


# ---------------------------------------------------------------------------
# Error messages
# ---------------------------------------------------------------------------

def test_a_bytes_payload_is_unwrapped():
    """
    rawpy raises with bytes, so the naive str() reads b'Input/output error'
    and leaks Python repr into the interface.
    """
    message = describe_error(Exception("b'Input/output error'"))
    assert not message.startswith("b'")
    assert "Input/output error" in message


def test_an_io_error_from_the_decoder_mentions_a_corrupt_file():
    """The user-facing hint, as opposed to the bytes-unwrapping test above."""
    message = describe_error(Exception("b'Input/output error'"))
    assert "truncated or corrupt" in message and not message.startswith("b'")


def test_a_truncation_error_explains_itself():
    assert "truncated or corrupt" in describe_error(Exception("Unexpected end of file"))


def test_an_ordinary_message_is_left_alone():
    assert describe_error(ValueError("No such file: ghost.dng")) == "No such file: ghost.dng"


def test_an_empty_message_falls_back_to_the_type():
    assert describe_error(RuntimeError("")) == "RuntimeError"


def test_double_quoted_bytes_are_unwrapped():
    assert describe_error(Exception('b"disk failure"')) == "disk failure"


# ---------------------------------------------------------------------------
# The editor survives them
# ---------------------------------------------------------------------------

@pytest.fixture
def broken_library(tmp_path, monkeypatch):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    (tmp_path / "empty.dng").write_bytes(b"")
    (tmp_path / "garbage.dng").write_bytes(b"not a raw file")
    (tmp_path / "good.dng").write_bytes(b"dng")
    return tmp_path


@pytest.fixture
def client(broken_library):
    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        yield c


@pytest.fixture
def failing_decoder(monkeypatch):
    def boom(path):
        if "good" in str(path):
            import numpy as np
            return np.full((64, 96, 3), 6000, dtype=np.uint16), 5000, np.ones(4)
        raise OSError(b"Input/output error")

    monkeypatch.setattr(editor, "decode_preview_linear", boom)


def test_opening_a_damaged_file_returns_a_readable_error(client, failing_decoder, fake_exiftool):
    fake_exiftool.stdout_lines = ["-"] * 21
    res = client.post("/api/open", json={"path": "garbage.dng"})

    assert res.status_code == 500
    error = res.get_json()["error"]
    assert not error.startswith("b'")
    assert "truncated or corrupt" in error


def test_a_damaged_file_does_not_break_the_next_good_one(client, failing_decoder, fake_exiftool):
    """One bad frame in a shoot must not poison the session."""
    fake_exiftool.stdout_lines = ["-"] * 21
    client.post("/api/open", json={"path": "garbage.dng"})

    res = client.post("/api/open", json={"path": "good.dng"})
    assert res.status_code == 200
    assert res.get_json()["preview"].startswith("data:image/jpeg")


def test_a_damaged_file_is_not_left_in_the_cache(client, failing_decoder, fake_exiftool):
    """A failed decode must not poison the cache with a half-built entry."""
    fake_exiftool.stdout_lines = ["-"] * 21
    before = editor.cache_stats()
    res = client.post("/api/open", json={"path": "garbage.dng"})

    assert res.status_code == 500                   # the failure really happened...
    stats = editor.cache_stats()                    # ...and left nothing behind
    assert stats["previews"] == before["previews"]
    assert stats["decodes"] == before["decodes"]
    assert str((editor.LIBRARY_ROOT / "garbage.dng").resolve()) not in editor._base_cache


def test_previewing_a_damaged_file_fails_cleanly(client, failing_decoder):
    res = client.post("/api/preview", json={"path": "empty.dng", "adjustments": {}})
    assert res.status_code == 500
    assert "error" in res.get_json()


def test_damaged_files_still_appear_in_the_library(client):
    """
    They must be listed, not hidden: a photographer needs to see that a
    frame arrived damaged rather than silently lose it.
    """
    files = client.get("/api/files").get_json()["files"]
    assert set(files) == {"empty.dng", "garbage.dng", "good.dng"}
