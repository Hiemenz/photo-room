"""JPEG / TIFF / PNG (and HEIC with pillow-heif) are editable: decode seam, Develop, export."""
import io
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageCms

import editor
import exporter
import imgload
import library
import process
import tile_server

needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")


def _save(path: Path, size=(64, 48), rgb=(200, 120, 40), **kw) -> Path:
    Image.new("RGB", size, rgb).save(path, **kw)
    return path


def test_suffixes_and_editable_flags():
    for name in ("a.jpg", "a.JPEG", "a.tif", "a.tiff", "a.png"):
        assert library.kind_of(name) == "image"
        assert library.is_editable(name)
        assert imgload.is_image(name)
    assert library.is_editable("a.cr2") and library.is_editable("a.dng")
    assert not library.is_editable("a.mp4")
    assert not library.is_editable("a.txt")
    assert library.is_editable("a.heic") == imgload.heif_available()


def test_decode_is_linear_light_uint16(tmp_path):
    p = _save(tmp_path / "a.jpg", rgb=(128, 128, 128), quality=100)
    lin = imgload.decode_linear16(p)
    assert lin.dtype == np.uint16 and lin.shape == (48, 64, 3)
    assert abs(lin[10, 10, 1] / 65535.0 - 0.2158) < 0.01     # sRGB 128/255 in linear light


def test_exif_orientation_is_applied(tmp_path):
    img = Image.new("RGB", (60, 40), (0, 0, 0))
    exif = img.getexif()
    exif[274] = 6
    p = tmp_path / "r.jpg"
    img.save(p, exif=exif)
    assert imgload.decode_linear16(p).shape[:2] == (60, 40)
    assert imgload.oriented_size(p) == (40, 60)


def test_embedded_profile_is_converted_to_the_working_space(tmp_path):
    p = tmp_path / "adobe.jpg"
    Image.new("RGB", (16, 16), (200, 60, 60)).save(p, icc_profile=exporter.icc_profile_for("adobergb"))
    plain = _save(tmp_path / "plain.jpg", size=(16, 16), rgb=(200, 60, 60))
    assert not np.array_equal(imgload.decode_linear16(p, "srgb"), imgload.decode_linear16(plain, "srgb"))


def test_untagged_file_into_a_wide_space_is_converted(tmp_path):
    p = _save(tmp_path / "plain.jpg", rgb=(200, 60, 60))
    assert not np.array_equal(imgload.decode_linear16(p, "srgb"), imgload.decode_linear16(p, "prophoto"))


def test_other_modes_decode(tmp_path):
    Image.new("L", (8, 8), 100).save(tmp_path / "g.png")
    Image.new("RGBA", (8, 8), (10, 20, 30, 0)).save(tmp_path / "a.png")
    Image.new("CMYK", (8, 8), (0, 0, 0, 0)).save(tmp_path / "c.tif")
    for n in ("g.png", "a.png", "c.tif"):
        assert imgload.decode_linear16(tmp_path / n).shape == (8, 8, 3)


def test_preview_decode_matches_the_raw_signature(tmp_path):
    p = _save(tmp_path / "a.jpg", size=(400, 300))
    lin, temp, mul = process.decode_preview_linear(p)
    assert lin.dtype == np.uint16 and lin.ndim == 3
    assert temp == 5500 and len(mul) == 4
    assert process.camera_baseline_temp(p) == 5500
    rgb, _, _ = process.decode_for_analysis(p)
    assert rgb.dtype == np.uint8


def test_tile_server_uses_the_same_decode(tmp_path):
    lin, base = tile_server.load_full(_save(tmp_path / "a.png"))
    assert base == 5500 and lin.dtype == np.uint16


@needs_exiftool
def test_export_applies_edits_in_linear_light(tmp_path):
    p = _save(tmp_path / "a.jpg", rgb=(100, 100, 100), quality=100)
    flat = exporter.export_photo(p, tmp_path / "out", exporter.ExportOptions(format="png", keep_metadata=False))
    base = np.asarray(Image.open(flat), dtype=float).mean()
    assert abs(base - 100) < 2
    img, _ = exporter.render(p, exporter.ExportOptions(format="png"), adjustments={"exposure": 1.0})
    assert np.asarray(img, dtype=float).mean() > base + 15


@needs_exiftool
def test_export_to_wide_space_embeds_that_profile(tmp_path):
    p = _save(tmp_path / "a.jpg")
    dest = exporter.export_photo(p, tmp_path / "out", exporter.ExportOptions(
        format="jpeg", colorspace="adobergb", keep_metadata=False))
    icc = Image.open(dest).info["icc_profile"]
    assert "adobe" in ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc))).lower()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        yield c


@needs_exiftool
def test_open_edit_save_writes_an_xmp_sidecar_and_leaves_the_jpeg_alone(client, tmp_path):
    jpg = _save(tmp_path / "IMG_1.jpg", size=(320, 240), quality=95)
    before = jpg.read_bytes()
    res = client.post("/api/open", json={"path": "IMG_1.jpg"})
    data = res.get_json()
    assert res.status_code == 200, data
    assert data["source_kind"] == "image"
    assert data["baseline_temp"] == 5500
    adj = dict(data["adjustments"], exposure=0.5, color_temp=6500)
    prev = client.post("/api/preview", json={"path": "IMG_1.jpg", "adjustments": adj, "seq": 1})
    assert prev.status_code == 200 and "preview" in prev.get_json()
    saved = client.post("/api/save", json={"path": "IMG_1.jpg", "adjustments": adj})
    assert saved.status_code == 200, saved.get_json()
    assert (tmp_path / "IMG_1.xmp").exists()
    assert jpg.read_bytes() == before
    again = client.post("/api/open", json={"path": "IMG_1.jpg"}).get_json()
    assert again["adjustments"]["exposure"] == pytest.approx(0.5, abs=0.05)
    assert again["source"] == "saved"


@needs_exiftool
@pytest.mark.parametrize("mode,expect_gps", [("all", True), ("no_location", False), ("none", False)])
def test_export_of_an_edited_jpeg_honours_metadata_modes(tmp_path, mode, expect_gps):
    jpg = _save(tmp_path / "IMG_2.jpg", quality=95)
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-EXIF:Make=TestCam",
                    "-GPSLatitude=47.6", "-GPSLatitudeRef=N", "-GPSLongitude=122.3",
                    "-GPSLongitudeRef=W", str(jpg)], check=True, capture_output=True, timeout=60)
    editor.save_adjustments(jpg, {"exposure": 0.3, "color_temp": 5500})
    assert jpg.with_suffix(".xmp").exists()
    dest = exporter.export_photo(jpg, tmp_path / "out", exporter.ExportOptions(format="jpeg", metadata=mode))
    tags = json.loads(subprocess.run(["exiftool", "-j", "-n", "-Make", "-GPSLatitude", str(dest)],
                                     capture_output=True, text=True, timeout=60).stdout)[0]
    assert ("GPSLatitude" in tags) is expect_gps
    assert ("Make" in tags) is (mode != "none")


def test_lens_profile_is_ignored_for_images(tmp_path, monkeypatch):
    import lens
    monkeypatch.setattr(lens, "lensfun_available", lambda: True)
    monkeypatch.setattr(lens, "exif_meta", lambda p: {"camera": "x"})
    jpg = _save(tmp_path / "a.jpg")
    assert "_lens_meta" not in lens.attach_meta(jpg, {"lens_profile": True})
    assert "_lens_meta" in lens.attach_meta(tmp_path / "a.cr2", {"lens_profile": True})


def test_native_space_follows_an_embedded_wide_profile(tmp_path):
    p = tmp_path / "adobe.jpg"
    Image.new("RGB", (8, 8), (90, 120, 150)).save(p, icc_profile=exporter.icc_profile_for("adobergb"))
    assert imgload.native_space(p) == "adobergb"
    assert imgload.native_space(_save(tmp_path / "plain.jpg")) == "srgb"
