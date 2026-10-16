"""
Tests for reading Lightroom-style .xmp sidecars.

On a real shoot the sidecar and the copy inside the DNG carried different
values for the same photograph — exposure +1.12 against +1.11, highlights
-84 against -81. The sidecar is what Lightroom reads back when sidecar
writing is enabled, so it is the one matching what the photographer sees.
"""

from pathlib import Path

import pytest

import editor
from editor import read_saved_adjustments


SIDECAR_VALUES = ["5200", "-3", "1.12", "5", "-84", "50", "50", "-33", "10", "1",
                  "0", "0", "0", "0", "0", "False",
                  "0.00", "False", "0.0", "0.0", "1.0", "1.0"]

DNG_VALUES = ["5200", "-3", "1.11", "5", "-81", "48", "50", "-36", "10", "0",
              "0", "0", "0", "0", "0", "False",
              "0.00", "False", "0.0", "0.0", "1.0", "1.0"]


@pytest.fixture
def photo(tmp_path):
    p = tmp_path / "IMG_0001.dng"
    p.write_bytes(b"dng")
    return p


@pytest.fixture
def per_file_exiftool(monkeypatch):
    """Answers with different values depending on which file is read."""
    import subprocess

    class Tool:
        def __init__(self):
            self.by_suffix = {".xmp": SIDECAR_VALUES, ".dng": DNG_VALUES}
            self.read_paths: list[str] = []

        def __call__(self, cmd, **kwargs):
            target = cmd[-1]
            self.read_paths.append(target)
            values = self.by_suffix.get(Path(target).suffix, ["-"] * 22)
            return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(values), stderr="")

    tool = Tool()
    monkeypatch.setattr(editor.subprocess, "run", tool)
    return tool


def test_the_sidecar_is_preferred_when_present(photo, per_file_exiftool):
    photo.with_suffix(".xmp").write_text("<xmpmeta/>")

    values = read_saved_adjustments(photo)
    assert values["exposure"] == pytest.approx(1.12)
    assert values["highlights"] == pytest.approx(-84)
    assert values["shadows"] == pytest.approx(50)


def test_the_dng_is_read_when_there_is_no_sidecar(photo, per_file_exiftool):
    values = read_saved_adjustments(photo)
    assert values["exposure"] == pytest.approx(1.11)
    assert values["highlights"] == pytest.approx(-81)


def test_the_sidecar_file_is_the_one_opened(photo, per_file_exiftool):
    photo.with_suffix(".xmp").write_text("<xmpmeta/>")
    read_saved_adjustments(photo)
    assert any(p.endswith(".xmp") for p in per_file_exiftool.read_paths)


def test_preferring_the_sidecar_can_be_turned_off(photo, per_file_exiftool):
    photo.with_suffix(".xmp").write_text("<xmpmeta/>")
    values = read_saved_adjustments(photo, prefer_sidecar=False)
    assert values["exposure"] == pytest.approx(1.11)


def test_an_empty_sidecar_falls_back_to_the_dng(photo, monkeypatch):
    """A sidecar carrying no develop settings must not hide the DNG's."""
    import subprocess

    def run(cmd, **kwargs):
        target = cmd[-1]
        values = ["-"] * 22 if target.endswith(".xmp") else DNG_VALUES
        return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(values), stderr="")

    monkeypatch.setattr(editor.subprocess, "run", run)
    photo.with_suffix(".xmp").write_text("<xmpmeta/>")

    values = read_saved_adjustments(photo)
    assert values["exposure"] == pytest.approx(1.11)


def test_no_settings_anywhere_reads_as_unedited(photo, monkeypatch):
    import subprocess

    monkeypatch.setattr(
        editor.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="\n".join(["-"] * 22), stderr=""),
    )
    assert read_saved_adjustments(photo) is None


# ---------------------------------------------------------------------------
# Writing sidecars (opt-in)
# ---------------------------------------------------------------------------

def test_sidecar_writing_is_off_by_default(photo, fake_exiftool):
    """
    A sidecar is what Lightroom reads back, so creating one decides which
    application wins. That is the photographer's call, not a default.
    """
    from editor import save_adjustments

    save_adjustments(photo, {"exposure": 1.0})
    assert not photo.with_suffix(".xmp").exists()


def test_the_dng_and_the_sidecar_carry_the_same_settings(photo, fake_exiftool):
    """They are generated from one argument list, so they cannot drift."""
    from editor import _xmp_arguments, save_adjustments

    save_adjustments(photo, {"exposure": 1.25, "contrast": 30}, sidecar=True)

    writes = [c for c in fake_exiftool.calls if any("Exposure2012" in a for a in c)]
    assert len(writes) == 2, "expected one write to the DNG and one to the sidecar"

    def settings(cmd):
        return sorted(a for a in cmd if a.startswith("-XMP-crs:"))

    assert settings(writes[0]) == settings(writes[1])


def test_the_sidecar_is_named_after_the_photo(photo, fake_exiftool):
    from editor import save_adjustments

    save_adjustments(photo, {"exposure": 1.0}, sidecar=True)

    # The target follows -o when creating; the source is the last argument.
    cmd = fake_exiftool.calls[-1]
    target = cmd[cmd.index("-o") + 1] if "-o" in cmd else cmd[-1]
    assert target.endswith("IMG_0001.xmp")


def test_an_existing_sidecar_is_updated_in_place(photo, fake_exiftool):
    """
    Lightroom keeps far more in a sidecar than this tool manages — the tone
    curve, the lens profile. Replacing the file would discard all of it.
    """
    from editor import write_sidecar

    sidecar = photo.with_suffix(".xmp")
    sidecar.write_text("<xmpmeta/>")

    write_sidecar(photo, {"exposure": 1.0})
    assert "-overwrite_original" in fake_exiftool.calls[-1]
    assert "-o" not in fake_exiftool.calls[-1], "would have created a new file"


def test_an_absent_sidecar_is_created(photo, fake_exiftool, monkeypatch):
    from editor import write_sidecar

    target = photo.with_suffix(".xmp")

    def creating(cmd, **kwargs):
        if "-o" in cmd:
            target.write_text("<xmpmeta/>")
        import subprocess
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(editor.subprocess, "run", creating)
    assert write_sidecar(photo, {"exposure": 1.0}) == target
    assert target.exists()


def test_a_sidecar_that_never_appears_raises(photo, monkeypatch):
    """exiftool warns while still succeeding, so the file is the real test."""
    import subprocess

    from editor import write_sidecar

    monkeypatch.setattr(
        editor.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="disk full"),
    )
    with pytest.raises(RuntimeError, match="disk full"):
        write_sidecar(photo, {"exposure": 1.0})


def test_a_save_that_will_be_read_back_carries_no_warning(tmp_path, monkeypatch, fake_rawpy, fake_exiftool):
    """
    The warning exists because a sidecar overrides the DNG. With sidecar
    writing on, what is read back is what was just written.
    """
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    monkeypatch.setattr(editor, "WRITE_SIDECARS", True)
    (tmp_path / "a.dng").write_bytes(b"dng")
    (tmp_path / "a.xmp").write_text("<xmpmeta/>")

    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        data = c.post("/api/save", json={
            "path": "a.dng", "adjustments": {"exposure": 1.0},
        }).get_json()

    assert data["warning"] is None
