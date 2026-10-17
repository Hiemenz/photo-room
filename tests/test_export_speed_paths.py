"""The export speed-ups must not change what lands on disk."""
import shutil
import struct
import subprocess
import zlib

import numpy as np
import pytest
from PIL import Image

import exporter


def _png16_samples(path):
    data = path.read_bytes()
    pos, idat, w, h = 8, b"", 0, 0
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            w, h = struct.unpack(">II", body[:8])
        if kind == b"IDAT":
            idat += body
        pos += 12 + n
    raw = np.frombuffer(zlib.decompress(idat), dtype=np.uint8).reshape(h, 1 + w * 6)
    assert (raw[:, 0] == 0).all()
    return raw[:, 1:].copy().view(">u2").reshape(h, w, 3)


@pytest.mark.parametrize("level", [0, 1, 6])
def test_png16_writer_round_trips_values_at_any_level(tmp_path, level):
    arr = np.random.default_rng(1).random((300, 1100, 3), dtype=np.float32)       # > MIN_PIXELS: threaded
    exporter.write_png16(tmp_path / "a.png", arr, b"icc-bytes", level=level)
    expect = (np.clip(arr, 0, 1) * 65535.0 + 0.5).astype(np.uint16)
    assert np.array_equal(_png16_samples(tmp_path / "a.png"), expect)


def test_png_compression_level_changes_size_not_pixels(tmp_path):
    img = Image.fromarray((np.add.outer(np.arange(200), np.arange(300))[..., None] % 256 * np.ones(3)).astype(np.uint8))
    sizes = {}
    for level in (1, 9):
        p = tmp_path / f"l{level}.png"
        img.save(p, "PNG", compress_level=level)
        sizes[level] = p.stat().st_size
        assert np.array_equal(np.asarray(Image.open(p)), np.asarray(img))
    assert sizes[9] <= sizes[1]


def test_options_validate_encoder_knobs():
    assert exporter.ExportOptions(png_compression=3).validate().png_compression == 3
    with pytest.raises(ValueError):
        exporter.ExportOptions(png_compression=12).validate()
    assert exporter.ExportOptions().png_compression == 1 and not exporter.ExportOptions().optimize_jpeg


@pytest.mark.skipif(not shutil.which("exiftool"), reason="needs exiftool")
@pytest.mark.exiftool
def test_png_metadata_splice_equals_a_direct_exiftool_rewrite(tmp_path, monkeypatch):
    """Tags written on a 1x1 stand-in and spliced in are the tags exiftool writes on the real file."""
    import json
    src = tmp_path / "IMG_1.jpg"
    Image.fromarray((np.add.outer(np.arange(64), np.arange(96))[..., None] % 256 * np.ones(3)).astype(np.uint8)).save(src, "JPEG")
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-Make=Acme", "-Model=Cam 9", "-Artist=Spliced",
                    "-GPSLatitude=51.5", "-GPSLatitudeRef=N", "-GPSLongitude=0.1", "-GPSLongitudeRef=W",
                    "-IPTC:Keywords=one", "-XMP-dc:Subject=two", "-XMP-xmp:Rating=3", str(src)], check=True)
    opts = exporter.ExportOptions(format="png").validate()

    def run(flag):
        monkeypatch.setenv("PHOTO_ROOM_PNG_SPLICE", flag)
        out = exporter.export_photo(src, tmp_path / ("spliced" if flag == "1" else "direct"), opts)
        res = subprocess.run(["exiftool", "-j", "-a", "-G1", "-n", "-struct", str(out)], capture_output=True, text=True)
        tags = json.loads(res.stdout)[0]
        for k in list(tags):
            if k.startswith(("File:", "System:", "ExifTool:", "PNG:Compression")) or k in ("SourceFile", "ICC-header:ProfileDateTime"):
                tags.pop(k)
        return tags, out

    (a, pa), (b, pb) = run("1"), run("0")
    assert a == b and any(k.endswith("Make") for k in a) and any("GPSLatitude" in k for k in a)
    assert np.array_equal(np.asarray(Image.open(pa)), np.asarray(Image.open(pb)))
    icc = lambda p: Image.open(p).info.get("icc_profile")      # profiles carry a creation time: compare all but that
    assert icc(pa) and len(icc(pa)) == len(icc(pb)) and icc(pa)[:24] == icc(pb)[:24] and icc(pa)[48:] == icc(pb)[48:]


# --- camera default crop -----------------------------------------------------

class _Sizes:
    def __init__(self, **kw):
        self.__dict__.update(dict(iwidth=3906, iheight=2602, left_margin=42, top_margin=18, flip=0,
                                  crop_left_margin=52, crop_top_margin=24, crop_width=3888, crop_height=2591))
        self.__dict__.update(kw)


class _Raw:
    def __init__(self, **kw):
        self.sizes = _Sizes(**kw)


def test_camera_crop_matches_the_canon_1000d_frame():
    import process
    assert process.camera_crop(_Raw()) == (6, 10, 2591, 3888)
    assert process.camera_crop(_Raw(), scale=2) == (3, 5, 1295, 1944)


@pytest.mark.parametrize("kw", [dict(flip=6), dict(crop_width=0), dict(crop_left_margin=30),
                                dict(crop_width=3906, crop_height=2602, crop_left_margin=42, crop_top_margin=18),
                                dict(crop_width=4000)])
def test_camera_crop_is_ignored_when_absent_rotated_or_inconsistent(kw):
    import process
    assert process.camera_crop(_Raw(**kw)) is None


def test_no_sizes_means_no_crop():
    import process
    assert process.camera_crop(object()) is None


@pytest.mark.parametrize("full_sensor,expected", [(False, (3888, 2591)), (True, (3906, 2602))])
def test_export_uses_the_camera_crop_unless_full_sensor(tmp_path, monkeypatch, fake_rawpy, full_sensor, expected):
    import process
    rgb = np.zeros((2602, 3906, 3), dtype=np.uint8)
    rgb[6, 10] = 255                      # first pixel inside the crop
    fake_rawpy["rgb"] = rgb
    inner = process.rawpy.imread

    def imread(path):
        r = inner(path)
        r.sizes = _Sizes()
        return r
    monkeypatch.setattr(process.rawpy, "imread", imread)
    raw = tmp_path / "a.CR2"
    raw.write_bytes(b"x")
    monkeypatch.setattr(exporter, "_saved_adjustments", lambda p, *a, **k: {})
    opts = exporter.ExportOptions(format="png", keep_metadata=False, full_sensor=full_sensor)
    out = exporter.export_photo(raw, tmp_path / "o", opts)
    im = Image.open(out)
    assert im.size == expected
    assert (np.asarray(im)[0, 0] == 255).all() == (not full_sensor)
