"""
Tests for the Camera Raw process version.

Process version selects the rendering engine, so it is not ordinary
metadata: replacing a file's 15.4 with 11.0 downgrades it from Process
Version 6 to 5 and changes how the photograph actually looks.
"""

import subprocess
from pathlib import Path

import pytest

import editor
from editor import DEFAULT_PROCESS_VERSION, _process_version_for, _version_tuple


@pytest.fixture
def photo(tmp_path):
    p = tmp_path / "IMG_0001.dng"
    p.write_bytes(b"dng")
    return p


@pytest.fixture
def declaring(monkeypatch):
    """exiftool reports whatever process version the test sets."""
    state = {"version": "-"}

    def run(cmd, **kwargs):
        if any("ProcessVersion" in a for a in cmd) and "-s3" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=state["version"], stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(editor.subprocess, "run", run)
    return state


def test_versions_compare_numerically():
    """String comparison would call "6.7" newer than "15.4"."""
    assert _version_tuple("15.4") > _version_tuple("11.0")
    assert _version_tuple("11.0") > _version_tuple("6.7")
    assert not _version_tuple("6.7") > _version_tuple("11.0")


def test_a_malformed_version_compares_as_nothing():
    assert _version_tuple("not-a-version") == ()


def test_a_file_with_no_version_gets_ours(photo, declaring):
    assert _process_version_for(photo) == DEFAULT_PROCESS_VERSION


def test_a_newer_version_is_kept(photo, declaring):
    """
    Lightroom Classic writes 15.4. Overwriting it with 11.0 would change
    the rendering engine, not just the metadata.
    """
    declaring["version"] = "15.4"
    assert _process_version_for(photo) == "15.4"


def test_an_older_version_is_upgraded(photo, declaring):
    """Process 2012 predates Texture and Dehaze, which this tool writes."""
    declaring["version"] = "6.7"
    assert _process_version_for(photo) == DEFAULT_PROCESS_VERSION


def test_the_same_version_is_left_alone(photo, declaring):
    declaring["version"] = DEFAULT_PROCESS_VERSION
    assert _process_version_for(photo) == DEFAULT_PROCESS_VERSION


def test_an_unreadable_file_stops_the_save_instead_of_guessing(photo, monkeypatch):
    """Falling back to 11.0 would silently downgrade a file declaring a newer version."""
    def boom(*a, **k):
        raise OSError("exiftool missing")

    monkeypatch.setattr(editor.subprocess, "run", boom)
    with pytest.raises(RuntimeError):
        _process_version_for(photo)


def test_saving_preserves_a_newer_version(photo, declaring, monkeypatch):
    """End to end: the write must carry the version that was already there."""
    calls = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        if any("ProcessVersion" in a and "-s3" in cmd for a in cmd):
            return subprocess.CompletedProcess(cmd, 0, stdout="15.4", stderr="")
        if "-s3" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="5500", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(editor.subprocess, "run", run)
    editor.save_adjustments(photo, {"exposure": 1.0})

    writes = [c for c in calls if any("Exposure2012" in a for a in c)]
    assert any("-XMP-crs:ProcessVersion=15.4" in a for c in writes for a in c)


def test_the_default_supports_every_slider_written():
    """
    Process Version 5 (11.0) is the oldest that supports Texture and Dehaze.
    Declaring something older would have Camera Raw ignore them.
    """
    assert _version_tuple(DEFAULT_PROCESS_VERSION) >= (11, 0)
