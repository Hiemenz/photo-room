"""Auto tone, auto white balance, the Kelvin mapping, histogram and clipping."""

import os
import time
from pathlib import Path

import numpy as np
import pytest

import analysis
import editor


def scene(h=300, w=450, seed=0, lo=0.15, hi=0.85):
    """A balanced multi-colour scene (random hue patches plus greys), 0..1 float."""
    rng = np.random.default_rng(seed)
    cells = rng.uniform(lo, hi, (h // 15, w // 15, 3)).astype(np.float32)
    img = np.kron(cells, np.ones((15, 15, 1), np.float32))
    img[:, : w // 6] = rng.uniform(lo, hi, (h, 1, 1))     # a block of neutrals
    img[:, : w // 6] = img[:, : w // 6, :1]
    return img


def tone_stats(img, settings):
    out = editor._stage_saturation(editor._stage_tone(img, 5500, settings), settings)
    lum = editor._luminance(out)
    return {"median": float(np.median(lum)),
            "hi": float((lum >= 0.985).mean()), "lo": float((lum <= 0.012).mean()),
            "span": float(np.percentile(lum, 95) - np.percentile(lum, 5))}


# ---------------------------------------------------------------------------
# Kelvin mapping
# ---------------------------------------------------------------------------

REAL_RAW = Path(os.environ.get("PHOTO_ROOM_SAMPLE_RAW",
                               Path.home() / ".cache/photo-room-test-raw/IMG_9406.CR2"))


@pytest.mark.skipif(not REAL_RAW.exists(), reason="real CC0 CR2 sample not cached")
def test_real_raw_daylight_reads_daylight_kelvin():
    rawpy = pytest.importorskip("rawpy")
    with rawpy.imread(str(REAL_RAW)) as raw:
        mul = np.array(raw.camera_whitebalance[:4], dtype=float)
        as_shot = analysis.baseline_kelvin(raw, mul / mul[1])
        # LibRaw's own daylight multipliers are D65: the mapping must say ~6500 K.
        day = analysis.kelvin_from_camera(raw.daylight_whitebalance, raw.rgb_xyz_matrix)
    assert 6300 <= day <= 6700
    assert 5000 <= as_shot <= 6500          # was 3091 K before the fix


def test_kelvin_grows_with_red_over_blue_without_a_matrix():
    # A warm (tungsten) light needs little red gain; a blue (shade) one a lot.
    tungsten = analysis.kelvin_from_camera([1.1, 1.0, 2.3, 1.0])
    daylight = analysis.kelvin_from_camera([2.0, 1.0, 1.0, 1.0])
    shade = analysis.kelvin_from_camera([2.6, 1.0, 0.8, 1.0])
    assert tungsten < daylight < shade
    assert 2500 <= tungsten and shade <= 10000


def test_kelvin_with_daylight_multipliers_reference():
    k = analysis.kelvin_from_camera([2.4, 1.0, 1.2, 1.0], None, [2.4, 1.0, 1.2, 1.0])
    assert k == 6500


def test_decode_for_analysis_uses_the_fixed_mapping(fake_rawpy, tmp_path):
    import process

    f = tmp_path / "a.cr2"
    f.write_bytes(b"x")
    fake_rawpy["whitebalance"] = [2.0, 1.0, 1.0, 1.0]       # R/B = 2: daylight
    _, temp, _ = process.decode_for_analysis(f)
    assert temp == 6500
    fake_rawpy["whitebalance"] = [1.1, 1.0, 2.3, 1.0]       # tungsten
    assert process.decode_for_analysis(f)[1] < 3500


# ---------------------------------------------------------------------------
# White balance maths
# ---------------------------------------------------------------------------

def apply_wb(img, temp, tint, baseline):
    return editor._stage_tone(img, baseline, {"color_temp": temp, "tint": tint})


@pytest.mark.parametrize("cast", [(1.25, 1.0, 0.8), (0.8, 1.0, 1.2), (1.0, 1.15, 1.0), (1.1, 0.9, 1.05)])
def test_wb_solve_neutralises_a_pixel_in_the_editors_model(cast):
    pixel = np.array(cast, np.float32) * 0.5
    temp, tint = analysis.wb_solve(pixel, 5000)
    out = apply_wb(pixel.reshape(1, 1, 3), temp, tint, 5000)[0, 0]
    assert np.ptp(out) < 0.012, (cast, temp, tint, out)


def test_wb_solve_direction_matches_lightroom():
    # An orange (warm) pixel needs a cooler, i.e. lower, Temp.
    warm, _ = analysis.wb_solve([0.7, 0.5, 0.3], 5000)
    cool, _ = analysis.wb_solve([0.3, 0.5, 0.7], 5000)
    assert warm < 5000 < cool


def test_wb_solve_neutral_pixel_is_a_no_op():
    assert analysis.wb_solve([0.4, 0.4, 0.4], 5200) == (5200, 0)


def test_wb_solve_clamps_to_slider_range():
    temp, _ = analysis.wb_solve([1.0, 0.5, 0.02], 5000)
    assert analysis.TEMP_MIN <= temp <= analysis.TEMP_MAX


def test_presets():
    presets = {p["id"]: p for p in analysis.wb_presets(5800, (5400, 4))}
    assert list(presets) == ["as_shot", "auto", "daylight", "cloudy", "shade",
                             "tungsten", "fluorescent", "flash"]
    assert presets["as_shot"]["color_temp"] == 5800
    assert presets["auto"]["color_temp"] == 5400 and presets["auto"]["tint"] == 4
    assert (presets["tungsten"]["color_temp"], presets["tungsten"]["tint"]) == (2850, 0)
    assert presets["cloudy"]["color_temp"] > presets["daylight"]["color_temp"]
    assert presets["fluorescent"]["tint"] > 0
    assert "auto" not in {p["id"] for p in analysis.wb_presets(5800)}


def cast_image(img, cast):
    """Multiply by a colour cast in LINEAR light (a coloured illuminant), return encoded."""
    import colour

    lin = colour.to_linear(img) * np.array(cast, np.float32)
    return np.clip(colour.to_encoded(np.clip(lin, 0, 1)), 0, 1)


def residual(img, temp, tint, baseline):
    import colour

    out = colour.to_linear(apply_wb(img, temp, tint, baseline))
    m = out.reshape(-1, 3).mean(axis=0)
    return abs(m[0] / m[2] - 1), abs(m[1] / ((m[0] + m[2]) / 2) - 1)


@pytest.mark.parametrize("cast", [(1.3, 1.0, 0.75), (0.8, 1.0, 1.25), (1.0, 1.12, 1.0), (1.2, 0.95, 0.85)])
def test_auto_wb_removes_a_colour_cast(cast):
    img = cast_image(scene(lo=0.1, hi=0.7), cast)
    res = analysis.auto_white_balance(img, 5000)
    rb, rg = residual(img, res["color_temp"], res["tint"], 5000)
    before = residual(img, 5000, 0, 5000)
    assert rb < 0.12 and rg < 0.06, (res, rb, rg)
    assert rb < before[0] * 0.35 or before[0] < 0.05


def test_auto_wb_leaves_a_neutral_scene_alone():
    res = analysis.auto_white_balance(scene(lo=0.1, hi=0.7), 5200)
    assert abs(res["color_temp"] - 5200) < 300 and abs(res["tint"]) < 8


def test_auto_wb_is_not_fooled_by_skin():
    """Half the frame is a face; the rest is a neutral scene under a cool cast."""
    cast = (0.85, 1.0, 1.2)
    img = scene(seed=3, lo=0.1, hi=0.7)
    skin = np.array([0.80, 0.56, 0.44], np.float32)
    img[:, 225:] = skin * np.random.default_rng(1).uniform(0.9, 1.1, (300, 225, 1))
    img = cast_image(img, cast)
    res = analysis.auto_white_balance(img, 5000)
    # Treating the face as a neutral would warm the frame up by far more.
    naive_temp, _ = analysis.wb_solve(img.reshape(-1, 3).mean(axis=0), 5000)
    assert res["color_temp"] > naive_temp + 200
    assert res["color_temp"] > 5000          # the cool cast is corrected the right way


def test_auto_wb_on_a_blank_frame_returns_as_shot():
    res = analysis.auto_white_balance(np.zeros((50, 50, 3), np.float32), 5300)
    assert (res["color_temp"], res["tint"]) == (5300, 0)


# ---------------------------------------------------------------------------
# Auto tone
# ---------------------------------------------------------------------------

def gradient(h=300, w=450, lo=0.0, hi=1.0, seed=0):
    rng = np.random.default_rng(seed)
    ramp = np.linspace(lo, hi, w, dtype=np.float32)[None, :, None]
    return np.repeat(ramp, h, 0).repeat(3, 2) * rng.uniform(0.97, 1.03, (h, w, 3)).astype(np.float32)


CASES = {
    "dark": lambda: np.clip(scene(lo=0.01, hi=0.16), 0, 1),
    "bright": lambda: np.clip(scene(lo=0.75, hi=1.0), 0, 1),
    "flat": lambda: np.clip(scene(lo=0.42, hi=0.58), 0, 1),
    "high_contrast": lambda: np.where(np.random.default_rng(2).random((300, 450, 1)) < 0.5, 0.01, 0.99)
                             .repeat(3, 2).astype(np.float32),
    "normal": lambda: scene(),
}


@pytest.mark.parametrize("name", list(CASES))
def test_auto_tone_avoids_clipping_and_centres_the_midtone(name):
    img = CASES[name]()
    result = analysis.auto_tone(img)
    after = tone_stats(img, result["settings"])
    before = tone_stats(img, {})
    if name != "high_contrast":
        assert 0.33 <= after["median"] <= 0.6, (name, after, result["settings"])
    assert after["hi"] <= max(0.006, before["hi"]), (name, after)
    assert after["lo"] <= max(0.006, before["lo"]), (name, after)


def test_auto_tone_directions():
    dark = analysis.auto_tone(CASES["dark"]())["settings"]
    bright = analysis.auto_tone(CASES["bright"]())["settings"]
    flat = analysis.auto_tone(CASES["flat"]())["settings"]
    hc = analysis.auto_tone(CASES["high_contrast"]())["settings"]
    assert dark["exposure"] > 1.0
    assert bright["exposure"] < -0.3
    assert flat["contrast"] > 15                       # a flat frame gets more contrast
    assert hc["contrast"] <= 0                         # a harsh one does not
    # Valid slider ranges, and the right shape for the UI.
    for s in (dark, bright, flat, hc):
        assert set(s) == {"exposure", "contrast", "highlights", "shadows", "whites", "blacks", "vibrance"}
        assert -5 <= s["exposure"] <= 5
        assert all(-100 <= v <= 100 for k, v in s.items() if k != "exposure")


def test_auto_tone_improves_flat_spread_and_recovers_blown_highlights():
    flat = CASES["flat"]()
    s = analysis.auto_tone(flat)["settings"]
    assert tone_stats(flat, s)["span"] > tone_stats(flat, {})["span"] * 1.2

    sky = scene(lo=0.2, hi=0.7)
    sky[:80] = 1.0                                     # a blown sky
    sky[:80, :, :] = np.clip(sky[:80] * 1.0, 0, 1)
    res = analysis.auto_tone(sky)
    assert res["settings"]["highlights"] < 0 or res["settings"]["exposure"] < 0


def test_auto_tone_is_deterministic():
    img = CASES["normal"]()
    assert analysis.auto_tone(img)["settings"] == analysis.auto_tone(img)["settings"]


def test_auto_tone_greyscale_gets_no_vibrance():
    g = np.repeat(gradient(lo=0.1, hi=0.9)[:, :, :1], 3, axis=2)
    assert analysis.auto_tone(g)["settings"]["vibrance"] == 0


def test_auto_tone_dull_colour_gets_vibrance():
    dull = 0.5 + (scene(seed=4) - 0.5) * 0.08
    assert analysis.auto_tone(dull)["settings"]["vibrance"] > 10


def test_auto_tone_is_fast_on_a_preview():
    img = np.random.default_rng(5).random((933, 1400, 3), dtype=np.float32) * 0.4
    analysis.auto_tone(img)                            # warm up imports
    t0 = time.perf_counter()
    analysis.auto_tone(img)
    assert (time.perf_counter() - t0) < 0.1


# ---------------------------------------------------------------------------
# Histogram and clipping
# ---------------------------------------------------------------------------

def test_histogram_matches_numpy_and_counts_every_pixel():
    img = np.random.default_rng(6).random((120, 200, 3), dtype=np.float32)
    h = analysis.histogram(img, stride=1)
    u8 = analysis.to_uint8(img)
    for i, c in enumerate("rgb"):
        assert h[c] == np.bincount(u8[:, :, i].ravel(), minlength=256).tolist()
    assert sum(h["lum"]) == 120 * 200 and len(h["g"]) == 256


def test_histogram_fewer_bins_and_stride():
    img = np.random.default_rng(7).random((100, 100, 3), dtype=np.float32)
    h64 = analysis.histogram(img, bins=64, stride=1)
    assert len(h64["r"]) == 64 and sum(h64["r"]) == 10000
    assert sum(analysis.histogram(img, stride=2)["b"]) == 2500


def test_histogram_is_under_10ms_on_a_preview():
    img = np.random.default_rng(8).random((933, 1400, 3), dtype=np.float32)
    analysis.histogram(img)
    best = min(_time(lambda: analysis.histogram(img)) for _ in range(5))
    assert best < 0.010, best


def _time(fn):
    t0 = time.perf_counter()
    fn()
    return time.perf_counter() - t0


def test_clipping_masks_and_percentages():
    img = np.full((100, 100, 3), 0.5, np.float32)
    img[:10, :] = 1.0                       # 10 % blown on all channels
    img[10:15, :, 0] = 1.0                  # 5 % blown in red only
    img[90:, :50] = 0.0                     # 5 % crushed
    res = analysis.clipping(img)
    p = res["percent"]
    assert p["highlights"] == 15.0 and p["shadows"] == 5.0
    assert p["highlights_channels"] == {"r": 15.0, "g": 10.0, "b": 10.0}
    assert p["shadows_channels"]["r"] == 5.0
    assert res["masks"]["highlights"][:15].all() and not res["masks"]["highlights"][15:].any()


def test_clipping_threshold_is_configurable():
    img = np.full((10, 10, 3), 0.97, np.float32)
    assert analysis.clipping(img)["percent"]["highlights"] == 0.0
    assert analysis.clipping(img, highlight=0.95)["percent"]["highlights"] == 100.0
    assert analysis.clipping(img, highlight=240)["percent"]["highlights"] == 100.0   # 0..255 levels
    with pytest.raises(ValueError):
        analysis.clipping(img, shadow=0.9, highlight=0.5)


def test_clipping_overlay_colours():
    img = np.full((4, 4, 3), 0.5, np.float32)
    img[0, 0] = 1.0
    img[3, 3] = 0.0
    rgba = analysis.clipping_rgba(analysis.clipping(img)["masks"])
    assert tuple(rgba[0, 0]) == (*analysis.HIGHLIGHT_COLOR, 255)      # red
    assert tuple(rgba[3, 3]) == (*analysis.SHADOW_COLOR, 255)         # blue
    assert rgba[1, 1, 3] == 0                                         # transparent elsewhere
    assert rgba[0, 0, 0] > rgba[0, 0, 2] and rgba[3, 3, 2] > rgba[3, 3, 0]
