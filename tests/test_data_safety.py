"""Atomic, serialised writes and DNG-safety (security review M3, M4, M5)."""

import hashlib
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest
from PIL import Image

import editor
import fsutil
import history
import library
import masks


def run_threads(n, fn):
    errors = []

    def wrap(i):
        try:
            fn(i)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=wrap, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return errors


# -- fsutil ----------------------------------------------------------------

def test_atomic_write_keeps_old_file_when_the_rename_fails(tmp_path, monkeypatch):
    target = tmp_path / "x.json"
    target.write_text("old")

    def boom(*a):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        fsutil.atomic_write_text(target, "new")
    assert target.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]  # no stray temp


def test_atomic_write_fsyncs_before_rename(tmp_path, monkeypatch):
    order = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b))[1])
    fsutil.atomic_write_text(tmp_path / "y.json", "{}")
    assert order.index("fsync") < order.index("replace")


# -- history / masks -------------------------------------------------------

def test_concurrent_history_records_neither_corrupt_nor_lose_steps(tmp_path):
    photo = tmp_path / "IMG_1.CR2"
    photo.write_bytes(b"raw")
    errors = run_threads(
        40, lambda i: history.record(photo, f"s{i}", {"exposure": i / 10}))
    assert errors == []
    data = json.loads(history.history_path(photo).read_text())  # valid JSON
    assert len(data["entries"]) == 40
    assert not list(tmp_path.glob("*.tmp"))


def test_corrupt_history_is_set_aside_not_silently_discarded(tmp_path):
    photo = tmp_path / "IMG_1.CR2"
    photo.write_bytes(b"raw")
    history.history_path(photo).write_text('{"entries": [')
    assert history.load(photo)["entries"] == []
    assert history.history_path(photo).with_name(
        history.history_path(photo).name + ".bad").exists()


def test_concurrent_mask_saves_stay_valid(tmp_path):
    photo = tmp_path / "IMG_1.CR2"
    photo.write_bytes(b"raw")
    mask = {"id": "m1", "type": "radial", "cx": 0.5, "cy": 0.5, "rx": 0.2, "ry": 0.2,
            "feather": 0.5, "invert": False, "adjustments": {"exposure": 0.5}}
    errors = run_threads(30, lambda i: masks.save_sidecar(photo, [dict(mask, id=f"m{i}")]))
    assert errors == []
    json.loads(masks.sidecar_path(photo).read_text())


# -- real exiftool: sidecar races and DNG safety ---------------------------

def _have_exiftool():
    import shutil
    return shutil.which("exiftool") is not None


needs_exiftool = pytest.mark.skipif(not _have_exiftool(), reason="exiftool not installed")


def _exif(*args):
    return subprocess.run(["exiftool", *args], capture_output=True, text=True).stdout


@pytest.mark.exiftool
@needs_exiftool
def test_concurrent_ratings_never_wipe_an_existing_sidecar(tmp_path):
    for trial in range(3):
        raw = tmp_path / f"IMG_{trial}.CR2"
        raw.write_bytes(b"not really a raw")
        side = raw.with_suffix(".xmp")
        subprocess.run(["exiftool", "-XMP-dc:Subject=keepme", "-XMP-crs:Exposure2012=1.5",
                        "-o", str(side), os.devnull], capture_output=True)
        assert side.exists()
        errors = run_threads(16, lambda i: editor.write_rating(raw, (i % 5) + 1))
        assert errors == []
        out = _exif("-s3", "-XMP-dc:Subject", "-XMP-crs:Exposure2012", str(side))
        assert "keepme" in out and "1.50" in out.replace("+", "") or "1.5" in out


def _tiny_dng(path: Path) -> bytes:
    Image.new("RGB", (8, 8), (10, 20, 30)).save(path, "TIFF")
    return path.read_bytes()


@pytest.mark.exiftool
@needs_exiftool
def test_rating_a_dng_does_not_touch_the_dng(tmp_path, monkeypatch):
    monkeypatch.delenv("PHOTO_ROOM_EMBED_DNG", raising=False)
    dng = tmp_path / "L1000001.dng"
    before = _tiny_dng(dng)
    editor.write_rating(dng, 4)
    assert dng.read_bytes() == before
    assert dng.with_suffix(".xmp").exists()
    assert editor.read_rating(dng) == 4


@pytest.mark.exiftool
@needs_exiftool
def test_labels_keywords_and_saves_on_a_dng_use_the_sidecar(tmp_path, monkeypatch):
    monkeypatch.delenv("PHOTO_ROOM_EMBED_DNG", raising=False)
    dng = tmp_path / "L1000002.dng"
    before = _tiny_dng(dng)
    library.write_xmp(dng, label="Red", keywords=["a", "b"])
    editor.save_adjustments(dng, {"exposure": 0.5})
    assert hashlib.sha256(dng.read_bytes()).digest() == hashlib.sha256(before).digest()
    side = dng.with_suffix(".xmp")
    assert "Red" in _exif("-s3", "-XMP-xmp:Label", str(side))


def test_dng_edits_target_the_sidecar_unless_opted_in(fake_exiftool, monkeypatch, tmp_path):
    dng = tmp_path / "x.dng"
    dng.write_bytes(b"dng")
    monkeypatch.delenv("PHOTO_ROOM_EMBED_DNG", raising=False)
    editor.write_rating(dng, 3)
    editor.save_adjustments(dng, {"exposure": 1.0})
    library.write_xmp(dng, label="Red")
    for call in fake_exiftool.calls:
        assert str(dng) not in call, call
    monkeypatch.setenv("PHOTO_ROOM_EMBED_DNG", "1")
    fake_exiftool.calls.clear()
    editor.write_rating(dng, 3)
    assert str(dng) in fake_exiftool.calls[-1]
