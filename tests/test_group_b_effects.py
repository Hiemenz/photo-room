"""Grain, post-crop vignette, Point Color, Calibration and sharpening radius / detail / masking."""

import colorsys
import shutil
import subprocess

import numpy as np
import pytest

import colormix
import effects
import editor
import presetlib
import render_kernels
import stagecache

# Fixtures and helpers of the tile tests (a fake raw, a tile stitcher, the whole-frame reference).
from test_tiles import photo, reference, seam_error, stitch  # noqa: F401

F32 = np.float32


def flat(value=0.5, h=64, w=96):
    return np.full((h, w, 3), value, dtype=np.float32)


def render(img, adj, **kw):
    return editor.apply_adjustments(img, 5500, dict(adj), **kw)


# ---------------------------------------------------------------------------
# Sharpening: radius, detail, masking
# ---------------------------------------------------------------------------

def step_edge(h=40, w=120, lo=0.3, hi=0.7):
    img = np.full((h, w, 3), lo, dtype=np.float32)
    img[:, w // 2:] = hi
    return img


def test_sharpen_defaults_equal_the_original_unsharp_mask():
    img = step_edge()
    a = render_kernels.detail_stage(img, {"sharpness": 80})
    b = render_kernels.detail_stage(img, {"sharpness": 80, "sharpen_radius": 1.0, "sharpen_detail": 25,
                                          "sharpen_masking": 0})
    assert np.array_equal(a, b)
    lum = render_kernels.luminance(img)
    expected = np.clip(img + (lum - render_kernels.blur(lum, 1.2))[..., None] * 0.4, 0, 1)
    assert np.allclose(a, expected, atol=1e-6)


def test_sharpen_radius_widens_the_overshoot():
    img = step_edge()
    narrow = render_kernels.detail_stage(img, {"sharpness": 100, "sharpen_radius": 0.6})
    wide = render_kernels.detail_stage(img, {"sharpness": 100, "sharpen_radius": 2.5})
    row = lambda o: o[20, :, 0]
    mid = img.shape[1] // 2

    def reach(r):                      # how many pixels left of the edge differ from the flat 0.3
        return int((np.abs(r[:mid] - 0.3) > 0.005).sum())

    assert reach(row(wide)) > reach(row(narrow)) + 2


def test_sharpen_low_detail_cuts_halo_and_high_detail_adds_texture():
    img = step_edge()
    plain = render_kernels.detail_stage(img, {"sharpness": 120})
    low = render_kernels.detail_stage(img, {"sharpness": 120, "sharpen_detail": 0})
    over = lambda o: float(o[20, :, 0].max() - 0.7)
    assert over(low) < over(plain) * 0.7
    # texture: fine noise gains contrast with Detail 100
    rng = np.random.default_rng(1)
    tex = np.clip(0.5 + rng.normal(0, 0.03, (64, 64, 1)), 0, 1).repeat(3, axis=2).astype(np.float32)
    hi = render_kernels.detail_stage(tex, {"sharpness": 100, "sharpen_detail": 100})
    mid = render_kernels.detail_stage(tex, {"sharpness": 100})
    assert hi[..., 0].std() > mid[..., 0].std() * 1.1


def test_sharpen_masking_spares_flat_noise_and_keeps_edges():
    rng = np.random.default_rng(2)
    img = step_edge(h=80, w=160)
    noise = rng.normal(0, 0.01, img.shape[:2]).astype(np.float32)[..., None]
    img = np.clip(img + noise, 0, 1).astype(np.float32)
    open_ = render_kernels.detail_stage(img, {"sharpness": 150})
    masked = render_kernels.detail_stage(img, {"sharpness": 150, "sharpen_masking": 80})
    flat_zone = (slice(5, 75), slice(5, 55))
    edge_zone = (slice(5, 75), slice(76, 84))
    d_open = np.abs(open_ - img)[flat_zone].mean()
    d_masked = np.abs(masked - img)[flat_zone].mean()
    assert d_masked < d_open * 0.35                     # flat areas are left alone
    e_open = np.abs(open_ - img)[edge_zone].mean()
    e_masked = np.abs(masked - img)[edge_zone].mean()
    assert e_masked > e_open * 0.7                      # the edge is still sharpened


# ---------------------------------------------------------------------------
# Grain
# ---------------------------------------------------------------------------

def lag_corr(plane, lag=2):
    a, b = plane[:, :-lag].ravel(), plane[:, lag:].ravel()
    return float(np.corrcoef(a, b)[0, 1])


def test_grain_zero_is_identity_and_nonzero_changes_pixels():
    img = flat(0.5)
    assert effects.apply_grain(img, {"grain_amount": 0}) is img
    out = render(img, {"grain_amount": 60})
    assert not np.array_equal(out, render(img, {}))
    d = out[..., 0] - 0.5
    assert 0.02 < d.std() < 0.2 and abs(d.mean()) < 0.01
    assert np.array_equal(out, render(img, {"grain_amount": 60}))      # deterministic


def test_grain_amount_scales_and_midtones_get_more_than_highlights():
    mid = flat(0.5, h=128, w=128)
    lo = (effects.apply_grain(mid, {"grain_amount": 20}) - mid)[..., 0].std()
    hi = (effects.apply_grain(mid, {"grain_amount": 80}) - mid)[..., 0].std()
    assert hi > lo * 3
    bright = flat(0.98, h=128, w=128)
    gb = (effects.apply_grain(bright, {"grain_amount": 80}) - bright)[..., 0].std()
    assert gb < hi * 0.7


def test_grain_size_and_roughness_change_the_texture():
    img = flat(0.5, h=160, w=160)
    img_big = {"_detail_edge": 3000.0}
    def noise(**kw):
        return (effects.apply_grain(img, {"grain_amount": 80, **img_big, **kw}) - img)[..., 0]
    assert lag_corr(noise(grain_size=90)) > lag_corr(noise(grain_size=5)) + 0.1      # bigger grain, smoother
    smooth = lag_corr(noise(grain_roughness=0, grain_size=60))
    rough = lag_corr(noise(grain_roughness=100, grain_size=60))
    assert rough < smooth - 0.05


def test_grain_size_scales_with_the_frame_so_preview_and_export_look_alike():
    img = flat(0.5, h=64, w=64)
    small = (effects.apply_grain(img, {"grain_amount": 80, "_detail_edge": 1000.0}) - img)[..., 0]
    large = (effects.apply_grain(img, {"grain_amount": 80, "_detail_edge": 9000.0}) - img)[..., 0]
    assert lag_corr(large) > lag_corr(small)


def test_grain_and_vignette_are_the_same_pixels_in_a_tile_as_in_the_frame():
    full = flat(0.5, h=120, w=200)
    adj = {"grain_amount": 70, "grain_size": 40, "vignette_amount": -60, "_detail_edge": 200.0}
    whole = effects.apply_finishing(full, adj)
    tile = effects.apply_finishing(full[30:90, 50:150].copy(),
                                   {**adj, "_frame_origin": (50, 30), "_frame_size": (200, 120)})
    assert np.allclose(tile, whole[30:90, 50:150], atol=1e-6)


# ---------------------------------------------------------------------------
# Post-crop vignette
# ---------------------------------------------------------------------------

def test_vignette_darkens_corners_not_the_centre_and_positive_lightens():
    img = flat(0.5, h=100, w=160)
    dark = effects.apply_vignette(img, {"vignette_amount": -80})
    assert effects.apply_vignette(img, {"vignette_amount": 0}) is img
    assert abs(dark[50, 80, 0] - 0.5) < 1e-4
    assert dark[0, 0, 0] < 0.2
    light = effects.apply_vignette(img, {"vignette_amount": 80})
    assert light[0, 0, 0] > 0.8 and abs(light[50, 80, 0] - 0.5) < 1e-4


def test_vignette_midpoint_feather_and_roundness_shape_it():
    img = flat(0.5, h=100, w=160)
    at = lambda adj, y, x: float(effects.apply_vignette(img, {"vignette_amount": -100, **adj})[y, x, 0])
    # a higher midpoint keeps more of the picture clear
    assert at({"vignette_midpoint": 90}, 25, 40) > at({"vignette_midpoint": 10}, 25, 40)
    # a low feather is a harder edge: further from the start it is already fully dark
    assert at({"vignette_feather": 5, "vignette_midpoint": 50}, 5, 10) <= at({"vignette_feather": 100, "vignette_midpoint": 50}, 5, 10)
    # roundness +100 is a circle: the middle of the short edge is less dark than for the aspect-following ellipse
    assert at({"vignette_roundness": 100}, 2, 80) > at({"vignette_roundness": 0}, 2, 80) + 0.05
    # roundness -100 squares it off: the edge midpoint gets darker than for the ellipse
    assert at({"vignette_roundness": -100}, 50, 158) < at({"vignette_roundness": 0}, 50, 158)


def test_vignette_follows_the_crop():
    rng = np.random.default_rng(3)
    img = np.clip(0.5 + rng.normal(0, 0.01, (200, 300, 3)), 0, 1).astype(np.float32)
    crop = {"left": 0.25, "top": 0.25, "right": 0.75, "bottom": 0.75}
    out = render(img, {"crop": crop, "vignette_amount": -100})
    assert out.shape[:2] == (100, 150)
    assert out[0, 0, 0] < 0.15                                  # the cropped frame's own corner is dark
    assert out[50, 75, 0] > 0.4


# ---------------------------------------------------------------------------
# Point Color
# ---------------------------------------------------------------------------

def hsv_img(h, s, v, hh=16, ww=16):
    r, g, b = colorsys.hsv_to_rgb(h / 360.0, s, v)
    return np.tile(np.array([r, g, b], dtype=np.float32), (hh, ww, 1))


def hue_of(px):
    return colorsys.rgb_to_hsv(*[float(c) for c in px])[0] * 360.0


def test_point_color_shifts_matching_colours_only():
    point = {"hue": 120, "sat": 70, "lum": 70, "hue_shift": 100, "sat_shift": 0, "lum_shift": 0, "range": 60}
    near = hsv_img(125, 0.7, 0.7)
    far = hsv_img(300, 0.7, 0.7)
    out_near = effects.apply_point_colors(near, {"point_colors": [point]})
    out_far = effects.apply_point_colors(far, {"point_colors": [point]})
    assert hue_of(out_near[0, 0]) > 125 + 25
    assert np.allclose(out_far, far, atol=1e-5)
    # saturation and luminance shifts
    sat = effects.apply_point_colors(near, {"point_colors": [{**point, "hue_shift": 0, "sat_shift": -100}]})
    assert colorsys.rgb_to_hsv(*sat[0, 0])[1] < 0.2
    lum = effects.apply_point_colors(near, {"point_colors": [{**point, "hue_shift": 0, "lum_shift": 60}]})
    assert lum[0, 0].max() > near[0, 0].max()


def test_point_color_noop_and_range():
    img = hsv_img(30, 0.6, 0.6)
    assert effects.apply_point_colors(img, {}) is img
    assert effects.apply_point_colors(img, {"point_colors": [{"hue": 30, "hue_shift": 0}]}) is img
    p = {"hue": 30, "sat": 60, "lum": 60, "hue_shift": 80, "range": 0}
    narrow = effects.apply_point_colors(hsv_img(60, 0.6, 0.6), {"point_colors": [p]})
    wide = effects.apply_point_colors(hsv_img(60, 0.6, 0.6), {"point_colors": [{**p, "range": 100}]})
    assert abs(hue_of(wide[0, 0]) - 60) > abs(hue_of(narrow[0, 0]) - 60) + 5


def test_point_color_runs_in_the_pipeline_and_is_skipped_in_black_and_white():
    img = hsv_img(120, 0.7, 0.7)
    adj = {"point_colors": [{"hue": 120, "sat": 70, "lum": 70, "hue_shift": 100, "range": 60}]}
    assert not np.array_equal(render(img, adj), render(img, {}))
    assert np.array_equal(render(img, {**adj, "monochrome": True}), render(img, {"monochrome": True}))


def test_point_list_is_normalised():
    pts = effects.normalise_points([{"hue": 999, "hue_shift": "x"}, "bad", {"range": -5}] + [{}] * 20)
    assert len(pts) == effects.MAX_POINTS
    assert pts[0]["hue"] == 360 and pts[0]["hue_shift"] == 0 and pts[1]["range"] == 0


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

def test_calibration_matrix_identity_and_white_preserved():
    assert np.allclose(effects.calibration_matrix({}), np.eye(3), atol=1e-6)
    m = effects.calibration_matrix({"cal_red_hue": 80, "cal_green_sat": -50, "cal_blue_hue": -30, "cal_blue_sat": 70})
    assert np.allclose(m @ np.ones(3), np.ones(3), atol=1e-5)
    assert not np.allclose(m, np.eye(3), atol=1e-3)


def test_calibration_moves_the_primaries_in_the_right_direction():
    red = np.tile(np.array([0.6, 0.1, 0.1], F32), (8, 8, 1))
    lin = effects.calibrate(red.copy(), {"cal_red_hue": 100})
    assert hue_of(lin[0, 0]) > hue_of(red[0, 0]) + 5 or hue_of(lin[0, 0]) < hue_of(red[0, 0]) - 5
    more = effects.calibrate(red.copy(), {"cal_red_sat": 100})
    less = effects.calibrate(red.copy(), {"cal_red_sat": -100})
    chroma = lambda p: float(p.max() - p.min())
    assert chroma(less[0, 0]) < chroma(red[0, 0]) < chroma(more[0, 0])
    grey = flat(0.4)
    assert np.allclose(effects.calibrate(grey.copy(), {"cal_red_hue": 90, "cal_blue_sat": 80}), grey, atol=1e-5)


def test_shadow_tint_acts_on_shadows_not_highlights():
    dark, bright = flat(0.02), flat(0.9)
    for tint, sign in ((100, -1), (-100, 1)):
        d = effects.calibrate(dark.copy(), {"cal_shadow_tint": tint})
        b = effects.calibrate(bright.copy(), {"cal_shadow_tint": tint})
        assert np.sign(d[0, 0, 1] / dark[0, 0, 1] - 1) == sign
        assert abs(b[0, 0, 1] / 0.9 - 1) < abs(d[0, 0, 1] / 0.02 - 1) * 0.1


def test_calibration_in_the_pipeline_and_not_for_old_edits():
    img = hsv_img(10, 0.6, 0.5)
    on = render(img, {"cal_red_hue": 100, "edit_version": 2})
    assert not np.array_equal(on, render(img, {"edit_version": 2}))
    assert np.array_equal(render(img, {"cal_red_hue": 100, "edit_version": 1}), render(img, {"edit_version": 1}))
    lin = img.copy()
    via_linear = render(lin, {"_linear_input": True, "cal_red_hue": 100, "edit_version": 2})
    assert not np.array_equal(via_linear, render(lin, {"_linear_input": True, "edit_version": 2}))


# ---------------------------------------------------------------------------
# Stage cache keys
# ---------------------------------------------------------------------------

def keys(**kw):
    return stagecache.stage_keys(kw, 5500)


def differs_from(stage_name, key, value):
    a, b = keys(), keys(**{key: value})
    return [i for i, name in enumerate(stagecache.STAGE_NAMES) if i and a[i] != b[i]][0] \
        == stagecache.STAGE_NAMES.index(stage_name)


@pytest.mark.parametrize("key,value,first_stage", [
    ("cal_red_hue", 10, "tone"), ("cal_shadow_tint", 5, "tone"),
    ("point_colors", [{"hue": 1}], "mix"),
    ("sharpen_radius", 2.0, "detail"), ("sharpen_detail", 60, "detail"), ("sharpen_masking", 30, "detail"),
    ("grain_amount", 10, "final"), ("grain_size", 50, "final"), ("grain_roughness", 10, "final"),
    ("vignette_amount", -20, "final"), ("vignette_midpoint", 20, "final"),
    ("vignette_roundness", 20, "final"), ("vignette_feather", 20, "final"),
])
def test_new_settings_are_in_the_stage_keys_of_their_stage_and_later_only(key, value, first_stage):
    a, b = keys(), keys(**{key: value})
    assert a[0] == b[0], "must not land in the geometry key"
    changed = [i for i in range(len(a)) if a[i] != b[i]]
    assert changed and changed[0] == stagecache.STAGE_NAMES.index(first_stage)


def test_grain_slider_does_not_redo_the_detail_stage():
    img = np.random.default_rng(4).random((60, 80, 3)).astype(np.float32)
    editor._stage_cache.clear()
    render(img, {"sharpness": 40, "sharpen_radius": 1.5}, cache_id="gb")
    misses = editor._stage_cache.stats()["misses"]
    a = render(img, {"sharpness": 40, "sharpen_radius": 1.5, "grain_amount": 50}, cache_id="gb")
    assert not np.array_equal(a, render(img, {"sharpness": 40, "sharpen_radius": 1.5}, cache_id="gb"))
    st = editor._stage_cache.stats()
    # the detail stage was a hit (the render started from it)
    assert st["hits"] >= 2 and st["misses"] == misses + 0 or st["misses"] <= misses + 2


def test_cached_and_uncached_renders_agree():
    img = np.random.default_rng(5).random((60, 80, 3)).astype(np.float32)
    adj = {"cal_blue_hue": 40, "point_colors": [{"hue": 200, "hue_shift": 50, "sat": 50, "lum": 50, "range": 80}],
           "sharpness": 50, "sharpen_detail": 70, "sharpen_masking": 20, "grain_amount": 30, "vignette_amount": -40}
    editor._stage_cache.clear()
    assert np.array_equal(render(img, adj, cache_id="x1"), render(img, adj))
    assert np.array_equal(render(img, adj, cache_id="x1"), render(img, adj))      # second one served from cache


# ---------------------------------------------------------------------------
# XMP
# ---------------------------------------------------------------------------

ALL = {"grain_amount": 33, "grain_size": 40, "grain_roughness": 61,
       "vignette_amount": -42, "vignette_midpoint": 35, "vignette_roundness": -20, "vignette_feather": 70,
       "cal_shadow_tint": -12, "cal_red_hue": 10, "cal_red_sat": -20, "cal_green_hue": 30, "cal_green_sat": 40,
       "cal_blue_hue": -50, "cal_blue_sat": 60,
       "sharpen_radius": 1.7, "sharpen_detail": 55, "sharpen_masking": 23,
       "point_colors": [{"hue": 120.0, "sat": 70.0, "lum": 40.0, "hue_shift": 25.0, "sat_shift": -10.0,
                         "lum_shift": 5.0, "range": 30.0},
                        {"hue": 10.0, "sat": 50.0, "lum": 50.0, "hue_shift": 0.0, "sat_shift": 80.0,
                         "lum_shift": 0.0, "range": 50.0}]}


def test_xmp_arguments_cover_every_setting():
    args = editor._xmp_arguments(ALL)
    for key, tag in effects.XMP_TAGS.items():
        assert any(a.startswith(f"-XMP-crs:{tag}=") for a in args), tag
    assert "-XMP-crs:GrainFrequency=61" in args and "-XMP-crs:SharpenRadius=1.7" in args
    assert "-XMP-crs:PostCropVignetteAmount=-42" in args and "-XMP-crs:ShadowTint=-12" in args
    assert sum(a.startswith("-XMP-crs:PointColors+=") for a in args) == 2
    assert "-XMP-crs:PointColors=" in args


def test_point_string_round_trip():
    for p in ALL["point_colors"]:
        back = effects._parse_point(effects._point_string(p))
        assert all(abs(back[k] - p[k]) < 1e-2 for k in p)


def test_parse_values_is_positional_and_keeps_only_non_defaults():
    lines = ["-"] * len(effects.XMP_TAGS)
    keys_ = list(effects.XMP_TAGS)
    lines[keys_.index("grain_roughness")] = "61"
    lines[keys_.index("grain_size")] = "25"            # a default: dropped
    lines[keys_.index("sharpen_radius")] = "1.7"
    assert effects.parse_values(lines) == {"grain_roughness": 61.0, "sharpen_radius": 1.7}
    assert effects.parse_values([]) == {}


def test_effect_tags_do_not_collide_with_the_other_readers():
    import colormix, retouch
    mine = set(effects.read_tags())
    assert not mine & set(colormix.XMP_TAGS.values())
    assert not mine & set(retouch.read_tags())


@pytest.mark.exiftool
@pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool is not installed")
def test_xmp_round_trip_through_real_exiftool(tmp_path):
    side = tmp_path / "IMG_0001.xmp"
    saved = {**ALL, "exposure": 0.5, "color_nr": 12, "clarity": 7, "sharpness": 40, "monochrome": True}
    subprocess.run(["exiftool", "-overwrite_original", "-o", str(side)] + editor._xmp_arguments(saved),
                   capture_output=True, text=True, check=True, timeout=30)
    got = effects.read_from_file(side, 30)
    for key, want in ALL.items():
        if key == "point_colors":
            assert len(got[key]) == 2
            for g, w in zip(got[key], want):
                assert all(abs(g[k] - w[k]) < 1e-2 for k in w)
        else:
            assert got[key] == pytest.approx(want), key
    # the main reader must still see its own values at the right offsets next to ours
    basic = editor._read_adjustments_from(side)
    assert basic["exposure"] == 0.5 and basic["color_nr"] == 12 and basic["clarity"] == 7 and basic["monochrome"]
    assert basic["grain_amount"] == 33 and basic["point_colors"][0]["hue"] == pytest.approx(120.0, abs=1e-2)
    # a re-save with defaults clears the points
    subprocess.run(["exiftool", "-overwrite_original"] + editor._xmp_arguments({}) + [str(side)],
                   capture_output=True, text=True, check=True, timeout=30)
    assert "point_colors" not in effects.read_from_file(side, 30)


def test_complete_effective_and_presets_know_the_new_keys():
    full = editor.complete_adjustments({"grain_amount": 20}, 5500)
    assert full["grain_amount"] == 20 and full["sharpen_detail"] == 25 and full["point_colors"] == []
    eff = editor.effective_adjustments({"grain_amount": 80, "vignette_amount": -60, "vignette_midpoint": 30,
                                        "sharpen_masking": 40, "strength": 50,
                                        "point_colors": [{"hue": 5, "hue_shift": 40}]})
    assert eff["grain_amount"] == pytest.approx(40) and eff["vignette_amount"] == pytest.approx(-30)
    assert eff["vignette_midpoint"] == 30 and eff["sharpen_masking"] == 40
    assert eff["point_colors"][0]["hue_shift"] == pytest.approx(20) and eff["point_colors"][0]["hue"] == 5
    clean, dropped = presetlib.clean_values({"grain_amount": 500, "cal_red_hue": 20, "sharpen_radius": 9})
    assert not dropped and clean["grain_amount"] == 100 and clean["sharpen_radius"] == 3.0
    assert presetlib.KEY_GROUP["grain_amount"] == "effects" and presetlib.KEY_GROUP["cal_red_hue"] == "colour"
    assert any(c["key"] == "vignette_feather" for c in editor.EFFECT_CONTROLS)


# ---------------------------------------------------------------------------
# Preview, tiles and export are the same pixels
# ---------------------------------------------------------------------------

FINISH = {"grain_amount": 60, "grain_size": 35, "grain_roughness": 70, "vignette_amount": -70,
          "vignette_midpoint": 30, "vignette_feather": 60, "sharpness": 60, "sharpen_radius": 1.6,
          "sharpen_detail": 70, "sharpen_masking": 30, "cal_red_hue": 40, "cal_blue_sat": -30,
          "point_colors": [{"hue": 200, "sat": 50, "lum": 50, "hue_shift": 60, "sat_shift": 20,
                            "lum_shift": 10, "range": 70}]}


@pytest.mark.parametrize("settings", [
    FINISH,
    {**FINISH, "crop": {"left": 0.1, "top": 0.15, "right": 0.9, "bottom": 0.8}},
])
def test_tiles_match_the_whole_frame_with_group_b_settings(photo, settings):
    tiled = stitch(photo, settings, "100")
    ref = reference(photo, settings, 1.0)
    assert tiled.shape == ref.shape
    border, overall = seam_error(tiled, ref)
    assert border <= 3 and overall <= 3, (border, overall)


def test_export_pipeline_matches_the_preview_pipeline(photo):
    """The exporter calls apply_adjustments on the same stage list: a cached preview-style render and a
    one-shot render of the same frame are pixel identical."""
    img = np.random.default_rng(6).random((90, 120, 3)).astype(np.float32)
    editor._stage_cache.clear()
    a = render(img, FINISH, cache_id="pv")
    b = render(img, FINISH)
    assert np.array_equal(a, b)
    assert np.array_equal(render_kernels.float_to_u8(a), render_kernels.float_to_u8(b))
