"""Tile server: no seams, correct grid, caching, frame mode, metadata."""

import numpy as np
import pytest

import editor
import tile_server


def textured(h=700, w=1000, seed=0):
    """Smooth colour field plus fine texture and hard edges: stresses every local filter."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    img = np.stack([0.5 + 0.35 * np.sin(xx / 90.0), 0.5 + 0.3 * np.sin(yy / 70.0 + 1),
                    0.5 + 0.3 * np.cos((xx + yy) / 110.0)], axis=-1)
    img += rng.normal(0, 0.03, img.shape).astype(np.float32)
    img[(xx // 60 + yy // 60) % 2 == 0] *= 0.8                      # checker edges
    return (np.clip(img, 0, 1) * 255).astype(np.uint8)


@pytest.fixture
def photo(tmp_path, monkeypatch, fake_rawpy):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    f = tmp_path / "IMG_0001.CR2"
    f.write_bytes(b"fake-cr2")
    fake_rawpy["rgb"] = textured()
    tile_server.clear_caches()
    monkeypatch.setattr(tile_server, "TILE_SIZE", 256)
    yield f
    tile_server.clear_caches()


@pytest.fixture
def client():
    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        yield c


def stitch(path, adj, z, size=256):
    """Assemble every tile of one zoom level into a single image."""
    meta = tile_server.describe(path, adj)
    lvl = next(l for l in meta["levels"] if l["z"] == z)
    out = np.zeros((lvl["height"], lvl["width"], 3), np.uint8)
    for ty in range(lvl["rows"]):
        for tx in range(lvl["cols"]):
            t = tile_server.render_tile_array(path, adj, z, tx, ty, tile=size)
            out[ty * size:ty * size + t.shape[0], tx * size:tx * size + t.shape[1]] = t
    return out


def reference(path, adj, scale):
    """The whole frame through the same pipeline in one piece (what an export at that size does)."""
    import render_kernels

    lin, baseline = tile_server.load_full(path)
    if scale <= 1.0:
        src = tile_server._scaled_frame(path, lin, scale)
    else:
        src = tile_server._resize(tile_server._encode(lin), round(lin.shape[1] * scale),
                                  round(lin.shape[0] * scale), up=True)
    a = dict(adj)
    a.setdefault("color_temp", baseline)
    return render_kernels.float_to_u8(editor.apply_adjustments(np.ascontiguousarray(src), baseline, a))


LOCAL = {"clarity": 60, "texture": 40, "dehaze": 25, "sharpness": 50,
         "exposure": 0.3, "contrast": 20, "vibrance": 20, "shadows": 20}


def seam_error(tiled, ref, size=256):
    """Largest difference on pixels within 3 px of a tile border, and overall."""
    diff = np.abs(tiled.astype(int) - ref.astype(int)).max(axis=2)
    border = np.zeros(diff.shape, bool)
    for k in range(size, diff.shape[1], size):
        border[:, k - 3:k + 3] = True
    for k in range(size, diff.shape[0], size):
        border[k - 3:k + 3, :] = True
    return int(diff[border].max()), int(diff.max())


@pytest.mark.parametrize("settings", [
    {}, {"clarity": 80}, {"dehaze": 60}, {"texture": 70, "sharpness": 80}, LOCAL,
    {**LOCAL, "crop": {"left": 0.1, "top": 0.15, "right": 0.9, "bottom": 0.8}},
])
def test_tiles_have_no_seams_at_100_percent(photo, settings):
    tiled = stitch(photo, settings, "100")
    ref = reference(photo, settings, 1.0)
    assert tiled.shape == ref.shape
    border, overall = seam_error(tiled, ref)
    assert border <= 2 and overall <= 2, (settings, border, overall)


@pytest.mark.parametrize("z,scale", [("50", 0.5), ("fit", None), ("200", 2.0)])
def test_tiles_have_no_seams_at_other_zooms(photo, z, scale):
    if scale is None:
        scale = tile_server.resolve_zoom("fit", 1000, 700, 640)
    tiled = stitch(photo, dict(LOCAL), z) if z != "fit" else None
    if z == "fit":
        # a small fit edge so the fit level is not just 100 %
        adj = dict(LOCAL)
        meta = tile_server.describe(photo, adj, fit_edge=640)
        lvl = next(l for l in meta["levels"] if l["z"] == "fit")
        assert lvl["scale"] == pytest.approx(0.64) and lvl["width"] == 640
        out = np.zeros((lvl["height"], lvl["width"], 3), np.uint8)
        for ty in range(lvl["rows"]):
            for tx in range(lvl["cols"]):
                t = tile_server.render_tile_array(photo, adj, "fit", tx, ty, fit_edge=640, tile=256)
                out[ty * 256:ty * 256 + t.shape[0], tx * 256:tx * 256 + t.shape[1]] = t
        ref = reference(photo, adj, scale)
        assert seam_error(out, ref)[0] <= 2
        return
    ref = reference(photo, dict(LOCAL), scale)
    assert tiled.shape == ref.shape
    border, overall = seam_error(tiled, ref)
    assert border <= 3 and overall <= 3, (z, border, overall)


def test_without_a_halo_there_would_be_seams(photo, monkeypatch):
    """Guard for the test itself: a zero halo must be detected as a seam."""
    monkeypatch.setattr(tile_server, "halo_px", lambda adj, edge: 0)
    monkeypatch.setattr(tile_server, "ALIGN", 1)
    adj = {"clarity": 100, "dehaze": 60}
    tiled = stitch(photo, adj, "100")
    border, _ = seam_error(tiled, reference(photo, adj, 1.0))
    assert border > 6


def test_frame_mode_straighten_and_masks(photo):
    adj = {"straighten": 3.0, "clarity": 40,
           "masks": [{"type": "radial", "id": "m1", "cx": 0.5, "cy": 0.5, "rx": 0.3, "ry": 0.3,
                      "feather": 0.5, "invert": False, "adjust": {"exposure": 0.5}}]}
    meta = tile_server.describe(photo, adj)
    assert meta["mode"] == "frame" and meta["exact"]
    tiled = stitch(photo, adj, "50")
    ref = reference(photo, adj, 0.5)
    assert tiled.shape == ref.shape                 # geometry.py's inscribed rectangle, exactly
    assert np.array_equal(tiled, ref)


def test_frame_mode_size_matches_rendered_size(photo):
    for adj in ({"straighten": -7.5}, {"straighten": 2, "crop": {"left": .1, "top": .1, "right": .8, "bottom": .9}}):
        meta = tile_server.describe(photo, adj)
        lvl = next(l for l in meta["levels"] if l["z"] == "100")
        assert (lvl["height"], lvl["width"]) == tile_server._frame(photo, adj, 1.0).shape[:2]


def test_frame_mode_refuses_giant_frames(photo, monkeypatch):
    monkeypatch.setattr(tile_server, "FRAME_MAX_PIXELS", 100_000)
    with pytest.raises(MemoryError):
        tile_server.get_tile(photo, {"straighten": 1}, "100", 0, 0)


# --- HTTP --------------------------------------------------------------

def test_tile_endpoint_and_lru_cache(photo, client):
    meta = client.post("/api/tile/meta", json={"path": "IMG_0001.CR2", "adjustments": {"exposure": 0.5}}).get_json()
    assert meta["tile_size"] == 256 and meta["source"] == {"width": 1000, "height": 700}
    lvl = {l["z"]: l for l in meta["levels"]}
    assert lvl["100"]["cols"] == 4 and lvl["100"]["rows"] == 3 and lvl["200"]["width"] == 2000
    url = f"/api/tile?photo=IMG_0001.CR2&z=100&x=3&y=2&sid={meta['settings_id']}"
    r1 = client.get(url)
    assert r1.status_code == 200 and r1.mimetype == "image/jpeg" and r1.headers["X-Tile-Cache"] == "miss"
    r2 = client.get(url)
    assert r2.headers["X-Tile-Cache"] == "hit" and r2.data == r1.data
    from PIL import Image
    import io
    assert Image.open(io.BytesIO(r1.data)).size == (1000 - 3 * 256, 700 - 2 * 256)   # edge tile is cropped
    png = client.get(url + "&format=png")
    assert png.mimetype == "image/png"
    # a different setting is a different tile
    other = client.get("/api/tile?photo=IMG_0001.CR2&z=100&x=0&y=0&settings=" + '{"exposure":1.5}')
    base = client.get("/api/tile?photo=IMG_0001.CR2&z=100&x=0&y=0&settings=" + '{"exposure":0}')
    assert other.data != base.data


def test_tile_endpoint_errors(photo, client):
    assert client.get("/api/tile?photo=IMG_0001.CR2&z=100&x=9&y=0&settings={}").status_code == 404
    assert client.get("/api/tile?photo=IMG_0001.CR2&z=300&x=0&y=0&settings={}").status_code == 400
    assert client.get("/api/tile?photo=IMG_0001.CR2&z=100&x=a&y=0&settings={}").status_code == 400
    assert client.get("/api/tile?photo=../x.CR2&z=100&x=0&y=0").status_code == 400
    assert client.get("/api/tile?photo=IMG_0001.CR2&z=100&x=0&y=0&sid=nope").status_code == 410
    assert client.get("/api/tile?photo=IMG_0001.CR2&z=100&x=0&y=0&settings=notjson").status_code == 400


def test_tile_cache_is_byte_bounded(photo, monkeypatch):
    size = len(tile_server.get_tile(photo, {"exposure": 0.05}, "100", 0, 0, "png")[0])
    monkeypatch.setattr(tile_server, "TILE_CACHE_BYTES", int(size * 2.5))
    for x in range(1, 5):
        tile_server.get_tile(photo, {"exposure": 0.1 * x}, "100", 0, 0, "png")
    assert 1 <= len(tile_server._tiles) <= 3 and tile_server._tile_bytes <= size * 3.5


def test_settings_hash_ignores_private_keys():
    assert tile_server.settings_hash({"a": 1, "_x": 2}) == tile_server.settings_hash({"a": 1})
    assert tile_server.settings_hash({"a": 1}) != tile_server.settings_hash({"a": 2})
