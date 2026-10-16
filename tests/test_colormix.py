"""Tests for the HSL / B&W mixer and colour grading (M3)."""
import numpy as np
import pytest

import colormix
import editor
from colormix import (BANDS, apply_gray_mixer, apply_grading, apply_hsl, band_weights,
                      grade_masks, hsv_to_rgb, rgb_to_hsv)
from editor import apply_adjustments, save_adjustments, read_saved_adjustments
from pathlib import Path

BASELINE = 5500


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    (tmp_path / "IMG_0001.dng").write_bytes(b"fake-dng")
    return tmp_path


@pytest.fixture
def client(library):
    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        yield c


@pytest.fixture
def colourful():
    rng = np.random.default_rng(3)
    return rng.random((24, 32, 3)).astype(np.float32)


def test_band_weights_are_a_partition_of_unity():
    h = np.linspace(0, 360, 3601, endpoint=False, dtype=np.float32)
    w = band_weights(h)
    assert w.shape == (3600 + 1, len(BANDS))
    assert np.allclose(w.sum(axis=-1), 1.0, atol=1e-5)
    assert (w >= -1e-6).all()


def test_band_weights_peak_at_centres_and_only_two_overlap():
    for i, c in enumerate(colormix.BAND_CENTRES):
        w = band_weights(np.float32(c))
        assert w[i] == pytest.approx(1.0, abs=1e-5)
    w = band_weights(np.linspace(0, 360, 721, dtype=np.float32)[:-1])
    assert ((w > 1e-6).sum(axis=-1) <= 2).all()


def test_band_weights_are_smooth_and_wrap():
    h = np.linspace(0, 360, 36001, endpoint=False, dtype=np.float32)
    w = band_weights(h)
    step = np.abs(np.diff(np.vstack([w, w[:1]]), axis=0))
    assert step.max() < 0.01          # no jumps anywhere, including 360 -> 0
    # first derivative continuous: second difference stays small too
    second = np.abs(np.diff(w, n=2, axis=0))
    assert second.max() < 1e-3


def test_band_mix_matches_full_weights():
    h = np.random.default_rng(1).random(500).astype(np.float32) * 360
    v = np.arange(8, dtype=np.float32) * 7 - 20
    assert np.allclose(band_weights(h) @ v, colormix.band_mix(h, v), atol=1e-4)


def test_hsv_round_trip(colourful):
    h, s, v = rgb_to_hsv(colourful)
    assert np.allclose(hsv_to_rgb(h, s, v), colourful, atol=1e-5)


def test_zero_settings_are_an_exact_noop(colourful):
    assert apply_hsl(colourful, {}) is colourful
    assert apply_hsl(colourful, {f"hue_{b}": 0 for b in BANDS}) is colourful
    assert apply_gray_mixer(colourful, {}) is colourful
    assert apply_grading(colourful, {"grade_blending": 80, "grade_balance": 40}) is colourful
    plain = apply_adjustments(colourful, BASELINE, {})
    zeroed = apply_adjustments(colourful, BASELINE, dict(colormix.COLOR_DEFAULTS))
    assert np.array_equal(plain, zeroed)


def test_saturation_band_only_touches_its_hue():
    red = np.full((4, 4, 3), [0.8, 0.2, 0.2], np.float32)
    blue = np.full((4, 4, 3), [0.2, 0.2, 0.8], np.float32)
    assert apply_hsl(red, {"sat_red": -100}).std(axis=-1).max() < red.std(axis=-1).max()
    assert np.allclose(apply_hsl(blue, {"sat_red": -100}), blue, atol=1e-5)


def test_hue_band_rotates_hue():
    red = np.full((2, 2, 3), [0.8, 0.2, 0.2], np.float32)
    out = apply_hsl(red, {"hue_red": 100})
    h_in, h_out = rgb_to_hsv(red)[0][0, 0], rgb_to_hsv(out)[0][0, 0]
    assert h_out == pytest.approx(h_in + colormix.MAX_HUE_SHIFT_DEG, abs=0.5)


def test_luminance_band_brightens_and_darkens():
    green = np.full((2, 2, 3), [0.2, 0.6, 0.2], np.float32)
    assert apply_hsl(green, {"lum_green": 60}).mean() > green.mean()
    assert apply_hsl(green, {"lum_green": -60}).mean() < green.mean()


def test_grey_pixels_are_untouched_by_the_mixer():
    grey = np.full((3, 3, 3), 0.5, np.float32)
    assert np.allclose(apply_hsl(grey, {f"sat_{b}": 100 for b in BANDS} | {"hue_red": 100}), grey)


def test_bw_mixer_changes_grey_brightness_by_band():
    red = np.full((2, 2, 3), [0.8, 0.2, 0.2], np.float32)
    mono = {"monochrome": True}
    base = apply_adjustments(red, BASELINE, mono)
    lighter = apply_adjustments(red, BASELINE, {**mono, "gray_red": 80})
    darker = apply_adjustments(red, BASELINE, {**mono, "gray_red": -80})
    assert lighter.mean() > base.mean() > darker.mean()
    assert np.allclose(lighter[..., 0], lighter[..., 1])      # still grey
    # the colour-mode HSL sliders do not leak into B&W
    assert np.array_equal(base, apply_adjustments(red, BASELINE, {**mono, "lum_red": 80}))


def test_grade_masks_sum_to_one_and_balance_moves_pivot():
    luma = np.linspace(0, 1, 501, dtype=np.float32)
    for blend in (0, 50, 100):
        for bal in (-100, 0, 100):
            s, m, h = grade_masks(luma, blend, bal)
            assert np.allclose(s + m + h, 1.0, atol=1e-6)
            assert min(s.min(), m.min(), h.min()) >= -1e-6
    _, _, h_lo = grade_masks(luma, 50, -100)
    _, _, h_hi = grade_masks(luma, 50, 100)
    assert h_hi.sum() > h_lo.sum()      # positive balance favours highlights


def test_grade_masks_are_smooth():
    luma = np.linspace(0, 1, 5001, dtype=np.float32)
    for m in grade_masks(luma, 20, 30):
        assert np.abs(np.diff(m)).max() < 0.01


@pytest.mark.parametrize("rng", ["shadow", "mid", "highlight", "global"])
def test_each_wheel_tints_its_own_tones(rng):
    ramp = np.repeat(np.linspace(0, 1, 64, dtype=np.float32)[None, :, None], 3, axis=2).repeat(2, axis=0)
    out = apply_grading(ramp, {f"grade_{rng}_hue": 0, f"grade_{rng}_sat": 100})
    tint = out[..., 0] - out[..., 2]            # red hue -> red over blue
    if rng == "shadow":
        assert tint[0, 8] > 0.01 and abs(tint[0, 60]) < 1e-6
    elif rng == "highlight":
        assert tint[0, 56] > 0.01 and abs(tint[0, 4]) < 1e-6
    elif rng == "mid":
        assert tint[0, 32] > 0.01 and abs(tint[0, 0]) < 1e-6 and abs(tint[0, 63]) < 1e-6
    else:
        assert (tint[0, 5:] > 0).all()


def test_wheel_luminance_lifts_its_range():
    ramp = np.repeat(np.linspace(0.05, 0.95, 64, dtype=np.float32)[None, :, None], 3, axis=2)
    out = apply_grading(ramp, {"grade_shadow_lum": 100})
    assert out[0, 2, 0] > ramp[0, 2, 0] and out[0, 60, 0] == pytest.approx(ramp[0, 60, 0])


def test_grading_applies_in_monochrome_as_split_toning():
    grey = np.full((2, 2, 3), 0.3, np.float32)
    out = apply_adjustments(grey, BASELINE, {"monochrome": True, "grade_global_hue": 30, "grade_global_sat": 80})
    assert not np.allclose(out[..., 0], out[..., 2])


def test_every_control_changes_the_render(colourful):
    plain = apply_adjustments(colourful, BASELINE, {})
    for c in colormix.COLOR_CONTROLS:
        key = c["key"]
        if key in ("grade_blending", "grade_balance") or key.endswith("_hue") and key.startswith("grade_"):
            continue
        adj = {key: 60}
        if key.startswith("gray_"):
            adj["monochrome"] = True
            plain_ref = apply_adjustments(colourful, BASELINE, {"monochrome": True})
        else:
            plain_ref = plain
        assert not np.array_equal(apply_adjustments(colourful, BASELINE, adj), plain_ref), key
    # hue/blending/balance only matter alongside saturation
    base = {"grade_shadow_sat": 80, "grade_mid_sat": 80, "grade_highlight_sat": 80}
    ref = apply_adjustments(colourful, BASELINE, base)
    for extra in ({"grade_shadow_hue": 200}, {"grade_blending": 5}, {"grade_balance": 70}):
        assert not np.array_equal(apply_adjustments(colourful, BASELINE, {**base, **extra}), ref), extra


def test_edit_strength_scales_color_settings(colourful):
    full = apply_adjustments(colourful, BASELINE, {"sat_red": 100, "strength": 100})
    zero = apply_adjustments(colourful, BASELINE, {"sat_red": 100, "strength": 0})
    assert np.allclose(zero, apply_adjustments(colourful, BASELINE, {}))
    assert not np.allclose(full, zero)
    eff = editor.effective_adjustments({"sat_red": 80, "grade_shadow_hue": 200, "grade_blending": 20, "strength": 50})
    assert eff["sat_red"] == pytest.approx(40) and eff["grade_shadow_hue"] == 200 and eff["grade_blending"] == 20


# --- XMP ---------------------------------------------------------------

def test_xmp_tag_names():
    t = colormix.XMP_TAGS
    assert t["hue_red"] == "HueAdjustmentRed" and t["sat_aqua"] == "SaturationAdjustmentAqua"
    assert t["lum_magenta"] == "LuminanceAdjustmentMagenta" and t["gray_orange"] == "GrayMixerOrange"
    assert t["grade_shadow_hue"] == "SplitToningShadowHue" and t["grade_mid_sat"] == "ColorGradeMidtoneSat"
    assert t["grade_highlight_lum"] == "ColorGradeHighlightLum" and t["grade_global_hue"] == "ColorGradeGlobalHue"
    assert t["grade_blending"] == "ColorGradeBlending" and t["grade_balance"] == "SplitToningBalance"
    assert len(set(t.values())) == len(t) == 8 * 4 + 12 + 2


def test_xmp_round_trip(fake_exiftool):
    settings = {"hue_red": 12, "sat_blue": -40, "lum_green": 25, "gray_yellow": -30,
                "grade_shadow_hue": 210, "grade_shadow_sat": 35, "grade_mid_lum": -10,
                "grade_highlight_hue": 40, "grade_global_sat": 15,
                "grade_blending": 70, "grade_balance": -25}
    save_adjustments(Path("x.dng"), {"exposure": 0.5, **settings})
    args = " ".join(fake_exiftool.write_call())
    assert "HueAdjustmentRed=12" in args and "ColorGradeBlending=70" in args
    fake_exiftool.stdout_lines = None
    saved = read_saved_adjustments(Path("x.dng"))
    for k, v in settings.items():
        assert saved[k] == pytest.approx(v), k
    assert "hue_orange" not in saved          # defaults are not reported


def test_zero_settings_round_trip_adds_no_keys(fake_exiftool):
    save_adjustments(Path("x.dng"), {"exposure": 0.5})
    fake_exiftool.stdout_lines = None
    saved = read_saved_adjustments(Path("x.dng"))
    assert not set(saved) & set(colormix.COLOR_DEFAULTS)


# --- API / presets -------------------------------------------------------

def test_preview_accepts_color_settings(client, library, fake_rawpy, fake_exiftool):
    r = client.post("/api/preview", json={"path": "IMG_0001.dng", "adjustments": {
        "sat_red": 50, "grade_shadow_sat": 60, "grade_shadow_hue": 220, "gray_blue": 20, "monochrome": True}})
    assert r.status_code == 200 and r.get_json()["preview"].startswith("data:image/jpeg")


def test_preset_carries_color_settings():
    import style
    adj = {"sat_red": 30, "grade_global_hue": 45, "grade_global_sat": 20, "color_temp": 5500}
    style.save_preset("warm", adj)
    got = style.get_preset("warm")
    assert got["sat_red"] == 30 and got["grade_global_hue"] == 45
