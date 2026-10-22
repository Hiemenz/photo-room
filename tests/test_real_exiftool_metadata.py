"""
The metadata panel against the REAL exiftool: the readout, sidecar writes that
never touch the photo, undo, unicode and newlines, presets applied through the
API, and the new fields surviving an export in every metadata mode.

Run with ``pytest -m exiftool`` (CI does).
"""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

import editor
import exporter
import metadata

pytestmark = pytest.mark.exiftool


def exiftool(*args: str) -> str:
    return subprocess.run(["exiftool", *args], capture_output=True, text=True, check=True).stdout


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def photo(tmp_path, monkeypatch):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    path = tmp_path / "IMG_0001.jpg"
    Image.new("RGB", (120, 80), (90, 120, 150)).save(path, "JPEG")
    exiftool("-overwrite_original", "-Make=Canon", "-Model=Canon EOS 1000D", "-LensModel=EF 50mm f/1.8",
             "-ISO=400", "-ExposureTime=1/250", "-FNumber=2.8", "-FocalLength=50",
             "-DateTimeOriginal=2026:09:14 18:42:07", "-GPSLatitude=47.6062", "-GPSLatitudeRef=N",
             "-GPSLongitude=122.3321", "-GPSLongitudeRef=W", "-GPSAltitude=56", "-EXIF:Artist=Camera Owner",
             str(path))
    return path


@pytest.fixture
def client(photo):
    editor.app.config["TESTING"] = True
    editor.app.config["PHOTOROOM_TOKEN"] = "t"
    with editor.app.test_client() as c:
        c.environ_base["HTTP_X_PHOTOROOM_TOKEN"] = "t"
        yield c


def test_readout_reports_the_camera_settings(photo):
    info = metadata.read(photo)["info"]
    assert info["camera"] == "Canon EOS 1000D"
    assert info["lens"] == "EF 50mm f/1.8"
    assert (info["iso"], info["shutter"], info["aperture"], info["focal_length"]) == (400, "1/250 s", "f/2.8", "50 mm")
    assert info["captured"] == "2026-09-14 18:42:07"
    assert info["dimensions"] == "120 × 80"
    assert info["gps"]["lat"] == pytest.approx(47.6062) and info["gps"]["lon"] == pytest.approx(-122.3321)
    assert "N" in info["gps"]["text"] and "W" in info["gps"]["text"]


def test_fields_are_written_to_the_sidecar_and_the_photo_is_untouched(photo):
    before = sha(photo)
    metadata.write(photo, {"title": "Ferry é中", "caption": "Line one\nLine two", "creator": "Kevin",
                           "copyright": "(c) 2026 Kevin", "copyright_status": "copyrighted",
                           "usage_terms": "All rights reserved"})
    assert sha(photo) == before
    side = photo.with_suffix(".xmp")
    assert side.exists()
    out = json.loads(exiftool("-j", "-XMP-dc:all", "-XMP-xmpRights:all", str(side)))[0]
    assert out["Title"] == "Ferry é中" and out["Description"] == "Line one\nLine two"
    assert out["Creator"] == "Kevin" and out["Rights"] == "(c) 2026 Kevin"
    assert str(out["Marked"]) == "True" and out["UsageTerms"] == "All rights reserved"
    got = metadata.read(photo)["fields"]
    assert got["title"] == "Ferry é中" and got["caption"] == "Line one\nLine two"
    assert got["copyright_status"] == "copyrighted"


def test_camera_artist_prefills_but_is_not_in_the_undo_snapshot(photo):
    assert metadata.read(photo)["fields"]["creator"] == "Camera Owner"
    prev = metadata.write(photo, {"creator": "Kevin"})
    assert prev == {"creator": ""}                      # the sidecar had nothing: undo clears, not "Camera Owner"


def test_undo_restores_the_previous_values(photo):
    metadata.write(photo, {"title": "One", "copyright_status": "public_domain"})
    prev = metadata.write(photo, {"title": "Two", "copyright_status": "copyrighted"})
    assert prev == {"title": "One", "copyright_status": "public_domain"}
    metadata.write(photo, prev)
    fields = metadata.read(photo)["fields"]
    assert fields["title"] == "One" and fields["copyright_status"] == "public_domain"
    metadata.write(photo, {"title": "", "copyright_status": "unknown"})
    out = exiftool("-XMP-dc:Title", "-XMP-xmpRights:Marked", "-s3", str(photo.with_suffix(".xmp"))).strip()
    assert out == ""


def test_writing_keeps_label_and_keywords_in_the_sidecar(photo):
    import library
    library.write_xmp(photo, label="Red", keywords=["ferry", "dusk"])
    metadata.write(photo, {"title": "T"})
    out = json.loads(exiftool("-j", "-XMP-dc:Subject", "-XMP-xmp:Label", str(photo.with_suffix(".xmp"))))[0]
    assert out["Subject"] == ["ferry", "dusk"] and out["Label"] == "Red"


def test_a_value_that_looks_like_an_option_is_just_text(photo):
    metadata.write(photo, {"title": "-overwrite_original", "caption": "=x"})
    assert metadata.read(photo)["fields"]["title"] == "-overwrite_original"


def test_api_read_write_preset_roundtrip(client, photo):
    r = client.get("/api/metadata?path=IMG_0001.jpg").get_json()
    assert r["items"][0]["info"]["iso"] == 400
    r = client.post("/api/metadata/presets", json={"name": "Copyright", "fields": {
        "creator": "Kevin", "copyright": "(c) 2026 Kevin", "copyright_status": "copyrighted"}})
    assert r.status_code == 200
    r = client.post("/api/metadata/presets/apply", json={"name": "Copyright", "paths": ["IMG_0001.jpg"]})
    assert r.status_code == 200, r.get_json()
    got = client.get("/api/metadata?path=IMG_0001.jpg").get_json()["items"][0]["fields"]
    assert got["copyright"] == "(c) 2026 Kevin" and got["title"] == ""


@pytest.mark.parametrize("mode", ["all", "copyright", "no_location", "no_develop"])
def test_exports_carry_the_sidecar_rights(photo, tmp_path, mode):
    metadata.write(photo, {"title": "Ferry", "creator": "Kevin", "copyright": "(c) 2026 Kevin",
                           "copyright_status": "copyrighted", "usage_terms": "Contact me"})
    out = tmp_path / f"out_{mode}.jpg"
    Image.new("RGB", (60, 40)).save(out, "JPEG")
    exporter.copy_metadata(photo, out, mode=mode)
    tags = json.loads(exiftool("-j", "-XMP-dc:all", "-XMP-xmpRights:all", str(out)))[0]
    assert tags["Rights"] == "(c) 2026 Kevin" and tags["Creator"] == "Kevin"
    assert str(tags["Marked"]) == "True" and tags["UsageTerms"] == "Contact me"
    if mode == "copyright":
        assert "Title" not in tags                      # titles and captions stay home in rights-only mode
    else:
        assert tags["Title"] == "Ferry"
