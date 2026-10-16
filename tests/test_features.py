"""Tests for edit strength, black and white, and the histogram."""

from pathlib import Path

import numpy as np
import pytest

from editor import (
    CONTROLS,
    HISTOGRAM_BINS,
    _luminance,
    apply_adjustments,
    compute_histogram,
    effective_adjustments,
)


BASELINE_TEMP = 5500


@pytest.fixture
def scene():
    """A colourful gradient — enough tonal and colour range to be meaningful."""
    rng = np.random.default_rng(0)
    img = rng.random((32, 32, 3), dtype=np.float32) * 0.6 + 0.2
    return img


# ---------------------------------------------------------------------------
# Edit strength
# ---------------------------------------------------------------------------

def test_zero_strength_returns_the_original(scene):
    """At 0% the photo must be untouched no matter what the sliders say."""
    heavy = {"exposure": 2.0, "contrast": 80, "saturation": 90, "shadows": 60}
    out = apply_adjustments(scene, BASELINE_TEMP, {**heavy, "strength": 0})
    np.testing.assert_allclose(out, scene, atol=1e-5)


def test_full_strength_matches_an_unscaled_edit(scene):
    adj = {"exposure": 1.0, "contrast": 40}
    plain = apply_adjustments(scene, BASELINE_TEMP, adj)
    full = apply_adjustments(scene, BASELINE_TEMP, {**adj, "strength": 100})
    np.testing.assert_allclose(full, plain, atol=1e-6)


def test_partial_strength_lands_between_original_and_edit(scene):
    adj = {"exposure": 1.5, "contrast": 50}
    full = apply_adjustments(scene, BASELINE_TEMP, adj)
    half = apply_adjustments(scene, BASELINE_TEMP, {**adj, "strength": 50})

    # Half must differ from both ends, and sit between them everywhere.
    assert not np.allclose(half, scene, atol=1e-3)
    assert not np.allclose(half, full, atol=1e-3)
    lo, hi = np.minimum(scene, full), np.maximum(scene, full)
    assert ((half >= lo - 1e-5) & (half <= hi + 1e-5)).all()


def test_strength_scales_monotonically(scene):
    """More strength must mean more departure from the original."""
    adj = {"exposure": 1.5, "saturation": 60}
    distances = [
        float(np.abs(apply_adjustments(scene, BASELINE_TEMP, {**adj, "strength": s}) - scene).mean())
        for s in (0, 25, 50, 75, 100)
    ]
    assert distances == sorted(distances)
    assert distances[0] == pytest.approx(0.0, abs=1e-6)


def test_strength_above_100_exaggerates_the_edit(scene):
    adj = {"exposure": 0.5}
    full = float(np.abs(apply_adjustments(scene, BASELINE_TEMP, adj) - scene).mean())
    over = float(np.abs(
        apply_adjustments(scene, BASELINE_TEMP, {**adj, "strength": 150}) - scene
    ).mean())
    assert over > full


def test_strength_output_stays_in_range(scene):
    """Extrapolation past 100% must still not produce invalid pixels."""
    for s in (0, 50, 100, 150):
        out = apply_adjustments(
            scene, BASELINE_TEMP, {"exposure": 4.0, "contrast": 100, "strength": s}
        )
        assert out.min() >= 0.0 and out.max() <= 1.0
        assert np.isfinite(out).all()


# ---------------------------------------------------------------------------
# Strength when saved
# ---------------------------------------------------------------------------

def test_effective_adjustments_scales_toward_neutral():
    adj = {"exposure": 2.0, "contrast": 60, "shadows": 40, "strength": 50}
    out = effective_adjustments(adj)
    assert out["exposure"] == pytest.approx(1.0)
    assert out["contrast"] == pytest.approx(30.0)
    assert out["shadows"] == pytest.approx(20.0)


def test_effective_adjustments_is_identity_at_full_strength():
    adj = {"exposure": 1.25, "contrast": 30, "strength": 100}
    out = effective_adjustments(adj)
    assert out["exposure"] == pytest.approx(1.25)
    assert out["contrast"] == pytest.approx(30)


def test_effective_adjustments_zeroes_everything_at_zero_strength():
    adj = {"exposure": 2.0, "contrast": 60, "vibrance": -40, "strength": 0}
    out = effective_adjustments(adj)
    assert out["exposure"] == pytest.approx(0.0)
    assert out["contrast"] == pytest.approx(0.0)
    assert out["vibrance"] == pytest.approx(0.0)


def test_temperature_scales_from_the_camera_baseline_not_5500k():
    """
    Scaling temp toward the generic 5500K default would tint the photo as
    strength drops. It must collapse toward the camera's own white balance.
    """
    adj = {"color_temp": 7000, "_baseline_temp": 3800, "strength": 50}
    assert effective_adjustments(adj)["color_temp"] == pytest.approx(5400)

    at_zero = effective_adjustments({**adj, "strength": 0})
    assert at_zero["color_temp"] == pytest.approx(3800)


# ---------------------------------------------------------------------------
# Black and white
# ---------------------------------------------------------------------------

def test_monochrome_makes_all_channels_equal(scene):
    out = apply_adjustments(scene, BASELINE_TEMP, {"monochrome": True})
    np.testing.assert_allclose(out[:, :, 0], out[:, :, 1], atol=1e-5)
    np.testing.assert_allclose(out[:, :, 1], out[:, :, 2], atol=1e-5)


def test_monochrome_preserves_luminance(scene):
    """Converting to grey must not change how bright the photo is."""
    out = apply_adjustments(scene, BASELINE_TEMP, {"monochrome": True})
    assert float(_luminance(out).mean()) == pytest.approx(
        float(_luminance(scene).mean()), abs=1e-4
    )


def test_monochrome_off_leaves_colour_intact(scene):
    out = apply_adjustments(scene, BASELINE_TEMP, {"monochrome": False})
    np.testing.assert_allclose(out, scene, atol=1e-6)


def test_colour_sliders_act_as_channel_mixer_under_monochrome(scene):
    """
    Monochrome runs after white balance, so temperature changes which colours
    map to which greys. If it ran first, these would be identical.
    """
    warm = apply_adjustments(scene, BASELINE_TEMP, {"monochrome": True, "color_temp": 9000})
    cool = apply_adjustments(scene, BASELINE_TEMP, {"monochrome": True, "color_temp": 3000})
    assert not np.allclose(warm, cool, atol=1e-3)


def test_monochrome_ignores_edit_strength(scene):
    """
    The file stores monochrome as a boolean, so a blended preview showed a
    half-desaturated version of a photograph that saves fully grey. It is
    all-or-nothing on screen to match.
    """
    for strength in (25, 50, 100):
        out = apply_adjustments(scene, BASELINE_TEMP, {"monochrome": True, "strength": strength})
        spread = float(np.abs(out[:, :, 0] - out[:, :, 2]).max())
        assert spread < 1e-4, f"strength {strength}% left colour in a monochrome render"


def test_monochrome_never_overshoots_into_inverted_colour(scene):
    """
    Grey is the limit of desaturation. Extrapolating past it would swing the
    channels back apart with the colours reversed, which is not "more" B&W.
    """
    for strength in (100, 120, 150):
        out = apply_adjustments(
            scene, BASELINE_TEMP, {"monochrome": True, "strength": strength}
        )
        spread = float(np.abs(out[:, :, 0] - out[:, :, 2]).max())
        assert spread < 1e-4, f"strength {strength}% reintroduced colour (spread {spread:.4f})"


# ---------------------------------------------------------------------------
# Histogram
# ---------------------------------------------------------------------------

def test_histogram_has_all_channels_at_the_expected_size(scene):
    h = compute_histogram(scene)
    for key in ("r", "g", "b", "lum"):
        assert len(h[key]) == HISTOGRAM_BINS


def test_histogram_loses_no_pixels_from_its_sample(scene):
    """
    The histogram counts a strided sample rather than every pixel — at 64
    bins drawn 280px wide the difference is invisible and it runs on every
    slider move. Every sampled pixel must still land in exactly one bin.
    """
    from editor import HISTOGRAM_STRIDE

    h = compute_histogram(scene)
    sampled = scene[::HISTOGRAM_STRIDE, ::HISTOGRAM_STRIDE]
    expected = sampled.shape[0] * sampled.shape[1]
    for key in ("r", "g", "b", "lum"):
        assert sum(h[key]) == expected, f"{key} lost pixels"


def test_histogram_shape_survives_subsampling(scene):
    """Sampling must not change the distribution the user sees."""
    import editor

    h_sampled = compute_histogram(scene)
    original = editor.HISTOGRAM_STRIDE
    try:
        editor.HISTOGRAM_STRIDE = 1
        h_full = compute_histogram(scene)
    finally:
        editor.HISTOGRAM_STRIDE = original

    def normalise(counts):
        total = max(1, sum(counts))
        return np.array([c / total for c in counts])

    assert np.abs(normalise(h_sampled["lum"]) - normalise(h_full["lum"])).max() < 0.05


def test_histogram_tracks_brightness(scene):
    """Brightening must shift weight toward the upper bins."""
    def centre(img):
        counts = compute_histogram(img)["lum"]
        idx = np.arange(len(counts))
        return float((idx * counts).sum() / max(1, sum(counts)))

    dark = apply_adjustments(scene, BASELINE_TEMP, {"exposure": -1.5})
    bright = apply_adjustments(scene, BASELINE_TEMP, {"exposure": 1.5})
    assert centre(dark) < centre(scene) < centre(bright)


def test_histogram_reports_highlight_clipping():
    blown = np.full((16, 16, 3), 1.0, dtype=np.float32)
    h = compute_histogram(blown)
    assert h["clipped_highlights"] == pytest.approx(100.0)
    assert h["clipped_shadows"] == pytest.approx(0.0)


def test_histogram_reports_shadow_clipping():
    crushed = np.zeros((16, 16, 3), dtype=np.float32)
    h = compute_histogram(crushed)
    assert h["clipped_shadows"] == pytest.approx(100.0)
    assert h["clipped_highlights"] == pytest.approx(0.0)


def test_histogram_reports_no_clipping_for_a_safe_image():
    safe = np.full((16, 16, 3), 0.5, dtype=np.float32)
    h = compute_histogram(safe)
    assert h["clipped_shadows"] == 0.0
    assert h["clipped_highlights"] == 0.0


def test_monochrome_histogram_channels_coincide(scene):
    """With colour removed, the three channel curves must be identical."""
    grey = apply_adjustments(scene, BASELINE_TEMP, {"monochrome": True})
    h = compute_histogram(grey)
    assert h["r"] == h["g"] == h["b"]


def test_histogram_values_are_json_safe(scene):
    """numpy ints do not survive jsonify; the counts must be plain ints."""
    h = compute_histogram(scene)
    assert all(type(v) is int for v in h["lum"])
    assert type(h["clipped_shadows"]) is float
