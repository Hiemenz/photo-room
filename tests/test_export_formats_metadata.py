"""TIFF / WebP export and Lightroom-style metadata options."""

import subprocess

import numpy as np
import pytest
from PIL import Image

import export_presets
import exporter
from exporter import ExportOptions, export_photo
from test_real_exiftool_export import make_source, add_sidecar, tags_of, exiftool_json


# ---- no exiftool needed ----------------------------------------------------

def test_validate_formats_and_depth():
    assert ExportOptions(format="tif").validate().format == "tiff"
    assert ExportOptions(format="tiff", bit_depth=16).validate().bit_depth == 16
    ExportOptions(format="webp").validate()
    with pytest.raises(ValueError):
        ExportOptions(format="webp", bit_depth=16).validate()
    with pytest.raises(ValueError):
        ExportOptions(format="jpeg", bit_depth=16).validate()
    with pytest.raises(ValueError):
        ExportOptions(format="gif").validate()


def test_metadata_mode_validation_and_default():
    o = ExportOptions().validate()
    assert o.metadata == "all" and o.keep_metadata is True
    assert ExportOptions(keep_metadata=False).validate().metadata == "none"
    o = ExportOptions(metadata="No Location").validate()
    assert o.metadata == "no_location"
    with pytest.raises(ValueError):
        ExportOptions(metadata="bogus").validate()


def test_metadata_args():
    assert "--GPS*" in exporter.metadata_args("no_location")
    assert "--GPS*" not in exporter.metadata_args("all")
    assert "-all:all" not in exporter.metadata_args("copyright")
    assert "--MakerNotes:all" in exporter.metadata_args("no_develop")


def test_tiff16_roundtrip(tmp_path):
    arr = np.linspace(0, 1, 8 * 5 * 3, dtype=np.float32).reshape(5, 8, 3)
    p = tmp_path / "a.tif"
    exporter.write_tiff16(p, arr, exporter.icc_profile_for("srgb"))
    import cv2
    back = cv2.imread(str(p), cv2.IMREAD_UNCHANGED)
    assert back.dtype == np.uint16 and back.shape == (5, 8, 3)
    got = back[..., ::-1].astype(np.float64) / 65535
    assert np.abs(got - arr).max() < 1e-4


def test_preset_roundtrip():
    s = export_presets.clean_settings({"format": "tiff", "bit_depth": 16, "metadata": "copyright"})
    assert s["format"] == "tiff" and s["bit_depth"] == 16 and s["metadata"] == "copyright"
    assert export_presets.clean_settings({"format": "jpeg"})["metadata"] == "all"


# ---- real exiftool ---------------------------------------------------------

ET = pytest.mark.exiftool


@pytest.fixture
def source(tmp_path):
    src = make_source(tmp_path / "IMG_0001.jpg")
    add_sidecar(src)
    return src


def _out(source, tmp_path, **kw):
    return export_photo(source, tmp_path / "out", ExportOptions(**kw))


@ET
@pytest.mark.parametrize("fmt,depth", [("tiff", 8), ("tiff", 16), ("webp", 8)])
def test_new_formats_decode_and_keep_metadata(source, tmp_path, fmt, depth):
    dest = _out(source, tmp_path, format=fmt, bit_depth=depth)
    assert dest.suffix == {"tiff": ".tif", "webp": ".webp"}[fmt]
    with Image.open(dest) as im:
        im.load()
        assert im.size == (160, 120)
    got = tags_of(dest)
    assert got["Model"] == "Canon EOS 5D Mark IV" and "GPSLatitude" in got
    assert exiftool_json(dest, "-ICC_Profile:ProfileDescription").get("ICC_Profile:ProfileDescription")


@ET
def test_tiff16_has_16_bit_samples(source, tmp_path):
    dest = _out(source, tmp_path, format="tiff", bit_depth=16)
    out = subprocess.run(["exiftool", "-s3", "-BitsPerSample", str(dest)], capture_output=True, text=True).stdout
    assert out.split() == ["16", "16", "16"]


@ET
@pytest.mark.parametrize("fmt", ["jpeg", "png", "tiff", "webp"])
def test_strip_location(source, tmp_path, fmt):
    dest = _out(source, tmp_path, format=fmt, metadata="no_location")
    got = tags_of(dest)
    assert not [k for k in got if k.startswith("GPS")]
    for gone in ("City", "Province-State", "Country-PrimaryLocationName", "Country"):
        assert gone not in got, gone
    assert got["Model"] == "Canon EOS 5D Mark IV"
    if fmt != "webp":  # WebP has no IPTC block
        assert "Keywords" in got and "CopyrightNotice" in got


@ET
@pytest.mark.parametrize("fmt", ["jpeg", "png", "tiff"])
def test_copyright_only(source, tmp_path, fmt):
    dest = _out(source, tmp_path, format=fmt, metadata="copyright")
    got = tags_of(dest)
    for name in ("Copyright", "Artist", "CopyrightNotice", "Rights"):
        assert name in got, name
    for name in ("Make", "Model", "LensModel", "ISO", "Keywords", "Caption-Abstract", "Rating"):
        assert name not in got, name
    assert not [k for k in got if k.startswith("GPS")]


@ET
def test_no_develop_keeps_exif_and_never_crs(source, tmp_path):
    dest = _out(source, tmp_path, metadata="no_develop")
    got = tags_of(dest)
    assert got["Model"] == "Canon EOS 5D Mark IV" and "GPSLatitude" in got
    assert "Exposure2012" not in got


@ET
def test_none_and_default(source, tmp_path):
    none = _out(source, tmp_path, metadata="none")
    assert "Model" not in tags_of(none) and "Copyright" not in tags_of(none)
    default = _out(source, tmp_path)
    assert "GPSLatitude" in tags_of(default)
