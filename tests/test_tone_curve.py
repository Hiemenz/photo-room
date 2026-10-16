"""Tone curve: interpolation, XMP round trip, rendering, preview endpoint, presets."""

from pathlib import Path

import numpy as np
import pytest

import editor
import style
import tonecurve
from editor import apply_adjustments, read_saved_adjustments, save_adjustments
from test_editor_api import client, library  # noqa: F401


def _curve(**points):
    tc = tonecurve.default_curve()
    tc["points"].update(points)
    return tc


# --- interpolation ---------------------------------------------------------

def test_linear_curve_is_identity():
    xs = np.linspace(0, 1, 257)
    assert np.allclose(tonecurve.interpolate(tonecurve.LINEAR, xs), xs, atol=1e-9)
    img = np.random.default_rng(0).random((8, 8, 3))
    assert tonecurve.apply_tone_curve(img, tonecurve.default_curve()) is img
    assert tonecurve.is_identity(None)


def test_curve_passes_through_points_and_endpoints_fixed():
    pts = [[0, 0], [64, 40], [192, 220], [255, 255]]
    xs = np.array([0.0, 64 / 255, 192 / 255, 1.0])
    out = tonecurve.interpolate(pts, xs)
    assert np.allclose(out * 255, [0, 40, 220, 255], atol=1e-6)
    lut = tonecurve.build_luts(_curve(rgb=pts))
    assert np.allclose(lut[:, 0], 0) and np.allclose(lut[:, -1], 1)


@pytest.mark.parametrize("pts", [
    [[0, 0], [10, 200], [20, 210], [255, 255]],        # steep then flat: overshoot bait
    [[0, 0], [128, 128], [129, 250], [255, 255]],
    [[0, 0], [100, 30], [160, 230], [255, 255]],
    [[0, 0], [50, 100], [200, 100], [255, 255]],        # plateau
])
def test_interpolation_is_monotone_without_overshoot(pts):
    out = tonecurve.interpolate(pts, np.linspace(0, 1, 2001))
    assert np.all(np.diff(out) >= -1e-12)
    assert out.min() >= 0 and out.max() <= 1


def test_non_monotone_input_is_repaired():
    pts = tonecurve.normalise_points([[0, 0], [100, 200], [150, 50], [255, 255]])
    ys = [p[1] for p in pts]
    assert ys == sorted(ys)


def test_parametric_moves_its_region_and_keeps_ends():
    tc = tonecurve.normalise({"parametric": {"shadows": 100}})
    lut = tonecurve.build_luts(tc)[0]
    grid = np.linspace(0, 1, tonecurve.LUT_SIZE)
    assert lut[0] == 0 and lut[-1] == 1
    assert lut[int(0.12 * (tonecurve.LUT_SIZE - 1))] > grid[int(0.12 * (tonecurve.LUT_SIZE - 1))] + 0.05
    i90 = int(0.9 * (tonecurve.LUT_SIZE - 1))
    assert abs(lut[i90] - grid[i90]) < 1e-6
    assert np.all(np.diff(lut) >= 0)


# --- rendering -------------------------------------------------------------

def test_render_applies_curve():
    base = np.full((6, 6, 3), 0.5, dtype=np.float32)
    plain = apply_adjustments(base, 5500, {"color_temp": 5500})
    up = apply_adjustments(base, 5500, {"color_temp": 5500, "tone_curve": _curve(rgb=[[0, 0], [128, 200], [255, 255]])})
    assert plain.mean() == pytest.approx(0.5, abs=0.01)
    assert up.mean() > 0.7
    red = apply_adjustments(base, 5500, {"color_temp": 5500, "tone_curve": _curve(red=[[0, 0], [128, 200], [255, 255]])})
    assert red[0, 0, 0] > 0.7 and red[0, 0, 1] == pytest.approx(0.5, abs=0.01)
    assert base.min() == 0.5   # input untouched


def test_garbage_curve_does_not_crash():
    base = np.full((4, 4, 3), 0.4, dtype=np.float32)
    out = apply_adjustments(base, 5500, {"color_temp": 5500, "tone_curve": {"points": {"rgb": "x"}, "parametric": {"shadows": "a"}}})
    assert out.shape == base.shape


# --- XMP -------------------------------------------------------------------

def test_xmp_round_trip(fake_exiftool):
    tc = _curve(rgb=[[0, 0], [64, 50], [192, 210], [255, 255]], blue=[[0, 10], [255, 255]])
    tc["parametric"].update(shadows=15, darks=-10, lights=20, highlights=-30, split1=20)
    save_adjustments(Path("x.dng"), {"exposure": 0.5, "tone_curve": tc})
    written = " ".join(fake_exiftool.write_call())
    assert "ToneCurvePV2012=0, 0|64, 50|192, 210|255, 255" in written
    assert "ParametricShadows=15" in written

    fake_exiftool.stdout_lines = None
    saved = read_saved_adjustments(Path("x.dng"))["tone_curve"]
    assert saved["points"]["rgb"] == [[0, 0], [64, 50], [192, 210], [255, 255]]
    assert saved["points"]["blue"][0] == [0, 10]
    assert saved["points"]["red"] == [[0, 0], [255, 255]]
    assert saved["parametric"]["shadows"] == 15 and saved["parametric"]["highlights"] == -30
    assert saved["parametric"]["split1"] == 20


def test_neutral_curve_clears_tags(fake_exiftool):
    save_adjustments(Path("x.dng"), {"exposure": 0.5, "tone_curve": tonecurve.default_curve()})
    assert "-XMP-crs:ToneCurvePV2012=" in fake_exiftool.write_call()
    fake_exiftool.stdout_lines = None
    assert "tone_curve" not in read_saved_adjustments(Path("x.dng"))


def test_strength_scales_the_saved_curve(fake_exiftool):
    tc = _curve(rgb=[[0, 0], [128, 228], [255, 255]])
    out = editor.effective_adjustments({"strength": 50, "tone_curve": tc})
    assert out["tone_curve"]["points"]["rgb"][1][1] == pytest.approx(178)


# --- API / presets ---------------------------------------------------------

def test_preview_endpoint_accepts_curve(client, library, fake_rawpy):
    flat = client.post("/api/preview", json={"path": "IMG_0001.dng", "adjustments": {}})
    curved = client.post("/api/preview", json={
        "path": "IMG_0001.dng",
        "adjustments": {"tone_curve": _curve(rgb=[[0, 0], [128, 230], [255, 255]])},
    })
    assert flat.status_code == 200 and curved.status_code == 200
    assert flat.get_json()["preview"] != curved.get_json()["preview"]


def test_preset_keeps_curve():
    tc = _curve(rgb=tonecurve.PRESETS["Strong Contrast"])
    style.save_preset("punchy", {"exposure": 0.2, "tone_curve": tc})
    assert style.get_preset("punchy")["tone_curve"]["points"]["rgb"] == tc["points"]["rgb"]


def test_batch_apply_writes_curve(client, library, fake_exiftool):
    r = client.post("/api/batch_apply", json={
        "paths": ["IMG_0001.dng"], "overwrite_foreign": True,
        "adjustments": {"tone_curve": _curve(rgb=[[0, 0], [128, 200], [255, 255]])},
    })
    assert r.status_code == 200
    assert "ToneCurvePV2012=0, 0|128, 200|255, 255" in " ".join(fake_exiftool.write_call())
