"""Tests for metadata parsing, auto-adjustment logic, and the revert paths."""

from pathlib import Path

import pytest

import process
from process import (
    compute_auto_adjustments,
    extract_original_raw,
    get_embedded_original_name,
    get_iso,
    get_shoot_date,
    resolve_output_dir,
    strip_xmp_adjustments,
)


# ---------------------------------------------------------------------------
# EXIF parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw_value,expected",
    [
        ("2026:09:14 12:34:56", "2026/09/14"),
        ("1999:01:01 00:00:00", "1999/01/01"),
        ("-", None),                     # exiftool's "tag absent" marker
        ("", None),
        ("garbage", None),               # no colon at all
        ("2026:09 12:34:56", None),      # too few date parts
    ],
)
def test_get_shoot_date_parsing(fake_exiftool, raw_value, expected):
    fake_exiftool.stdout_lines = [raw_value]
    assert get_shoot_date(Path("x.CR2")) == expected


@pytest.mark.parametrize(
    "raw_value,expected",
    [("100", 100), ("6400", 6400), ("-", 0), ("", 0), ("not-a-number", 0)],
)
def test_get_iso_parsing(fake_exiftool, raw_value, expected):
    fake_exiftool.stdout_lines = [raw_value]
    assert get_iso(Path("x.CR2")) == expected


def test_exiftool_get_maps_tags_positionally(fake_exiftool):
    """Values come back as bare lines, so tag/value alignment is by position."""
    fake_exiftool.stdout_lines = ["800", "2026:09:14 10:00:00"]
    result = process._exiftool_get(Path("x.CR2"), "ISO", "DateTimeOriginal")
    assert result == {"ISO": "800", "DateTimeOriginal": "2026:09:14 10:00:00"}


def test_exiftool_get_pads_missing_values(fake_exiftool):
    """Fewer output lines than requested tags must not raise."""
    fake_exiftool.stdout_lines = ["800"]
    result = process._exiftool_get(Path("x.CR2"), "ISO", "Missing")
    assert result["ISO"] == "800"
    assert result["Missing"] == "-"


# ---------------------------------------------------------------------------
# Output paths
# ---------------------------------------------------------------------------

def test_resolve_output_dir_defaults_beside_source():
    src = Path("/photos/shoot/IMG_1.CR2")
    assert resolve_output_dir(src, None, date_folders=False) == Path("/photos/shoot")


def test_resolve_output_dir_honors_explicit_base():
    src = Path("/photos/shoot/IMG_1.CR2")
    assert resolve_output_dir(src, Path("/out"), date_folders=False) == Path("/out")


def test_resolve_output_dir_appends_date_folders(fake_exiftool):
    fake_exiftool.stdout_lines = ["2026:09:14 12:00:00"]
    out = resolve_output_dir(Path("/photos/IMG_1.CR2"), Path("/out"), date_folders=True)
    assert out == Path("/out/2026/09/14")


def test_resolve_output_dir_skips_date_when_exif_missing(fake_exiftool):
    """A photo with no date must land in the base dir, not a malformed path."""
    fake_exiftool.stdout_lines = ["-"]
    out = resolve_output_dir(Path("/photos/IMG_1.CR2"), Path("/out"), date_folders=True)
    assert out == Path("/out")


# ---------------------------------------------------------------------------
# Auto adjustments
# ---------------------------------------------------------------------------

def test_auto_adjustments_returns_all_expected_keys(fake_rawpy, fake_exiftool):
    fake_exiftool.stdout_lines = ["100"]
    adj = compute_auto_adjustments(Path("x.CR2"))
    assert set(adj) == {
        "exposure", "shadows", "highlights",
        "color_temp", "tint", "luminance_nr", "iso",
    }


def test_auto_adjustments_clamp_to_documented_ranges(fake_rawpy, fake_exiftool):
    fake_exiftool.stdout_lines = ["100"]
    adj = compute_auto_adjustments(Path("x.CR2"))
    assert -2.0 <= adj["exposure"] <= 2.0
    assert 2500 <= adj["color_temp"] <= 10000
    assert -50 <= adj["tint"] <= 50
    assert 0 <= adj["shadows"] <= 80
    assert -100 <= adj["highlights"] <= 0


def test_exposure_is_quantized_to_quarter_stops(fake_rawpy, fake_exiftool):
    """Values are rounded to 1/4 EV so they read cleanly in Camera Raw."""
    fake_exiftool.stdout_lines = ["100"]
    exposure = compute_auto_adjustments(Path("x.CR2"))["exposure"]
    assert (exposure * 4) == pytest.approx(round(exposure * 4))


@pytest.mark.parametrize(
    "whitebalance",
    [
        [40.0, 1.0, 1.0, 1.0],    # absurdly red — would compute far below 2500K
        [1.0, 1.0, 40.0, 1.0],    # absurdly blue — would compute far above 10000K
    ],
)
def test_extreme_white_balance_is_clamped(fake_rawpy, fake_exiftool, whitebalance):
    """A wild camera multiplier must not yield a temp Camera Raw would reject."""
    fake_exiftool.stdout_lines = ["100"]
    fake_rawpy["whitebalance"] = whitebalance
    temp = compute_auto_adjustments(Path("x.CR2"))["color_temp"]
    assert 2500 <= temp <= 10000, f"temp {temp}K escaped the valid range"


def test_higher_red_multiplier_yields_higher_temp(fake_rawpy, fake_exiftool):
    """A high red gain means the camera balanced for a blue (high-Kelvin) light, as in Lightroom;
    a warm tungsten scene needs little red gain and reads low. (This used to run the other way.)"""
    fake_exiftool.stdout_lines = ["100"]

    fake_rawpy["whitebalance"] = [2.5, 1.0, 1.0, 1.0]
    shade = compute_auto_adjustments(Path("x.CR2"))["color_temp"]

    fake_rawpy["whitebalance"] = [1.0, 1.0, 2.5, 1.0]
    tungsten = compute_auto_adjustments(Path("x.CR2"))["color_temp"]

    assert tungsten < shade


@pytest.mark.parametrize(
    "iso,expected_nr",
    [(100, 0), (400, 0), (800, 5), (1600, 10), (3200, 20), (6400, 60)],
)
def test_noise_reduction_scales_with_iso(fake_rawpy, fake_exiftool, iso, expected_nr):
    fake_exiftool.stdout_lines = [str(iso)]
    assert compute_auto_adjustments(Path("x.CR2"))["luminance_nr"] == expected_nr


def test_noise_reduction_never_exceeds_cap(fake_rawpy, fake_exiftool):
    """Even absurd ISO values must stay within the 0-60 range."""
    fake_exiftool.stdout_lines = ["102400"]
    assert compute_auto_adjustments(Path("x.CR2"))["luminance_nr"] <= 60


def test_dark_image_gets_positive_exposure(fake_rawpy, fake_exiftool, gradient_rgb):
    import numpy as np

    fake_exiftool.stdout_lines = ["100"]
    fake_rawpy["rgb"] = (gradient_rgb * 0.1).astype(np.uint8)
    assert compute_auto_adjustments(Path("x.CR2"))["exposure"] > 0


def test_bright_image_gets_negative_exposure(fake_rawpy, fake_exiftool):
    import numpy as np

    fake_exiftool.stdout_lines = ["100"]
    fake_rawpy["rgb"] = np.full((64, 64, 3), 240, dtype=np.uint8)
    assert compute_auto_adjustments(Path("x.CR2"))["exposure"] < 0


def test_blown_highlights_are_pulled_back(fake_rawpy, fake_exiftool):
    import numpy as np

    fake_exiftool.stdout_lines = ["100"]
    fake_rawpy["rgb"] = np.full((64, 64, 3), 253, dtype=np.uint8)
    assert compute_auto_adjustments(Path("x.CR2"))["highlights"] < 0


def test_crushed_shadows_are_lifted(fake_rawpy, fake_exiftool):
    import numpy as np

    fake_exiftool.stdout_lines = ["100"]
    fake_rawpy["rgb"] = np.full((64, 64, 3), 2, dtype=np.uint8)
    assert compute_auto_adjustments(Path("x.CR2"))["shadows"] > 0


def test_black_frame_does_not_divide_by_zero(fake_rawpy, fake_exiftool):
    """A fully black frame has median 0; exposure must degrade gracefully."""
    import numpy as np

    fake_exiftool.stdout_lines = ["100"]
    fake_rawpy["rgb"] = np.zeros((64, 64, 3), dtype=np.uint8)
    adj = compute_auto_adjustments(Path("x.CR2"))
    assert adj["exposure"] == 0.0


# ---------------------------------------------------------------------------
# Revert: clearing adjustments
# ---------------------------------------------------------------------------

def test_strip_clears_the_whole_crs_group(fake_exiftool):
    strip_xmp_adjustments(Path("x.dng"))
    cmd = fake_exiftool.write_call()
    assert "-XMP-crs:all=" in cmd, "must clear every develop setting, not just some"


def test_strip_does_not_leave_a_backup_copy(fake_exiftool):
    """-overwrite_original keeps the library free of _original clutter."""
    strip_xmp_adjustments(Path("x.dng"))
    assert "-overwrite_original" in fake_exiftool.write_call()


def test_strip_raises_on_exiftool_failure(fake_exiftool):
    fake_exiftool.returncode = 1
    fake_exiftool.stderr = "file is read-only"
    with pytest.raises(RuntimeError, match="read-only"):
        strip_xmp_adjustments(Path("x.dng"))


# ---------------------------------------------------------------------------
# Revert: recovering the embedded original
# ---------------------------------------------------------------------------

def test_embedded_original_name_returned_when_present(fake_exiftool):
    fake_exiftool.stdout_lines = ["IMG_0001.CR2"]
    assert get_embedded_original_name(Path("x.dng")) == "IMG_0001.CR2"


@pytest.mark.parametrize("absent", ["-", ""])
def test_embedded_original_name_none_when_absent(fake_exiftool, absent):
    fake_exiftool.stdout_lines = [absent]
    assert get_embedded_original_name(Path("x.dng")) is None


def test_extract_uses_the_dng_converter_not_exiftool(fake_exiftool, fake_converter, tmp_path):
    """
    exiftool's OriginalRawFileData returns the compressed blob as stored,
    which is not a loadable raw file. Only Adobe's -x reverses that packing.
    """
    fake_exiftool.stdout_lines = ["IMG_0042.CR2"]
    fake_converter.produces = b"II*\x00original-sensor-bytes"

    out = extract_original_raw(Path("x.dng"), tmp_path)

    assert out == tmp_path / "IMG_0042.CR2"
    cmd = fake_converter.last_call()
    assert "-x" in cmd, "must use the converter's extract flag"
    assert not any("OriginalRawFileData" in a for a in cmd)


def test_extract_output_is_a_real_raw_file(fake_exiftool, fake_converter, tmp_path):
    """Guard the actual failure seen: a file written that is not a valid raw."""
    fake_exiftool.stdout_lines = ["IMG_1.CR2"]
    fake_converter.produces = b"II*\x00" + b"\x00" * 64

    out = extract_original_raw(Path("x.dng"), tmp_path)
    assert out.read_bytes()[:3] == b"II*", "recovered file is not a TIFF/CR2 container"


def test_extract_creates_missing_output_directory(fake_exiftool, fake_converter, tmp_path):
    fake_exiftool.stdout_lines = ["IMG_1.CR2"]
    nested = tmp_path / "deep" / "nested"
    out = extract_original_raw(Path("x.dng"), nested)
    assert out.exists()


def test_extract_refuses_when_no_original_embedded(fake_exiftool, fake_converter, tmp_path):
    fake_exiftool.stdout_lines = ["-"]
    with pytest.raises(RuntimeError, match="no embedded original"):
        extract_original_raw(Path("x.dng"), tmp_path)


def test_extract_reports_converter_failure(fake_exiftool, fake_converter, tmp_path):
    fake_exiftool.stdout_lines = ["IMG_1.CR2"]
    fake_converter.returncode = 1
    fake_converter.stderr = "cannot read file"
    with pytest.raises(RuntimeError, match="cannot read file"):
        extract_original_raw(Path("x.dng"), tmp_path)


def test_extract_fails_when_converter_writes_nothing(fake_exiftool, fake_converter, tmp_path):
    """A silent no-op must raise rather than report a recovery that never happened."""
    fake_exiftool.stdout_lines = ["IMG_1.CR2"]
    fake_converter.produces = None
    with pytest.raises(RuntimeError, match="Could not extract"):
        extract_original_raw(Path("x.dng"), tmp_path)


def test_embed_writes_every_adjustment_field(fake_exiftool):
    adj = {
        "color_temp": 5200, "tint": -3, "exposure": 0.75,
        "shadows": 20, "highlights": -30, "luminance_nr": 15,
    }
    process.embed_xmp_adjustments(Path("x.dng"), adj)
    cmd = " ".join(fake_exiftool.write_call())
    assert "ColorTemperature=5200" in cmd
    assert "Tint=-3" in cmd
    assert "Exposure2012=0.75" in cmd
    assert "Shadows2012=20" in cmd
    assert "Highlights2012=-30" in cmd
    assert "LuminanceSmoothing=15" in cmd
