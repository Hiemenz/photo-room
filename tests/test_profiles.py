"""Camera profiles: built-ins, DCP / ICC parsing, the pipeline hook, cache keys, XMP round trip, API."""

import base64
import struct

import numpy as np
import pytest
from PIL import ImageCms

import colour
import editor
import profiles as P
import stagecache


@pytest.fixture(autouse=True)
def lib(tmp_path, monkeypatch):
    monkeypatch.setenv("PHOTO_ROOM_PROFILE_DIR", str(tmp_path / "profiles"))
    editor._stage_cache.clear()


# ---------------------------------------------------------------------------
# A minimal DCP writer (the container is "IIRC" + one IFD)
# ---------------------------------------------------------------------------

def make_dcp(name="Test Profile", curve=None, hsm=None, look=None, hsm_enc=None, matrices=True, big=False):
    entries = []          # (tag, type, count, payload bytes)
    o = ">" if big else "<"

    def add(tag, typ, vals):
        if typ == 2:
            data = vals.encode() + b"\0"
            entries.append((tag, 2, len(data), data))
        elif typ == 10:
            data = b"".join(struct.pack(o + "ii", int(round(v * 10000)), 10000) for v in vals)
            entries.append((tag, 10, len(vals), data))
        else:
            fmt = {3: "H", 4: "I", 11: "f"}[typ]
            entries.append((tag, typ, len(vals), struct.pack(o + fmt * len(vals), *vals)))

    add(50936, 2, name)
    add(50708, 2, "Test Camera")
    if matrices:
        add(50721, 10, [0.7, -0.1, -0.1, -0.3, 1.1, 0.1, -0.05, 0.1, 0.6])
        add(50964, 10, [0.8, 0.1, 0.05, 0.3, 0.7, 0.0, 0.0, 0.0, 0.8])
    if curve is not None:
        add(50940, 11, [v for p in curve for v in p])
    for tag_dims, tag_data, tab in ((50937, 50938, hsm), (50981, 50982, look)):
        if tab is not None:
            dims, data = tab
            add(tag_dims, 4, list(dims))
            add(tag_data, 11, [float(x) for x in np.asarray(data).reshape(-1)])
    if hsm_enc is not None:
        add(51107, 4, [hsm_enc])
    entries.sort()
    ifd_len = 2 + 12 * len(entries) + 4
    off = 8 + ifd_len
    ifd = struct.pack(o + "H", len(entries))
    blobs = b""
    for tag, typ, n, data in entries:
        if len(data) <= 4:
            ifd += struct.pack(o + "HHI", tag, typ, n) + data.ljust(4, b"\0")
        else:
            ifd += struct.pack(o + "HHII", tag, typ, n, off + len(blobs))
            blobs += data + (b"\0" if len(data) % 2 else b"")
    ifd += struct.pack(o + "I", 0)
    head = (b"MM" + struct.pack(">HI", 0x4352, 8)) if big else (b"II" + struct.pack("<HI", 0x4352, 8))
    return head + ifd + blobs


def identity_hsm(h=6, s=2, v=1):
    d = np.zeros((v, h, s, 3), np.float32)
    d[..., 1] = 1.0
    d[..., 2] = 1.0
    return (h, s, v), d


def ramp(n=64):
    g = np.linspace(0, 1, n, dtype=np.float32)
    img = np.stack([np.tile(g, (n, 1)), np.tile(g[::-1, None], (1, n)), np.full((n, n), 0.4, np.float32)], axis=2)
    return img


def colourful():
    rng = np.random.default_rng(3)
    return rng.random((40, 40, 3)).astype(np.float32)


# ---------------------------------------------------------------------------
# Built-ins
# ---------------------------------------------------------------------------

def test_every_builtin_exists_and_is_listed():
    names = {p["name"] for p in P.list_profiles()}
    assert {"Standard", "Neutral", "Landscape", "Portrait", "Vivid", "Flat", "Monochrome"} <= names
    for pid in P.builtin_ids():
        assert P.get(pid).kind == "builtin" and P.exists(pid)


@pytest.mark.parametrize("space", ["srgb", "adobergb", "prophoto"])
@pytest.mark.parametrize("linear", [False, True])
def test_builtins_keep_black_white_and_grey_neutral(space, linear):
    g = np.linspace(0, 1, 33, dtype=np.float32)
    grey = np.stack([g, g, g], axis=1)[None]
    src = colour.to_linear(grey, space) if linear else grey
    for pid in P.builtin_ids():
        out = P.apply(src, P.get(pid), space, linear=linear)
        enc = colour.to_encoded(out, space) if linear else out
        assert np.allclose(enc[..., 0], enc[..., 1], atol=2e-3) and np.allclose(enc[..., 1], enc[..., 2], atol=2e-3)
        assert enc[0, 0].max() < 2e-3 and enc[0, -1].min() > 0.998


def test_builtin_looks_behave_as_named():
    img = colourful()
    std, vivid, flat, mono, neutral = (P.apply(img, P.get(f"builtin-{n}")) for n in
                                       ("standard", "vivid", "flat", "monochrome", "neutral"))

    def chroma(a):
        return (a.max(axis=2) - a.min(axis=2)).mean()

    assert chroma(vivid) > chroma(std) > chroma(neutral)
    assert chroma(mono) < 1e-3                       # luma-weighted, so a few 1e-4 of float error at most
    # contrast: spread of luminance about the middle
    mid = np.full((8, 8, 3), 0.25, np.float32)
    hi = np.full((8, 8, 3), 0.75, np.float32)
    assert P.apply(mid, P.get("builtin-vivid")).mean() < 0.25 < P.apply(mid, P.get("builtin-flat")).mean()
    assert P.apply(hi, P.get("builtin-vivid")).mean() > 0.75 > P.apply(hi, P.get("builtin-flat")).mean()


def test_landscape_boosts_green_and_portrait_calms_skin():
    green = np.full((4, 4, 3), (0.2, 0.6, 0.25), np.float32)
    skin = np.full((4, 4, 3), (0.8, 0.55, 0.45), np.float32)

    def sat(a):
        return float((a.max(axis=2) - a.min(axis=2)).mean() / a.max(axis=2).mean())

    # compare with a profile that has the same matrix and curve but no hue table
    base_l = P.get("builtin-neutral")
    assert sat(P.apply(green, P.get("builtin-landscape"))) > sat(P.apply(green, base_l))
    assert sat(P.apply(skin, P.get("builtin-portrait"))) < sat(P.apply(skin, P.get("builtin-standard")))


def test_apply_returns_a_new_array_and_never_mutates_input():
    img = colourful()
    keep = img.copy()
    out = P.apply(img, P.get("builtin-vivid"))
    assert out is not img and np.array_equal(img, keep) and out.dtype == np.float32


# ---------------------------------------------------------------------------
# DCP
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("big", [False, True])
def test_dcp_parse_reads_name_matrices_curve_and_tables(big):
    dims, data = identity_hsm()
    raw = make_dcp("My Look", curve=[(0, 0), (0.5, 0.7), (1, 1)], hsm=(dims, data), look=(dims, data), big=big)
    p = P.parse_dcp(raw)
    assert p.name == "My Look" and p.kind == "dcp" and p.info["camera"] == "Test Camera"
    assert p.info["color_matrix1"][0][0] == pytest.approx(0.7) and p.info["forward_matrix1"][1][1] == pytest.approx(0.7)
    assert p.info["matrices_applied"] is False
    assert p.curve[0].tolist() == [0, 0.5, 1] and p.curve[1][1] == pytest.approx(0.7)
    assert p.hue_sat.dims == (6, 2, 1) and p.look.dims == (6, 2, 1)


def test_dcp_curve_is_applied_in_linear_light():
    p = P.parse_dcp(make_dcp(curve=[(0, 0), (0.5, 0.7), (1, 1)]))
    lin = np.full((2, 2, 3), 0.5, np.float32)
    assert P.apply(lin, p, "srgb", linear=True) == pytest.approx(0.7, abs=1e-6)
    enc = colour.to_encoded(lin, "srgb")
    assert colour.to_linear(P.apply(enc, p, "srgb"), "srgb") == pytest.approx(0.7, abs=2e-3)


def test_identity_hue_sat_map_changes_nothing_and_a_shift_moves_hue():
    dims, data = identity_hsm(h=12)
    img = colourful()
    same = P.apply(img, P.parse_dcp(make_dcp(hsm=(dims, data), curve=[(0, 0), (1, 1)])), "srgb")
    assert np.abs(same - img).max() < 2e-3
    shifted = data.copy()
    shifted[..., 0] = 30.0                              # rotate every hue by 30 degrees
    out = P.apply(np.full((2, 2, 3), (0.8, 0.2, 0.2), np.float32),
                  P.parse_dcp(make_dcp(hsm=(dims, shifted), hsm_enc=1)), "srgb")
    r, g, b = out[0, 0]
    assert r > g > b and g > 0.4                        # red turned towards orange/yellow
    sat0 = data.copy()
    sat0[..., 1] = 0.0                                  # saturation scale zero = grey
    grey = P.apply(img, P.parse_dcp(make_dcp(hsm=(dims, sat0))), "srgb")
    assert (grey.max(axis=2) - grey.min(axis=2)).max() < 5e-3


def test_hue_table_value_scale_and_val_divisions():
    dims, data = identity_hsm(h=6, s=2, v=2)
    data[1, ..., 2] = 0.5                               # bright half of the value axis halved
    p = P.parse_dcp(make_dcp(hsm=(dims, data), hsm_enc=1))
    out = P.apply(np.full((2, 2, 3), (1.0, 0.0, 0.0), np.float32), p, "srgb")
    assert out[0, 0, 0] == pytest.approx(0.5, abs=0.02)


@pytest.mark.parametrize("mutate", [
    lambda r: b"",
    lambda r: b"XX" + r[2:],
    lambda r: r[:2] + b"\x00\x00" + r[4:],
    lambda r: r[:20],
    lambda r: r[:4] + struct.pack("<I", 10 ** 8) + r[8:],
])
def test_damaged_dcp_is_rejected_with_a_message(mutate):
    raw = make_dcp(curve=[(0, 0), (1, 1)])
    with pytest.raises(P.ProfileError):
        P.parse_dcp(mutate(raw))


def test_dcp_with_inconsistent_table_or_nothing_to_apply_is_rejected():
    dims, data = identity_hsm()
    with pytest.raises(P.ProfileError, match="size"):
        P.parse_dcp(make_dcp(hsm=((7, 2, 1), data)))
    with pytest.raises(P.ProfileError, match="no tone curve"):
        P.parse_dcp(make_dcp())
    with pytest.raises(P.ProfileError, match="not increasing"):
        P.parse_dcp(make_dcp(curve=[(0, 0), (0.6, 0.5), (0.5, 0.7), (1, 1)]))
    nan = data.copy()
    nan[0, 0, 0, 0] = np.nan
    with pytest.raises(P.ProfileError, match="invalid numbers"):
        P.parse_dcp(make_dcp(hsm=(dims, nan)))


def test_a_truncated_tag_does_not_read_past_the_file():
    raw = make_dcp(curve=[(0, 0), (0.5, 0.6), (1, 1)] * 1)
    with pytest.raises(P.ProfileError):
        P.parse_dcp(raw[:-6])


# ---------------------------------------------------------------------------
# ICC
# ---------------------------------------------------------------------------

def _icc(name):
    return ImageCms.ImageCmsProfile(ImageCms.createProfile(name)).tobytes()


def test_srgb_icc_profile_on_srgb_pixels_is_close_to_identity():
    p = P.parse_icc(_icc("sRGB"), "srgb.icc")
    assert p.kind == "icc" and p.icc is not None
    img = colourful()
    assert np.abs(P.apply(img, p, "srgb") - img).max() < 0.01


def test_a_gamma_icc_profile_reinterprets_the_colours():
    """A profile whose TRC is gamma 1.0 reads the encoded values as linear, so they come out brighter when re-encoded."""
    raw = bytearray(_icc("sRGB"))
    p = P.parse_icc(bytes(raw), "x.icc")
    p.icc.trc = [np.linspace(0, 1, 1024)] * 3                      # linear TRC, primaries unchanged
    out = P.apply(np.full((2, 2, 3), 0.5, np.float32), p, "srgb")
    assert out.mean() > 0.7


def test_lut_icc_and_non_rgb_profiles_are_refused():
    lab = ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB")).tobytes()
    with pytest.raises(P.ProfileError):
        P.parse_icc(lab)
    with pytest.raises(P.ProfileError, match="Not an ICC"):
        P.parse_icc(b"\0" * 300)


# ---------------------------------------------------------------------------
# Library and detection
# ---------------------------------------------------------------------------

def test_import_stores_beside_library_and_is_idempotent(tmp_path):
    raw = make_dcp("Studio", curve=[(0, 0), (0.5, 0.6), (1, 1)])
    a = P.import_bytes(raw, "studio.dcp")
    b = P.import_bytes(raw, "again.dcp")
    assert a == b and a["name"] == "Studio" and not a["builtin"]
    assert (tmp_path / "profiles" / f"{a['id']}.dcp").exists()
    assert P.get(a["id"]).curve is not None
    assert a["id"] in [p["id"] for p in P.list_profiles()]
    assert P.delete(a["id"]) and not P.exists(a["id"])
    with pytest.raises(P.ProfileNotFound):
        P.get(a["id"])


def test_import_detects_icc_by_content_and_rejects_junk():
    assert P.import_bytes(_icc("sRGB"), "cam.icc")["kind"] == "icc"
    with pytest.raises(P.ProfileError, match="Unrecognised"):
        P.import_bytes(b"hello world", "x.dcp")


def test_hostile_ids_do_not_touch_the_filesystem(tmp_path):
    for bad in ("../../etc/passwd", "..", "x" * 12, "", None):
        with pytest.raises(P.ProfileNotFound):
            P.get(bad)
        assert P.revision(bad) in ("", "missing")


# ---------------------------------------------------------------------------
# Pipeline hook and stage cache
# ---------------------------------------------------------------------------

def _adj(**kw):
    a = {"edit_version": 2}
    a.update(kw)
    return a


def test_profile_runs_before_the_edits_and_changes_the_render():
    base = colourful()
    plain = editor.apply_adjustments(base, 5500, _adj(color_temp=5500))
    prof = editor.apply_adjustments(base, 5500, _adj(color_temp=5500, camera_profile="builtin-vivid"))
    assert np.abs(plain - base).max() < 1e-3
    assert np.abs(prof - plain).mean() > 0.01
    # exposure applies on top of the profile: the profiled render reaches the same place from either order
    exp = editor.apply_adjustments(base, 5500, _adj(color_temp=5500, camera_profile="builtin-vivid", exposure=1.0))
    assert exp.mean() > prof.mean()


def test_linear_input_and_encoded_input_agree():
    base = colourful()
    adj = _adj(color_temp=5500, camera_profile="builtin-landscape", exposure=0.3)
    a = editor.apply_adjustments(base, 5500, dict(adj))
    b = editor.apply_adjustments(colour.to_linear(base, "srgb"), 5500, dict(adj, _linear_input=True))
    assert np.abs(a - b).max() < 0.01


def test_missing_profile_is_skipped_not_fatal():
    base = colourful()
    out = editor.apply_adjustments(base, 5500, _adj(color_temp=5500, camera_profile="0123456789ab"))
    assert np.abs(out - base).max() < 1e-3
    trimmed, warn = editor._without_missing_profile(_adj(camera_profile="0123456789ab"))
    assert trimmed["camera_profile"] == "" and "0123456789ab" in warn[0]


def test_legacy_edits_ignore_profiles():
    base = colourful()
    a = editor.apply_adjustments(base, 5500, {"edit_version": 1, "color_temp": 5500})
    b = editor.apply_adjustments(base, 5500, {"edit_version": 1, "color_temp": 5500, "camera_profile": "builtin-vivid"})
    assert np.array_equal(a, b)


def test_profile_is_in_the_cache_key_and_slider_drags_reuse_it(monkeypatch):
    k1 = stagecache.stage_keys(_adj(camera_profile="builtin-vivid"), 5500)
    k2 = stagecache.stage_keys(_adj(camera_profile="builtin-flat"), 5500)
    k0 = stagecache.stage_keys(_adj(), 5500)
    assert all(a != b for a, b in zip(k1, k2)) and all(a != b for a, b in zip(k0, k1))
    # an edit slider leaves the geometry (profile) key alone
    assert stagecache.stage_keys(_adj(camera_profile="builtin-vivid", exposure=1.0), 5500)[0] == k1[0]

    calls = []
    real = P.apply
    monkeypatch.setattr(P, "apply", lambda *a, **k: calls.append(1) or real(*a, **k))
    base = colourful()
    for exp in (0.0, 0.2, 0.4, 0.6):
        editor.apply_adjustments(base, 5500, _adj(color_temp=5500, camera_profile="builtin-vivid", exposure=exp),
                                 cache_id="photo-a")
    assert len(calls) == 1
    editor.apply_adjustments(base, 5500, _adj(color_temp=5500, camera_profile="builtin-flat"), cache_id="photo-a")
    assert len(calls) == 2


def test_replacing_an_imported_file_changes_its_revision():
    entry = P.import_bytes(make_dcp("A", curve=[(0, 0), (0.5, 0.6), (1, 1)]), "a.dcp")
    r1 = P.revision(entry["id"])
    P.delete(entry["id"])
    assert P.revision(entry["id"]) == "missing" != r1


def test_imported_profile_renders():
    entry = P.import_bytes(make_dcp("Contrasty", curve=[(0, 0), (0.25, 0.1), (0.75, 0.9), (1, 1)]), "c.dcp")
    base = np.full((8, 8, 3), 0.3, np.float32)
    out = editor.apply_adjustments(base, 5500, _adj(color_temp=5500, camera_profile=entry["id"]))
    assert out.mean() < 0.3


# ---------------------------------------------------------------------------
# XMP
# ---------------------------------------------------------------------------

def test_xmp_arguments_for_builtin_imported_foreign_and_none():
    args = P.xmp_arguments({"camera_profile": "builtin-landscape"})
    assert "-XMP-crs:CameraProfile=photo-room Landscape" in args and "-XMP-crs:CameraProfileDigest=" in args
    entry = P.import_bytes(make_dcp("Studio", curve=[(0, 0), (1, 1)]), "s.dcp")
    imp = P.xmp_arguments({"camera_profile": entry["id"]})
    assert "-XMP-crs:CameraProfile=Studio" in imp and f"-XMP-crs:CameraProfileDigest={entry['digest']}" in imp
    assert P.xmp_arguments({"camera_profile_foreign": "Adobe Standard"}) == ["-XMP-crs:CameraProfile=Adobe Standard"]
    assert P.xmp_arguments({}) == ["-XMP-crs:CameraProfile=", "-XMP-crs:CameraProfileDigest="]


def test_parse_xmp_resolves_builtin_digest_name_and_foreign():
    entry = P.import_bytes(make_dcp("Studio", curve=[(0, 0), (1, 1)]), "s.dcp")
    assert P.parse_xmp("photo-room Vivid", "-") == {"camera_profile": "builtin-vivid"}
    assert P.parse_xmp("Anything", entry["digest"]) == {"camera_profile": entry["id"]}
    assert P.parse_xmp("Studio", "-") == {"camera_profile": entry["id"]}
    assert P.parse_xmp("Adobe Color", "-") == {"camera_profile_foreign": "Adobe Color"}
    assert P.parse_xmp("-", "-") == {} and P.parse_xmp(None, None) == {}


def test_saving_writes_the_profile_and_reading_restores_it(tmp_path, fake_exiftool):
    raw = tmp_path / "IMG_0001.CR2"
    raw.write_bytes(b"raw")
    adj = editor.complete_adjustments({}, 5500)
    adj.update(camera_profile="builtin-portrait", exposure=0.5)
    editor.save_adjustments(raw, adj)
    written = [a for call in fake_exiftool.calls for a in call if a.startswith("-XMP-crs:CameraProfile=")]
    assert written and written[-1] == "-XMP-crs:CameraProfile=photo-room Portrait"
    back = editor.read_saved_adjustments(raw)
    assert back["camera_profile"] == "builtin-portrait" and back["exposure"] == 0.5


def test_a_lightroom_profile_is_preserved_on_save(tmp_path, fake_exiftool):
    raw = tmp_path / "IMG_0002.CR2"
    raw.write_bytes(b"raw")
    editor.save_adjustments(raw, editor.complete_adjustments({}, 5500))
    fake_exiftool.tags["CameraProfile"] = "Adobe Standard"
    back = editor.read_saved_adjustments(raw)
    assert back["camera_profile_foreign"] == "Adobe Standard" and "camera_profile" not in back
    editor.save_adjustments(raw, editor.complete_adjustments(back, 5500))
    assert fake_exiftool.tags["CameraProfile"] == "Adobe Standard"
    # choosing one of ours replaces it
    chosen = dict(editor.complete_adjustments(back, 5500), camera_profile="builtin-flat")
    chosen.pop("camera_profile_foreign", None)
    editor.save_adjustments(raw, chosen)
    assert fake_exiftool.tags["CameraProfile"] == "photo-room Flat"


def test_complete_adjustments_always_has_the_key():
    assert editor.complete_adjustments({}, 5500)["camera_profile"] == ""


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    editor.app.config["TESTING"] = True
    return editor.app.test_client()


def test_api_list_import_delete(client):
    listing = client.get("/api/profiles").get_json()["profiles"]
    assert any(p["id"] == "builtin-standard" for p in listing)
    b64 = base64.b64encode(make_dcp("Api", curve=[(0, 0), (1, 1)])).decode()
    r = client.post("/api/profiles/import", json={"name": "api.dcp", "data": b64})
    assert r.status_code == 200 and r.get_json()["profile"]["name"] == "Api"
    pid = r.get_json()["profile"]["id"]
    assert client.post("/api/profiles/import", json={"data": "not base64!!"}).status_code == 400
    bad = client.post("/api/profiles/import", json={"data": base64.b64encode(b"junk").decode()})
    assert bad.status_code == 400 and "error" in bad.get_json()
    assert client.post("/api/profiles/delete", json={"id": "builtin-vivid"}).status_code == 400
    assert client.post("/api/profiles/delete", json={"id": pid}).get_json()["ok"] is True
    assert not P.exists(pid)


# ---------------------------------------------------------------------------
# Raw only, like Lightroom: JPEG / TIFF / PNG / HEIC take no camera profile
# ---------------------------------------------------------------------------

def test_for_source_drops_the_profile_for_images_only(tmp_path):
    adj = {"camera_profile": "builtin-vivid", "exposure": 1.0}
    for name in ("a.jpg", "a.TIF", "a.png"):
        out = P.for_source(tmp_path / name, adj)
        assert out["camera_profile"] == "" and out["exposure"] == 1.0
    assert adj["camera_profile"] == "builtin-vivid"            # input untouched
    assert P.for_source(tmp_path / "a.cr2", adj) is adj
    assert P.for_source(tmp_path / "a.jpg", {"exposure": 1}) == {"exposure": 1}


def test_thumb_endpoint_rejects_images(client, tmp_path, monkeypatch):
    from PIL import Image
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    Image.new("RGB", (32, 24), (200, 100, 50)).save(tmp_path / "a.jpg")
    r = client.get("/api/profiles/thumb", query_string={"path": "a.jpg", "id": "builtin-vivid"})
    assert r.status_code == 400 and "raw" in r.get_json()["error"]
