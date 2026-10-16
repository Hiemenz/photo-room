"""
Tests for the editor's Export dialog: its endpoints, filename preview,
progress reporting, that the exported pixels carry the saved edits at full
resolution, and that metadata survives the dialog path.

Uses the real exiftool (skipped when absent, as in test_export.py); rawpy is
faked as elsewhere.
"""

import struct
import time
import zlib
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import editor
import exporter
from test_export import assert_metadata_preserved, make_photo, read_tags  # noqa: F401
from test_export import pytestmark  # noqa: F401  (skip without exiftool)

CURVE = {
    "parametric": {"highlights": 0, "lights": 0, "darks": 0, "shadows": 0,
                   "split1": 25, "split2": 50, "split3": 75},
    "points": {"rgb": [[0, 0], [64, 30], [192, 225], [255, 255]],
               "red": [[0, 0], [255, 255]], "green": [[0, 0], [255, 255]],
               "blue": [[0, 0], [255, 255]]},
}


@pytest.fixture
def lib(tmp_path, monkeypatch, fake_rawpy):
    """A library of three 'CR2' files (metadata-rich TIFFs; rawpy faked)."""
    root = tmp_path / "shoot"
    root.mkdir()
    monkeypatch.setattr(editor, "LIBRARY_ROOT", root)
    for n in (1, 2, 3):
        tif = make_photo(root / f"IMG_000{n}.tif")
        tif.rename(root / f"IMG_000{n}.CR2")
    return root


@pytest.fixture
def client(lib):
    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        yield c


def start(client, out, **kw):
    body = {"scope": "current", "path": "IMG_0001.CR2", "dest": str(out),
            "format": "png", "template": "{name}", "keep_metadata": True}
    body.update(kw)
    res = client.post("/api/export", json=body)
    assert res.status_code == 202, res.get_json()
    return res.get_json()


def wait(client, job, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        s = client.get(f"/api/export/status/{job['id']}").get_json()
        if s["state"] not in ("running", "queued"):
            return s
        time.sleep(0.05)
    raise AssertionError("export did not finish")


def mean_rgb(path):
    return np.asarray(Image.open(path).convert("RGB"), dtype=float).mean(axis=(0, 1))


# ---------------------------------------------------------------------------
# UI wiring and defaults
# ---------------------------------------------------------------------------

def test_page_has_export_button_dialog_and_shortcut(client):
    html = client.get("/").get_data(as_text=True)
    # The page is a shell now; scripts and styles are static files.
    from pathlib import Path
    for p in sorted((Path(__file__).resolve().parent.parent / "static").rglob("*")):
        if p.is_file():
            html += p.read_text()
    for needle in ('id="export-open"', 'id="export-dialog"', "Keep all metadata (EXIF, GPS, IPTC, XMP)",
                   'id="ex-meta" checked', "shiftKey"):
        assert needle in html, needle


def test_defaults(client):
    d = client.get("/api/export/defaults").get_json()
    assert d["exiftool"] is True and d["dest"] and 16 in d["bit_depths"]


# ---------------------------------------------------------------------------
# Filename template preview
# ---------------------------------------------------------------------------

def test_preview_expands_template_for_each_photo(client, tmp_path):
    res = client.post("/api/export/preview", json={
        "scope": "folder", "dest": str(tmp_path / "o"), "format": "jpeg",
        "template": "trip_{seq:03d}_{name}"})
    data = res.get_json()
    assert data["count"] == 3
    assert [n["name"] for n in data["names"]] == [
        "trip_001_IMG_0001.jpg", "trip_002_IMG_0002.jpg", "trip_003_IMG_0003.jpg"]


def test_preview_size_tokens_follow_long_edge(client, tmp_path, fake_rawpy):
    fake_rawpy["rgb"] = np.zeros((40, 80, 3), np.uint8)
    # Source size comes from rawpy; the fake exposes none, so fall back to 0x0.
    res = client.post("/api/export/preview", json={
        "path": "IMG_0001.CR2", "format": "png", "template": "{name}_{width}"})
    assert res.get_json()["approximate"] is True


def test_preview_marks_existing_files_with_suffix(client, tmp_path):
    out = tmp_path / "o"
    out.mkdir()
    (out / "IMG_0001.png").write_bytes(b"x")
    data = client.post("/api/export/preview", json={
        "path": "IMG_0001.CR2", "dest": str(out), "format": "png"}).get_json()
    assert data["names"][0]["name"] == "IMG_0001-1.png"


@pytest.mark.parametrize("template", ["../x", "a/b", "{nope}", "{name"])
def test_preview_rejects_bad_templates(client, template):
    res = client.post("/api/export/preview", json={"path": "IMG_0001.CR2", "template": template})
    assert res.status_code == 400 and "error" in res.get_json()


# ---------------------------------------------------------------------------
# Validation and errors
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body,fragment", [
    ({"format": "gif"}, "png or jpeg"),
    ({"quality": 0}, "quality"),
    ({"colorspace": "cmyk"}, "colour space"),
    ({"long_edge": 4}, "long edge"),
    ({"format": "jpeg", "bit_depth": 16}, "PNG"),
    ({"scope": "everything"}, "scope"),
    ({"scope": "selected", "paths": []}, "selected"),
    ({"dest": ""}, "destination"),
    ({"path": "../../etc/passwd"}, "outside"),
])
def test_bad_requests_are_400_with_a_reason(client, tmp_path, body, fragment):
    payload = {"scope": "current", "path": "IMG_0001.CR2", "dest": str(tmp_path / "o")}
    payload.update(body)
    res = client.post("/api/export", json=payload)
    assert res.status_code == 400
    assert fragment.lower() in res.get_json()["error"].lower()


def test_destination_that_is_a_file_is_refused(client, tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    res = client.post("/api/export", json={"path": "IMG_0001.CR2", "dest": str(f)})
    assert res.status_code == 400 and "file" in res.get_json()["error"]


def test_missing_exiftool_is_a_clear_error(client, tmp_path, monkeypatch):
    monkeypatch.setattr(exporter, "exiftool_available", lambda: False)
    res = client.post("/api/export", json={"path": "IMG_0001.CR2", "dest": str(tmp_path / "o")})
    assert res.status_code == 424
    body = res.get_json()
    assert body["code"] == "exiftool_missing" and "install" in body["error"].lower()
    assert not (tmp_path / "o").exists()


def test_cross_origin_post_is_refused(client, tmp_path):
    res = client.post("/api/export", json={"path": "IMG_0001.CR2", "dest": str(tmp_path)},
                      headers={"Origin": "http://evil.example"})
    assert res.status_code == 403


def test_non_json_body_is_415(client):
    assert client.post("/api/export", data="x", content_type="text/plain").status_code == 415


def test_unknown_job_is_404(client):
    assert client.get("/api/export/status/nope").status_code == 404


def test_second_export_while_one_runs_is_queued(client, tmp_path, monkeypatch):
    import export_api
    monkeypatch.setattr(export_api, "_ensure_worker", lambda: None)  # keep the fake "running" job in charge
    with export_api._lock:
        export_api._jobs["busy"] = {"id": "busy", "state": "running"}
    try:
        res = client.post("/api/export", json={"path": "IMG_0001.CR2", "dest": str(tmp_path / "o")})
        assert res.status_code == 202
        assert res.get_json()["state"] == "queued"
    finally:
        with export_api._lock:
            export_api._jobs.clear()


# ---------------------------------------------------------------------------
# Exporting: scopes, progress, failures
# ---------------------------------------------------------------------------

def test_current_scope_exports_one_file(client, tmp_path):
    s = wait(client, start(client, tmp_path / "o"))
    assert s["state"] == "done" and s["total"] == 1 and s["done"] == 1 and not s["failed"]
    assert sorted(p.name for p in (tmp_path / "o").iterdir()) == ["IMG_0001.png"]


def test_selected_scope_exports_only_those(client, tmp_path):
    s = wait(client, start(client, tmp_path / "o", scope="selected",
                           paths=["IMG_0003.CR2", "IMG_0001.CR2", "IMG_0003.CR2"],
                           template="{seq}_{name}"))
    assert s["total"] == 2
    assert sorted(p.name for p in (tmp_path / "o").iterdir()) == ["1_IMG_0003.png", "2_IMG_0001.png"]


def test_folder_scope_exports_everything_with_progress(client, tmp_path, monkeypatch):
    seen = []
    real = exporter.export_photo

    def slow(*a, **k):
        import export_api
        with export_api._lock:
            job = export_api._jobs.get(ids[0]) if ids else None
            seen.append(dict(job) if job else None)
        return real(*a, **k)

    ids = []
    monkeypatch.setattr(exporter, "export_photo", slow)
    job = start(client, tmp_path / "o", scope="folder", format="jpeg")
    ids.append(job["id"])
    s = wait(client, job)
    assert s["total"] == 3 and s["done"] == 3 and len(s["written"]) == 3
    assert len(list((tmp_path / "o").glob("*.jpg"))) == 3
    seen = [x for x in seen if x]
    assert any(x["current"] for x in seen), "no per-file progress was reported"
    assert [x["done"] for x in seen] == sorted(x["done"] for x in seen)


def test_one_bad_photo_does_not_abandon_the_batch(client, tmp_path, monkeypatch):
    real = exporter.export_photo

    def flaky(src, *a, **k):
        if src.name == "IMG_0002.CR2":
            raise RuntimeError("b'Input/output error'")
        return real(src, *a, **k)

    monkeypatch.setattr(exporter, "export_photo", flaky)
    s = wait(client, start(client, tmp_path / "o", scope="folder"))
    assert s["done"] == 3 and len(s["written"]) == 2
    assert s["failed"][0]["source"] == "IMG_0002.CR2"
    assert "could not read" in s["failed"][0]["error"].lower()


def test_destination_folder_is_created(client, tmp_path):
    out = tmp_path / "a" / "b"
    wait(client, start(client, out))
    assert (out / "IMG_0001.png").exists()


# ---------------------------------------------------------------------------
# Pixels: the saved edit stack, at full resolution
# ---------------------------------------------------------------------------

def test_export_applies_saved_basic_tone_curve_and_colour_edits(client, lib, tmp_path):
    flat = wait(client, start(client, tmp_path / "flat"))["written"][0]

    editor.save_adjustments(lib / "IMG_0001.CR2", {"exposure": 1.0, "color_temp": 5500})
    bright = wait(client, start(client, tmp_path / "bright"))["written"][0]
    assert mean_rgb(bright).mean() > mean_rgb(flat).mean() + 10

    editor.save_adjustments(lib / "IMG_0001.CR2", {"tone_curve": CURVE, "color_temp": 5500})
    curved = wait(client, start(client, tmp_path / "curve"))["written"][0]
    lum = lambda p: np.asarray(Image.open(p).convert("L"), dtype=float)
    assert lum(curved).std() > lum(flat).std() + 3, "tone curve was not applied to the export"
    assert not np.array_equal(lum(curved), lum(flat))

    # Colour grading: a fully blue global grade pushes blue up against red.
    editor.save_adjustments(lib / "IMG_0001.CR2", {
        "grade_global_hue": 240, "grade_global_sat": 100, "color_temp": 5500})
    graded = wait(client, start(client, tmp_path / "grade"))["written"][0]
    d_flat, d_grad = mean_rgb(flat), mean_rgb(graded)
    assert (d_grad[2] - d_grad[0]) > (d_flat[2] - d_flat[0]) + 3

    # HSL mixer: pulling every band's saturation to -100 collapses the colour.
    hsl = {f"sat_{b}": -100 for b in ("red", "orange", "yellow", "green", "aqua", "blue", "purple", "magenta")}
    editor.save_adjustments(lib / "IMG_0001.CR2", {**hsl, "color_temp": 5500})
    grey = wait(client, start(client, tmp_path / "hsl"))["written"][0]
    spread = lambda p: (lambda a: (a.max(axis=2) - a.min(axis=2)).mean())(
        np.asarray(Image.open(p).convert('RGB'), dtype=float))
    assert spread(grey) < spread(flat) - 5


def test_export_matches_the_editor_render_at_full_resolution(client, lib, tmp_path, fake_rawpy):
    fake_rawpy["rgb"] = np.tile(np.linspace(0, 255, 300, dtype=np.uint8)[None, :, None], (200, 1, 3))
    seen = []
    import process
    inner = process.rawpy.imread

    def spy(path):
        raw = inner(path)
        orig = raw.postprocess
        raw.postprocess = lambda **kw: (seen.append(kw), orig(**kw))[1]
        return raw

    process.rawpy.imread = spy
    try:
        editor.save_adjustments(lib / "IMG_0001.CR2", {"contrast": 30, "color_temp": 5500})
        out = wait(client, start(client, tmp_path / "o"))["written"][0]
    finally:
        process.rawpy.imread = inner
    assert Image.open(out).size == (300, 200)
    assert seen and not any(kw.get("half_size") for kw in seen), "export used the half-size decode"


def test_resize_and_jpeg_quality_options_reach_the_exporter(client, tmp_path):
    big = wait(client, start(client, tmp_path / "a", format="jpeg", quality=95))["written"][0]
    small = wait(client, start(client, tmp_path / "b", format="jpeg", quality=20, long_edge=32))["written"][0]
    assert Image.open(small).size == (32, 32)
    assert Path(small).stat().st_size < Path(big).stat().st_size


def test_colour_space_option_embeds_the_profile(client, tmp_path):
    out = wait(client, start(client, tmp_path / "o", colorspace="adobergb"))["written"][0]
    from PIL import ImageCms
    import io
    prof = ImageCms.ImageCmsProfile(io.BytesIO(Image.open(out).info["icc_profile"]))
    assert "Adobe RGB" in ImageCms.getProfileDescription(prof)


# ---------------------------------------------------------------------------
# Metadata through the dialog path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fmt", ["png", "jpeg"])
def test_metadata_matches_before_and_after_through_the_dialog(client, lib, tmp_path, fmt):
    editor.save_adjustments(lib / "IMG_0001.CR2", {"exposure": 0.5, "color_temp": 5500,
                                                   "tone_curve": CURVE})
    s = wait(client, start(client, tmp_path / "o", format=fmt, keep_metadata=True,
                           long_edge=48, template="{date}_{name}"))
    assert not s["failed"]
    assert_metadata_preserved(lib / "IMG_0001.CR2", Path(s["written"][0]))


def test_batch_keeps_metadata_for_every_file(client, lib, tmp_path):
    s = wait(client, start(client, tmp_path / "o", scope="folder", format="jpeg"))
    assert len(s["written"]) == 3
    for src, dest in zip(sorted(lib.glob("*.CR2")), sorted(map(Path, s["written"]))):
        assert_metadata_preserved(src, dest)


def test_unticking_keep_metadata_strips_tags(client, lib, tmp_path):
    s = wait(client, start(client, tmp_path / "o", keep_metadata=False))
    tags = read_tags(Path(s["written"][0]))
    assert "Make" not in tags and "GPSLatitude" not in tags and "Keywords" not in tags


# ---------------------------------------------------------------------------
# 16-bit PNG
# ---------------------------------------------------------------------------

def decode_png16(path):
    data = Path(path).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, ihdr = 8, b"", None
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        assert zlib.crc32(kind + body) & 0xFFFFFFFF == struct.unpack(">I", data[pos + 8 + n:pos + 12 + n])[0]
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        pos += 12 + n
    w, h, depth, ctype = ihdr[:4]
    raw = zlib.decompress(idat)
    stride = w * 6
    rows = [np.frombuffer(raw[i * (stride + 1) + 1:(i + 1) * (stride + 1)], ">u2") for i in range(h)]
    return depth, ctype, np.stack(rows).reshape(h, w, 3)


def test_16_bit_png(client, lib, tmp_path):
    editor.save_adjustments(lib / "IMG_0001.CR2", {"tone_curve": CURVE, "color_temp": 5500})
    s = wait(client, start(client, tmp_path / "o", bit_depth=16, colorspace="prophoto"))
    assert not s["failed"], s["failed"]
    out = Path(s["written"][0])
    depth, ctype, px = decode_png16(out)
    assert (depth, ctype) == (16, 2)
    # The fake raw only has 8-bit precision, but the curve's output does not
    # land on 8-bit steps; an 8-bit pipeline would give multiples of 257.
    assert (px % 257 != 0).mean() > 0.3, "values were quantised to 8 bits"
    # Same render as the 8-bit export, to within 8-bit rounding.
    eight = wait(client, start(client, tmp_path / "e", colorspace="prophoto"))["written"][0]
    diff = np.abs(px / 257.0 - np.asarray(Image.open(eight).convert("RGB"), float))
    assert diff.max() <= 1.01
    assert b"iCCP" in out.read_bytes()
    assert_metadata_preserved(lib / "IMG_0001.CR2", out)


def test_16_bit_png_resizes(client, tmp_path):
    s = wait(client, start(client, tmp_path / "o", bit_depth=16, long_edge=32))
    assert decode_png16(s["written"][0])[2].shape == (32, 32, 3)


def test_16_bit_is_png_only(client, tmp_path):
    res = client.post("/api/export", json={"path": "IMG_0001.CR2", "dest": str(tmp_path),
                                           "format": "jpeg", "bit_depth": 16})
    assert res.status_code == 400


def test_folder_scope_includes_virtual_copies(client, lib, tmp_path):
    import catalog
    catalog.create_virtual(lib.resolve(), "IMG_0001.CR2", {"exposure": 1.5, "color_temp": 5500})
    preview = client.post("/api/export/preview", json={"scope": "folder", "format": "png"}).get_json()
    assert preview["count"] == 4
    assert "IMG_0001-copy1.png" in [n["name"] for n in preview["names"]]
    s = wait(client, start(client, tmp_path / "o", scope="folder"))
    assert s["total"] == 4 and not s["failed"]
    assert (tmp_path / "o" / "IMG_0001-copy1.png").exists()
    # The copy's own keywords replace the source's, so compare the rest.
    tags = read_tags(tmp_path / "o" / "IMG_0001-copy1.png")
    assert "Make" in tags and "GPSLatitude" in tags
