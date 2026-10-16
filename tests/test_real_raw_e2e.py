"""
End to end on a REAL Canon CR2: decode with rawpy, edit, export, compare tags.

Skipped by default (it needs a ~10 MB download). Enable with either

    PHOTO_ROOM_REAL_RAW=1 pytest -m realraw      # downloads the sample once
    PHOTO_ROOM_RAW_SAMPLE=/path/to/any.CR2 pytest -m realraw

Sample: ``IMG_9406.CR2`` (Canon EOS 1000D, 3888x2592, EF-S18-55mm) from
https://raw.pixls.us/ . Every sample on raw.pixls.us is released by its
uploader under CC0 1.0 (public domain dedication), so it may be fetched and
used freely; it is cached outside the repository (``~/.cache/photo-room-test-raw``)
and never committed. The download is pinned by SHA-256.
"""

import hashlib
import json
import os
import subprocess
import urllib.request
from pathlib import Path

import pytest

import editor
import exporter
from exporter import ExportOptions, export_photo

pytestmark = [pytest.mark.exiftool, pytest.mark.realraw]

SAMPLE_URL = "https://raw.pixls.us/data/Canon/EOS%201000D/IMG_9406.CR2"
SAMPLE_SHA256 = "08b0843a7968315b68bd3c71a96c952ebf275d5b5eba4b6094ae8b103522584c"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "photo-room-test-raw"

# Groups whose tags must survive. MakerNotes (Canon's own block) is included:
# a real raw has it, a synthetic JPEG cannot.
GROUPS = ("EXIF", "GPS", "IPTC", "XMP", "MakerNotes", "ICC_Profile")

# Describe the file or the render, not the photograph (each justified):
EXPECTED_TO_CHANGE = {
    "ImageWidth", "ImageHeight", "ExifImageWidth", "ExifImageHeight",   # output has its own size
    "Orientation",                                                       # applied to the pixels, then 1
    "ThumbnailImage", "ThumbnailOffset", "ThumbnailLength", "PreviewImage",  # of the unedited frame
    "PreviewImageStart", "PreviewImageLength", "Compression",
    "StripOffsets", "StripByteCounts", "RowsPerStrip", "BitsPerSample",
    "PhotometricInterpretation", "SamplesPerPixel", "PlanarConfiguration",
    "YCbCrSubSampling", "YCbCrPositioning", "ColorSpace", "ComponentsConfiguration",
    "XMPToolkit", "Format", "MetadataDate", "ModifyDate", "ExifVersion", "FlashpixVersion",
    # raw-specific: describe the sensor data / the CR2 container, not the photo
    "RawJpgSize", "RawDataOffset", "RawDataLength", "SensorWidth", "SensorHeight",
    "SensorLeftBorder", "SensorTopBorder", "SensorRightBorder", "SensorBottomBorder",
    "BlackMaskLeftBorder", "BlackMaskRightBorder", "BlackMaskTopBorder", "BlackMaskBottomBorder",
    "CanonImageWidth", "CanonImageHeight", "CanonImageSize",
    "OriginalDecisionDataOffset", "CRWParam", "CR2CFAPattern", "ColorData",
    "ColorDataVersion", "RawImageSegmentation", "SRawType",          # CR2 container structure
    # DCF interoperability marker of the *source* ("R98: sRGB basic file"); wrong for an
    # output that may carry Adobe RGB or ProPhoto, and exiftool does not write the IFD.
    "InteropIndex", "InteropVersion",
}


def _fetch() -> Path:
    override = os.environ.get("PHOTO_ROOM_RAW_SAMPLE")
    if override:
        return Path(override)
    if os.environ.get("PHOTO_ROOM_REAL_RAW") != "1":
        pytest.skip("set PHOTO_ROOM_REAL_RAW=1 (downloads ~10 MB) or PHOTO_ROOM_RAW_SAMPLE=<file>")
    CACHE.mkdir(parents=True, exist_ok=True)
    target = CACHE / "IMG_9406.CR2"
    if not target.exists():
        part = target.with_suffix(".part")
        with urllib.request.urlopen(SAMPLE_URL, timeout=120) as resp, open(part, "wb") as fh:
            fh.write(resp.read())
        if hashlib.sha256(part.read_bytes()).hexdigest() != SAMPLE_SHA256:
            part.unlink()
            pytest.fail("downloaded sample does not match its pinned SHA-256")
        part.replace(target)
    return target


@pytest.fixture(scope="module")
def sample() -> Path:
    return _fetch()


@pytest.fixture
def raw(sample, tmp_path):
    dest = tmp_path / sample.name
    dest.write_bytes(sample.read_bytes())
    return dest


def tags(path: Path) -> dict:
    out = subprocess.run(["exiftool", "-j", "-G0", "-a", "-n", "-struct", str(path)],
                         capture_output=True, text=True, check=True).stdout
    found = {}
    for key, value in json.loads(out)[0].items():
        group, _, name = key.partition(":")
        if group in GROUPS and name not in EXPECTED_TO_CHANGE and not name.startswith("CurrentIPTCDigest"):
            found.setdefault(f"{group}:{name}", value)
    return found


def _same(a, b) -> bool:
    if isinstance(a, list) and isinstance(b, list):
        return sorted(map(str, a)) == sorted(map(str, b))
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 1e-4
        except (TypeError, ValueError):
            return False
    return a == b


def test_rawpy_decodes_the_real_cr2(raw):
    import rawpy

    with rawpy.imread(str(raw)) as r:
        assert r.sizes.raw_width >= 3888 and r.sizes.raw_height >= 2592
        rgb = r.postprocess(use_camera_wb=True, no_auto_bright=True, half_size=True)
    assert rgb.ndim == 3 and rgb.shape[2] == 3 and rgb.mean() > 5


@pytest.mark.parametrize("fmt,depth", [("jpeg", 8), ("png", 8), ("png", 16)])
def test_edit_and_export_a_real_raw_keeps_every_tag(raw, tmp_path, fmt, depth):
    before = raw.read_bytes()
    adj = {"exposure": 0.4, "contrast": 12, "highlights": -30, "shadows": 20, "vibrance": 15,
           "clarity": 10, "sharpness": 30, "straighten": 1.5,
           "tone_curve": {"points": {"rgb": [[0, 0], [64, 52], [192, 204], [255, 255]]}},
           "masks": [{"type": "linear", "x": 0.5, "y": 0.3, "angle": 0, "feather": 60,
                      "adj": {"exposure": -0.5}}]}
    editor.save_adjustments(raw, adj, frame_size=(1944, 1296), source_size=(3888, 2592))
    editor.write_rating(raw, 4)
    import library
    library.write_xmp(raw, label="Blue", keywords=["real-raw", "cc0"])
    assert raw.read_bytes() == before, "the raw must never be modified"

    dest = export_photo(raw, tmp_path / "out", ExportOptions(
        format=fmt, bit_depth=depth, long_edge=1200,
        sharpen={"for": "screen", "amount": "standard"},
        watermark={"type": "text", "text": "test", "size": 3}))

    # Pixel geometry, not just tags: the file must be framed exactly like the
    # preview (straighten 1.5 trims to the inscribed rectangle ONCE).
    import numpy as np
    from PIL import Image
    with Image.open(dest) as exported:
        ew, eh = exported.size
    full_w, full_h = 3906, 2602                   # rawpy's full sensor area for this CR2
    scale = 1200 / max(full_w, full_h)
    scaled = (max(1, round(full_h * scale)), max(1, round(full_w * scale)), 3)
    expected = editor.apply_geometry(np.zeros(scaled, np.float32), {"straighten": 1.5}).shape
    assert (eh, ew) == expected[:2], f"export {(ew, eh)} != preview geometry {(expected[1], expected[0])}"
    assert max(ew, eh) < 1200, "straighten must have trimmed the corners"

    src, out = tags(raw), tags(dest)
    # Develop settings are baked into the pixels; the sidecar's crs block must not travel.
    src = {k: v for k, v in src.items() if not k.startswith("XMP:") or k.split(":")[1] in
           ("Rating", "Label", "Subject")}
    missing = sorted(set(src) - set(out))
    assert not missing, f"dropped by export: {missing}"
    altered = {k: (src[k], out[k]) for k in src if not _same(src[k], out[k])}
    assert not altered, f"altered by export: {altered}"

    got = {k.split(":")[1]: v for k, v in out.items()}
    assert got["Model"] == "Canon EOS 1000D" and got["ISO"] == 200 and got["Rating"] == 4
    assert got["Label"] == "Blue" and "real-raw" in got["Subject"]
    assert not any("Exposure2012" in k for k in out)
    res = subprocess.run(["exiftool", "-validate", "-warning", "-error", "-a", "-G1", str(dest)],
                         capture_output=True, text=True)
    assert "Warning" not in res.stdout and "Error" not in res.stdout, res.stdout
    assert exporter.icc_profile_for("srgb")                    # export embeds a profile
    assert json.loads(subprocess.run(
        ["exiftool", "-j", "-ICC_Profile:ProfileDescription", str(dest)],
        capture_output=True, text=True).stdout)[0].get("ProfileDescription")
