"""JPEG / HEIC / video files (what watch-cards copies) are listed by the Library."""

import io

import pytest
from PIL import Image

import library
import libindex
import thumbs


def _jpeg(path, size=(400, 200), orientation=None):
    img = Image.new("RGB", size, (200, 80, 40))
    exif = Image.Exif()
    if orientation:
        exif[274] = orientation
    img.save(path, format="JPEG", exif=exif)


@pytest.fixture
def idx(tmp_path, monkeypatch):
    monkeypatch.setattr(libindex, "DB_ENABLED", False)
    (tmp_path / "A.CR2").write_bytes(b"raw")
    _jpeg(tmp_path / "A.JPG")                 # RAW+JPEG pair: only the raw is listed
    _jpeg(tmp_path / "B.jpg")
    (tmp_path / "C.HEIC").write_bytes(b"heic")
    (tmp_path / "D.MP4").write_bytes(b"video")
    (tmp_path / "notes.txt").write_text("x")

    def fake_read(self, jobs):
        return {rel: libindex.parse_meta({}, None) for rel, _ in jobs}

    monkeypatch.setattr(libindex.Index, "_read_batch", fake_read)
    i = libindex.Index(tmp_path, None)
    i.scan()
    yield i
    i.close()


def test_kind_of():
    assert library.kind_of("x.CR2") == "raw"
    assert library.kind_of("x.JPeG") == "image"
    assert library.kind_of("x.heif") == "image"
    assert library.kind_of("x.MOV") == "video"
    assert library.kind_of("x.txt") == ""


def test_index_lists_all_kinds_with_editable_flag(idx):
    recs = {r["path"]: r for r in idx.items()}
    assert set(recs) == {"A.CR2", "B.jpg", "C.HEIC", "D.MP4"}
    assert recs["A.CR2"]["kind"] == "raw" and recs["A.CR2"]["editable"] is True
    assert recs["B.jpg"]["kind"] == "image" and recs["B.jpg"]["editable"] is False
    assert recs["C.HEIC"]["editable"] is False
    assert recs["D.MP4"]["kind"] == "video" and recs["D.MP4"]["editable"] is False


def test_jpeg_thumbnail_applies_orientation(tmp_path):
    p = tmp_path / "o.jpg"
    _jpeg(p, (400, 200), orientation=6)       # rotate 90: becomes portrait
    img = Image.open(io.BytesIO(thumbs.build(p)))
    assert img.height > img.width


def test_jpeg_thumbnail_all_sizes_cached(tmp_path):
    p = tmp_path / "s.jpg"
    _jpeg(p)
    thumbs.get_all(p, tmp_path)
    assert all(thumbs.cache_path(p, tmp_path, s).exists() for s in thumbs.SIZES)


def test_video_has_no_thumbnail(tmp_path):
    p = tmp_path / "v.mp4"
    p.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="video"):
        thumbs.build(p)


def test_undecodable_heic_degrades_to_error_not_crash(tmp_path):
    p = tmp_path / "x.heic"
    p.write_bytes(b"not an image")
    with pytest.raises(RuntimeError, match="Cannot decode"):
        thumbs.build(p)
