"""Group C: spot shapes, visualise / dust finder, red-eye, crop orientation, extras sidecar."""
import json

import numpy as np
import pytest

from test_editor_api import client, library  # noqa: F401
import editor
import extras
import folders
import geometry
import redeye
import retouch
import spots
import tile_server


def textured(h=200, w=300, seed=1):
    rng = np.random.default_rng(seed)
    base = np.linspace(0.3, 0.7, w, dtype=np.float32)[None, :, None] * np.ones((h, 1, 3), np.float32)
    return np.clip(base + rng.normal(0, 0.01, (h, w, 3)).astype(np.float32), 0, 1)


def spot(**kw):
    s = {"x": 0.3, "y": 0.5, "r": 0.05, "sx": 0.7, "sy": 0.5, "feather": 0,
         "opacity": 100, "mode": "clone"}
    s.update(kw)
    return s


# ---------------------------------------------------------------------------
# Spot shapes
# ---------------------------------------------------------------------------

def test_shape_is_normalised_and_defaults_to_circle():
    out = spots.normalise([spot(), spot(shape="square"), spot(shape="star")])
    assert [s["shape"] for s in out] == ["circle", "square", "circle"]


def test_square_spot_covers_the_corners_a_circle_leaves_alone():
    img = textured()
    g = spots.geometry(spots.normalise([spot()])[0], 300, 200)
    r = g["r"]
    corner = (g["cy"] - r + 1, g["cx"] - r + 1)
    circle = spots.apply(img, {"spots": [spot()]})
    square = spots.apply(img, {"spots": [spot(shape="square")]})
    assert np.array_equal(circle[corner], img[corner])
    assert not np.array_equal(square[corner], img[corner])
    assert np.array_equal(square[5, 5], img[5, 5])


def test_square_heal_is_deterministic():
    img = textured()
    adj = {"spots": [spot(shape="square", mode="heal", feather=40)]}
    assert np.array_equal(spots.apply(img, adj), spots.apply(img, adj))


# ---------------------------------------------------------------------------
# Visualise spots and the dust finder
# ---------------------------------------------------------------------------

def dusty():
    img = textured(400, 600)
    for (x, y) in ((100, 120), (300, 250), (480, 80)):
        yy, xx = np.mgrid[0:400, 0:600]
        img[np.hypot(xx - x, yy - y) < 5] *= 0.4
    return img


def test_visualise_marks_the_dust_dark_on_a_white_page():
    img = dusty()
    out = spots.visualise(img, 60)
    assert out.dtype == np.uint8 and out.ndim == 2
    assert out[110:131, 90:111].min() < 60 and out[300, 400] > 230
    more = spots.visualise(img, 100)
    less = spots.visualise(img, 0)
    assert int(more.mean()) <= int(less.mean())


def test_dust_finder_proposes_the_specks_and_is_deterministic():
    img = dusty()
    found = spots.find_dust(img, 50)
    assert len(found) == 3
    for x, y in ((100 / 599, 120 / 399), (300 / 599, 250 / 399), (480 / 599, 80 / 399)):
        assert any(abs(c["x"] - x) < 0.01 and abs(c["y"] - y) < 0.01 for c in found)
    assert found == spots.find_dust(img, 50)
    assert spots.find_dust(textured(400, 600), 50) == []
    assert len(spots.find_dust(img, 50, limit=1)) == 1


def test_dust_and_visualise_endpoints(client, fake_rawpy, fake_exiftool):
    r = client.post("/api/spot/dust", json={"path": "IMG_0001.dng", "sensitivity": 80})
    assert r.status_code == 200 and isinstance(r.get_json()["candidates"], list)
    for c in r.get_json()["candidates"]:
        assert 0 <= c["sx"] <= 1 and 0 <= c["sy"] <= 1
    r = client.post("/api/spot/visualise", json={"path": "IMG_0001.dng", "threshold": 40})
    assert r.status_code == 200 and r.get_json()["image"].startswith("data:image/png;base64,")
    assert client.post("/api/spot/dust", json={"path": "IMG_0001.dng", "sensitivity": "x"}).status_code == 400
    assert client.post("/api/spot/visualise", json={}).status_code == 400


# ---------------------------------------------------------------------------
# Red eye / pet eye
# ---------------------------------------------------------------------------

def eye_image(colour):
    img = np.full((200, 300, 3), 0.55, np.float32)
    yy, xx = np.mgrid[0:200, 0:300]
    img[np.hypot(xx - 150, yy - 100) < 8] = colour
    return img


def test_red_eye_removes_the_red_and_leaves_the_rest():
    img = eye_image((0.9, 0.15, 0.15))
    out = redeye.apply(img, {"redeye": [{"x": 150 / 299, "y": 100 / 199, "r": 0.04}]})
    assert out[100, 150, 0] < 0.5 * img[100, 150, 0]
    assert out[100, 150, 0] <= out[100, 150, 1] + 0.1
    assert np.array_equal(out[10, 10], img[10, 10])
    assert np.array_equal(img, eye_image((0.9, 0.15, 0.15)))          # input untouched


def test_pet_eye_darkens_green_eyeshine_and_amount_zero_is_a_noop():
    img = eye_image((0.3, 0.9, 0.3))
    eye = {"x": 150 / 299, "y": 100 / 199, "r": 0.04, "kind": "pet", "darken": 80}
    out = redeye.apply(img, {"redeye": [eye]})
    assert out[100, 150].max() < 0.35
    assert np.allclose(redeye.apply(img, {"redeye": [{**eye, "amount": 0}]}), img)


def test_redeye_normalise_clamps_and_drops_junk():
    out = redeye.normalise([{"x": 2, "y": 0.5, "r": 9, "kind": "x", "amount": -1}, {"y": 1}, "bad"])
    assert len(out) == 1 and out[0]["x"] == 1.0 and out[0]["r"] == redeye.MAX_RADIUS
    assert out[0]["kind"] == "red" and out[0]["amount"] == 0
    assert len(redeye.normalise([{"x": 0, "y": 0}] * 50)) == redeye.MAX_EYES
    assert redeye.normalise(None) == []


# ---------------------------------------------------------------------------
# Orientation
# ---------------------------------------------------------------------------

def test_orient_turns_and_mirrors():
    img = np.arange(2 * 3 * 3, dtype=np.float32).reshape(2, 3, 3)
    assert retouch.orient(img, {}) is img
    assert retouch.orient(img, {"rot90": 1}).shape == (3, 2, 3)
    assert np.array_equal(retouch.orient(img, {"rot90": 1})[0, 1], img[0, 0])      # clockwise
    assert np.array_equal(retouch.orient(img, {"rot90": 2}), img[::-1, ::-1])
    assert np.array_equal(retouch.orient(img, {"flip_h": True}), img[:, ::-1])
    assert np.array_equal(retouch.orient(img, {"flip_v": True}), img[::-1])
    assert np.array_equal(retouch.orient(img, {"rot90": 5}), retouch.orient(img, {"rot90": 1}))


def test_sizes_agree_with_the_rendered_frame():
    base = textured(200, 300)
    for adj in ({"rot90": 1}, {"rot90": 3, "crop": {"left": .1, "top": .1, "right": .9, "bottom": .7}},
                {"rot90": 1, "straighten": 4.0, "flip_h": True}):
        framed = editor.apply_geometry(retouch.apply_transform(base, adj), adj)
        assert tile_server.geometry_size(adj, 300, 200) == (framed.shape[1], framed.shape[0])
    assert retouch.frame_after_transform(300, 200, {"rot90": 1}) == (200, 300)


def test_raw_frame_view_ignores_orientation(client, fake_rawpy, fake_exiftool):
    a = client.post("/api/preview", json={"path": "IMG_0001.dng", "adjustments": {"rot90": 1}}).get_json()
    b = client.post("/api/preview", json={"path": "IMG_0001.dng", "adjustments": {}}).get_json()
    c = client.post("/api/preview", json={"path": "IMG_0001.dng", "adjustments": {"rot90": 1},
                                           "raw_frame": True}).get_json()
    assert a["preview"] != b["preview"] and c["preview"] == b["preview"]


@pytest.fixture
def photo(tmp_path, monkeypatch, fake_rawpy):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    f = tmp_path / "IMG_0001.CR2"
    f.write_bytes(b"fake-cr2")
    rng = np.random.default_rng(3)
    img = rng.integers(0, 255, (160, 240, 3)).astype(np.uint8)
    img[60:70, 100:110] = (230, 30, 30)
    fake_rawpy["rgb"] = img
    tile_server.clear_caches()
    monkeypatch.setattr(tile_server, "TILE_SIZE", 128)
    yield f
    tile_server.clear_caches()


def test_tiles_match_the_preview_pipeline_for_every_group_c_setting(photo):
    import render_kernels

    adj = {"spots": [spot(shape="square", mode="heal")],
           "redeye": [{"x": 0.44, "y": 0.4, "r": 0.03}],
           "rot90": 1, "flip_h": True,
           "crop": {"left": 0.1, "top": 0.1, "right": 0.9, "bottom": 0.8}, "crop_ratio": "3:2"}
    meta = tile_server.describe(photo, adj)
    assert meta["mode"] == "frame"
    lvl = next(l for l in meta["levels"] if l["z"] == "100")
    out = np.zeros((lvl["height"], lvl["width"], 3), np.uint8)
    for ty in range(lvl["rows"]):
        for tx in range(lvl["cols"]):
            t = tile_server.render_tile_array(photo, adj, "100", tx, ty, tile=128)
            out[ty * 128:ty * 128 + t.shape[0], tx * 128:tx * 128 + t.shape[1]] = t
    lin, baseline = tile_server.load_full(photo)
    src = tile_server._scaled_frame(photo, lin, 1.0)
    a = dict(adj)
    a.setdefault("color_temp", baseline)
    ref = render_kernels.float_to_u8(editor.apply_adjustments(np.ascontiguousarray(src), baseline, a))
    assert out.shape == ref.shape
    assert int(np.abs(out.astype(int) - ref.astype(int)).max()) <= 2


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_complete_fills_the_new_keys():
    full = retouch.complete({"rot90": 7, "flip_h": 1, "crop_ratio": "junk", "redeye": [{"x": .5, "y": .5}]})
    assert full["rot90"] == 3 and full["flip_h"] is True and full["flip_v"] is False
    assert full["crop_ratio"] == "" and full["redeye"][0]["kind"] == "red"
    assert retouch.complete({"crop_ratio": "orig"})["crop_ratio"] == "orig"
    assert retouch.complete({"crop_ratio": "2:3"})["crop_ratio"] == "2:3"


def test_extras_sidecar_round_trips_and_cleans_up(tmp_path):
    photo = tmp_path / "IMG_1.CR2"
    photo.write_bytes(b"x")
    adj = {"spots": [spot(shape="square")], "redeye": [{"x": .2, "y": .3, "kind": "pet"}],
           "rot90": 2, "flip_v": True, "crop_ratio": "4:5"}
    extras.save_sidecar(photo, adj)
    assert extras.sidecar_path(photo).name == "IMG_1.CR2.extras.json"
    back = extras.load_sidecar(photo)
    assert back["spots"][0]["shape"] == "square" and back["redeye"][0]["kind"] == "pet"
    assert (back["rot90"], back["flip_v"], back["crop_ratio"]) == (2, True, "4:5")
    extras.save_sidecar(photo, {"spots": [spot()]})           # nothing special left
    assert not extras.sidecar_path(photo).exists()
    extras.sidecar_path(photo).write_text("{not json")
    assert extras.load_sidecar(photo) == {}


def test_xmp_wins_when_spot_counts_differ():
    sq = [spot(shape="square")]
    assert extras.reconcile_spots(sq, spots.normalise([spot()]))[0]["shape"] == "square"
    assert extras.reconcile_spots(sq, spots.normalise([spot(), spot()])) == spots.normalise([spot(), spot()])


def test_save_and_read_back_group_c_settings(client, library, fake_rawpy, fake_exiftool):
    photo = library / "IMG_0001.dng"
    adj = {**retouch.DEFAULTS, "spots": [spot(shape="square")], "redeye": [{"x": .2, "y": .3}],
           "rot90": 3, "flip_h": True, "crop_ratio": "orig"}
    assert client.post("/api/save", json={"path": "IMG_0001.dng", "adjustments": adj}).status_code == 200
    assert extras.sidecar_path(photo).exists()
    fake_exiftool.stdout_lines = ["-"] * 22
    read = editor.read_saved_adjustments(photo, source_size=(100, 100))
    assert read["rot90"] == 3 and read["flip_h"] is True and read["crop_ratio"] == "orig"
    assert read["redeye"][0]["x"] == pytest.approx(0.2)
    # The fake exiftool has no RetouchAreas, so the sidecar's spots lose to the (empty) XMP...
    assert read["spots"] == []
    # ...but with the XMP's circle present the sidecar's shape comes back.
    merged = extras.merge_into({"spots": spots.normalise([spot()])}, extras.load_sidecar(photo))
    assert merged["spots"][0]["shape"] == "square"
    fake_exiftool.stdout_lines = None
    # A save that says nothing about these keys leaves them alone.
    client.post("/api/save", json={"path": "IMG_0001.dng", "adjustments": {"color_temp": 5300}})
    assert extras.load_sidecar(photo)["rot90"] == 3
    # Saving them neutral removes the file.
    r = client.post("/api/save", json={"path": "IMG_0001.dng", "adjustments": {**retouch.DEFAULTS}})
    assert r.status_code == 200, r.get_json()
    assert not extras.sidecar_path(photo).exists()


def test_photo_specific_and_folders_follow_the_photo(tmp_path):
    for key in ("redeye", "rot90", "flip_h", "flip_v", "crop_ratio"):
        assert key in geometry.PHOTO_SPECIFIC_KEYS
    (tmp_path / "a.CR2").write_bytes(b"x")
    (tmp_path / "a.CR2.extras.json").write_text(json.dumps({"rot90": 1}))
    names = {p.name for p in folders.group_files(tmp_path / "a.CR2")}
    assert "a.CR2.extras.json" in names
