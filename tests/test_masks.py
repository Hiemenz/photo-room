"""Local adjustments (M7): mask geometry, brush rasterisation, rendering, persistence, endpoints."""

import base64
import io
import json
import shutil
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import editor
import history
import masks
import style
from editor import apply_adjustments, read_saved_adjustments, save_adjustments
from test_editor_api import client, library  # noqa: F401

BASELINE = 5500
SHAPE = (60, 90)
needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")


def linear(**kw):
    return masks.normalise_mask({"type": "linear", "x": 0.5, "y": 0.5, "angle": 0, "feather": 40,
                                 "adj": {"exposure": 1.0}, **kw})


def radial(**kw):
    return masks.normalise_mask({"type": "radial", "x": 0.5, "y": 0.5, "rx": 0.3, "ry": 0.3,
                                 "feather": 50, "adj": {"exposure": 1.0}, **kw})


def brush(strokes, **kw):
    return masks.normalise_mask({"type": "brush", "strokes": strokes, "adj": {"exposure": 1.0}, **kw})


def stroke(pts, **kw):
    return {"size": 0.1, "feather": 50, "flow": 100, "density": 100, "pts": pts, **kw}


@pytest.fixture
def photo():
    return np.random.default_rng(5).random(SHAPE + (3,)).astype(np.float32) * 0.6 + 0.1


# --- normalisation ---------------------------------------------------------

def test_normalise_rejects_garbage_and_clamps():
    assert masks.normalise_masks("nope") == [] and masks.normalise_masks(None) == []
    assert masks.normalise_mask({"type": "wavy"}) is None and masks.normalise_mask(5) is None
    m = masks.normalise_mask({"type": "linear", "x": float("nan"), "angle": 540, "feather": 900,
                              "adj": {"exposure": 99, "bogus": 3, "contrast": "x"}})
    assert m["x"] == 0.5 and m["angle"] == -180 and m["feather"] == 100
    assert m["adj"] == {"exposure": 4.0}


def test_normalise_unique_ids_and_cap():
    many = [{"type": "linear", "id": "same"} for _ in range(40)]
    out = masks.normalise_masks(many)
    assert len(out) == masks.MAX_MASKS and len({m["id"] for m in out}) == len(out)


def test_brush_points_are_compact_flat_and_capped():
    m = brush([stroke([0.1, 0.1, 0.2, 0.2, 0.3])])      # odd count: last dropped
    assert m["strokes"][0]["pts"] == [0.1, 0.1, 0.2, 0.2]
    assert brush([stroke([])])["strokes"] == []
    huge = brush([stroke([0.5] * 100000)])
    assert len(huge["strokes"][0]["pts"]) == masks.MAX_POINTS * 2


# --- linear geometry -------------------------------------------------------

def test_linear_full_at_top_zero_at_bottom_half_at_centre():
    a = masks.linear_mask(SHAPE, linear())
    assert a.shape == SHAPE and a.dtype == np.float32
    assert a[0].min() == 1.0 and a[-1].max() == 0.0
    assert a[SHAPE[0] // 2 - 1: SHAPE[0] // 2 + 1].mean() == pytest.approx(0.5, abs=0.06)
    assert (np.diff(a[:, 10]) <= 1e-7).all()               # monotone down the gradient
    assert np.allclose(a, a[:, :1])                        # angle 0: constant along rows


def test_linear_feather_widens_transition_and_zero_is_hard():
    def width(f):
        a = masks.linear_mask(SHAPE, linear(feather=f))[:, 0]
        return int(((a > 0.01) & (a < 0.99)).sum())
    assert width(0) <= 2 and width(20) < width(60) < width(100)


def test_linear_angle_rotates_gradient():
    a = masks.linear_mask((80, 80), linear(angle=90))
    # angle 90: effect on the right, fading leftwards (clockwise quarter turn)
    assert np.allclose(a, a[:1]) and a[0, -1] != a[0, 0]
    assert abs(float(a[0, 0] - a[0, -1])) == pytest.approx(1.0)
    b = masks.linear_mask((80, 80), linear(angle=180))
    assert b[-1].min() == 1.0 and b[0].max() == 0.0
    c = masks.linear_mask((80, 80), linear(angle=45))
    assert c[0, -1] == 1.0 and c[-1, 0] == 0.0 and c[0, 0] == pytest.approx(0.5, abs=0.05)


def test_mask_invert_is_complement():
    m = linear()
    inv = dict(m, invert=True)
    assert np.allclose(masks.mask_array(m, SHAPE) + masks.mask_array(inv, SHAPE), 1.0)


# --- radial geometry -------------------------------------------------------

def test_radial_inside_outside_and_invert():
    m = radial(feather=30)
    a = masks.mask_array(m, (100, 100))
    assert a[50, 50] == 1.0 and a[0, 0] == 0.0 and a[50, 0] == 0.0
    assert masks.mask_array(dict(m, invert=True), (100, 100))[50, 50] == 0.0


def test_radial_feather_controls_soft_band():
    def soft(f):
        a = masks.mask_array(radial(feather=f), (100, 100))[50]
        return int(((a > 0.01) & (a < 0.99)).sum())
    assert soft(0) <= 3 and soft(25) < soft(100)
    full = masks.mask_array(radial(feather=100), (100, 100))
    assert full[50, 50] > 0.99 and full[50, 55] < full[50, 50]     # soft all the way in


def test_radial_radii_and_rotation():
    m = radial(rx=0.4, ry=0.1, feather=0)
    a = masks.mask_array(m, (100, 100))
    assert a[50, 85] == 1.0 and a[85, 50] == 0.0           # wide, not tall
    r = masks.mask_array(dict(m, angle=90), (100, 100))
    assert r[85, 50] == 1.0 and r[50, 85] == 0.0
    assert np.allclose(r, a.T, atol=1e-6)


# --- brush rasterisation ---------------------------------------------------

def test_brush_is_deterministic_and_cache_independent():
    m = brush([stroke([0.1, 0.2, 0.5, 0.6, 0.9, 0.3], flow=40), stroke([0.2, 0.8, 0.7, 0.8], erase=True)])
    a = masks.mask_array(m, SHAPE)
    masks._raster_cache.clear()
    b = masks.mask_array(m, SHAPE)
    assert a.tobytes() == b.tobytes()
    assert masks.brush_mask(SHAPE, m) is masks.brush_mask(SHAPE, m)       # cached


def test_brush_paints_around_stroke_only():
    a = masks.mask_array(brush([stroke([0.5, 0.5, 0.6, 0.5])]), (100, 100))
    assert a[50, 55] == 1.0 and a[0, 0] == 0.0 and a[95, 95] == 0.0
    assert 0 < a.mean() < 0.2


def test_brush_flow_density_feather_erase():
    one = [0.5, 0.5]
    hard = masks.mask_array(brush([stroke(one, feather=0)]), (100, 100))
    assert set(np.unique(hard)) == {0.0, 1.0}
    soft = masks.mask_array(brush([stroke(one, feather=100)]), (100, 100))
    assert soft[50, 50] > 0.98 and 0 < soft[50, 56] < 1
    # low flow accumulates over repeated passes
    pass1 = masks.mask_array(brush([stroke(one, flow=30)]), (100, 100))[50, 50]
    pass3 = masks.mask_array(brush([stroke(one, flow=30), stroke(one, flow=30), stroke(one, flow=30)]),
                             (100, 100))[50, 50]
    assert pass1 == pytest.approx(0.3, abs=1e-5) and pass3 > pass1
    # density caps one stroke
    assert masks.mask_array(brush([stroke(one, density=40)]), (100, 100)).max() == pytest.approx(0.4, abs=1e-5)
    # erase removes paint, and an erase-only brush paints nothing
    painted = brush([stroke(one), stroke(one, erase=True, size=0.03)])
    a = masks.mask_array(painted, (100, 100))
    assert a[50, 50] == 0.0 and a[50, 56] > 0
    assert masks.mask_array(brush([stroke(one, erase=True)]), (100, 100)).max() == 0.0


def test_brush_is_resolution_independent():
    m = brush([stroke([0.2, 0.2, 0.8, 0.7])])
    small = masks.mask_array(m, (50, 50))
    big = masks.mask_array(m, (200, 200))
    shrunk = np.asarray(Image.fromarray(big).resize((50, 50), Image.BOX))
    assert np.abs(small - shrunk).mean() < 0.03


def test_brush_stores_strokes_not_a_bitmap():
    m = brush([stroke([0.1, 0.1, 0.9, 0.9])])
    assert len(json.dumps(m)) < 600
    assert "strokes" in m and not any(isinstance(v, (bytes, str)) and len(str(v)) > 200 for v in m.values())


# --- rendering -------------------------------------------------------------

def test_zero_effect_masks_are_exact_noops(photo):
    zero = [linear(adj={}), radial(adj={"exposure": 0}), brush([stroke([0.5, 0.5])], adj={}),
            dict(linear(), enabled=False), brush([])]
    assert masks.apply_masks(photo, zero) is photo
    assert masks.apply_masks(photo, None) is photo and masks.apply_masks(photo, []) is photo
    plain = apply_adjustments(photo, BASELINE, {})
    with_masks = apply_adjustments(photo, BASELINE, {"masks": zero})
    assert np.array_equal(plain, with_masks)


def test_mask_outside_everything_is_still_a_noop(photo):
    far = radial(x=-0.4, y=-0.4, rx=0.05, ry=0.05, feather=0)
    assert np.array_equal(masks.apply_masks(photo, [far]), photo)


def test_render_changes_only_inside_the_mask(photo):
    m = linear(feather=20, adj={"exposure": 1.5})
    base = apply_adjustments(photo, BASELINE, {})
    out = apply_adjustments(photo, BASELINE, {"masks": [m]})
    arr = masks.mask_array(m, SHAPE)
    outside = arr == 0.0
    assert outside.sum() > 100
    assert np.array_equal(out[outside], base[outside])
    top = arr == 1.0
    assert (out[top] > base[top]).mean() > 0.9
    # stronger mask value, stronger change
    delta = np.abs(out - base).mean(axis=2)
    assert delta[arr > 0.9].mean() > delta[(arr > 0.1) & (arr < 0.4)].mean() > 0


def test_inside_matches_full_local_adjustment(photo):
    m = radial(feather=0, adj={"exposure": 1.0, "saturation": -40})
    out = apply_adjustments(photo, BASELINE, {"masks": [m]})
    full = masks.apply_local(photo, m["adj"], blur=editor._blur)
    inside = masks.mask_array(m, SHAPE) == 1.0
    assert np.allclose(out[inside], full[inside], atol=1e-6)


@pytest.mark.parametrize("key,value", [
    ("exposure", 1.0), ("contrast", 60), ("highlights", -60), ("shadows", 60), ("temp", 50),
    ("tint", 50), ("saturation", -60), ("clarity", 60), ("texture", 60), ("dehaze", 50), ("sharpness", 80),
])
def test_every_local_slider_changes_pixels_inside_only(photo, key, value):
    m = radial(feather=0, adj={key: value}, x=0.5, y=0.5, rx=0.25, ry=0.25)
    base = apply_adjustments(photo, BASELINE, {})
    out = apply_adjustments(photo, BASELINE, {"masks": [m]})
    arr = masks.mask_array(m, SHAPE)
    assert np.abs(out[arr == 1.0] - base[arr == 1.0]).max() > 1e-3, key
    assert np.array_equal(out[arr == 0.0], base[arr == 0.0])


def test_local_slider_direction(photo):
    brighter = masks.apply_local(photo, {"exposure": 1.0}, blur=editor._blur)
    darker = masks.apply_local(photo, {"exposure": -1.0}, blur=editor._blur)
    assert brighter.mean() > photo.mean() > darker.mean()
    warm = masks.apply_local(photo, {"temp": 60}, blur=editor._blur)
    assert warm[..., 0].mean() > photo[..., 0].mean() and warm[..., 2].mean() < photo[..., 2].mean()
    grey = masks.apply_local(photo, {"saturation": -100}, blur=editor._blur)
    assert np.allclose(grey[..., 0], grey[..., 1], atol=1e-5)
    assert not np.shares_memory(grey, photo)               # the source image is never mutated


def test_multiple_masks_stack_and_do_not_mutate_input(photo):
    before = photo.copy()
    a = linear(adj={"exposure": 1.0})
    b = radial(adj={"saturation": -100}, feather=0)
    both = masks.apply_masks(photo, [a, b])
    assert np.array_equal(photo, before)
    only_a = masks.apply_masks(photo, [a])
    assert not np.array_equal(both, only_a)


def test_strength_scales_local_adjustments():
    m = linear(adj={"exposure": 2.0, "contrast": 40})
    half = masks.scale_for_strength([m], 0.5)[0]
    assert half["adj"] == {"exposure": 1.0, "contrast": 20.0}
    eff = editor.effective_adjustments({"strength": 50, "masks": [m]})
    assert eff["masks"][0]["adj"]["exposure"] == 1.0


# --- XMP -------------------------------------------------------------------

def test_xmp_arguments_structure_and_clearing():
    args = masks.xmp_arguments([linear(name="Sky"), radial(), brush([stroke([0.3, 0.3, 0.4, 0.4])])], aspect=1.5)
    group = next(a for a in args if "MaskGroupBasedCorrections" in a)
    paint = next(a for a in args if "PaintBasedCorrections" in a)
    assert "What=Mask/Gradient" in group and "What=Mask/CircularGradient" in group
    assert "LocalExposure2012=1" in group and "CorrectionName=Sky" in group
    assert "What=Mask/Brush" in paint and "Dabs=[m 0.3 0.3,d 0.4 0.4]" in paint
    # no masks at all still emits the two tags, empty, which clears stale ones
    assert masks.xmp_arguments([]) == ["-XMP-crs:MaskGroupBasedCorrections=", "-XMP-crs:PaintBasedCorrections="]
    # zero-effect masks and empty brushes are not written
    assert masks.xmp_counts([linear(adj={}), brush([])]) == (0, 0, 0)


def test_gradient_xmp_points_round_trip_through_parse():
    for angle in (0, 30, -75, 135):
        for aspect in (1.5, 1.0, 0.75):
            m = linear(x=0.4, y=0.55, angle=angle, feather=35)
            zx, zy, fx, fy = masks._gradient_points(m, aspect)
            back = masks.parse_xmp({"MaskGroupBasedCorrections": [{
                "CorrectionName": "x", "LocalExposure2012": 1.0,
                "CorrectionMasks": [{"What": "Mask/Gradient", "ZeroX": zx, "ZeroY": zy, "FullX": fx, "FullY": fy,
                                     "MaskInverted": False}]}]}, aspect)[0]
            assert back["x"] == pytest.approx(0.4, abs=1e-3) and back["y"] == pytest.approx(0.55, abs=1e-3)
            assert back["angle"] == pytest.approx(angle, abs=0.1) and back["feather"] == pytest.approx(35, abs=0.2)


def test_reconcile_prefers_sidecar_only_when_xmp_agrees():
    rich = [linear(), brush([stroke([0.2, 0.2])])]
    same = masks.parse_xmp({"MaskGroupBasedCorrections": [{"LocalExposure2012": 1, "CorrectionMasks": [
        {"What": "Mask/Gradient", "ZeroX": .5, "ZeroY": .6, "FullX": .5, "FullY": .4}]}],
        "PaintBasedCorrections": [{"LocalExposure2012": 1, "CorrectionMasks": [
            {"What": "Mask/Brush", "Dabs": ["m 0.2 0.2"]}]}]})
    assert masks.reconcile(rich, same) == rich
    assert masks.reconcile(rich, same[:1]) == same[:1]      # someone removed the brush in Lightroom
    assert masks.reconcile([], same) == same
    assert masks.reconcile(rich, []) == []


def test_parse_skips_unsupported_masks_and_junk():
    assert masks.parse_xmp(None) == [] and masks.parse_exiftool_json("not json") == []
    out = masks.parse_xmp({"MaskGroupBasedCorrections": [
        {"CorrectionMasks": [{"What": "Mask/Image"}], "LocalExposure2012": 1},
        "junk", {"CorrectionMasks": [{"What": "Mask/Gradient", "ZeroX": "x"}]}]})
    assert out == []


def test_save_writes_mask_xmp_and_sidecar_json(tmp_path, fake_exiftool):
    photo = tmp_path / "x.dng"
    photo.write_bytes(b"x")
    m = brush([stroke([0.2, 0.2, 0.4, 0.4])], name="Face")
    save_adjustments(photo, {"exposure": 0.5, "masks": [m, linear()]}, frame_size=(300, 200))
    written = " ".join(fake_exiftool.write_call())
    assert "MaskGroupBasedCorrections=[" in written and "PaintBasedCorrections=[" in written
    assert masks.load_sidecar(photo) == masks.normalise_masks([m, linear()]) or len(masks.load_sidecar(photo)) == 2
    # a save that says nothing about masks leaves both the XMP tags and the JSON alone
    fake_exiftool.calls.clear()
    save_adjustments(photo, {"exposure": 0.1})
    assert "MaskGroupBasedCorrections" not in " ".join(fake_exiftool.write_call())
    assert len(masks.load_sidecar(photo)) == 2
    # an explicit empty list clears both
    save_adjustments(photo, {"exposure": 0.1, "masks": []})
    assert "-XMP-crs:MaskGroupBasedCorrections=" in fake_exiftool.write_call()
    assert masks.load_sidecar(photo) == [] and not masks.sidecar_path(photo).exists()


@needs_exiftool
def test_real_exiftool_round_trip_keeps_full_fidelity(tmp_path):
    photo = tmp_path / "IMG_9.CR2"
    photo.write_bytes(b"fake-cr2")
    original = masks.normalise_masks([
        linear(name="Sky", angle=20, feather=30, adj={"exposure": -0.7, "saturation": 12, "dehaze": 10}),
        radial(x=0.3, y=0.6, rx=0.2, ry=0.1, angle=40, invert=True, adj={"shadows": 25}),
        brush([stroke([0.2, 0.2, 0.3, 0.25, 0.4, 0.4], flow=60, density=80),
               stroke([0.3, 0.3, 0.35, 0.3], erase=True)], name="Face", adj={"clarity": -30, "temp": 10}),
    ])
    save_adjustments(photo, {"exposure": 0.25, "masks": original}, frame_size=(3000, 2000))
    xmp = photo.with_suffix(".xmp").read_text()
    assert "MaskGroupBasedCorrections" in xmp and "Mask/Gradient" in xmp and "Mask/CircularGradient" in xmp
    assert "PaintBasedCorrections" in xmp and "Mask/Brush" in xmp and "LocalExposure2012" in xmp

    # JSON sidecar and XMP agree: the full-fidelity copy comes back unchanged
    back = read_saved_adjustments(photo)["masks"]
    assert back == original

    # JSON gone (copied elsewhere): the XMP subset alone still rebuilds every mask
    masks.delete_sidecar(photo)
    approx = read_saved_adjustments(photo)["masks"]
    assert [m["type"] for m in approx] == ["linear", "radial", "brush"]
    lin = approx[0]
    assert lin["angle"] == pytest.approx(20, abs=0.1) and lin["feather"] == pytest.approx(30, abs=0.5)
    assert lin["adj"]["exposure"] == pytest.approx(-0.7) and lin["name"] == "Sky"
    rad = approx[1]
    assert rad["rx"] == pytest.approx(0.2, abs=1e-3) and rad["invert"] is True
    br = approx[2]
    assert [s["erase"] for s in br["strokes"]] == [False, True]
    assert br["strokes"][0]["pts"] == [0.2, 0.2, 0.3, 0.25, 0.4, 0.4]
    assert br["strokes"][0]["flow"] == pytest.approx(60) and br["strokes"][0]["density"] == pytest.approx(80)

    # stale JSON (someone deleted a mask from the XMP) is ignored
    save_adjustments(photo, {"masks": original}, frame_size=(3000, 2000))
    save_adjustments(photo, {"masks": original[:1]}, frame_size=(3000, 2000))
    other = tmp_path / "IMG_9.CR2.masks.json"
    other.write_text(json.dumps({"version": 1, "masks": original}))
    assert len(read_saved_adjustments(photo)["masks"]) == 1

    # clearing removes them from the XMP too
    save_adjustments(photo, {"masks": []}, frame_size=(3000, 2000))
    assert "MaskGroupBasedCorrections" not in photo.with_suffix(".xmp").read_text()
    assert "masks" not in (read_saved_adjustments(photo) or {})


# --- presets, history, endpoints ---------------------------------------------

def test_presets_and_history_carry_masks(tmp_path):
    m = [linear(name="Sky")]
    style.save_preset("sky", {"exposure": 0.2, "masks": m})
    assert style.get_preset("sky")["masks"] == m
    photo = tmp_path / "a.dng"
    photo.write_bytes(b"x")
    data, _ = history.record(photo, "Add mask", {"exposure": 0.2, "masks": m})
    _, adj = history.jump(photo, data["entries"][0]["id"])
    assert adj["masks"] == m


def test_open_reset_and_preview_endpoints(client, library, fake_rawpy, fake_exiftool):
    fake_exiftool.stdout_lines = ["-"] * 22
    data = client.post("/api/open", json={"path": "IMG_0001.dng"}).get_json()
    assert data["adjustments"]["masks"] == []

    m = radial(feather=0, adj={"exposure": 2.0}, rx=0.2, ry=0.2)
    plain = client.post("/api/preview", json={"path": "IMG_0001.dng", "adjustments": {}}).get_json()
    local = client.post("/api/preview", json={"path": "IMG_0001.dng", "adjustments": {"masks": [m]}}).get_json()
    assert plain["preview"] != local["preview"]
    junk = client.post("/api/preview", json={"path": "IMG_0001.dng", "adjustments": {"masks": "x"}})
    assert junk.status_code == 200 and junk.get_json()["preview"] == plain["preview"]

    fake_exiftool.stdout_lines = None
    save_adjustments(library / "IMG_0001.dng", {"masks": [m]})
    assert masks.sidecar_path(library / "IMG_0001.dng").exists()
    res = client.post("/api/reset", json={"path": "IMG_0001.dng"}).get_json()
    assert res["adjustments"]["masks"] == []
    assert not masks.sidecar_path(library / "IMG_0001.dng").exists()


def test_save_endpoint_persists_masks(client, library, fake_rawpy, fake_exiftool):
    m = linear()
    res = client.post("/api/save", json={"path": "IMG_0001.dng", "adjustments": {"masks": [m]}})
    assert res.status_code == 200 and res.get_json()["ok"]
    assert masks.load_sidecar(library / "IMG_0001.dng") == [m]
    assert "MaskGroupBasedCorrections=[" in " ".join(fake_exiftool.write_call())


def test_mask_overlay_endpoint(client, library, fake_rawpy, fake_exiftool):
    m = radial(feather=0)
    body = {"path": "IMG_0001.dng", "adjustments": {"masks": [m]}, "mask_id": m["id"]}
    res = client.post("/api/mask_overlay", json=body)
    assert res.status_code == 200
    out = res.get_json()
    png = Image.open(io.BytesIO(base64.b64decode(out["overlay"].split(",", 1)[1])))
    assert png.mode == "RGBA" and png.size == (out["width"], out["height"])
    px = np.asarray(png)
    assert px[png.size[1] // 2, png.size[0] // 2].tolist() == [255, 40, 40, int(0.55 * 255 + 0.5)]
    assert px[0, 0, 3] == 0
    assert client.post("/api/mask_overlay", json=dict(body, mask_id="nope")).status_code == 404
    assert client.post("/api/mask_overlay", json={"path": "../x", "adjustments": {}}).status_code == 400


def test_index_page_exposes_local_controls(client):
    body = client.get("/").get_data(as_text=True)
    # The page is a shell now; scripts and styles are static files.
    from pathlib import Path
    for p in sorted((Path(__file__).resolve().parent.parent / "static").rglob("*")):
        if p.is_file():
            body += p.read_text()
    for c in masks.LOCAL_CONTROLS:
        assert c["label"] in body
    assert "mask-panel" in body and 'id="mask-layer"' in body
