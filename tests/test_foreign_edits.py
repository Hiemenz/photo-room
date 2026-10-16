"""
Tests protecting edits made in other software.

A DNG exported from Lightroom carries its develop settings inside the file.
Saving over them, or stripping them with a revert, destroys editing work
that may exist nowhere else. Found on a real 424-frame shoot where every
frame had been edited in Lightroom Classic the previous day.
"""

from pathlib import Path

import pytest

import editor
from process import detect_foreign_edits, has_sidecar


@pytest.mark.parametrize("creator", [
    "Adobe Photoshop Lightroom Classic 14.3.1 (Macintosh)",
    "Adobe Camera Raw 16.2",
    "Adobe Photoshop 2024",
    "Adobe Bridge 2024",
])
def test_adobe_software_is_detected(fake_exiftool, creator):
    fake_exiftool.stdout_lines = ["-", creator, "-"]
    assert detect_foreign_edits(Path("x.dng")) == creator


def test_a_crs_version_alone_counts_as_foreign(fake_exiftool):
    """
    Lightroom stamps crs:Version; this tool never does. Its presence means
    the settings came from somewhere else even when the creator is unknown.
    """
    fake_exiftool.stdout_lines = ["17.5", "-", "-"]
    assert detect_foreign_edits(Path("x.dng")) == "another Camera Raw application"


def test_a_file_this_tool_wrote_is_not_foreign(fake_exiftool):
    fake_exiftool.stdout_lines = ["-", "-", "-"]
    assert detect_foreign_edits(Path("x.dng")) is None


def test_a_non_adobe_creator_is_not_flagged(fake_exiftool):
    """Only software that writes Camera Raw settings matters here."""
    fake_exiftool.stdout_lines = ["-", "Canon EOS 5D Mark IV", "-"]
    assert detect_foreign_edits(Path("x.dng")) is None


def test_the_software_tag_is_checked_too(fake_exiftool):
    fake_exiftool.stdout_lines = ["-", "-", "Adobe Photoshop Lightroom Classic 14.3"]
    assert "Lightroom" in detect_foreign_edits(Path("x.dng"))


def test_a_sidecar_is_detected(tmp_path):
    dng = tmp_path / "IMG_1.dng"
    dng.write_bytes(b"dng")
    assert has_sidecar(dng) is False

    (tmp_path / "IMG_1.xmp").write_text("<xmpmeta/>")
    assert has_sidecar(dng) is True


# ---------------------------------------------------------------------------
# Batch apply
# ---------------------------------------------------------------------------

@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    for name in ("mine.dng", "lightroom.dng"):
        (tmp_path / name).write_bytes(b"dng")
    return tmp_path


@pytest.fixture
def client(library):
    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        yield c


@pytest.fixture
def one_foreign(monkeypatch):
    monkeypatch.setattr(
        editor, "detect_foreign_edits",
        lambda p: "Adobe Photoshop Lightroom Classic" if "lightroom" in p.name else None,
    )


def test_batch_apply_skips_files_edited_elsewhere(client, one_foreign, fake_rawpy, fake_exiftool):
    data = client.post("/api/batch_apply", json={
        "paths": ["mine.dng", "lightroom.dng"], "adjustments": {"exposure": 1.0},
    }).get_json()

    assert data["applied"] == ["mine.dng"]
    assert len(data["skipped"]) == 1
    assert data["skipped"][0]["path"] == "lightroom.dng"


def test_the_message_says_what_was_skipped(client, one_foreign, fake_rawpy, fake_exiftool):
    data = client.post("/api/batch_apply", json={
        "paths": ["lightroom.dng"], "adjustments": {"exposure": 1.0},
    }).get_json()
    assert "skipped" in data["message"]
    assert "other software" in data["message"]


def test_skipping_is_not_reported_as_success(client, one_foreign, fake_rawpy, fake_exiftool):
    data = client.post("/api/batch_apply", json={
        "paths": ["lightroom.dng"], "adjustments": {"exposure": 1.0},
    }).get_json()
    assert data["ok"] is False


def test_an_explicit_override_applies_anyway(client, one_foreign, fake_rawpy, fake_exiftool):
    """The photographer may genuinely want to replace those settings."""
    data = client.post("/api/batch_apply", json={
        "paths": ["mine.dng", "lightroom.dng"],
        "adjustments": {"exposure": 1.0},
        "overwrite_foreign": True,
    }).get_json()

    assert sorted(data["applied"]) == ["lightroom.dng", "mine.dng"]
    assert data["skipped"] == []


def test_opening_a_foreign_file_reports_it(client, one_foreign, fake_rawpy, fake_exiftool):
    fake_exiftool.stdout_lines = ["-"] * 22
    data = client.post("/api/open", json={"path": "lightroom.dng"}).get_json()
    assert "Lightroom" in data["foreign_editor"]


def test_opening_an_ordinary_file_reports_nothing(client, one_foreign, fake_rawpy, fake_exiftool):
    fake_exiftool.stdout_lines = ["-"] * 22
    data = client.post("/api/open", json={"path": "mine.dng"}).get_json()
    assert data["foreign_editor"] is None
