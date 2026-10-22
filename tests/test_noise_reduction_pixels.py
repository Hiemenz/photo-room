"""Noise reduction (luminance and colour) must change pixels in the preview, tile and export paths."""

import cv2
import numpy as np
import pytest

import editor
import presetlib
import render_kernels
import stagecache


def noisy(seed=0, h=96, w=128, sigma=0.06, chroma=True):
    rng = np.random.default_rng(seed)
    clean = np.full((h, w, 3), 0.4, dtype=np.float32)
    clean[:, w // 2:] = 0.7                      # a hard edge to preserve
    n = rng.normal(0, sigma, (h, w, 3)).astype(np.float32)
    if not chroma:
        n[:] = n[:, :, :1]
    return clean, np.clip(clean + n, 0, 1).astype(np.float32)


def spread(a, ref):
    return float(np.std(a - ref))


def test_zero_is_identity():
    _, img = noisy()
    assert render_kernels.denoise_stage(img, {"luminance_nr": 0, "color_nr": 0}) is img


def test_luminance_nr_reduces_luma_noise_and_keeps_the_edge():
    clean, img = noisy(chroma=False)
    out = editor.apply_adjustments(img, 5500, {"luminance_nr": 80})
    base = editor.apply_adjustments(img, 5500, {"luminance_nr": 0})
    assert not np.array_equal(out, base)
    assert spread(out, clean) < spread(base, clean) * 0.8
    step = out[:, 70:74].mean() - out[:, 54:58].mean()
    assert step > 0.2                            # the 0.3 edge survives


def test_stronger_slider_smooths_more():
    clean, img = noisy(chroma=False)
    a = render_kernels.denoise_stage(img, {"luminance_nr": 20})
    b = render_kernels.denoise_stage(img, {"luminance_nr": 90})
    assert spread(b, clean) < spread(a, clean) < spread(img, clean)


def test_colour_nr_removes_chroma_noise_but_not_luminance_detail():
    _, img = noisy()
    out = render_kernels.denoise_stage(img, {"color_nr": 80})
    c_in = cv2.cvtColor(img, cv2.COLOR_RGB2YCrCb)
    c_out = cv2.cvtColor(out, cv2.COLOR_RGB2YCrCb)
    assert np.std(c_out[:, :, 1:]) < np.std(c_in[:, :, 1:]) * 0.6
    assert np.std(c_out[:, :, 0] - c_in[:, :, 0]) < 0.02    # luminance essentially untouched


def test_colour_nr_does_not_bleed_across_an_edge():
    _, img = noisy(sigma=0.0)
    img[:, 64:, 0] += 0.2                         # reddish half
    img = np.clip(img, 0, 1)
    out = render_kernels.denoise_stage(img, {"color_nr": 100})
    assert abs(out[:, 66:70].mean() - img[:, 66:70].mean()) < 0.03


def test_tile_edge_override_is_honoured():
    clean, img = noisy(chroma=False)
    out = render_kernels.denoise_stage(img, {"luminance_nr": 60, "_detail_edge": 6000})
    assert spread(out, clean) < spread(img, clean)


def test_sliders_do_not_hit_a_stale_cache():
    _, img = noisy()
    a = editor.apply_adjustments(img, 5500, {"luminance_nr": 0}, cache_id="p")
    b = editor.apply_adjustments(img, 5500, {"luminance_nr": 70}, cache_id="p")
    c = editor.apply_adjustments(img, 5500, {"color_nr": 70}, cache_id="p")
    assert not np.array_equal(a, b) and not np.array_equal(a, c)
    assert stagecache.IGNORED == frozenset()
    k0 = stagecache.stage_keys({"luminance_nr": 0}, 5500)
    k1 = stagecache.stage_keys({"luminance_nr": 10}, 5500)
    assert k0[stagecache.DETAIL] != k1[stagecache.DETAIL]
    assert k0[stagecache.SATURATION] == k1[stagecache.SATURATION]   # colour stages stay cached


def test_export_path_applies_noise_reduction():
    """The exporter calls editor.apply_adjustments with linear input; it must denoise too."""
    import colour
    _, img = noisy(chroma=False)
    lin = colour.to_linear(img, None)
    out = editor.apply_adjustments(lin, 5500, {"_linear_input": True, "luminance_nr": 80, "color_nr": 50})
    off = editor.apply_adjustments(lin, 5500, {"_linear_input": True})
    assert not np.array_equal(out, off)


def test_control_and_xmp_round_trip():
    assert any(c["key"] == "color_nr" for c in editor.CONTROLS)
    args = editor._xmp_arguments({"luminance_nr": 33, "color_nr": 44})
    assert "-XMP-crs:LuminanceSmoothing=33" in args
    assert "-XMP-crs:ColorNoiseReduction=44" in args
    assert presetlib._XMP_SIMPLE["ColorNoiseReduction"] == "color_nr"


def test_edit_strength_does_not_scale_colour_nr():
    out = editor.effective_adjustments({"color_nr": 40, "exposure": 2.0, "strength": 50})
    assert out["color_nr"] == pytest.approx(40)


def test_colour_nr_is_read_back(fake_exiftool):
    from test_editor_api import ALL_SAVED
    from pathlib import Path
    fake_exiftool.stdout_lines = ALL_SAVED + ["-", "-", "-", "37"]
    out = editor._read_basic_adjustments_from(Path("x.dng"))
    assert out["color_nr"] == pytest.approx(37)
