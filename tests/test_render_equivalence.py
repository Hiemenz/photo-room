"""
The fast render kernels must reproduce the original NumPy code.

``tests/legacy_pipeline.py`` is a frozen copy of the pre-optimisation stages;
every fast stage is compared with it on synthetic photos (and, when the CC0
sample raw is available, on a real one). Tolerance: 1/255 in float, and at most
one level after 8-bit quantisation.
"""
import os
from pathlib import Path

import numpy as np
import pytest

import colormix
import editor
import fastops
import render_kernels
import tonecurve
import legacy_pipeline as L

TOL = 1.0 / 255.0


def synthetic(h=640, w=900, seed=3):
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    img = np.stack([x / w, y / h, (x + y) / (w + h)], 2) * 0.85
    img += rng.normal(0, 0.05, (h, w, 3)).astype(np.float32)
    img[:40, :40] = 1.0          # clipped corner
    img[-40:, -40:] = 0.0        # crushed corner
    return np.clip(img, 0, 1).astype(np.float32)


CURVE = {"points": {"rgb": [[0, 0], [64, 50], [128, 140], [255, 255]], "red": [[0, 0], [255, 240]]}}
FULL = {"exposure": 0.4, "contrast": 20, "shadows": 25, "highlights": -30, "blacks": 10, "whites": -10,
        "tint": 12, "color_temp": 6800, "saturation": 12, "vibrance": 20, "clarity": 45, "texture": 30,
        "sharpness": 35, "dehaze": 25, "hue_red": 12, "sat_blue": -15, "lum_green": 20,
        "grade_shadow_sat": 25, "grade_shadow_hue": 230, "grade_global_sat": 8, "grade_global_lum": 5,
        "grade_mid_lum": -10, "grade_highlight_sat": 15, "grade_highlight_hue": 40, "tone_curve": CURVE}


def diff(a, b):
    return float(np.abs(np.asarray(a, np.float64) - np.asarray(b, np.float64)).max())


def q8(a):
    return (np.clip(a, 0, 1) * 255 + 0.5).astype(np.uint8).astype(int)


@pytest.fixture(params=[1, 4], ids=["1thread", "4threads"])
def threads(request):
    fastops.set_threads(request.param)
    yield request.param
    fastops.set_threads(None)


@pytest.fixture(scope="module")
def img():
    return synthetic()


def full_adj():
    return editor.effective_adjustments(dict(FULL))


def test_tone_stage(img, threads):
    adj = full_adj()
    assert diff(L.legacy_stage_tone(img, 5000, adj), editor._stage_tone(img, 5000, adj)) <= TOL


@pytest.mark.parametrize("key,val", [("exposure", 1.2), ("exposure", -2), ("color_temp", 9000), ("tint", -60),
                                     ("shadows", 100), ("highlights", -100), ("blacks", -80), ("whites", 90),
                                     ("contrast", -50), ("contrast", 100)])
def test_tone_stage_each_control(img, key, val):
    adj = editor.effective_adjustments({key: val})
    assert diff(L.legacy_stage_tone(img, 5200, adj), editor._stage_tone(img, 5200, adj)) <= TOL


def test_neutral_tone_is_identity(img):
    adj = editor.effective_adjustments({"color_temp": 5000})
    assert diff(editor._stage_tone(img, 5000, adj), img) == 0


def test_saturation_stage(img, threads):
    for sat, vib in ((30, 0), (-60, 0), (0, 50), (20, -40)):
        adj = {"saturation": sat, "vibrance": vib}
        assert diff(L.legacy_stage_saturation(img, adj), editor._stage_saturation(img, adj)) <= TOL


def test_hsl_and_grading(img, threads):
    adj = full_adj()
    assert diff(L.legacy_apply_hsl(img, adj), colormix.apply_hsl(img, adj)) <= TOL
    assert diff(L.legacy_apply_grading(img, adj), colormix.apply_grading(img, adj)) <= TOL


def test_hsl_with_cached_analysis_matches(img, threads):
    adj = full_adj()
    an = colormix.Analysis(img)
    assert diff(colormix.apply_hsl(img, adj, an), colormix.apply_hsl(img, adj)) == 0


@pytest.mark.parametrize("key", ["clarity", "texture", "dehaze", "sharpness"])
def test_each_detail_control(img, key, threads):
    adj = {key: 60}
    assert diff(L.legacy_apply_detail(img, adj), editor._apply_detail(img, adj)) <= TOL


def test_detail_all_together(img, threads):
    adj = full_adj()
    assert diff(L.legacy_apply_detail(img, adj), editor._apply_detail(img, adj)) <= TOL


def test_blur_matches_pillow_original_exactly(img, threads):
    plane = img[..., 1]
    for radius in (1.2, 3.0, 9.0, 27.0, 80.0):
        assert diff(L.legacy_blur(plane, radius), editor._blur(plane, radius)) <= 1e-6, radius


def test_tone_curve_lut_matches_np_interp(img):
    for tc in (CURVE, {"points": {"rgb": [[0, 30], [255, 220]]}}, {"parametric": {"highlights": 40, "shadows": -30}}):
        old = L.legacy_apply_tone_curve(img, tc)
        new = tonecurve.apply_tone_curve(img, tc)
        assert diff(old, new) <= 0.25 / 255
        assert new is not img and img.flags.writeable and diff(old, img) > 0.01   # a real curve


def test_whole_pipeline_8bit_within_one_level(img, threads):
    """Every stage chained, as apply_adjustments runs them, then quantised like the export."""
    adj = full_adj()

    def legacy():
        x = L.legacy_stage_tone(img, 5000, adj)
        x = L.legacy_stage_saturation(x, adj)
        x = L.legacy_apply_hsl(x, adj)
        x = L.legacy_apply_grading(x, adj)
        return L.legacy_apply_detail(x, adj)

    new = editor.apply_adjustments(img, 5000, adj)
    assert diff(legacy(), new) <= TOL
    assert np.abs(q8(legacy()) - render_kernels.float_to_u8(new).astype(int)).max() <= 1


def test_float_u8_round_trip():
    a = np.random.default_rng(0).random((500, 700, 3), dtype=np.float32)
    assert np.array_equal(render_kernels.float_to_u8(a), (a * 255.0 + 0.5).astype(np.uint8))
    u = (a * 255).astype(np.uint8)
    assert diff(render_kernels.u8_to_float(u), u.astype(np.float32) / 255.0) <= 1e-6


def test_stage_cache_keys_unchanged():
    """Keys decide which cached stage image may be reused; speed work must not touch them."""
    import stagecache
    adj = full_adj()
    keys = stagecache.stage_keys(adj, 5000)
    adj2 = dict(adj, sharpness=1)
    keys2 = stagecache.stage_keys(adj2, 5000)
    assert keys[:stagecache.DETAIL] == keys2[:stagecache.DETAIL]       # upstream stays reusable
    assert keys[stagecache.DETAIL] != keys2[stagecache.DETAIL]
    adj3 = dict(adj, exposure=0.5)
    assert all(a != b for a, b in zip(keys[stagecache.TONE:], stagecache.stage_keys(adj3, 5000)[stagecache.TONE:]))


# --- real photograph -------------------------------------------------------

SAMPLE = Path(os.environ.get("PHOTO_ROOM_RAW_SAMPLE",
                             Path.home() / ".cache" / "photo-room-test-raw" / "IMG_9406.CR2"))


@pytest.mark.realraw
def test_real_raw_matches_legacy_at_preview_and_full_resolution():
    rawpy = pytest.importorskip("rawpy")
    if not SAMPLE.exists():
        pytest.skip("real raw sample not downloaded")
    with rawpy.imread(str(SAMPLE)) as raw:
        rgb = raw.postprocess(use_camera_wb=True, no_auto_bright=True, output_bps=8)
    adj = full_adj()
    full = render_kernels.u8_to_float(rgb)
    crop = np.ascontiguousarray(full[600:1500, 800:2300])          # full-res pixels, 1.35 MP
    for label, base in (("full-res crop", crop),):
        x = L.legacy_stage_tone(base, 5000, adj)
        x = L.legacy_stage_saturation(x, adj)
        x = L.legacy_apply_hsl(x, adj)
        x = L.legacy_apply_grading(x, adj)
        x = L.legacy_apply_detail(x, adj)
        new = editor.apply_adjustments(base, 5000, adj)
        assert diff(x, new) <= TOL, label
        assert np.abs(q8(x) - render_kernels.float_to_u8(new).astype(int)).max() <= 1, label
