"""Group G: targeted adjustment sampling, soft proofing, reference view, Guided Upright."""

import struct

import numpy as np
import pytest

import develop_extras
import editor
import softproof
import targeted
import transform


# --- helpers -----------------------------------------------------------------

def _s15(v):
    return struct.pack(">i", int(round(v * 65536)))


def narrow_profile(path):
    """A matrix/TRC RGB profile whose primaries are pulled toward white (a small gamut)."""
    cols = np.array([[0.4360747, 0.2225045, 0.0139322],
                     [0.3850649, 0.7168786, 0.0971045],
                     [0.1430804, 0.0606169, 0.7141733]])
    white = cols.sum(axis=0)
    cols = 0.55 * cols + 0.15 * white

    def xyz(v):
        return b"XYZ \0\0\0\0" + b"".join(_s15(c) for c in v)

    gamma = b"curv\0\0\0\0" + struct.pack(">I", 1) + struct.pack(">H", int(2.2 * 256)) + b"\0\0"
    text = b"narrow test"
    desc = b"desc\0\0\0\0" + struct.pack(">I", len(text) + 1) + text + b"\0" + b"\0" * 78
    tags = [(b"desc", desc), (b"wtpt", xyz(white)), (b"rXYZ", xyz(cols[0])), (b"gXYZ", xyz(cols[1])),
            (b"bXYZ", xyz(cols[2])), (b"rTRC", gamma), (b"gTRC", gamma), (b"bTRC", gamma),
            (b"cprt", b"text\0\0\0\0test\0")]
    offset = 128 + 4 + 12 * len(tags)
    table, body = b"", b""
    for sig, data in tags:
        pad = (-len(data)) % 4
        table += sig + struct.pack(">II", offset + len(body), len(data))
        body += data + b"\0" * pad
    header = bytearray(128)
    header[8:12] = struct.pack(">I", 0x02400000)
    header[12:16] = b"mntr"
    header[16:20] = b"RGB "
    header[20:24] = b"XYZ "
    header[36:40] = b"acsp"
    header[68:80] = b"".join(_s15(v) for v in (0.9642, 1.0, 0.8249))
    blob = bytes(header) + struct.pack(">I", len(tags)) + table + body
    blob = struct.pack(">I", len(blob)) + blob[4:]
    path.write_bytes(blob)
    return path


@pytest.fixture
def icc_dir(tmp_path, monkeypatch):
    d = tmp_path / "icc"
    d.mkdir()
    narrow_profile(d / "narrow.icc")
    (d / "junk.icc").write_bytes(b"not a profile")
    monkeypatch.setenv("PHOTO_ROOM_ICC_DIR", str(d))
    return d


def _narrow_id():
    return next(p["id"] for p in softproof.list_profiles() if p["name"] == "narrow test")


# --- targeted adjustment -------------------------------------------------------

def test_targeted_sample_finds_band_and_level():
    img = np.zeros((20, 20, 3), np.float32)
    img[:, :10] = (0.9, 0.1, 0.1)         # red
    img[:, 10:] = (0.4, 0.4, 0.4)         # grey
    red = targeted.sample(img, 0.2, 0.5)
    assert red["band"]["name"] == "red"
    assert red["level"] == pytest.approx(69, abs=1)
    grey = targeted.sample(img, 0.9, 0.5)
    assert grey["band"] is None                 # a grey has no colour band to move
    assert grey["level"] == pytest.approx(102, abs=1)


def test_band_for_hue_wraps_around():
    assert targeted.band_for_hue(355)[1] == "red"
    assert targeted.band_for_hue(125)[1] == "green"
    assert targeted.band_for_hue(300)[1] in ("purple", "magenta")


def test_targeted_rejects_non_finite():
    with pytest.raises(ValueError):
        targeted.sample(np.zeros((4, 4, 3)), float("nan"), 0.5)


# --- soft proofing ------------------------------------------------------------

def test_profile_list_has_srgb_and_skips_junk(icc_dir):
    names = [p["name"] for p in softproof.list_profiles()]
    assert names[0].startswith("sRGB")
    assert "narrow test" in names
    assert not any("junk" in n for n in names)   # junk.icc is not offered


def test_srgb_proof_is_nearly_identity():
    img = (np.random.default_rng(1).random((16, 16, 3)) * 255).astype(np.uint8)
    out, pct = softproof.proof(img, {"gamut_warning": True})
    assert np.abs(out.astype(int) - img).max() <= 2
    assert pct == 0.0


def test_narrow_gamut_flags_saturated_pixels(icc_dir):
    img = np.full((8, 8, 3), 128, np.uint8)
    img[:, :4] = (255, 0, 0)                      # outside the small gamut
    settings = {"profile": _narrow_id(), "gamut_warning": True}
    out, pct = softproof.proof(img, settings)
    assert pct == pytest.approx(50.0, abs=1.0)
    assert tuple(out[0, 0]) == softproof.ALARM
    assert tuple(out[0, 7]) != softproof.ALARM
    plain, none = softproof.proof(img, {"profile": _narrow_id()})
    assert none == 0.0
    assert plain[0, 0, 0] < 255 or plain[0, 0, 1] > 0          # red was pulled into gamut


def test_simulate_paper_changes_the_white(icc_dir):
    img = np.full((4, 4, 3), 255, np.uint8)
    a, _ = softproof.proof(img, {"profile": _narrow_id(), "intent": "relative"})
    b, _ = softproof.proof(img, {"profile": _narrow_id(), "simulate_paper": True})
    assert a.shape == b.shape == img.shape


def test_settings_are_coerced():
    s = softproof.normalise_settings({"intent": "bogus", "enabled": "yes", "profile": 7, "bpc": False})
    assert s["intent"] == "relative" and s["enabled"] is False and s["profile"] == "srgb" and s["bpc"] is False


def test_unknown_profile_is_refused():
    with pytest.raises(ValueError):
        softproof.resolve_profile("/etc/passwd")


# --- Guided Upright -----------------------------------------------------------

def test_guided_upright_levels_from_two_tilted_lines():
    img = np.zeros((400, 600, 3), np.float32)
    # Two "horizon" guides tilted about 3 degrees (clockwise down to the right).
    drop = 600 * np.tan(np.radians(3)) / 400
    guides = [{"x1": 0.0, "y1": 0.30, "x2": 1.0, "y2": 0.30 + drop},
              {"x1": 0.0, "y1": 0.70, "x2": 1.0, "y2": 0.70 + drop}]
    res = transform.upright(img, "level", guides)
    assert res["info"]["guided"] is True and res["info"]["horizontal"] == 2
    assert res["xf_rotate"] == pytest.approx(-3.0, abs=0.6)


def test_guided_upright_vertical_needs_only_two_lines():
    img = np.zeros((400, 600, 3), np.float32)
    # Verticals converging toward the top, as when tilting a camera up.
    guides = [{"x1": 0.30, "y1": 1.0, "x2": 0.38, "y2": 0.0},
              {"x1": 0.70, "y1": 1.0, "x2": 0.62, "y2": 0.0}]
    res = transform.upright(img, "vertical", guides)
    assert res["xf_vertical"] != 0


def test_guided_upright_ignores_junk_guides():
    img = np.zeros((100, 100, 3), np.float32)
    res = transform.upright(img, "full", [{"x1": "a"}, None, {"x1": .5, "y1": .5, "x2": .5, "y2": .5}])
    assert res["info"]["lines"] == 0 and res["xf_rotate"] == 0


# --- sidecar ------------------------------------------------------------------

def test_extras_roundtrip_and_cleanup(tmp_path):
    photo = tmp_path / "IMG_1.CR2"
    photo.write_bytes(b"x")
    saved = develop_extras.save(photo, {
        "upright_guides": [{"x1": 0, "y1": 0.2, "x2": 1, "y2": 0.25}, {"x1": 0.5}, "bad"] * 3,
        "softproof": {"enabled": True, "profile": "srgb", "intent": "perceptual"},
        "reference": "other/IMG_2.CR2", "junk": 1})
    assert len(saved["upright_guides"]) == 2      # the first MAX_GUIDES entries, junk dropped
    assert "junk" not in saved
    assert develop_extras.load(photo) == saved
    assert develop_extras.sidecar_path(photo).name == "IMG_1.CR2.develop.json"
    develop_extras.save(photo, {})
    assert not develop_extras.sidecar_path(photo).exists()      # nothing to remember, no file


def test_corrupt_sidecar_reads_as_empty(tmp_path):
    photo = tmp_path / "IMG_1.CR2"
    develop_extras.sidecar_path(photo).write_text("{not json")
    assert develop_extras.load(photo) == {}


# --- HTTP ---------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch, fake_rawpy):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    for name in ("IMG_0001.CR2", "IMG_0002.CR2"):
        (tmp_path / name).write_bytes(b"fake-cr2")
    rgb = np.zeros((120, 180, 3), np.uint8)
    rgb[:, :90] = (200, 40, 40)
    rgb[:, 90:] = (100, 100, 100)
    fake_rawpy["rgb"] = rgb
    fake_rawpy["whitebalance"] = [2.0, 1.0, 1.0, 1.0]
    editor._base_cache.clear()
    editor._decode_cache.clear()
    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        yield c


def test_targeted_endpoint(client):
    r = client.post("/api/develop/targeted", json={"path": "IMG_0001.CR2", "x": 0.2, "y": 0.5})
    assert r.status_code == 200
    assert r.get_json()["band"]["name"] in ("red", "orange", "magenta")
    grey = client.post("/api/develop/targeted", json={"path": "IMG_0001.CR2", "x": 0.9, "y": 0.5}).get_json()
    assert grey["band"] is None
    assert client.post("/api/develop/targeted", json={"path": "IMG_0001.CR2", "x": 3}).status_code == 400


def test_softproof_endpoint(client, icc_dir):
    prof = client.get("/api/develop/softproof/profiles").get_json()
    assert {"relative", "absolute"} <= set(prof["intents"])
    assert any(p["name"] == "narrow test" for p in prof["profiles"])
    r = client.post("/api/develop/softproof", json={
        "path": "IMG_0001.CR2",
        "settings": {"profile": _narrow_id(), "gamut_warning": True, "simulate_paper": True}})
    body = r.get_json()
    assert r.status_code == 200 and body["image"].startswith("data:image/jpeg;base64,")
    assert body["out_of_gamut"] > 0
    bad = client.post("/api/develop/softproof", json={"path": "IMG_0001.CR2", "settings": {"profile": "icc:nope"}})
    assert bad.status_code == 400


def test_reference_endpoint(client):
    r = client.post("/api/develop/reference", json={"path": "IMG_0002.CR2"})
    assert r.status_code == 200 and r.get_json()["name"] == "IMG_0002.CR2"
    assert client.post("/api/develop/reference", json={"path": "../x"}).status_code == 400


def test_extras_endpoint_persists_to_sidecar(client, tmp_path):
    guides = [{"x1": 0.1, "y1": 0.1, "x2": 0.1, "y2": 0.9}]
    r = client.post("/api/develop/extras", json={"path": "IMG_0001.CR2", "extras": {"upright_guides": guides}})
    assert r.get_json()["upright_guides"][0]["x2"] == 0.1
    client.post("/api/develop/extras", json={"path": "IMG_0001.CR2", "extras": {"reference": "IMG_0002.CR2"}})
    got = client.get("/api/develop/extras?path=IMG_0001.CR2").get_json()
    assert got["reference"] == "IMG_0002.CR2" and len(got["upright_guides"]) == 1     # merged, not replaced
    assert (tmp_path / "IMG_0001.CR2.develop.json").exists()
    client.post("/api/develop/extras", json={"path": "IMG_0001.CR2", "extras": {"upright_guides": [], "reference": ""}})
    assert not (tmp_path / "IMG_0001.CR2.develop.json").exists()


def test_upright_endpoint_accepts_guides(client):
    guides = [{"x1": 0, "y1": 0.3, "x2": 1, "y2": 0.33}, {"x1": 0, "y1": 0.7, "x2": 1, "y2": 0.73}]
    r = client.post("/api/upright", json={"path": "IMG_0001.CR2", "mode": "level", "guides": guides})
    body = r.get_json()
    assert r.status_code == 200 and body["detected"]["guided"] is True
    assert client.post("/api/upright", json={"path": "IMG_0001.CR2", "guides": "x"}).status_code == 400
