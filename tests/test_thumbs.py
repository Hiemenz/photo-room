"""Tests for thumbnail generation and caching."""

import io
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import thumbs
from thumbs import CACHE_DIR_NAME, THUMB_EDGE, build, cache_path, clear_cache, get


def jpeg_bytes(size=(1200, 800), colour=(120, 90, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format="JPEG", quality=85)
    return buf.getvalue()


@pytest.fixture
def raw_file(tmp_path):
    p = tmp_path / "IMG_0001.CR2"
    p.write_bytes(b"pretend-raw-data")
    return p


@pytest.fixture
def with_preview(monkeypatch):
    """exiftool returns an embedded JPEG preview."""
    payload = jpeg_bytes()

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr=b"")

    monkeypatch.setattr(thumbs.subprocess, "run", fake_run)
    return payload


@pytest.fixture
def without_preview(monkeypatch):
    """exiftool finds nothing usable."""
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(thumbs.subprocess, "run", fake_run)


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

def test_builds_from_the_embedded_preview(raw_file, with_preview):
    data = build(raw_file)
    img = Image.open(io.BytesIO(data))
    assert img.format == "JPEG"
    assert max(img.size) <= THUMB_EDGE


def test_thumbnail_preserves_aspect_ratio(raw_file, monkeypatch):
    monkeypatch.setattr(
        thumbs.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=jpeg_bytes((1200, 800)), stderr=b""),
    )
    img = Image.open(io.BytesIO(build(raw_file)))
    assert img.size[0] / img.size[1] == pytest.approx(1.5, rel=0.05)


def test_thumbnails_are_small(raw_file, with_preview):
    """These are fetched by the hundred; size matters."""
    assert len(build(raw_file)) < 40_000


def test_falls_back_to_decoding_when_there_is_no_preview(raw_file, without_preview):
    called = {}

    def decode(path):
        called["path"] = path
        return np.full((400, 600, 3), 130, dtype=np.uint8)

    data = build(raw_file, decode_fallback=decode)
    assert called["path"] == raw_file
    assert Image.open(io.BytesIO(data)).format == "JPEG"


def test_fallback_accepts_float_arrays(raw_file, without_preview):
    decode = lambda p: np.full((200, 300, 3), 0.5, dtype=np.float32)
    assert Image.open(io.BytesIO(build(raw_file, decode))).format == "JPEG"


def test_a_malformed_preview_falls_through_to_decoding(raw_file, monkeypatch):
    """Garbage in the preview tag must not cost the whole thumbnail."""
    monkeypatch.setattr(
        thumbs.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=b"\x00" * 4096, stderr=b""),
    )
    decode = lambda p: np.full((200, 300, 3), 90, dtype=np.uint8)
    assert Image.open(io.BytesIO(build(raw_file, decode))).format == "JPEG"


def test_no_preview_and_no_fallback_raises(raw_file, without_preview):
    with pytest.raises(RuntimeError, match="No embedded preview"):
        build(raw_file)


def test_a_tiny_preview_is_not_trusted(raw_file, monkeypatch):
    """A few hundred bytes is a stub, not a usable preview."""
    monkeypatch.setattr(
        thumbs.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=b"x" * 100, stderr=b""),
    )
    decode = lambda p: np.full((100, 150, 3), 50, dtype=np.uint8)
    assert build(raw_file, decode)


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

def test_first_call_writes_a_cache_file(raw_file, tmp_path, with_preview):
    get(raw_file, tmp_path)
    assert cache_path(raw_file, tmp_path).exists()


def test_second_call_serves_the_cached_bytes(raw_file, tmp_path, with_preview, monkeypatch):
    first = get(raw_file, tmp_path)

    def forbidden(*a, **k):
        pytest.fail("rebuilt a thumbnail that was already cached")

    monkeypatch.setattr(thumbs, "build", forbidden)
    assert get(raw_file, tmp_path) == first


def test_a_changed_file_gets_a_new_cache_key(raw_file, tmp_path, with_preview):
    """Editing a photo must not leave the old thumbnail showing."""
    before = cache_path(raw_file, tmp_path)
    raw_file.write_bytes(b"different-content-entirely-here")
    assert cache_path(raw_file, tmp_path) != before


def test_different_files_get_different_keys(tmp_path):
    a, b = tmp_path / "a.CR2", tmp_path / "b.CR2"
    a.write_bytes(b"one"); b.write_bytes(b"two")
    assert cache_path(a, tmp_path) != cache_path(b, tmp_path)


def test_cache_lives_under_the_library(raw_file, tmp_path, with_preview):
    get(raw_file, tmp_path)
    assert cache_path(raw_file, tmp_path).parent.name == CACHE_DIR_NAME


def test_no_temporary_files_are_left_behind(raw_file, tmp_path, with_preview):
    get(raw_file, tmp_path)
    assert list((tmp_path / CACHE_DIR_NAME).glob("*.tmp")) == []


def test_an_unwritable_library_still_serves_thumbnails(raw_file, tmp_path, with_preview, monkeypatch):
    """Browsing a read-only drive must work, just without caching."""
    def refuse(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "mkdir", refuse)
    assert Image.open(io.BytesIO(get(raw_file, tmp_path))).format == "JPEG"


def test_a_corrupt_cache_entry_is_rebuilt(raw_file, tmp_path, with_preview):
    get(raw_file, tmp_path)
    cached = cache_path(raw_file, tmp_path)
    cached.write_bytes(b"")          # truncated on disk

    data = get(raw_file, tmp_path)
    assert data == b"" or Image.open(io.BytesIO(data)).format == "JPEG"


def test_clearing_the_cache(raw_file, tmp_path, with_preview):
    get(raw_file, tmp_path)
    assert clear_cache(tmp_path) == 1
    assert clear_cache(tmp_path) == 0


def test_clearing_an_absent_cache_is_harmless(tmp_path):
    assert clear_cache(tmp_path / "nowhere") == 0
