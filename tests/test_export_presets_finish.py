"""
M9: export presets, output sharpening, watermark and the batch export queue.

Unit tests for each piece, endpoint tests, and the metadata guarantee: the
presets + sharpening + watermark paths must still carry every original tag
(real exiftool, before/after comparison). Skipped without exiftool, like the
other export tests.
"""

import json
import time

import numpy as np
import pytest
from PIL import Image

import editor
import export_api
import export_finish
import export_presets
import exporter
from exporter import ExportOptions, export_photo
from test_export import assert_metadata_preserved, make_photo
from test_export import pytestmark  # noqa: F401  (skip without exiftool)


def noise(h=60, w=90, seed=1):
    rng = np.random.default_rng(seed)
    return np.clip(0.5 + 0.1 * rng.standard_normal((h, w, 3)), 0, 1).astype(np.float32)


def lap_energy(a):
    g = a.mean(axis=2)
    return float(np.abs(g[1:-1, 1:-1] * 4 - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:]).mean())


# ---------------------------------------------------------------------------
# Sharpening
# ---------------------------------------------------------------------------

def test_sharpen_off_is_identity():
    a = noise()
    assert export_finish.sharpen(a, None) is a
    assert export_finish.sharpen(a, {"for": "", "amount": "high"}) is a


@pytest.mark.parametrize("medium", export_finish.MEDIA)
def test_sharpen_increases_local_contrast_in_every_medium(medium):
    a = noise()
    out = export_finish.sharpen(a, {"for": medium, "amount": "standard"})
    assert out.shape == a.shape and 0.0 <= out.min() and out.max() <= 1.0
    assert lap_energy(out) > lap_energy(a)


def test_sharpen_amounts_are_ordered():
    a = noise()
    e = [lap_energy(export_finish.sharpen(a, {"for": "screen", "amount": lvl})) for lvl in export_finish.AMOUNTS]
    assert e[0] < e[1] < e[2]


def test_sharpen_leaves_flat_areas_and_colour_alone():
    flat = np.full((40, 40, 3), [0.2, 0.5, 0.7], dtype=np.float32)
    assert np.allclose(export_finish.sharpen(flat, {"for": "matte", "amount": "high"}), flat, atol=1e-5)


@pytest.mark.parametrize("spec", [{"for": "paper"}, {"for": "screen", "amount": "max"}, "yes"])
def test_bad_sharpen_is_rejected(spec):
    with pytest.raises(ValueError):
        export_finish.normalize_sharpen(spec)


def test_sharpening_runs_after_resize(tmp_path):
    src = tmp_path / "a.png"
    Image.fromarray((noise(100, 200) * 255).astype(np.uint8)).save(src)
    plain = export_photo(src, tmp_path / "p", ExportOptions(format="png", long_edge=100))
    sharp = export_photo(src, tmp_path / "s", ExportOptions(format="png", long_edge=100,
                                                            sharpen={"for": "screen", "amount": "high"}))
    a, b = (np.asarray(Image.open(p)) for p in (plain, sharp))
    assert a.shape == b.shape == (50, 100, 3) and not np.array_equal(a, b)


# ---------------------------------------------------------------------------
# Watermark
# ---------------------------------------------------------------------------

def wm_text(**kw):
    return {"type": "text", "text": "HIEMENZ", "size": 10, "opacity": 100, "position": "br",
            "margin": 2, "color": "#ffffff", **kw}


def test_no_watermark_is_identity():
    a = noise()
    assert export_finish.apply_watermark(a, None) is a
    assert export_finish.apply_watermark(a, {"type": ""}) is a


def test_text_watermark_marks_only_its_corner():
    base = np.zeros((100, 200, 3), dtype=np.float32)
    out = export_finish.apply_watermark(base, wm_text())
    changed = np.argwhere(out.max(axis=2) > 0)
    assert len(changed)
    ys, xs = changed[:, 0], changed[:, 1]
    assert ys.min() > 50 and xs.min() > 100  # bottom-right quadrant only


@pytest.mark.parametrize("pos,quadrant", [("tl", (0, 0)), ("tr", (0, 1)), ("bl", (1, 0)), ("br", (1, 1))])
def test_watermark_positions(pos, quadrant):
    out = export_finish.apply_watermark(np.zeros((100, 200, 3), np.float32), wm_text(position=pos, size=8))
    ys, xs = np.nonzero(out.max(axis=2))
    assert (ys.mean() > 50) == bool(quadrant[0]) and (xs.mean() > 100) == bool(quadrant[1])


def test_watermark_opacity_scales_the_mark():
    base = np.zeros((100, 200, 3), dtype=np.float32)
    full = export_finish.apply_watermark(base, wm_text(opacity=100)).max()
    half = export_finish.apply_watermark(base, wm_text(opacity=50)).max()
    assert full > 0.9 and 0.4 < half < 0.6
    assert export_finish.apply_watermark(base, wm_text(opacity=0)).max() == 0


def test_watermark_margin_moves_the_mark_inward():
    base = np.zeros((100, 200, 3), dtype=np.float32)
    near = np.nonzero(export_finish.apply_watermark(base, wm_text(margin=0)).max(axis=2))
    far = np.nonzero(export_finish.apply_watermark(base, wm_text(margin=10)).max(axis=2))
    assert far[1].max() < near[1].max() and far[0].max() < near[0].max()


def test_watermark_size_scales_with_the_picture():
    small = np.nonzero(export_finish.apply_watermark(np.zeros((50, 100, 3), np.float32), wm_text()).max(axis=2))
    big = np.nonzero(export_finish.apply_watermark(np.zeros((200, 400, 3), np.float32), wm_text()).max(axis=2))
    assert (big[1].max() - big[1].min()) > 2.5 * (small[1].max() - small[1].min())


def test_image_watermark(tmp_path):
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (40, 20), (255, 0, 0, 255)).save(logo)
    spec = {"type": "image", "image": str(logo), "scale": 20, "opacity": 100, "position": "tl", "margin": 0}
    out = export_finish.apply_watermark(np.zeros((100, 200, 3), np.float32), spec)
    assert out[0, 0].tolist() == pytest.approx([1.0, 0, 0], abs=0.02)
    assert out[90, 190].max() == 0
    assert (out[..., 0] > 0.5).sum() == pytest.approx(40 * 20, rel=0.15)  # 40 px wide logo


def test_transparent_logo_pixels_stay_transparent(tmp_path):
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (20, 20), (255, 255, 255, 0)).save(logo)
    spec = {"type": "image", "image": str(logo), "scale": 20, "opacity": 100}
    base = noise()
    assert np.allclose(export_finish.apply_watermark(base, spec), base)


@pytest.mark.parametrize("spec", [
    {"type": "text", "text": "  "}, {"type": "text", "text": "x", "position": "zz"},
    {"type": "text", "text": "x", "opacity": 101}, {"type": "text", "text": "x", "color": "red"},
    {"type": "text", "text": "x", "size": "big"}, {"type": "image", "image": ""},
    {"type": "image", "image": "/nonexistent/logo.png"}, {"type": "sticker"}, "text",
])
def test_bad_watermarks_are_rejected(spec):
    with pytest.raises(ValueError):
        export_finish.normalize_watermark(spec)


def test_unreadable_watermark_image_is_rejected(tmp_path):
    bad = tmp_path / "x.png"
    bad.write_text("not an image")
    with pytest.raises(ValueError, match="readable"):
        export_finish.normalize_watermark({"type": "image", "image": str(bad)})


def test_unknown_font_falls_back():
    assert export_finish.load_font("No Such Font", 20) is not None


def test_watermark_only_on_the_export(tmp_path):
    src = make_photo(tmp_path / "a.jpg")
    before = src.read_bytes()
    out = export_photo(src, tmp_path / "o", ExportOptions(format="png", watermark=wm_text()))
    plain = export_photo(src, tmp_path / "p", ExportOptions(format="png"))
    assert not np.array_equal(np.asarray(Image.open(out)), np.asarray(Image.open(plain)))
    assert src.read_bytes() == before


def test_sixteen_bit_png_gets_finishing_too(tmp_path):
    src = make_photo(tmp_path / "a.jpg")
    out = export_photo(src, tmp_path / "o", ExportOptions(
        format="png", bit_depth=16, sharpen={"for": "glossy", "amount": "low"}, watermark=wm_text()))
    assert out.read_bytes()[24] == 16  # IHDR bit depth


# ---------------------------------------------------------------------------
# Metadata guarantee through the new paths
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("src_ext", [".jpg", ".tif"])
@pytest.mark.parametrize("fmt,depth", [("jpeg", 8), ("png", 8), ("png", 16)])
def test_preset_sharpen_and_watermark_keep_every_tag(tmp_path, src_ext, fmt, depth):
    src = make_photo(tmp_path / f"IMG_0001{src_ext}")
    settings = export_presets.clean_settings({
        "format": fmt, "bit_depth": depth, "quality": 85, "long_edge": 100,
        "sharpen": {"for": "matte", "amount": "high"}, "watermark": wm_text(),
    })
    out = export_photo(src, tmp_path / "o", export_presets.options_from_payload(settings))
    assert_metadata_preserved(src, out)


@pytest.mark.parametrize("name", list(export_presets.BUILTINS))
def test_every_builtin_preset_keeps_every_tag(tmp_path, name, fake_rawpy):
    ref = make_photo(tmp_path / "ref.tif")
    src = make_photo(tmp_path / "IMG_0001.tif")
    src = src.rename(tmp_path / "IMG_0001.CR2")  # a "raw": colour space comes from the decoder
    opts = export_presets.options_from_payload(export_presets.get_preset(name))
    out = export_photo(src, tmp_path / "o", opts)
    assert_metadata_preserved(ref, out)


def test_image_watermark_keeps_every_tag(tmp_path):
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (30, 30), (0, 0, 255, 200)).save(logo)
    src = make_photo(tmp_path / "IMG_0001.jpg")
    out = export_photo(src, tmp_path / "o", ExportOptions(
        format="jpeg", watermark={"type": "image", "image": str(logo)}))
    assert_metadata_preserved(src, out)


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

def test_builtin_presets_are_the_documented_four():
    p = {x["name"]: x["settings"] for x in export_presets.list_presets() if x["builtin"]}
    web = p["Web 2048px JPEG q80 sRGB"]
    assert (web["format"], web["quality"], web["long_edge"], web["colorspace"]) == ("jpeg", 80, 2048, "srgb")
    assert p["Full-res JPEG q92"]["quality"] == 92 and p["Full-res JPEG q92"]["long_edge"] is None
    assert (p["Full-res 16-bit PNG"]["format"], p["Full-res 16-bit PNG"]["bit_depth"]) == ("png", 16)
    assert p["Print AdobeRGB"]["colorspace"] == "adobergb"
    for s in p.values():
        export_presets.options_from_payload(s)  # all valid


def test_save_load_overwrite_delete_roundtrip(tmp_path):
    settings = {"format": "png", "bit_depth": 16, "template": "{seq:04d}", "dest": "/x/out",
                "sharpen": {"for": "glossy", "amount": "low"}, "watermark": wm_text()}
    assert export_presets.save_preset("  My   print ", settings) == "My print"
    got = export_presets.get_preset("My print")
    assert got["bit_depth"] == 16 and got["dest"] == "/x/out" and got["sharpen"] == {"for": "glossy", "amount": "low"}
    assert got["watermark"]["text"] == "HIEMENZ"
    export_presets.save_preset("my PRINT", {"format": "jpeg"})  # case-insensitive overwrite
    user = [p for p in export_presets.list_presets() if not p["builtin"]]
    assert [p["name"] for p in user] == ["my PRINT"] and user[0]["settings"]["format"] == "jpeg"
    assert export_presets.delete_preset("my PRINT") is True
    assert export_presets.delete_preset("my PRINT") is False
    assert export_presets.get_preset("my PRINT") is None


def test_presets_survive_a_restart_file_on_disk():
    export_presets.save_preset("keep", {"format": "png"})
    data = json.loads(export_presets.presets_file().read_text())
    assert data["keep"]["format"] == "png"


def test_corrupt_preset_file_is_ignored():
    f = export_presets.presets_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("{not json")
    assert [p for p in export_presets.list_presets() if not p["builtin"]] == []
    export_presets.save_preset("fresh", {"format": "png"})
    assert export_presets.get_preset("fresh")["format"] == "png"


@pytest.mark.parametrize("name,settings", [
    ("", {}), ("x" * 61, {}), ("Print AdobeRGB", {}), ("web 2048PX jpeg q80 srgb", {}),
    ("ok", {"format": "gif"}), ("ok", {"quality": 0}), ("ok", {"format": "jpeg", "bit_depth": 16}),
    ("ok", {"watermark": {"type": "text", "text": ""}}), ("ok", {"sharpen": {"for": "x"}}),
])
def test_bad_presets_are_rejected(name, settings):
    with pytest.raises(ValueError):
        export_presets.save_preset(name, settings)


def test_builtin_cannot_be_deleted():
    with pytest.raises(ValueError):
        export_presets.delete_preset("Print AdobeRGB")


# ---------------------------------------------------------------------------
# Endpoints and the queue
# ---------------------------------------------------------------------------

@pytest.fixture
def lib(tmp_path, monkeypatch, fake_rawpy):
    root = tmp_path / "shoot"
    root.mkdir()
    monkeypatch.setattr(editor, "LIBRARY_ROOT", root)
    for n in (1, 2, 3):
        make_photo(root / f"IMG_000{n}.tif").rename(root / f"IMG_000{n}.CR2")
    with export_api._lock:
        export_api._jobs.clear()
    export_api._loaded_for = None
    return root


@pytest.fixture
def client(lib):
    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        yield c


def start(client, out, **kw):
    body = {"scope": "current", "path": "IMG_0001.CR2", "dest": str(out), "format": "png"}
    body.update(kw)
    return client.post("/api/export", json=body)


def wait(client, job_id, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        s = client.get(f"/api/export/status/{job_id}").get_json()
        if s["state"] not in ("running", "queued"):
            return s
        time.sleep(0.05)
    raise AssertionError("export did not finish")


def wait_for(predicate, timeout=30, what="condition"):
    """Poll until ``predicate()`` is truthy; fail loudly instead of hanging."""
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def test_defaults_advertise_sharpen_and_watermark_options(client):
    d = client.get("/api/export/defaults").get_json()
    assert d["sharpen"] == {"for": ["screen", "matte", "glossy"], "amount": ["low", "standard", "high"]}
    assert "br" in d["watermark_positions"]


def test_dialog_has_the_new_controls(client):
    html = client.get("/").get_data(as_text=True)
    for ident in ("ex-preset", "ex-preset-save", "ex-sharp", "ex-wm", "ex-wm-text", "ex-wm-image", "ex-queue"):
        assert f'id="{ident}"' in html


def test_preset_endpoints_roundtrip(client):
    names = [p["name"] for p in client.get("/api/export/presets").get_json()["presets"]]
    assert names[:4] == list(export_presets.BUILTINS)
    r = client.post("/api/export/presets", json={"name": "Mine", "settings": {"format": "png", "bit_depth": 16}})
    assert r.status_code == 200 and "Mine" in [p["name"] for p in r.get_json()["presets"]]
    assert client.post("/api/export/presets", json={"name": "Print AdobeRGB", "settings": {}}).status_code == 400
    assert client.post("/api/export/presets", json={"name": "Bad", "settings": {"format": "gif"}}).status_code == 400
    assert client.post("/api/export/presets/delete", json={"name": "Print AdobeRGB"}).status_code == 400
    assert client.post("/api/export/presets/delete", json={"name": "Nope"}).status_code == 404
    assert client.post("/api/export/presets/delete", json={"name": "Mine"}).status_code == 200
    assert client.post("/api/export/presets", data="x", content_type="text/plain").status_code == 415


def test_export_with_sharpen_and_watermark_via_endpoint(client, tmp_path):
    r = start(client, tmp_path / "o", sharpen={"for": "screen", "amount": "standard"}, watermark=wm_text())
    assert r.status_code == 202
    s = wait(client, r.get_json()["id"])
    assert s["state"] == "done" and not s["failed"]
    out = tmp_path / "o" / "IMG_0001.png"
    plain = start(client, tmp_path / "p")
    wait(client, plain.get_json()["id"])
    assert not np.array_equal(np.asarray(Image.open(out)), np.asarray(Image.open(tmp_path / "p" / "IMG_0001.png")))


@pytest.mark.parametrize("extra", [
    {"sharpen": {"for": "paper"}}, {"watermark": {"type": "text", "text": ""}},
    {"watermark": {"type": "image", "image": "/nope.png"}},
])
def test_bad_finishing_options_are_400(client, tmp_path, extra):
    assert start(client, tmp_path / "o", **extra).status_code == 400


def test_endpoint_export_keeps_all_metadata_with_everything_on(client, lib, tmp_path):
    ref = make_photo(tmp_path / "ref.tif")
    r = start(client, tmp_path / "o", sharpen={"for": "matte", "amount": "high"}, watermark=wm_text(), long_edge=60)
    wait(client, r.get_json()["id"])
    assert_metadata_preserved(ref, tmp_path / "o" / "IMG_0001.png")


def test_queue_runs_batches_in_order_with_per_photo_results(client, tmp_path, monkeypatch):
    real = exporter.export_photo
    gate = {"open": False}

    def slow(*a, **k):
        wait_for(lambda: gate["open"], what="the test to open the gate")
        if a[0].name == "IMG_0002.CR2":
            raise RuntimeError("disk on fire")
        return real(*a, **k)

    monkeypatch.setattr(exporter, "export_photo", slow)
    first = start(client, tmp_path / "a", scope="folder").get_json()
    # The worker thread moves the first batch queued -> running asynchronously.
    # Until it has, the first batch itself still occupies queue position 1 and
    # the second would legitimately be position 2 (that was the flake), so wait
    # for the state the assertion below is about.
    wait_for(lambda: client.get(f"/api/export/status/{first['id']}").get_json()["state"] == "running",
             what="first batch to start running")
    second = start(client, tmp_path / "b").get_json()
    assert second["state"] == "queued" and second["queue_position"] == 1
    gate["open"] = True
    s1, s2 = wait(client, first["id"]), wait(client, second["id"])
    assert s1["state"] == "done" and s2["state"] == "done"
    assert [i["status"] for i in s1["items"]] == ["ok", "error", "ok"]
    assert "disk on fire" in s1["items"][1]["error"] and len(s1["failed"]) == 1
    assert all(i["output"] for i in s1["items"] if i["status"] == "ok")
    jobs = client.get("/api/export/jobs").get_json()["jobs"]
    assert [j["id"] for j in jobs] == [first["id"], second["id"]]


def test_cancel_a_running_batch_keeps_finished_photos(client, tmp_path, monkeypatch):
    real = exporter.export_photo
    seen = []
    ready = __import__("threading").Event()

    def one(*a, **k):
        seen.append(a[0].name)
        ready.wait(10)  # held until the test has asked for the cancel
        return real(*a, **k)

    monkeypatch.setattr(exporter, "export_photo", one)
    job = start(client, tmp_path / "o", scope="folder").get_json()
    wait_for(lambda: seen, what="the export worker to start the first photo")
    assert client.post(f"/api/export/cancel/{job['id']}").status_code == 200
    ready.set()
    s = wait(client, job["id"])
    assert s["state"] == "cancelled" and len(seen) == 1
    assert [i["status"] for i in s["items"]] == ["ok", "cancelled", "cancelled"]
    assert [p.name for p in (tmp_path / "o").iterdir()] == ["IMG_0001.png"]


def test_cancel_a_queued_batch_and_unknowns(client, tmp_path, monkeypatch):
    monkeypatch.setattr(export_api, "_ensure_worker", lambda: None)
    job = start(client, tmp_path / "o").get_json()
    assert job["state"] == "queued"
    r = client.post(f"/api/export/cancel/{job['id']}")
    assert r.get_json()["state"] == "cancelled" and r.get_json()["items"][0]["status"] == "cancelled"
    assert client.post(f"/api/export/cancel/{job['id']}").status_code == 409
    assert client.post("/api/export/cancel/nope").status_code == 404
    assert client.post("/api/export/resume/nope").status_code == 404
    assert client.post(f"/api/export/resume/{job['id']}").status_code == 409


def test_queue_is_bounded(client, tmp_path, monkeypatch):
    monkeypatch.setattr(export_api, "_ensure_worker", lambda: None)
    for _ in range(export_api.MAX_QUEUED):
        assert start(client, tmp_path / "o").status_code == 202
    assert start(client, tmp_path / "o").status_code == 409


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_interrupted_batch_resumes_after_an_editor_restart(client, tmp_path, monkeypatch):
    real = exporter.export_photo
    calls = []

    def crash_after_one(*a, **k):
        calls.append(a[0].name)
        if len(calls) == 2:
            raise SystemExit  # not an Exception: the "process dies" mid-photo; worker thread ends
        return real(*a, **k)

    monkeypatch.setattr(exporter, "export_photo", crash_after_one)
    job = start(client, tmp_path / "o", scope="folder").get_json()
    wait_for(lambda: len(calls) >= 2, 60, "the second export call")
    saved = json.loads(export_api.queue_file().read_text())["jobs"][0]
    assert saved["state"] == "running" and [i["status"] for i in saved["items"]] == ["ok", "pending", "pending"]

    # "Restart": forget everything in memory, then load from disk.
    monkeypatch.setattr(exporter, "export_photo", real)
    with export_api._lock:
        export_api._jobs.clear()
        export_api._worker = None
    export_api._loaded_for = None
    s = client.get(f"/api/export/status/{job['id']}").get_json()
    assert s["state"] == "interrupted" and s["done"] == 1
    r = client.post(f"/api/export/resume/{job['id']}")
    assert r.status_code == 202
    s = wait(client, job["id"])
    assert s["state"] == "done" and [i["status"] for i in s["items"]] == ["ok", "ok", "ok"]
    assert sorted(p.name for p in (tmp_path / "o").iterdir()) == ["IMG_0001.png", "IMG_0002.png", "IMG_0003.png"]
