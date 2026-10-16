"""Tests for crop and straighten."""

import numpy as np
from pathlib import Path
import pytest

from editor import _has_crop, _largest_inscribed_rect, apply_adjustments, apply_geometry


BASELINE_TEMP = 5500


@pytest.fixture
def frame():
    """A 200x300 frame with a distinct value per quadrant."""
    img = np.zeros((200, 300, 3), dtype=np.float32)
    img[:100, :150] = 0.2
    img[:100, 150:] = 0.4
    img[100:, :150] = 0.6
    img[100:, 150:] = 0.8
    return img


# ---------------------------------------------------------------------------
# No geometry
# ---------------------------------------------------------------------------

def test_no_geometry_returns_the_frame_unchanged(frame):
    out = apply_geometry(frame, {})
    assert out is frame, "should short-circuit rather than copy"


def test_zero_angle_and_full_crop_change_nothing(frame):
    out = apply_geometry(frame, {"straighten": 0.0, "crop": {"left": 0, "top": 0, "right": 1, "bottom": 1}})
    np.testing.assert_array_equal(out, frame)


# ---------------------------------------------------------------------------
# Crop
# ---------------------------------------------------------------------------

def test_crop_reduces_the_frame(frame):
    out = apply_geometry(frame, {"crop": {"left": 0.25, "top": 0.25, "right": 0.75, "bottom": 0.75}})
    assert out.shape[0] == pytest.approx(100, abs=2)
    assert out.shape[1] == pytest.approx(150, abs=2)


def test_crop_selects_the_right_region(frame):
    """Cropping to the bottom-right quadrant must yield only its value."""
    out = apply_geometry(frame, {"crop": {"left": 0.55, "top": 0.55, "right": 1.0, "bottom": 1.0}})
    assert float(out.min()) == pytest.approx(0.8, abs=1e-5)
    assert float(out.max()) == pytest.approx(0.8, abs=1e-5)


def test_crop_fractions_are_resolution_independent():
    """
    The same fractional crop must select the same part of the picture at any
    preview size — the whole reason it is stored as fractions.
    """
    small = np.linspace(0, 1, 100 * 150, dtype=np.float32).reshape(100, 150, 1).repeat(3, 2)
    large = np.linspace(0, 1, 400 * 600, dtype=np.float32).reshape(400, 600, 1).repeat(3, 2)
    crop = {"left": 0.25, "top": 0.25, "right": 0.75, "bottom": 0.75}

    a = apply_geometry(small, {"crop": crop})
    b = apply_geometry(large, {"crop": crop})
    assert a.shape[1] / a.shape[0] == pytest.approx(b.shape[1] / b.shape[0], rel=0.02)
    assert float(a.mean()) == pytest.approx(float(b.mean()), abs=0.02)


@pytest.mark.parametrize("crop", [
    {"left": 0.8, "top": 0, "right": 0.2, "bottom": 1},      # inverted
    {"left": 0.5, "top": 0.5, "right": 0.5, "bottom": 0.5},  # collapsed
    {"left": 0.0, "top": 0.0, "right": 0.01, "bottom": 0.01},  # sub-minimum
])
def test_a_degenerate_crop_is_ignored(frame, crop):
    """An empty array would break every downstream step, so refuse it."""
    out = apply_geometry(frame, {"crop": crop})
    assert out.shape[0] > 0 and out.shape[1] > 0
    assert np.isfinite(out).all()


def test_out_of_range_crop_values_are_clamped(frame):
    out = apply_geometry(frame, {"crop": {"left": -0.5, "top": -0.5, "right": 1.9, "bottom": 1.9}})
    assert out.shape[:2] == frame.shape[:2]


# ---------------------------------------------------------------------------
# Straighten
# ---------------------------------------------------------------------------

def test_straighten_leaves_no_empty_corners():
    """
    A rotation without the inscribed-rectangle crop leaves black wedges at
    the corners. Every pixel must still carry image data.
    """
    img = np.full((200, 300, 3), 0.5, dtype=np.float32)
    out = apply_geometry(img, {"straighten": 8.0})

    assert out.size > 0
    assert float(out.min()) > 0.3, "empty corner material survived the crop"


def test_straighten_shrinks_the_frame(frame):
    out = apply_geometry(frame, {"straighten": 10.0})
    assert out.shape[0] < frame.shape[0]
    assert out.shape[1] < frame.shape[1]


def test_larger_angles_crop_more(frame):
    areas = [
        apply_geometry(frame, {"straighten": a}).shape[0]
        * apply_geometry(frame, {"straighten": a}).shape[1]
        for a in (2.0, 6.0, 12.0)
    ]
    assert areas == sorted(areas, reverse=True)


def test_straighten_is_symmetric(frame):
    left = apply_geometry(frame, {"straighten": -7.0})
    right = apply_geometry(frame, {"straighten": 7.0})
    assert left.shape == right.shape


def test_straighten_actually_rotates():
    """A diagonal edge must land somewhere different after straightening."""
    img = np.zeros((200, 300, 3), dtype=np.float32)
    img[:, :150] = 1.0          # vertical edge down the middle
    out = apply_geometry(img, {"straighten": 10.0})

    # A rotated hard edge produces intermediate values along it.
    interior = out[10:-10, 10:-10]
    assert ((interior > 0.05) & (interior < 0.95)).any(), "no rotation occurred"


# ---------------------------------------------------------------------------
# Inscribed rectangle
# ---------------------------------------------------------------------------

def test_inscribed_rect_at_zero_angle_is_the_whole_frame():
    w, h = _largest_inscribed_rect(300, 200, 0.0)
    assert w == pytest.approx(300, rel=0.01)
    assert h == pytest.approx(200, rel=0.01)


def test_inscribed_rect_fits_inside_the_rotation():
    """The returned rectangle must actually fit, or corners show through."""
    for angle_deg in (3, 10, 25, 44):
        a = np.deg2rad(angle_deg)
        w, h = _largest_inscribed_rect(300.0, 200.0, a)
        # Projected extent of the rotated rectangle must not exceed the original.
        assert w * abs(np.cos(a)) + h * abs(np.sin(a)) <= 300.0 + 1e-6
        assert w * abs(np.sin(a)) + h * abs(np.cos(a)) <= 200.0 + 1e-6


def test_inscribed_rect_shrinks_as_the_angle_grows():
    areas = [
        np.prod(_largest_inscribed_rect(300.0, 200.0, np.deg2rad(a)))
        for a in (0, 5, 15, 30)
    ]
    assert areas == sorted(areas, reverse=True)


def test_inscribed_rect_handles_a_square():
    w, h = _largest_inscribed_rect(200.0, 200.0, np.deg2rad(45))
    assert w > 0 and h > 0
    assert w == pytest.approx(h, rel=0.02)


def test_inscribed_rect_handles_degenerate_input():
    assert _largest_inscribed_rect(0.0, 100.0, 0.5) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# Interaction with the rest of the pipeline
# ---------------------------------------------------------------------------

def test_geometry_runs_before_the_tonal_work(frame):
    """
    Cropping after the tonal pass would mean the histogram and the local
    contrast masks described material the photographer threw away.
    """
    crop = {"left": 0.55, "top": 0.55, "right": 1.0, "bottom": 1.0}
    out = apply_adjustments(frame, BASELINE_TEMP, {"crop": crop, "exposure": -1.0})

    # Only the 0.8 quadrant survives, darkened by a stop.
    assert float(out.mean()) == pytest.approx(0.4, abs=0.05)


def test_strength_compares_against_the_cropped_frame(frame):
    """
    At zero strength the result must be the cropped original, not the whole
    uncropped frame — the crop is framing, not an adjustment to dial back.
    """
    crop = {"left": 0.25, "top": 0.25, "right": 0.75, "bottom": 0.75}
    expected = apply_geometry(frame, {"crop": crop})
    out = apply_adjustments(frame, BASELINE_TEMP, {"crop": crop, "exposure": 2.0, "strength": 0})

    assert out.shape == expected.shape
    np.testing.assert_allclose(out, expected, atol=1e-5)


def test_edit_strength_does_not_scale_the_angle():
    """Sliding strength must not swing the horizon around."""
    from editor import effective_adjustments

    out = effective_adjustments({"straighten": 6.0, "exposure": 2.0, "strength": 50})
    assert out["straighten"] == pytest.approx(6.0)
    assert out["exposure"] == pytest.approx(1.0)


def test_cropped_output_stays_in_range(frame):
    out = apply_adjustments(
        frame, BASELINE_TEMP,
        {"crop": {"left": 0.2, "top": 0.2, "right": 0.8, "bottom": 0.8},
         "straighten": 5.0, "exposure": 2.0, "contrast": 80, "clarity": 60},
    )
    assert np.isfinite(out).all()
    assert out.min() >= 0.0 and out.max() <= 1.0


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_has_crop_is_false_for_an_untouched_photo():
    assert _has_crop({}) is False
    assert _has_crop({"straighten": 0.0, "crop": None}) is False
    assert _has_crop({"crop": {"left": 0, "top": 0, "right": 1, "bottom": 1}}) is False


@pytest.mark.parametrize("adj", [
    {"straighten": 2.0},
    {"crop": {"left": 0.1, "top": 0, "right": 1, "bottom": 1}},
    {"crop": {"left": 0, "top": 0, "right": 0.9, "bottom": 1}},
    {"crop": {"left": 0, "top": 0.05, "right": 1, "bottom": 1}},
    {"crop": {"left": 0, "top": 0, "right": 1, "bottom": 0.95}},
])
def test_has_crop_detects_every_edge(adj):
    assert _has_crop(adj) is True


# ---------------------------------------------------------------------------
# Crop coordinates under straighten
# ---------------------------------------------------------------------------

def test_crop_is_unchanged_without_straighten():
    """With no rotation the preview is the whole frame; nothing to convert."""
    crop = {"left": 0.1, "top": 0.2, "right": 0.8, "bottom": 0.9}
    from editor import crop_in_full_frame

    assert crop_in_full_frame(crop, 0.0, 300, 200) == pytest.approx(crop)


def test_crop_is_converted_to_full_frame_coordinates():
    """
    A crop dragged on screen is relative to the inscribed rectangle the
    preview shows, not the whole straightened frame. Writing it unchanged
    selected a different region: 81px on screen against 75px in the file.
    """
    from editor import crop_in_full_frame

    converted = crop_in_full_frame(
        {"left": 0.0, "top": 0.0, "right": 0.25, "bottom": 1.0}, 10.0, 300, 200
    )
    assert converted["right"] * 300 == pytest.approx(81, abs=1)


def test_the_converted_crop_matches_what_the_preview_shows():
    """The two must select the same pixels, which is the whole point."""
    import numpy as np

    from editor import _largest_inscribed_rect, crop_in_full_frame

    w, h, angle = 400.0, 300.0, 7.0
    crop = {"left": 0.2, "top": 0.1, "right": 0.7, "bottom": 0.85}

    keep_w, keep_h = _largest_inscribed_rect(w, h, np.deg2rad(angle))
    x0, y0 = (w - keep_w) / 2, (h - keep_h) / 2

    # What apply_geometry selects, in full-frame pixels.
    preview_left = x0 + crop["left"] * keep_w
    preview_right = x0 + crop["right"] * keep_w

    converted = crop_in_full_frame(crop, angle, w, h)
    assert converted["left"] * w == pytest.approx(preview_left, abs=0.5)
    assert converted["right"] * w == pytest.approx(preview_right, abs=0.5)


def test_straighten_alone_writes_the_inscribed_rectangle():
    """
    A full 0..1 rect would tell Camera Raw to keep the empty wedges the
    preview deliberately trims.
    """
    from editor import crop_in_full_frame

    crop = crop_in_full_frame(None, 10.0, 300, 200)
    assert crop["left"] > 0.01
    assert crop["right"] < 0.99
    assert (crop["right"] - crop["left"]) * 300 == pytest.approx(277, abs=2)


def test_a_degenerate_frame_size_is_survived():
    from editor import crop_in_full_frame

    crop = {"left": 0.1, "top": 0.1, "right": 0.9, "bottom": 0.9}
    assert crop_in_full_frame(crop, 10.0, 0, 0) == pytest.approx(crop)


def test_saving_a_straightened_crop_writes_converted_fractions(fake_exiftool):
    """End to end: the tags written must be in full-frame coordinates."""
    from editor import save_adjustments

    save_adjustments(
        Path("x.dng"),
        {"straighten": 10.0, "crop": {"left": 0.0, "top": 0.0, "right": 0.25, "bottom": 1.0}},
        frame_size=(300, 200),
    )
    written = " ".join(fake_exiftool.write_call())
    assert "CropAngle=10.00" in written

    # Compared numerically rather than as a string, so the assertion is about
    # the geometry and not about float formatting.
    value = float(next(a.split("=")[1] for a in written.split() if "CropRight=" in a))
    assert value * 300 == pytest.approx(81, abs=1), "not in full-frame coordinates"


def test_saving_without_straighten_writes_the_crop_as_given(fake_exiftool):
    from editor import save_adjustments

    save_adjustments(
        Path("x.dng"),
        {"straighten": 0.0, "crop": {"left": 0.1, "top": 0.2, "right": 0.8, "bottom": 0.9}},
        frame_size=(300, 200),
    )
    written = " ".join(fake_exiftool.write_call())
    assert "CropLeft=0.10000" in written
    assert "CropRight=0.80000" in written
