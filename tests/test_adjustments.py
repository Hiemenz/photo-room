"""Tests for the preview adjustment pipeline.

These assert *direction and invariants* rather than exact pixel values: the
preview is an approximation of Camera Raw, so pinning exact numbers would
make the suite brittle without proving anything useful.
"""

import numpy as np
import pytest

from editor import CONTROLS, _luminance, apply_adjustments


BASELINE_TEMP = 5500


@pytest.fixture
def mid():
    """A flat mid-grey image — no clipping headroom problems in either direction."""
    return np.full((16, 16, 3), 0.5, dtype=np.float32)


@pytest.fixture
def ramp():
    """A luminance ramp from black to white, for tonal-range targeting."""
    values = np.linspace(0.0, 1.0, 32, dtype=np.float32)
    return np.repeat(values[:, None, None], 32, axis=1).repeat(3, axis=2)


def mean_lum(img):
    return float(_luminance(img).mean())


# ---------------------------------------------------------------------------
# Invariants that must hold for every control
# ---------------------------------------------------------------------------

def test_no_adjustments_is_identity(ramp):
    """An empty adjustment dict must leave the image untouched."""
    out = apply_adjustments(ramp, BASELINE_TEMP, {})
    np.testing.assert_allclose(out, ramp, atol=1e-6)


def test_defaults_are_identity(ramp):
    """Every slider at its default must also be a no-op."""
    defaults = {c["key"]: c["default"] for c in CONTROLS}
    defaults["color_temp"] = BASELINE_TEMP  # default temp is per-camera
    out = apply_adjustments(ramp, BASELINE_TEMP, defaults)
    np.testing.assert_allclose(out, ramp, atol=1e-6)


@pytest.mark.parametrize("control", CONTROLS, ids=lambda c: c["key"])
@pytest.mark.parametrize("bound", ["min", "max"])
def test_extremes_stay_in_range(ramp, control, bound):
    """No slider at either extreme may produce out-of-range or non-finite pixels."""
    out = apply_adjustments(ramp, BASELINE_TEMP, {control["key"]: control[bound]})
    assert np.isfinite(out).all(), f"{control['key']}={control[bound]} produced NaN/inf"
    assert out.min() >= 0.0 and out.max() <= 1.0


def test_all_controls_at_once_stays_in_range(ramp):
    """Combined extremes must not compound into out-of-range values."""
    for bound in ("min", "max"):
        adj = {c["key"]: c[bound] for c in CONTROLS}
        out = apply_adjustments(ramp, BASELINE_TEMP, adj)
        assert np.isfinite(out).all()
        assert out.min() >= 0.0 and out.max() <= 1.0


def test_saturation_boost_cannot_push_channels_out_of_range():
    """
    Color work happens after the tonal clamp, so it needs a clamp of its own:
    a saturated pixel amplified further will otherwise exceed 1.0.
    """
    img = np.zeros((8, 8, 3), dtype=np.float32)
    img[:, :, 0], img[:, :, 1], img[:, :, 2] = 0.95, 0.5, 0.05
    out = apply_adjustments(img, BASELINE_TEMP, {"saturation": 100, "vibrance": 100})
    assert out.max() <= 1.0, "channel exceeded white — final clamp missing"
    assert out.min() >= 0.0, "channel went below black — final clamp missing"


def test_input_is_not_mutated(ramp):
    """apply_adjustments must not modify the cached base render in place."""
    original = ramp.copy()
    apply_adjustments(ramp, BASELINE_TEMP, {"exposure": 2.0, "contrast": 80})
    np.testing.assert_array_equal(ramp, original)


def test_output_shape_and_dtype_preserved(ramp):
    out = apply_adjustments(ramp, BASELINE_TEMP, {"exposure": 1.0})
    assert out.shape == ramp.shape
    assert out.dtype == np.float32


# ---------------------------------------------------------------------------
# Direction of each control
# ---------------------------------------------------------------------------

def test_positive_exposure_brightens(mid):
    assert mean_lum(apply_adjustments(mid, BASELINE_TEMP, {"exposure": 1.0})) > 0.5


def test_negative_exposure_darkens(mid):
    assert mean_lum(apply_adjustments(mid, BASELINE_TEMP, {"exposure": -1.0})) < 0.5


def test_exposure_one_stop_doubles_linear_light():
    """+1 EV doubles scene-linear light (tests/test_linear_light.py checks the numbers in depth)."""
    import colour
    img = np.full((8, 8, 3), 0.25, dtype=np.float32)
    out = apply_adjustments(img, BASELINE_TEMP, {"exposure": 1.0})
    np.testing.assert_allclose(colour.to_linear(out), 2 * colour.to_linear(img), rtol=1e-4)


def test_exposure_is_monotonic(mid):
    """Brightness must increase monotonically across the exposure range."""
    lums = [
        mean_lum(apply_adjustments(mid, BASELINE_TEMP, {"exposure": ev}))
        for ev in (-2.0, -1.0, 0.0, 1.0)
    ]
    assert lums == sorted(lums)
    assert len(set(lums)) == len(lums), "exposure steps produced no visible change"


def test_contrast_pushes_away_from_midpoint(ramp):
    """Positive contrast darkens shadows and brightens highlights."""
    out = apply_adjustments(ramp, BASELINE_TEMP, {"contrast": 50})
    lum_in, lum_out = _luminance(ramp), _luminance(out)
    dark, bright = lum_in < 0.4, lum_in > 0.6
    assert lum_out[dark].mean() < lum_in[dark].mean()
    assert lum_out[bright].mean() > lum_in[bright].mean()


def test_negative_contrast_compresses_toward_midpoint(ramp):
    out = apply_adjustments(ramp, BASELINE_TEMP, {"contrast": -50})
    spread_in = float(_luminance(ramp).std())
    spread_out = float(_luminance(out).std())
    assert spread_out < spread_in


def test_shadows_lift_darks_and_spare_brights(ramp):
    """The shadow control must target the low end, not the whole frame."""
    out = apply_adjustments(ramp, BASELINE_TEMP, {"shadows": 80})
    lum_in, lum_out = _luminance(ramp), _luminance(out)
    darks = lum_in < 0.2
    brights = lum_in > 0.9
    assert lum_out[darks].mean() > lum_in[darks].mean() + 0.01
    assert lum_out[brights].mean() == pytest.approx(lum_in[brights].mean(), abs=0.02)


def test_highlights_recover_brights_and_spare_darks(ramp):
    """Negative highlights must pull down the top end only."""
    out = apply_adjustments(ramp, BASELINE_TEMP, {"highlights": -80})
    lum_in, lum_out = _luminance(ramp), _luminance(out)
    brights = lum_in > 0.9
    darks = lum_in < 0.1
    assert lum_out[brights].mean() < lum_in[brights].mean() - 0.01
    assert lum_out[darks].mean() == pytest.approx(lum_in[darks].mean(), abs=0.02)


def test_blacks_target_deeper_range_than_shadows(ramp):
    """Blacks should act on a narrower, darker band than Shadows."""
    lum_in = _luminance(ramp)
    shadow_out = _luminance(apply_adjustments(ramp, BASELINE_TEMP, {"shadows": 100}))
    blacks_out = _luminance(apply_adjustments(ramp, BASELINE_TEMP, {"blacks": 100}))
    midtones = (lum_in > 0.35) & (lum_in < 0.5)
    shadow_shift = (shadow_out - lum_in)[midtones].mean()
    blacks_shift = (blacks_out - lum_in)[midtones].mean()
    assert shadow_shift > blacks_shift

def test_whites_target_brighter_range_than_highlights(ramp):
    lum_in = _luminance(ramp)
    high_out = _luminance(apply_adjustments(ramp, BASELINE_TEMP, {"highlights": 100}))
    whites_out = _luminance(apply_adjustments(ramp, BASELINE_TEMP, {"whites": 100}))
    midtones = (lum_in > 0.5) & (lum_in < 0.65)
    assert (high_out - lum_in)[midtones].mean() > (whites_out - lum_in)[midtones].mean()


# ---------------------------------------------------------------------------
# White balance
# ---------------------------------------------------------------------------

def test_raising_temp_warms_image(mid):
    """Above the camera baseline, red should rise and blue should fall."""
    out = apply_adjustments(mid, BASELINE_TEMP, {"color_temp": 9000})
    assert out[:, :, 0].mean() > 0.5
    assert out[:, :, 2].mean() < 0.5


def test_lowering_temp_cools_image(mid):
    out = apply_adjustments(mid, BASELINE_TEMP, {"color_temp": 3000})
    assert out[:, :, 0].mean() < 0.5
    assert out[:, :, 2].mean() > 0.5


def test_temp_at_baseline_is_neutral(mid):
    """Matching the camera's own temp must not shift color."""
    out = apply_adjustments(mid, BASELINE_TEMP, {"color_temp": BASELINE_TEMP})
    np.testing.assert_allclose(out, mid, atol=1e-6)


def test_temp_is_relative_to_camera_baseline(mid):
    """The same Kelvin value means different things for different cameras."""
    warm_cam = apply_adjustments(mid, 3000, {"color_temp": 6000})
    cool_cam = apply_adjustments(mid, 9000, {"color_temp": 6000})
    assert warm_cam[:, :, 0].mean() > cool_cam[:, :, 0].mean()


def test_positive_tint_shifts_magenta(mid):
    """Positive tint reduces green, which reads as magenta."""
    out = apply_adjustments(mid, BASELINE_TEMP, {"tint": 100})
    assert out[:, :, 1].mean() < 0.5


def test_negative_tint_shifts_green(mid):
    out = apply_adjustments(mid, BASELINE_TEMP, {"tint": -100})
    assert out[:, :, 1].mean() > 0.5


# ---------------------------------------------------------------------------
# Color
# ---------------------------------------------------------------------------

def test_saturation_increases_channel_spread():
    img = np.zeros((8, 8, 3), dtype=np.float32)
    img[:, :, 0], img[:, :, 1], img[:, :, 2] = 0.6, 0.5, 0.4
    out = apply_adjustments(img, BASELINE_TEMP, {"saturation": 80})
    assert out[:, :, 0].mean() - out[:, :, 2].mean() > 0.2 - 1e-6


def test_full_desaturation_is_monochrome():
    """-100 saturation must collapse all channels to a single grey value."""
    img = np.zeros((8, 8, 3), dtype=np.float32)
    img[:, :, 0], img[:, :, 1], img[:, :, 2] = 0.8, 0.4, 0.2
    out = apply_adjustments(img, BASELINE_TEMP, {"saturation": -100})
    np.testing.assert_allclose(out[:, :, 0], out[:, :, 1], atol=1e-5)
    np.testing.assert_allclose(out[:, :, 1], out[:, :, 2], atol=1e-5)


def test_saturation_leaves_neutral_grey_untouched(mid):
    """Grey has no color to saturate; it must survive any saturation value."""
    for value in (-100, -50, 50, 100):
        out = apply_adjustments(mid, BASELINE_TEMP, {"saturation": value})
        np.testing.assert_allclose(out, mid, atol=1e-6)


def test_vibrance_favors_muted_colors_over_saturated_ones():
    """This is what separates Vibrance from Saturation — it must hold."""
    muted = np.zeros((8, 8, 3), dtype=np.float32)
    muted[:, :, 0], muted[:, :, 1], muted[:, :, 2] = 0.52, 0.5, 0.48

    # Kept well clear of 0.0/1.0 so the final clamp can't mask the difference
    # in gain and make a uniform-vibrance bug look correct.
    vivid = np.zeros((8, 8, 3), dtype=np.float32)
    vivid[:, :, 0], vivid[:, :, 1], vivid[:, :, 2] = 0.8, 0.5, 0.2

    def gain(img):
        out = apply_adjustments(img, BASELINE_TEMP, {"vibrance": 60})
        assert out.max() < 1.0 and out.min() > 0.0, "test image clipped; result is unreliable"
        before = float(img[:, :, 0].mean() - img[:, :, 2].mean())
        after = float(out[:, :, 0].mean() - out[:, :, 2].mean())
        return after / before

    muted_gain, vivid_gain = gain(muted), gain(vivid)
    assert muted_gain > vivid_gain * 1.05, (
        f"vibrance treated muted ({muted_gain:.3f}) and saturated ({vivid_gain:.3f}) "
        "colors alike — that is plain saturation, not vibrance"
    )
