"""Tests for the editor's HTTP surface: routing, validation, and error handling."""

import base64
from pathlib import Path

import numpy as np
import pytest

import editor
from editor import (
    CONTROLS,
    GLOBAL_CONTROLS,
    read_saved_adjustments,
    save_adjustments,
)


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A library root holding two files the API is allowed to touch."""
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    (tmp_path / "IMG_0001.dng").write_bytes(b"fake-dng")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "IMG_0002.CR2").write_bytes(b"fake-cr2")
    return tmp_path


@pytest.fixture
def client(library):
    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        yield c


# Order must match read_saved_adjustments' tag list.
ALL_SAVED = ["5200", "-3", "0.75", "25", "-30", "20", "10", "-5", "40", "15",
             "12", "8", "5", "40", "0", "False",
             "0.00", "False", "0.0", "0.0", "1.0", "1.0"]


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "attack",
    [
        "../../../../etc/passwd",
        "../outside.dng",
        "sub/../../escape.dng",
        "/etc/passwd",
    ],
)
def test_paths_outside_library_are_rejected(client, attack):
    res = client.post("/api/open", json={"path": attack})
    assert res.status_code == 400
    assert "outside" in res.get_json()["error"].lower()


def test_absolute_path_inside_library_is_allowed(client, library, fake_rawpy, fake_exiftool):
    """An absolute path is fine as long as it resolves inside the root."""
    fake_exiftool.stdout_lines = ["-"] * 22
    res = client.post("/api/open", json={"path": str(library / "IMG_0001.dng")})
    assert res.status_code == 200


def test_nonexistent_file_is_rejected(client):
    res = client.post("/api/open", json={"path": "ghost.dng"})
    assert res.status_code == 400
    assert "No such file" in res.get_json()["error"]


def test_directory_is_not_a_valid_target(client):
    """A directory must be refused rather than blowing up in the decoder."""
    res = client.post("/api/open", json={"path": "sub"})
    assert res.status_code == 400


def test_missing_path_key_returns_400(client):
    for route in ("/api/open", "/api/preview", "/api/save", "/api/reset", "/api/recover"):
        assert client.post(route, json={}).status_code == 400, route


# ---------------------------------------------------------------------------
# Library listing
# ---------------------------------------------------------------------------

def test_files_lists_raw_files_recursively(client):
    files = client.get("/api/files").get_json()["files"]
    assert "IMG_0001.dng" in files
    assert "sub/IMG_0002.CR2" in files


def test_files_excludes_unrelated_formats(client, library):
    (library / "notes.txt").write_text("hello")
    (library / "export.jpg").write_bytes(b"jpeg")
    files = client.get("/api/files").get_json()["files"]
    assert "notes.txt" not in files
    assert "export.jpg" not in files


def test_files_deduplicates_case_variant_globs(client, library):
    """The .dng/.DNG globs can match the same file on a case-insensitive FS."""
    files = client.get("/api/files").get_json()["files"]
    assert len(files) == len(set(files))


def test_index_page_renders(client):
    res = client.get("/")
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    for control in CONTROLS:
        assert control["label"] in body, f"{control['label']} slider missing from UI"


# ---------------------------------------------------------------------------
# Opening a file
# ---------------------------------------------------------------------------

def test_open_returns_preview_and_full_slider_set(client, fake_rawpy, fake_exiftool):
    fake_exiftool.stdout_lines = ["-"] * 22       # no saved edits
    res = client.post("/api/open", json={"path": "IMG_0001.dng"})
    data = res.get_json()

    assert res.status_code == 200
    assert data["preview"].startswith("data:image/jpeg;base64,")
    expected = {c["key"] for c in CONTROLS + GLOBAL_CONTROLS} | set(editor.colormix.COLOR_DEFAULTS) | {"monochrome", "crop", "tone_curve", "masks", "edit_version"} | set(editor.retouch.DEFAULTS) | set(editor.effects.complete({})) | set(editor.lookxmp.DEFAULTS) | {"camera_profile"}
    assert set(data["adjustments"]) == expected


def test_open_prefers_saved_edits_over_auto(client, fake_rawpy, fake_exiftool):
    fake_exiftool.stdout_lines = ALL_SAVED
    data = client.post("/api/open", json={"path": "IMG_0001.dng"}).get_json()
    assert data["source"] == "saved"
    assert data["adjustments"]["color_temp"] == 5200
    assert data["adjustments"]["exposure"] == 0.75


def test_open_falls_back_to_auto_when_unedited(client, fake_rawpy, fake_exiftool):
    fake_exiftool.stdout_lines = ["-"] * 22
    data = client.post("/api/open", json={"path": "IMG_0001.dng"}).get_json()
    assert data["source"] == "auto"


def test_open_reports_whether_original_is_recoverable(client, fake_rawpy, fake_exiftool):
    fake_exiftool.stdout_lines = ["-"] * 22
    assert client.post("/api/open", json={"path": "IMG_0001.dng"}).get_json()["has_original"] is False

    editor._base_cache.clear()
    fake_exiftool.stdout_lines = ["IMG_0001.CR2"] * 22
    assert client.post("/api/open", json={"path": "IMG_0001.dng"}).get_json()["has_original"] is True


def test_open_surfaces_decoder_failure_as_500(client, fake_exiftool, monkeypatch):
    """A corrupt raw must return a readable error, not hang or crash the server."""
    def _boom(path):
        raise OSError("unsupported file format")

    monkeypatch.setattr(editor.rawpy, "imread", _boom)
    fake_exiftool.stdout_lines = ["-"] * 22
    res = client.post("/api/open", json={"path": "IMG_0001.dng"})
    assert res.status_code == 500
    assert "unsupported" in res.get_json()["error"]


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

def test_preview_returns_decodable_jpeg(client, fake_rawpy):
    res = client.post("/api/preview", json={"path": "IMG_0001.dng", "adjustments": {}})
    payload = res.get_json()["preview"].split(",", 1)[1]
    assert base64.b64decode(payload)[:2] == b"\xff\xd8", "not a JPEG"


def test_preview_reflects_adjustment_changes(client, fake_rawpy):
    def render(adj):
        return client.post(
            "/api/preview", json={"path": "IMG_0001.dng", "adjustments": adj}
        ).get_json()["preview"]

    assert render({"exposure": 0}) != render({"exposure": 2.0})


def test_preview_tolerates_unknown_slider_keys(client, fake_rawpy):
    """A stale front-end must not be able to 500 the server."""
    res = client.post(
        "/api/preview",
        json={"path": "IMG_0001.dng", "adjustments": {"nonexistent": 5}},
    )
    assert res.status_code == 200


def test_base_render_is_cached_across_requests(client, fake_rawpy, monkeypatch):
    """Slider drags must not re-decode the raw each time."""
    calls = {"n": 0}
    real = editor.rawpy.imread

    def counting(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(editor.rawpy, "imread", counting)
    for _ in range(4):
        client.post("/api/preview", json={"path": "IMG_0001.dng", "adjustments": {}})
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------

def test_save_writes_every_control_to_xmp(client, fake_exiftool):
    adj = {c["key"]: c["default"] for c in CONTROLS}
    adj.update(exposure=1.25, contrast=30, vibrance=-20, color_temp=6100)

    res = client.post("/api/save", json={"path": "IMG_0001.dng", "adjustments": adj})
    assert res.status_code == 200

    cmd = " ".join(fake_exiftool.write_call())
    for tag in (
        "Exposure2012=1.25", "Contrast2012=30", "Vibrance=-20",
        "ColorTemperature=6100", "Highlights2012", "Shadows2012",
        "Whites2012", "Blacks2012", "Saturation", "Tint",
    ):
        assert tag in cmd, f"{tag} missing from exiftool call"


def test_save_reports_exiftool_failure(client, monkeypatch, fake_exiftool):
    monkeypatch.setenv("PHOTO_ROOM_EMBED_DNG", "1")  # in-place DNG path is opt-in
    fake_exiftool.returncode = 1
    fake_exiftool.stderr = "permission denied"
    res = client.post("/api/save", json={"path": "IMG_0001.dng", "adjustments": {}})
    assert res.status_code == 500
    assert "permission denied" in res.get_json()["error"]


def test_save_does_not_leave_a_backup_copy(fake_exiftool):
    """Without -overwrite_original, exiftool litters the library with _original files."""
    save_adjustments(Path("x.dng"), {"exposure": 1.0})
    assert "-overwrite_original" in fake_exiftool.write_call()


def test_save_coerces_float_slider_values_to_int_tags(fake_exiftool):
    """The front-end sends floats; integer XMP tags must not get '30.0'."""
    save_adjustments(Path("x.dng"), {"contrast": 30.0, "tint": -3.0, "color_temp": 5200.0})
    cmd = " ".join(fake_exiftool.write_call())
    assert "Contrast2012=30" in cmd and "Contrast2012=30.0" not in cmd
    assert "ColorTemperature=5200" in cmd and "ColorTemperature=5200.0" not in cmd


def test_save_fills_defaults_for_omitted_controls(fake_exiftool):
    """A partial payload must still write a complete, coherent edit."""
    save_adjustments(Path("x.dng"), {"exposure": 1.0})
    cmd = " ".join(fake_exiftool.write_call())
    assert "Contrast2012=0" in cmd
    assert "Saturation=0" in cmd


# ---------------------------------------------------------------------------
# Reading saved edits back
# ---------------------------------------------------------------------------

def test_read_saved_returns_none_when_file_is_unedited(fake_exiftool):
    fake_exiftool.stdout_lines = ["-"] * 22
    assert read_saved_adjustments(Path("x.dng")) is None


def test_read_saved_parses_all_values(fake_exiftool):
    fake_exiftool.stdout_lines = ALL_SAVED
    saved = read_saved_adjustments(Path("x.dng"))
    assert saved["color_temp"] == 5200
    assert saved["tint"] == -3
    assert saved["exposure"] == 0.75
    assert saved["saturation"] == 15


def test_read_saved_skips_individually_missing_tags(fake_exiftool):
    """Partially-written XMP must yield the tags present, not crash."""
    lines = list(ALL_SAVED)
    lines[3] = "-"       # contrast absent
    fake_exiftool.stdout_lines = lines
    saved = read_saved_adjustments(Path("x.dng"))
    assert "contrast" not in saved
    assert saved["exposure"] == 0.75


def test_read_saved_ignores_unparseable_values(fake_exiftool):
    lines = list(ALL_SAVED)
    lines[2] = "not-a-number"
    fake_exiftool.stdout_lines = lines
    saved = read_saved_adjustments(Path("x.dng"))
    assert "exposure" not in saved


# ---------------------------------------------------------------------------
# Revert to original
# ---------------------------------------------------------------------------

def test_reset_clears_adjustments_and_returns_neutral_state(client, monkeypatch, fake_rawpy, fake_exiftool):
    monkeypatch.setenv("PHOTO_ROOM_EMBED_DNG", "1")  # in-place DNG path is opt-in
    res = client.post("/api/reset", json={"path": "IMG_0001.dng"})
    data = res.get_json()

    assert res.status_code == 200
    # calls[0] is now the check for edits made in other software.
    assert "-XMP-crs:all=" in " ".join(fake_exiftool.write_call())

    for control in CONTROLS:
        if control["key"] != "color_temp":
            assert data["adjustments"][control["key"]] == control["default"]


def test_reset_returns_camera_temp_not_a_fixed_default(client, fake_rawpy, fake_exiftool):
    """Reverting must restore the camera's own white balance."""
    fake_rawpy["whitebalance"] = [2.4, 1.0, 1.0, 1.0]
    data = client.post("/api/reset", json={"path": "IMG_0001.dng"}).get_json()
    assert data["adjustments"]["color_temp"] != 5500
    assert 2000 <= data["adjustments"]["color_temp"] <= 12000


def test_reset_returns_a_refreshed_preview(client, fake_rawpy, fake_exiftool):
    data = client.post("/api/reset", json={"path": "IMG_0001.dng"}).get_json()
    assert data["preview"].startswith("data:image/jpeg;base64,")


def test_reset_reports_failure(client, monkeypatch, fake_rawpy, fake_exiftool):
    monkeypatch.setenv("PHOTO_ROOM_EMBED_DNG", "1")  # in-place DNG path is opt-in
    fake_exiftool.returncode = 1
    fake_exiftool.stderr = "read-only file system"
    res = client.post("/api/reset", json={"path": "IMG_0001.dng"})
    assert res.status_code == 500
    assert "read-only" in res.get_json()["error"]


def test_recover_writes_original_beside_the_dng(client, library, fake_exiftool, fake_converter):
    fake_exiftool.stdout_lines = ["IMG_0001.CR2"]
    fake_converter.produces = b"II*\x00original-sensor-data"

    res = client.post("/api/recover", json={"path": "IMG_0001.dng"})

    assert res.status_code == 200
    recovered = library / "IMG_0001.CR2"
    assert recovered.read_bytes() == b"II*\x00original-sensor-data"


def test_recover_fails_cleanly_without_embedded_original(client, fake_exiftool):
    fake_exiftool.stdout_lines = ["-"]
    res = client.post("/api/recover", json={"path": "IMG_0001.dng"})
    assert res.status_code == 500
    assert "no embedded original" in res.get_json()["error"]


def test_recover_does_not_modify_the_dng(client, library, fake_exiftool):
    """Recovery is a read; the DNG must be left exactly as it was."""
    before = (library / "IMG_0001.dng").read_bytes()
    fake_exiftool.stdout_lines = ["IMG_0001.CR2"]
    fake_exiftool.stdout_bytes = b"original"
    client.post("/api/recover", json={"path": "IMG_0001.dng"})
    assert (library / "IMG_0001.dng").read_bytes() == before


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------

def test_save_then_read_round_trips(fake_exiftool):
    """Values written must come back identically on the next open."""
    original = {
        "color_temp": 6100, "tint": -12, "exposure": 1.25, "contrast": 30,
        "highlights": -45, "shadows": 55, "whites": 10, "blacks": -20,
        "vibrance": 35, "saturation": -15,
        "clarity": 18, "texture": -7, "dehaze": 22, "sharpness": 45,
    }
    save_adjustments(Path("x.dng"), original)

    # The fake keeps what was written, so this reads back through the real
    # parsing path rather than a hand-built response.
    fake_exiftool.stdout_lines = None
    saved = read_saved_adjustments(Path("x.dng"))

    assert saved.pop("monochrome") is False
    assert saved.pop("edit_version") == editor.EDIT_VERSION       # stamped by the save
    assert saved.pop("luminance_nr") == pytest.approx(0.0)
    assert saved.pop("color_nr") == pytest.approx(0.0)
    # Geometry round-trips alongside the sliders but is not part of this
    # comparison; an uncropped photo reports no crop and a zero angle.
    assert saved.pop("straighten") == pytest.approx(0.0)
    assert "crop" not in saved
    assert saved == pytest.approx(original)


def test_monochrome_round_trips(fake_exiftool):
    save_adjustments(Path("x.dng"), {"exposure": 0.5, "monochrome": True})
    assert "ConvertToGrayscale=True" in " ".join(fake_exiftool.write_call())

    fake_exiftool.stdout_lines = None
    assert read_saved_adjustments(Path("x.dng"))["monochrome"] is True
