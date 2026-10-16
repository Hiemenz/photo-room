"""Tests for the local-contrast controls: clarity, texture, dehaze, sharpness."""

import numpy as np
import pytest

from editor import _apply_detail, _blur, _luminance, apply_adjustments


BASELINE_TEMP = 5500


@pytest.fixture
def detailed():
    """A mid-grey field with fine detail and a soft large-scale gradient."""
    rng = np.random.default_rng(0)
    y, x = np.mgrid[0:96, 0:96]
    base = 0.45 + 0.1 * np.sin(x / 30.0)          # large-scale structure
    fine = 0.05 * rng.standard_normal((96, 96))    # fine texture
    plane = np.clip(base + fine, 0.05, 0.95).astype(np.float32)
    return np.repeat(plane[:, :, None], 3, axis=2)


def local_contrast(img, radius):
    """Average deviation from a blurred version — how much detail is present."""
    lum = _luminance(img)
    return float(np.abs(lum - _blur(lum, radius)).mean())


# ---------------------------------------------------------------------------
# Shared invariants
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["clarity", "texture", "dehaze", "sharpness"])
@pytest.mark.parametrize("value", [-100, -50, 50, 100, 150])
def test_detail_controls_stay_in_range(detailed, key, value):
    if key == "sharpness" and value < 0:
        pytest.skip("sharpness has no negative half")
    out = _apply_detail(detailed, {key: value})
    assert np.isfinite(out).all(), f"{key}={value} produced NaN/inf"
    assert out.min() >= 0.0 and out.max() <= 1.0


def test_all_detail_controls_at_zero_is_a_no_op(detailed):
    out = _apply_detail(detailed, {"clarity": 0, "texture": 0, "dehaze": 0, "sharpness": 0})
    np.testing.assert_array_equal(out, detailed)


def test_empty_adjustments_skip_the_work_entirely(detailed):
    """The blur is the most expensive step; it must not run when unused."""
    out = _apply_detail(detailed, {})
    assert out is detailed, "returned a copy instead of short-circuiting"


def test_combined_detail_controls_stay_in_range(detailed):
    out = _apply_detail(detailed, {"clarity": 100, "texture": 100, "dehaze": 100, "sharpness": 150})
    assert np.isfinite(out).all()
    assert out.min() >= 0.0 and out.max() <= 1.0


# ---------------------------------------------------------------------------
# Clarity
# ---------------------------------------------------------------------------

def test_positive_clarity_increases_large_scale_contrast(detailed):
    out = _apply_detail(detailed, {"clarity": 80})
    assert local_contrast(out, 8) > local_contrast(detailed, 8)


def test_negative_clarity_softens(detailed):
    out = _apply_detail(detailed, {"clarity": -80})
    assert local_contrast(out, 8) < local_contrast(detailed, 8)


def test_clarity_spares_the_extremes(detailed):
    """
    Clarity is midtone-weighted. Without that it produces halos around
    highlights and blocks up shadows.
    """
    img = detailed.copy()
    img[:20] = 0.98      # near-white band
    img[-20:] = 0.02     # near-black band

    out = _apply_detail(img, {"clarity": 100})
    assert np.abs(out[:20] - img[:20]).mean() < 0.02
    assert np.abs(out[-20:] - img[-20:]).mean() < 0.02

    mid = slice(30, 60)
    assert np.abs(out[mid] - img[mid]).mean() > 0.002


# ---------------------------------------------------------------------------
# Texture
# ---------------------------------------------------------------------------

def test_positive_texture_increases_fine_detail(detailed):
    out = _apply_detail(detailed, {"texture": 80})
    assert local_contrast(out, 1.5) > local_contrast(detailed, 1.5)


def test_negative_texture_smooths_fine_detail(detailed):
    out = _apply_detail(detailed, {"texture": -80})
    assert local_contrast(out, 1.5) < local_contrast(detailed, 1.5)


def test_texture_works_on_a_finer_scale_than_clarity(detailed):
    """The two controls must not be duplicates of each other."""
    tex = _apply_detail(detailed, {"texture": 100})
    cla = _apply_detail(detailed, {"clarity": 100})
    assert not np.allclose(tex, cla, atol=1e-3)

    fine_gain = local_contrast(tex, 1.5) / local_contrast(detailed, 1.5)
    coarse_gain = local_contrast(cla, 12) / local_contrast(detailed, 12)
    assert fine_gain > 1.0 and coarse_gain > 1.0


# ---------------------------------------------------------------------------
# Dehaze
# ---------------------------------------------------------------------------

def test_dehaze_removes_a_lifted_black_point():
    """Haze lifts the darkest channel; removing it must restore contrast."""
    rng = np.random.default_rng(1)
    clean = rng.random((64, 64, 3), dtype=np.float32) * 0.8 + 0.05
    hazy = clean * 0.5 + 0.45            # veiled: compressed and lifted

    out = _apply_detail(hazy, {"dehaze": 80})
    assert float(_luminance(out).std()) > float(_luminance(hazy).std())
    assert float(out.min()) < float(hazy.min())


def test_negative_dehaze_adds_haze():
    rng = np.random.default_rng(2)
    img = (rng.random((64, 64, 3), dtype=np.float32) * 0.8 + 0.1).astype(np.float32)
    out = _apply_detail(img, {"dehaze": -80})
    assert float(_luminance(out).std()) < float(_luminance(img).std())


def test_dehaze_cannot_divide_by_zero():
    """The veil divisor is floored; a white frame must not blow up."""
    white = np.ones((32, 32, 3), dtype=np.float32)
    out = _apply_detail(white, {"dehaze": 100})
    assert np.isfinite(out).all()


# ---------------------------------------------------------------------------
# Sharpness
# ---------------------------------------------------------------------------

def test_sharpness_increases_edge_contrast(detailed):
    out = _apply_detail(detailed, {"sharpness": 120})
    assert local_contrast(out, 1.2) > local_contrast(detailed, 1.2)


def test_sharpness_zero_changes_nothing(detailed):
    np.testing.assert_array_equal(_apply_detail(detailed, {"sharpness": 0}), detailed)


# ---------------------------------------------------------------------------
# Integration with the rest of the pipeline
# ---------------------------------------------------------------------------

def test_detail_runs_inside_apply_adjustments(detailed):
    plain = apply_adjustments(detailed, BASELINE_TEMP, {})
    with_clarity = apply_adjustments(detailed, BASELINE_TEMP, {"clarity": 80})
    assert not np.allclose(plain, with_clarity, atol=1e-3)


def test_edit_strength_scales_detail_controls(detailed):
    """Detail must obey strength like every other adjustment."""
    full = apply_adjustments(detailed, BASELINE_TEMP, {"clarity": 100})
    half = apply_adjustments(detailed, BASELINE_TEMP, {"clarity": 100, "strength": 50})
    zero = apply_adjustments(detailed, BASELINE_TEMP, {"clarity": 100, "strength": 0})

    np.testing.assert_allclose(zero, detailed, atol=1e-5)
    d_half = float(np.abs(half - detailed).mean())
    d_full = float(np.abs(full - detailed).mean())
    assert 0 < d_half < d_full


def test_blur_radius_scales_with_image_size():
    """
    Radii are relative to the image, so the same slider value looks the
    same whether the preview is 800px or 1400px wide.
    """
    rng = np.random.default_rng(3)
    small = (rng.random((64, 64, 3), dtype=np.float32) * 0.5 + 0.25).astype(np.float32)
    large = np.asarray(
        np.kron(small, np.ones((2, 2, 1), dtype=np.float32)), dtype=np.float32
    )

    s = _apply_detail(small, {"clarity": 100})
    l = _apply_detail(large, {"clarity": 100})

    # Effect strength should be comparable, not scale-dependent.
    ds = float(np.abs(s - small).mean())
    dl = float(np.abs(l - large).mean())
    assert dl == pytest.approx(ds, rel=0.6)
