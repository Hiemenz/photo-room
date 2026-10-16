"""
Tests for noise reduction, and for the two commands agreeing about it.

Conversion sets LuminanceSmoothing from the photograph's ISO. The editor
did not write it and offered no control, so resetting a photo cleared the
value and nothing ever put it back.
"""

import subprocess
from pathlib import Path

import pytest

import editor
import process


def test_the_editor_has_a_noise_reduction_control():
    """Conversion set a value the photographer could neither see nor change."""
    assert any(c["key"] == "luminance_nr" for c in editor.CONTROLS)


def test_both_writers_agree_on_the_tag_set(fake_exiftool):
    """
    A tag written by one command and not the other is a value that survives
    or vanishes depending on which one touched the file last.
    """
    adj = {
        "color_temp": 5000, "tint": 0, "exposure": 1.0, "shadows": 10,
        "highlights": -5, "luminance_nr": 20, "contrast": 25, "whites": 5,
        "blacks": -5, "vibrance": 15, "saturation": 3,
    }
    process.embed_xmp_adjustments(Path("x.dng"), adj)
    convert_tags = {
        a.split("=")[0].replace("-XMP-crs:", "")
        for a in fake_exiftool.write_call() if a.startswith("-XMP-crs:")
    }
    editor_tags = {
        a.split("=")[0].replace("-XMP-crs:", "") for a in editor._xmp_arguments(adj)
    }

    only_convert = convert_tags - editor_tags
    assert not only_convert, f"convert writes tags the editor drops: {only_convert}"


def test_the_editor_writes_noise_reduction(fake_exiftool):
    args = editor._xmp_arguments({"luminance_nr": 42})
    assert "-XMP-crs:LuminanceSmoothing=42" in args


def test_noise_reduction_round_trips(fake_exiftool):
    from editor import read_saved_adjustments, save_adjustments

    save_adjustments(Path("x.dng"), {"luminance_nr": 35})
    fake_exiftool.stdout_lines = None
    assert read_saved_adjustments(Path("x.dng"))["luminance_nr"] == pytest.approx(35)


def test_edit_strength_does_not_scale_noise_reduction():
    """
    Noise is a property of the ISO the photograph was shot at, not an
    intensity to dial back with the rest of an edit.
    """
    out = editor.effective_adjustments(
        {"luminance_nr": 40, "exposure": 2.0, "strength": 50}
    )
    assert out["luminance_nr"] == pytest.approx(40)
    assert out["exposure"] == pytest.approx(1.0)


def test_a_reset_photo_recovers_its_iso_based_value(tmp_path, monkeypatch, fake_rawpy):
    """
    Reset clears every develop setting including the noise reduction. With
    no saved value the automatic pass supplies it again from the ISO, so it
    is not lost for good.
    """
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    (tmp_path / "high_iso.dng").write_bytes(b"dng")

    def stripped(cmd, **kwargs):
        # Nothing saved, and a high ISO.
        if "-s3" in cmd and any("Rating" in a for a in cmd):
            return subprocess.CompletedProcess(cmd, 0, stdout="0", stderr="")
        if "-ISO" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="6400", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(["-"] * 22), stderr="")

    monkeypatch.setattr(editor.subprocess, "run", stripped)
    monkeypatch.setattr(process.subprocess, "run", stripped)

    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        data = c.post("/api/open", json={"path": "high_iso.dng"}).get_json()

    assert data["adjustments"]["luminance_nr"] > 0, "ISO-based value was not restored"


def test_the_style_store_can_be_redirected(monkeypatch, tmp_path):
    """
    Running the editor against test photographs otherwise records their
    edits as the photographer's own, teaching the model from throwaway data.
    """
    import style

    monkeypatch.setenv("PHOTO_ROOM_STYLE_DB", str(tmp_path / "elsewhere.duckdb"))
    assert style.resolve_db_path() == tmp_path / "elsewhere.duckdb"


def test_without_the_override_the_store_is_in_the_home_directory(monkeypatch):
    import style

    monkeypatch.delenv("PHOTO_ROOM_STYLE_DB", raising=False)
    assert style.resolve_db_path().name == "style.duckdb"
    assert ".photo-room" in str(style.resolve_db_path())
