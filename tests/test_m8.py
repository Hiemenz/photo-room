"""M8: spot removal, transform, lens corrections, chromatic aberration."""
import json
import shutil
import subprocess

import cv2
import numpy as np
import pytest

from test_editor_api import client, library  # noqa: F401
import editor
import lens
import retouch
import spots
import style
import transform


def textured(h=200, w=300, seed=1):
    rng = np.random.default_rng(seed)
    base = np.linspace(0.2, 0.8, w, dtype=np.float32)[None, :, None] * np.ones((h, 1, 3), np.float32)
    return np.clip(base + rng.normal(0, 0.03, (h, w, 3)).astype(np.float32), 0, 1)


# ---------------------------------------------------------------------------
# Spots
# ---------------------------------------------------------------------------

def spot(**kw):
    s = {"x": 0.3, "y": 0.5, "r": 0.04, "sx": 0.7, "sy": 0.5, "feather": 0,
         "opacity": 100, "mode": "clone"}
    s.update(kw)
    return s


def test_normalise_clamps_and_drops_junk():
    out = spots.normalise([spot(x=2.0, r=5.0, opacity=-4), {"y": 0.2}, "bad", spot(mode="weird")])
    assert len(out) == 2
    assert out[0]["x"] == 1.0 and out[0]["r"] == spots.MAX_RADIUS and out[0]["opacity"] == 0
    assert out[1]["mode"] == "heal"
    assert spots.normalise(None) == [] and spots.normalise({"x": 1}) == []
    assert len(spots.normalise([spot()] * 200)) == spots.MAX_SPOTS


def test_geometry_scales_with_resolution():
    s = spots.normalise([spot()])[0]
    small, big = spots.geometry(s, 300, 200), spots.geometry(s, 1200, 800)
    for key in ("cx", "cy", "sx", "sy", "r"):
        assert abs(big[key] - 4 * small[key]) <= 4


def test_source_pulled_inside_frame():
    s = spots.normalise([spot(sx=1.0, sy=0.0)])[0]
    g = spots.geometry(s, 300, 200)
    assert g["sx"] + g["r"] <= 299 and g["sy"] - g["r"] >= 0


def test_clone_copies_source_pixels_and_leaves_input_alone():
    img = textured()
    before = img.copy()
    out = spots.apply(img, {"spots": [spot()]})
    assert np.array_equal(img, before)                    # input pristine
    g = spots.geometry(spots.normalise([spot()])[0], 300, 200)
    assert np.allclose(out[g["cy"], g["cx"]], img[g["sy"], g["sx"]])
    far = out[5, 5]
    assert np.array_equal(far, img[5, 5])                 # nothing outside the spot


def test_opacity_zero_and_empty_are_noops():
    img = textured()
    assert spots.apply(img, {}) is img
    assert np.allclose(spots.apply(img, {"spots": [spot(opacity=0)]}), img)


def test_spots_are_deterministic():
    img = textured()
    adj = {"spots": [spot(mode="heal"), spot(x=0.5, y=0.2, sx=0.2, sy=0.7, mode="clone", feather=60)]}
    assert np.array_equal(spots.apply(img, adj), spots.apply(img, adj))


def test_heal_matches_destination_tone_better_than_clone():
    img = np.full((200, 300, 3), 0.3, np.float32)
    img[:, 150:] = 0.7                                   # bright right half
    kw = dict(x=0.25, y=0.5, sx=0.75, sy=0.5, r=0.05, feather=0)
    dst = (75, 100)     # x, y
    clone = spots.apply(img, {"spots": [spot(mode="clone", **kw)]})
    heal = spots.apply(img, {"spots": [spot(mode="heal", **kw)]})
    assert clone[dst[1], dst[0]].mean() == pytest.approx(0.7, abs=0.01)
    assert abs(heal[dst[1], dst[0]].mean() - 0.3) < 0.1


def test_preview_and_full_resolution_agree():
    full = cv2.GaussianBlur(textured(400, 600), (0, 0), 3)
    half = cv2.resize(full, (300, 200), interpolation=cv2.INTER_AREA)
    adj = {"spots": [spot(mode="clone", feather=40)]}
    a = cv2.resize(spots.apply(full, adj), (300, 200), interpolation=cv2.INTER_AREA)
    b = spots.apply(half, adj)
    assert float(np.abs(a - b).mean()) < 0.01


def test_spot_at_the_edge_does_not_crash():
    img = textured()
    for mode in ("clone", "heal"):
        out = spots.apply(img, {"spots": [spot(x=0.0, y=0.0, sx=0.5, sy=0.5, mode=mode),
                                          spot(x=1.0, y=1.0, mode=mode)]})
        assert out.shape == img.shape and np.isfinite(out).all()


def test_suggest_source_is_deterministic_and_away_from_the_spot():
    img = textured(400, 600)
    a = spots.suggest_source(img, 0.4, 0.5, 0.03)
    assert a == spots.suggest_source(img, 0.4, 0.5, 0.03)
    assert 0 <= a[0] <= 1 and 0 <= a[1] <= 1
    assert abs(a[0] - 0.4) + abs(a[1] - 0.5) > 0.03
    # A corner spot on a tiny frame still gets an answer.
    assert len(spots.suggest_source(np.zeros((20, 20, 3), np.float32), 0.0, 0.0, 0.2)) == 2


def test_spot_xmp_arguments_and_parse_round_trip():
    original = spots.normalise([spot(mode="heal", feather=30, opacity=80),
                                spot(x=0.1, y=0.9, sx=0.2, sy=0.8, mode="clone")])
    arg = spots.xmp_arguments(original, frame=(300, 200))[0]
    assert arg.startswith("-XMP-crs:RetouchAreas=[") and "SpotType=heal" in arg
    assert spots.xmp_arguments([]) == ["-XMP-crs:RetouchAreas="]
    # What exiftool -struct -json returns for that structure.
    payload = json.dumps([{"RetouchAreas": [
        {"SpotType": "heal", "SourceX": 0.7, "OffsetY": 0.0, "Opacity": 0.8, "Feather": 0.3,
         "Masks": [{"X": 0.3, "Y": 0.5, "SizeX": 0.04, "SizeY": 0.06}]},
        {"SpotType": "clone", "SourceX": 0.2, "OffsetY": -0.1, "Opacity": 1, "Feather": 0.5,
         "Masks": [{"X": 0.1, "Y": 0.9, "SizeX": 0.04}]}]}])
    back = spots.parse_xmp(payload)
    assert [s["mode"] for s in back] == ["heal", "clone"]
    assert back[0]["opacity"] == pytest.approx(80) and back[1]["sy"] == pytest.approx(0.8)
    assert spots.parse_xmp("not json") == [] and spots.parse_xmp("[{}]") == []


@pytest.mark.skipif(shutil.which("exiftool") is None, reason="needs the real exiftool")
def test_spot_xmp_round_trip_with_real_exiftool(tmp_path):
    original = spots.normalise([spot(mode="heal", feather=30, opacity=80),
                                spot(x=0.1, y=0.9, sx=0.2, sy=0.8, mode="clone")])
    target = tmp_path / "t.xmp"
    subprocess.run(["exiftool"] + spots.xmp_arguments(original, (300, 200))
                   + ["-o", str(target), "/dev/null"], capture_output=True)
    back = retouch.read_from_file(target, 30)["spots"]
    assert len(back) == 2
    for a, b in zip(original, back):
        for key in ("x", "y", "r", "sx", "sy", "feather", "opacity", "mode"):
            assert b[key] == pytest.approx(a[key], abs=1e-3) if key != "mode" else b[key] == a[key]


# ---------------------------------------------------------------------------
# Transform
# ---------------------------------------------------------------------------

def test_identity_is_a_noop():
    img = textured()
    assert transform.is_identity({})
    assert transform.apply(img, {}) is img
    assert np.allclose(transform.homography(300, 200, {}), np.eye(3))
    assert transform.output_size(300, 200, {}) == (300, 200)


def test_corners_map_as_documented():
    w, h = 600, 400
    c = transform.corners_after(w, h, {"xf_vertical": 40})
    assert c[1][0] - c[0][0] > w                       # top edge widened
    assert c[2][0] - c[3][0] < w                       # bottom narrowed
    c = transform.corners_after(w, h, {"xf_horizontal": 40})
    assert c[2][1] - c[1][1] > h                       # right edge enlarged
    c = transform.corners_after(w, h, {"xf_scale": 50})
    assert np.allclose(c[0], (w / 4, h / 4)) and np.allclose(c[2], (3 * w / 4, 3 * h / 4))
    c = transform.corners_after(w, h, {"xf_x": 100, "xf_y": -100})
    assert np.allclose(c[0], (0.25 * w, -0.25 * h))
    c = transform.corners_after(w, h, {"xf_rotate": 45})
    assert np.allclose(c[0], (229.2893, -153.5534), atol=1e-3)   # clockwise about the centre
    c = transform.corners_after(w, h, {"xf_aspect": 100})
    assert (c[1][0] - c[0][0]) > w and (c[3][1] - c[0][1]) < h


def test_values_are_clamped_and_junk_ignored():
    v = transform.values({"xf_vertical": 900, "xf_scale": "x", "xf_rotate": float("nan")})
    assert v["xf_vertical"] == 100 and v["xf_scale"] == 100 and v["xf_rotate"] == 0


@pytest.mark.parametrize("adj", [
    {"xf_vertical": 30}, {"xf_horizontal": -25}, {"xf_rotate": 7},
    {"xf_vertical": 20, "xf_rotate": -3, "xf_scale": 90}, {"xf_x": 40},
])
def test_auto_crop_stays_inside_the_image(adj):
    w, h = 600, 400
    p = transform.plan(w, h, adj)
    quad = transform.corners_after(w, h, adj)
    rect = np.array([[p["x0"], p["y0"]], [p["x0"] + p["w"], p["y0"]],
                     [p["x0"] + p["w"], p["y0"] + p["h"]], [p["x0"], p["y0"] + p["h"]]], float)
    assert transform._inside_convex(quad, rect, eps=0.0)
    img = np.ones((h, w, 3), np.float32)
    out = transform.apply(img, adj)
    assert out.shape[:2] == (p["h"], p["w"])
    assert np.allclose(out, 1.0, atol=1e-4)            # no empty corner pulled in


def test_transform_composes_with_straighten_and_crop():
    base = textured(400, 600)
    adj = {"color_temp": 5500, "xf_vertical": 25}
    tw, th = transform.output_size(600, 400, adj)
    out = editor.apply_adjustments(base, 5500, adj)
    assert out.shape[:2] == (th, tw)

    cropped = editor.apply_adjustments(base, 5500, {**adj, "crop": {"left": 0, "top": 0, "right": 0.5, "bottom": 0.5}})
    assert cropped.shape[:2] == (round(0.5 * th), round(0.5 * tw))

    rotated = editor.apply_adjustments(base, 5500, {**adj, "straighten": 5.0})
    assert rotated.shape[0] < th and rotated.shape[1] < tw      # straighten insets further

    # Neutral transform must not change the existing render at all.
    plain = editor.apply_adjustments(base, 5500, {"color_temp": 5500, "straighten": 3.0})
    same = editor.apply_adjustments(base, 5500, {"color_temp": 5500, "straighten": 3.0,
                                                 **transform.DEFAULTS})
    assert np.array_equal(plain, same)


def test_raw_frame_skips_geometry_but_keeps_spots():
    base = textured(200, 300)
    out = editor.apply_adjustments(base, 5500, {"color_temp": 5500, "xf_vertical": 30, "straighten": 4,
                                                "crop": {"left": 0.2, "top": 0, "right": 1, "bottom": 1},
                                                "_raw_frame": True, "spots": [spot()]})
    assert out.shape == base.shape
    assert not np.allclose(out, base, atol=1e-3)


def grid(w=600, h=400):
    img = np.full((h, w, 3), 0.9, np.float32)
    for x in range(50, w, 50):
        cv2.line(img, (x, 0), (x, h), (0, 0, 0), 2)
    for y in range(40, h, 40):
        cv2.line(img, (0, y), (w, y), (0, 0, 0), 2)
    return img


def test_upright_level_recovers_a_tilt():
    img = grid()
    tilted = cv2.warpPerspective(img, transform.homography(600, 400, {"xf_rotate": 6}), (600, 400),
                                 borderValue=(0.9, 0.9, 0.9))
    found = transform.upright(tilted, "level")
    assert found["xf_rotate"] == pytest.approx(-6, abs=0.8)
    assert found["xf_vertical"] == 0


def test_upright_vertical_recovers_keystone():
    img = grid()
    skewed = cv2.warpPerspective(img, transform.homography(600, 400, {"xf_vertical": 30}), (600, 400),
                                 borderValue=(0.9, 0.9, 0.9))
    for mode in ("vertical", "full", "auto"):
        found = transform.upright(skewed, mode)
        assert found["xf_vertical"] == pytest.approx(-30, abs=6), mode
    assert transform.upright(skewed, "level")["xf_vertical"] == 0


def test_upright_falls_back_to_neutral_without_lines():
    blank = np.full((200, 300, 3), 0.5, np.float32)
    found = transform.upright(blank, "auto")
    assert {k: found[k] for k in transform.DEFAULTS} == transform.DEFAULTS
    with pytest.raises(ValueError):
        transform.upright(blank, "bogus")


def test_transform_survives_missing_opencv(monkeypatch):
    monkeypatch.setattr(transform, "cv2", None)
    img = textured()
    assert transform.apply(img, {"xf_vertical": 30}) is img
    assert len(transform.detect_lines(img)) == 0


# ---------------------------------------------------------------------------
# Lens, vignetting, CA
# ---------------------------------------------------------------------------

@pytest.fixture
def no_lensfun(monkeypatch):
    monkeypatch.setattr(lens, "_lensfunpy", lambda: None)
    monkeypatch.setattr(lens, "_db", None)


def test_neutral_lens_is_noop():
    img = textured()
    assert lens.is_neutral({}) and lens.apply(img, {}) is img


def test_manual_distortion_and_vignette():
    img = grid(300, 200)
    straight = lens.apply(img, {"lens_distortion": 60})
    assert straight.shape == img.shape and not np.allclose(straight, img)
    assert np.allclose(lens.manual_distortion(img, 50)[100, 150], img[100, 150], atol=0.2)
    flat = np.full((200, 300, 3), 0.4, np.float32)
    lit = lens.apply(flat, {"lens_vignette": 80})
    assert lit[100, 150, 0] == pytest.approx(0.4, abs=0.01) and lit[0, 0, 0] > 0.6
    dark = lens.apply(flat, {"lens_vignette": -80})
    assert dark[0, 0, 0] < 0.3


def test_lens_profile_falls_back_when_lensfun_is_missing(no_lensfun, tmp_path):
    assert lens.lensfun_available() is False
    adj = {"lens_profile": True, "lens_distortion": 30}
    assert lens.attach_meta(tmp_path / "x.cr2", adj) is adj          # no subprocess, no change
    out = lens.apply(textured(), {**adj, "_lens_meta": {"maker": "Canon"}})
    assert out.shape == textured().shape                               # manual path ran
    assert lens.describe(tmp_path / "x.cr2") == {"lensfun": False, "profile": None, "camera": None}


def test_lens_profile_failure_never_raises(monkeypatch):
    class Boom:
        class Modifier:
            def __init__(self, *a, **k):
                raise RuntimeError("bad db")
    monkeypatch.setattr(lens, "_lensfunpy", lambda: Boom)
    monkeypatch.setattr(lens, "find_profile", lambda meta: (type("C", (), {"crop_factor": 1.0})(), object()))
    img = textured()
    out = lens.apply(img, {"lens_profile": True, "_lens_meta": {"focal": 35, "aperture": 4, "distance": 5},
                           "lens_vignette": 20})
    assert out.shape == img.shape


@pytest.mark.skipif(not lens.lensfun_available(), reason="lensfunpy not installed")
def test_real_lensfun_profile_applies():
    meta = {"maker": "Canon", "model": "Canon EOS 5D Mark III", "lens": "Canon EF 24-70mm f/2.8L USM",
            "focal": 24.0, "aperture": 2.8, "distance": 5.0, "crop": None}
    if lens.find_profile(meta) is None:
        pytest.skip("lensfun database lacks this lens")
    img = np.full((200, 300, 3), 0.4, np.float32)
    out = lens.apply(img, {"lens_profile": True, "_lens_meta": meta})
    assert out.shape == img.shape and out[0, 0].mean() > out[100, 150].mean()   # vignetting lifted


def shifted_edges(scale_red=1.004):
    h, w = 300, 400
    img = np.full((h, w, 3), 0.1, np.float32)
    for x in range(40, w, 60):
        cv2.rectangle(img, (x, 20), (x + 25, h - 20), (0.9, 0.9, 0.9), -1)
    red = cv2.warpAffine(img[:, :, 0], lens._scale_matrix(scale_red, w, h), (w, h),
                         borderMode=cv2.BORDER_REPLICATE)
    out = img.copy()
    out[:, :, 0] = red
    return out


def test_remove_ca_registers_red_to_green():
    img = shifted_edges()
    fixed = lens.apply(img, {"ca_remove": True})

    def agreement(a):
        r = lens._grad(np.ascontiguousarray(a[:, :, 0]))
        g = lens._grad(np.ascontiguousarray(a[:, :, 1]))
        r, g = r - r.mean(), g - g.mean()
        return float((r * g).sum() / np.sqrt((r * r).sum() * (g * g).sum()))

    assert agreement(fixed) > agreement(img) + 0.05
    clean = np.dstack([shifted_edges(1.0)[:, :, 1]] * 3)
    assert lens.remove_ca(clean) is clean                   # nothing to fix, nothing touched


def test_defringe_desaturates_purple_at_edges_only():
    img = np.full((100, 100, 3), 0.1, np.float32)
    img[:, 50:] = 0.9
    img[:, 48:52] = (0.7, 0.2, 0.8)                         # purple fringe on the edge
    img[:, 5:9] = (0.7, 0.2, 0.8)                           # purple stripe far from an edge... also an edge
    out = lens.defringe(img, 20, 0)
    sat = lambda p: p.max() - p.min()
    assert sat(out[50, 49]) < 0.5 * sat(img[50, 49])
    assert lens.defringe(img, 0, 0) is img


def test_lens_xmp_arguments_and_parse():
    adj = {"lens_profile": True, "lens_distortion": 12, "lens_vignette": -5, "ca_remove": True,
           "defringe_purple": 4, "defringe_green": 0}
    args = " ".join(lens.xmp_arguments(adj))
    for tag in ("LensProfileEnable=1", "LensManualDistortionAmount=12", "VignetteAmount=-5",
                "AutoLateralCA=1", "DefringePurpleAmount=4", "DefringeGreenAmount=0"):
        assert tag in args


def test_scalar_xmp_round_trip():
    adj = {**retouch.DEFAULTS, "xf_vertical": 12, "xf_rotate": -2.5, "xf_scale": 90, "xf_x": 5.5,
           "lens_profile": True, "ca_remove": True, "lens_distortion": -7, "defringe_green": 3}
    written = {a.split(":")[1].split("=")[0]: a.split("=", 1)[1]
               for a in retouch.xmp_arguments(adj) if "RetouchAreas" not in a}
    lines = [written.get(tag, "-") for tag in retouch.read_tags()]
    back = retouch.parse_values(lines)
    for key in ("xf_vertical", "xf_rotate", "xf_scale", "xf_x", "lens_distortion", "defringe_green"):
        assert back[key] == pytest.approx(adj[key])
    assert back["lens_profile"] is True and back["ca_remove"] is True
    assert "xf_aspect" not in back                          # defaults are not reported
    assert retouch.parse_values(["-"] * 20) == {}


# ---------------------------------------------------------------------------
# Endpoints, presets, save, reset
# ---------------------------------------------------------------------------

def test_open_returns_m8_defaults_and_lens_info(client, fake_rawpy, fake_exiftool):
    fake_exiftool.stdout_lines = ["-"] * 22
    data = client.post("/api/open", json={"path": "IMG_0001.dng"}).get_json()
    for key, default in retouch.DEFAULTS.items():
        assert data["adjustments"][key] == default
    assert "lensfun" in data["lens"]


def test_preview_renders_spots_and_raw_frame(client, fake_rawpy, fake_exiftool):
    adj = {"spots": [spot(mode="clone")], "xf_vertical": 30}
    a = client.post("/api/preview", json={"path": "IMG_0001.dng", "adjustments": adj})
    b = client.post("/api/preview", json={"path": "IMG_0001.dng", "adjustments": adj, "raw_frame": True})
    assert a.status_code == 200 and b.status_code == 200
    assert a.get_json()["preview"] != b.get_json()["preview"]


def test_upright_endpoint(client, fake_rawpy, fake_exiftool):
    r = client.post("/api/upright", json={"path": "IMG_0001.dng", "mode": "auto"})
    body = r.get_json()
    assert r.status_code == 200 and set(body["values"]) == set(transform.DEFAULTS)
    assert client.post("/api/upright", json={"path": "IMG_0001.dng", "mode": "x"}).status_code == 400
    assert client.post("/api/upright", json={"mode": "auto"}).status_code == 400


def test_spot_suggest_endpoint(client, fake_rawpy, fake_exiftool):
    r = client.post("/api/spot/suggest", json={"path": "IMG_0001.dng", "x": 0.5, "y": 0.5, "r": 0.05})
    body = r.get_json()
    assert r.status_code == 200 and 0 <= body["sx"] <= 1 and 0 <= body["sy"] <= 1
    assert client.post("/api/spot/suggest", json={"path": "IMG_0001.dng", "x": "a", "y": 1}).status_code == 400


def test_save_writes_m8_tags(client, fake_rawpy, fake_exiftool):
    adj = {**retouch.DEFAULTS, "xf_vertical": 15, "lens_profile": True, "ca_remove": True,
           "spots": [spot(mode="heal")]}
    res = client.post("/api/save", json={"path": "IMG_0001.dng", "adjustments": adj})
    assert res.status_code == 200
    cmd = " ".join(fake_exiftool.write_call())
    assert "PerspectiveVertical=15" in cmd and "LensProfileEnable=1" in cmd
    assert "AutoLateralCA=1" in cmd and "RetouchAreas=[{SpotType=heal" in cmd


def test_presets_carry_transform_and_lens_but_not_spots(tmp_path):
    db = tmp_path / "p.duckdb"
    style.save_preset("m8", {"xf_vertical": 10, "lens_distortion": 5, "ca_remove": True,
                             "spots": [spot()]}, db_path=db)
    got = style.get_preset("m8", db_path=db)
    assert got["xf_vertical"] == 10 and got["ca_remove"] is True and "spots" not in got


def test_reset_restores_m8_defaults(client, fake_rawpy, fake_exiftool):
    r = client.post("/api/reset", json={"path": "IMG_0001.dng", "overwrite_foreign": True})
    adj = r.get_json()["adjustments"]
    assert adj["spots"] == [] and adj["xf_vertical"] == 0 and adj["lens_profile"] is False


def test_history_round_trips_spots(tmp_path):
    import history
    photo = tmp_path / "a.dng"
    photo.write_bytes(b"x")
    full = retouch.complete({"spots": [spot()], "xf_vertical": 9})
    hist, _ = history.record(photo, "Add spot", full)
    assert hist["entries"][-1]["adjustments"]["spots"][0]["mode"] == "clone"
