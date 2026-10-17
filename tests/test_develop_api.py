"""HTTP endpoints for auto tone, white balance, eyedropper, clipping and histogram."""

import base64
import io

import numpy as np
import pytest
from PIL import Image

import colour
import editor

CAST = (1.45, 1.0, 0.65)           # a warm illuminant, in linear light
CARD = (slice(300, 340), slice(500, 560))      # rows, cols of a mid-grey card


def cast_scene(h=480, w=720, seed=11):
    rng = np.random.default_rng(seed)
    cells = rng.uniform(0.1, 0.7, (h // 20, w // 20, 3)).astype(np.float32)
    enc = np.kron(cells, np.ones((20, 20, 1), np.float32))
    enc[CARD] = 0.5                                              # the grey card
    lin = colour.to_linear(enc) * np.array(CAST, np.float32)
    return (np.clip(colour.to_encoded(np.clip(lin, 0, 1)), 0, 1) * 255 + 0.5).astype(np.uint8)


@pytest.fixture
def lib(tmp_path, monkeypatch, fake_rawpy):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    (tmp_path / "IMG_0001.CR2").write_bytes(b"fake-cr2")
    fake_rawpy["rgb"] = cast_scene()
    fake_rawpy["whitebalance"] = [2.0, 1.0, 1.0, 1.0]
    editor._base_cache.clear()
    editor._decode_cache.clear()
    return tmp_path


@pytest.fixture
def client(lib):
    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        yield c


def post(client, route, **body):
    body.setdefault("path", "IMG_0001.CR2")
    return client.post(f"/api/develop/{route}", json=body)


def card_rgb(adj):
    path = editor._safe_path("IMG_0001.CR2")
    base, baseline = editor._load_base_render(path)
    out = editor.apply_adjustments(base, baseline, adj)
    return out[CARD].reshape(-1, 3).mean(axis=0) * 255


# --- eyedropper --------------------------------------------------------

def test_eyedropper_neutralises_the_grey_card(client):
    before = card_rgb({"color_temp": 6500})
    assert before[0] - before[2] > 40                 # the cast is plainly there
    r = post(client, "wb_pick", x=530 / 720, y=320 / 480, radius=6, adjustments={})
    assert r.status_code == 200
    d = r.get_json()
    assert 2000 <= d["color_temp"] <= 12000 and d["tint"] != 0 or d["color_temp"] != 6500
    after = card_rgb({"color_temp": d["color_temp"], "tint": d["tint"]})
    assert np.ptp(after) <= 3, (d, after)             # neutral within 3/255
    assert np.ptp(d["rgb_after"]) <= 3
    # the loupe readout is the sampled colour as displayed now
    assert np.allclose(d["rgb"], card_rgb({}), atol=2)
    assert d["pixels"] > 100 and d["warnings"] == []


def test_eyedropper_cast_direction_is_lightroom_like(client):
    d = post(client, "wb_pick", x=530 / 720, y=320 / 480, radius=3, adjustments={}).get_json()
    base_temp = editor._load_base_render(editor._safe_path("IMG_0001.CR2"))[1]
    assert d["color_temp"] < base_temp                 # a warm cast needs a cooler Temp


def test_eyedropper_respects_crop_and_current_settings(client):
    adj = {"crop": {"left": 0.5, "top": 0.5, "right": 1.0, "bottom": 1.0}, "exposure": 1.0}
    # card spans x 500-560 of 720 -> in the right half; in cropped coordinates:
    x = (530 - 360) / 360
    y = (320 - 240) / 240
    d = post(client, "wb_pick", x=x, y=y, radius=4, adjustments=adj).get_json()
    full = post(client, "wb_pick", x=530 / 720, y=320 / 480, radius=4, adjustments={}).get_json()
    assert abs(d["color_temp"] - full["color_temp"]) <= 60 and abs(d["tint"] - full["tint"]) <= 3


def test_eyedropper_validation(client):
    assert post(client, "wb_pick", x=2, y=0.5, adjustments={}).status_code == 400
    assert post(client, "wb_pick", y=0.5, adjustments={}).status_code == 400
    assert post(client, "wb_pick", x=.5, y=.5, radius=500, adjustments={}).status_code == 400
    assert post(client, "wb_pick", x="a", y=.5, adjustments={}).status_code == 400
    assert post(client, "wb_pick", path="../x", x=.5, y=.5).status_code == 400
    assert post(client, "wb_pick", x=.5, y=.5, adjustments={"edit_version": 1}).status_code == 409


def test_eyedropper_too_dark(client, fake_rawpy):
    fake_rawpy["rgb"] = np.zeros((100, 100, 3), np.uint8)
    editor._base_cache.clear()
    assert post(client, "wb_pick", x=.5, y=.5, adjustments={}).status_code == 422


# --- auto white balance and presets -------------------------------------

def test_auto_wb_corrects_the_cast(client):
    d = post(client, "auto_wb", adjustments={}).get_json()
    base_temp = d["baseline_temp"]
    assert d["color_temp"] < base_temp                # warm cast: cooler
    out = colour.to_linear(editor.apply_adjustments(
        editor._load_base_render(editor._safe_path("IMG_0001.CR2"))[0], base_temp,
        {"color_temp": d["color_temp"], "tint": d["tint"]}))
    m = out.reshape(-1, 3).mean(axis=0)
    assert abs(m[0] / m[2] - 1) < 0.15
    assert d["adjustments"]["color_temp"] == d["color_temp"] and d["label"]


def test_presets_endpoint(client):
    d = post(client, "wb_presets", adjustments={}).get_json()
    ids = [p["id"] for p in d["presets"]]
    assert ids == ["as_shot", "auto", "daylight", "cloudy", "shade", "tungsten", "fluorescent", "flash"]
    assert d["presets"][0]["color_temp"] == d["baseline_temp"] == 6500
    assert client.get("/api/develop/wb_presets?path=IMG_0001.CR2").status_code in (200, 415)


def test_as_shot_temperature_reads_like_daylight(client, lib):
    """R/B = 2 multipliers (daylight) must not read ~3000 K (the old bug)."""
    base_temp = editor._load_base_render(editor._safe_path("IMG_0001.CR2"))[1]
    assert 5000 <= base_temp <= 7000


# --- auto tone ---------------------------------------------------------

def test_auto_tone_endpoint(client):
    d = post(client, "auto_tone", adjustments={"color_temp": 6500, "tint": 0, "clarity": 10}).get_json()
    assert set(d["settings"]) == {"exposure", "contrast", "highlights", "shadows", "whites", "blacks", "vibrance"}
    assert d["adjustments"]["clarity"] == 10 and d["adjustments"]["exposure"] == d["settings"]["exposure"]
    assert d["label"] == "Auto Tone" and d["ms"] < 1000
    assert 0.0 <= d["after"]["hi_clip"] <= 0.006


def test_auto_tone_uses_saved_settings_when_none_sent(client):
    assert post(client, "auto_tone").status_code == 200


def test_auto_tone_rejects_legacy_edits(client):
    assert post(client, "auto_tone", adjustments={"edit_version": 1}).status_code == 409


def test_auto_tone_is_one_history_step(client):
    d = post(client, "auto_tone", adjustments={}).get_json()
    n0 = len(client.post("/api/history", json={"path": "IMG_0001.CR2"}).get_json()["history"]["entries"])
    r = client.post("/api/history/record", json={"path": "IMG_0001.CR2", "label": d["label"],
                                                 "adjustments": editor.complete_adjustments(d["adjustments"], 6500)})
    assert r.get_json()["recorded"]
    h = client.post("/api/history", json={"path": "IMG_0001.CR2"}).get_json()["history"]["entries"]
    assert len(h) == n0 + 1 and h[-1]["label"] == "Auto Tone"


# --- clipping ----------------------------------------------------------

def test_clipping_overlay_and_percentages(client, fake_rawpy):
    img = np.full((200, 300, 3), 128, np.uint8)
    img[:40] = 255                      # 20 % blown
    img[160:, :150] = 0                 # 10 % crushed
    fake_rawpy["rgb"] = img
    editor._base_cache.clear()
    d = post(client, "clipping", adjustments={"color_temp": 6500}).get_json()
    assert d["percent"]["highlights"] == pytest.approx(20.0, abs=0.5)
    assert d["percent"]["shadows"] == pytest.approx(10.0, abs=0.5)
    png = Image.open(io.BytesIO(base64.b64decode(d["overlay"].split(",", 1)[1])))
    assert png.size == (300, 200) and png.mode == "RGBA"
    px = np.asarray(png)
    assert tuple(px[10, 10][:3]) == (255, 30, 30) and tuple(px[190, 10][:3]) == (0, 90, 255)
    assert px[100, 100, 3] == 0
    assert set(d["percent"]["highlights_channels"]) == {"r", "g", "b"}


def test_clipping_threshold_param_and_formats(client, fake_rawpy):
    img = np.full((50, 50, 3), 240, np.uint8)
    fake_rawpy["rgb"] = img
    editor._base_cache.clear()
    off = post(client, "clipping", adjustments={"color_temp": 6500}).get_json()
    on = post(client, "clipping", adjustments={"color_temp": 6500}, highlight=235).get_json()
    assert off["percent"]["highlights"] == 0 and on["percent"]["highlights"] == 100
    raw = post(client, "clipping", adjustments={"color_temp": 6500}, highlight=235, format="png")
    assert raw.mimetype == "image/png" and raw.headers["X-Clipping-Highlights"] == "100.0"
    bits = post(client, "clipping", adjustments={"color_temp": 6500}, highlight=235, format="bitmask").get_json()
    mask = np.unpackbits(np.frombuffer(base64.b64decode(bits["masks"]["highlights"]), np.uint8)
                         .reshape(50, -1), axis=1)[:, :50]
    assert mask.all()
    assert post(client, "clipping", shadow=250, highlight=10).status_code == 400
    assert post(client, "clipping", format="bmp").status_code == 400


# --- histogram ---------------------------------------------------------

def test_histogram_endpoint(client):
    d = post(client, "histogram", adjustments={"color_temp": 6500}, stride=1).get_json()
    assert len(d["r"]) == 256 and sum(d["lum"]) == 480 * 720
    assert set(d["clipping"]) >= {"shadows", "highlights"}
    d64 = post(client, "histogram", adjustments={}, bins=64).get_json()
    assert len(d64["g"]) == 64
    assert post(client, "histogram", bins=100).status_code == 400


# --- guards ------------------------------------------------------------

def test_requires_json_and_same_origin(client):
    r = client.post("/api/develop/auto_tone", data="path=x", content_type="text/plain")
    assert r.status_code == 415
    r = client.post("/api/develop/auto_tone", json={"path": "IMG_0001.CR2"},
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
