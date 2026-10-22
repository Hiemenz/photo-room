"""HDR merge and panorama stitching on synthetic bracketed / overlapping images."""
import shutil
import subprocess
import threading
import time

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

import merge  # noqa: E402


def _scene(h=240, w=320, seed=1):
    """Textured linear radiance spanning ~4 stops of range, with brighter-than-1 highlights."""
    rng = np.random.default_rng(seed)
    base = cv2.GaussianBlur(rng.random((h, w, 3)).astype(np.float32), (0, 0), 3)
    for _ in range(60):                       # hard-edged shapes, which is what alignment locks onto
        cv2.rectangle(base, (int(rng.integers(0, w - 20)), int(rng.integers(0, h - 20))),
                      (int(rng.integers(20, w)), int(rng.integers(20, h))),
                      [float(v) for v in rng.random(3) * 2 - 0.5], -1)
    base = (base - base.min()) / (base.max() - base.min())
    ramp = np.linspace(0.05, 3.0, w, dtype=np.float32)[None, :, None]
    return (0.15 + base * 0.6) * ramp + 0.02


def _encode8(lin):
    enc = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * np.power(np.maximum(lin, 1e-9), 1 / 2.4) - 0.055)
    return (np.clip(enc, 0, 1) * 255 + 0.5).astype(np.uint8)


def _save(path, lin):
    cv2.imwrite(str(path), np.ascontiguousarray(_encode8(lin)[:, :, ::-1]))


@pytest.fixture
def bracket(tmp_path):
    scene = _scene()
    paths = []
    for name, g in (("a_dark", 0.25), ("b_mid", 1.0), ("c_bright", 4.0)):
        p = tmp_path / f"{name}.png"
        _save(p, np.clip(scene * g * 1.0, 0, 1))   # clipped like a real sensor
        paths.append(p)
    return scene, paths


def test_hdr_recovers_radiance_beyond_a_single_frame(bracket):
    scene, paths = bracket
    res = merge.merge_hdr(paths, align=False)
    rad = res["radiance"]
    assert res["base"].name == "b_mid.png"
    expect = scene * 1.0            # the base frame's units
    right = (slice(None), slice(260, 300))
    assert (expect[right] > 1.1).mean() > 0.3          # the base frame itself clips here
    assert np.median(np.abs(rad[right] - expect[right]) / expect[right]) < 0.05
    mid = (slice(None), slice(100, 200))
    assert np.median(np.abs(rad[mid] - expect[mid]) / expect[mid]) < 0.04
    assert np.allclose(res["gains"], [0.25, 1.0, 4.0], rtol=0.03)


def test_hdr_aligns_a_handheld_shift(bracket, tmp_path):
    scene, paths = bracket
    shifted = np.roll(scene, (4, -3), axis=(0, 1))
    _save(paths[0], np.clip(shifted * 0.25, 0, 1))
    res = merge.merge_hdr(paths, align=True)
    dx, dy = res["shifts"][0]
    assert abs(dx - 3) <= 1 and abs(dy + 4) <= 1


def test_hdr_writes_a_new_16bit_linear_tiff_and_leaves_sources_alone(bracket, tmp_path):
    scene, paths = bracket
    before = {p: p.read_bytes() for p in paths}
    out = tmp_path / "merged.tif"
    res = merge.run_hdr(paths, out, align=False)
    assert res["output"] == str(out)
    img = cv2.imread(str(out), cv2.IMREAD_UNCHANGED)
    assert img.dtype == np.uint16 and img.shape == (240, 320, 3)
    assert {p: p.read_bytes() for p in paths} == before
    again = merge.run_hdr(paths, out, align=False)      # never overwrites
    assert again["output"].endswith("merged-1.tif") and out.exists()
    if shutil.which("exiftool"):
        desc = subprocess.run(["exiftool", "-s3", "-ImageDescription", str(out)],
                              capture_output=True, text=True, timeout=60).stdout
        assert "HDR merge of 3 frames" in desc


def test_hdr_float_output(bracket, tmp_path):
    _, paths = bracket
    res = merge.run_hdr(paths, tmp_path / "f.tif", align=False, float_output=True)
    img = cv2.imread(res["output"], cv2.IMREAD_UNCHANGED)
    assert img.dtype == np.float32 and img.max() > 1.0


def test_hdr_default_name_is_beside_the_first_source(bracket):
    _, paths = bracket
    res = merge.run_hdr(paths, align=False)
    assert res["output"] == str(paths[0].with_name("a_dark-HDR.tif"))


def test_hdr_rejects_bad_input(tmp_path, bracket):
    _, paths = bracket
    with pytest.raises(merge.MergeError, match="at least 2"):
        merge.merge_hdr(paths[:1])
    with pytest.raises(merge.MergeError, match="overwrite one of the source"):
        merge._check_output(tmp_path / "S.TIF", [tmp_path / "s.tif", tmp_path / "S.TIF"])
    with pytest.raises(merge.MergeError, match=r"\.tif"):
        merge._check_output(tmp_path / "x.dng", paths)
    small = tmp_path / "small.png"
    _save(small, _scene(100, 100))
    with pytest.raises(merge.MergeError, match="different sizes"):
        merge.merge_hdr([paths[0], small])


def test_hdr_memory_guard_names_the_fix(bracket, monkeypatch):
    _, paths = bracket
    monkeypatch.setenv("PHOTO_ROOM_MERGE_MB", "0.1")
    with pytest.raises(merge.MergeError, match="--half"):
        merge.merge_hdr(paths)


def test_hdr_cancel_stops_before_writing(bracket, tmp_path):
    _, paths = bracket
    out = tmp_path / "never.tif"
    with pytest.raises(merge.MergeCancelled):
        merge.run_hdr(paths, out, align=False, cancelled=lambda: True)
    assert not out.exists()
    assert not list(tmp_path.glob(".merge-*"))


def test_hdr_progress_is_monotonic(bracket):
    _, paths = bracket
    seen = []
    merge.run_hdr(paths, align=False, progress=lambda f, m: seen.append(f))
    assert seen == sorted(seen) and seen[-1] == 1.0


def test_hdr_from_fake_raws(monkeypatch, tmp_path):
    """The raw path: linear decodes at different exposures from a faked rawpy."""
    import process
    import rawpy

    scene = _scene(120, 160)
    files = {}
    for name, g in (("r1.cr2", 0.25), ("r2.cr2", 1.0), ("r3.cr2", 4.0)):
        (tmp_path / name).write_bytes(b"x")
        files[name] = (np.clip(scene * g * 1.0, 0, 1) * 65535).astype(np.uint16)

    class Raw:
        def __init__(self, arr):
            self.arr = arr

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(rawpy, "imread", lambda p: Raw(files[p.rsplit("/", 1)[-1]]))
    monkeypatch.setattr(process, "decode_linear", lambda raw, half_size=False, space="srgb": raw.arr)
    monkeypatch.setattr(process, "camera_crop", lambda raw, scale=1: None)
    res = merge.merge_hdr([tmp_path / n for n in files], align=False)
    assert res["base"].name == "r2.cr2"
    assert res["radiance"].max() > 1.1


@pytest.mark.skipif(not shutil.which("exiftool"), reason="needs exiftool")
def test_metadata_comes_from_the_base_frame(bracket, tmp_path):
    _, paths = bracket
    jpgs = []
    for p in paths:
        j = p.with_suffix(".jpg")
        cv2.imwrite(str(j), cv2.imread(str(p)))
        subprocess.run(["exiftool", "-overwrite_original", "-Make=TestCam", f"-Model=Base-{j.stem}",
                        "-DateTimeOriginal=2024:05:06 07:08:09", "-Orientation#=6", str(j)],
                       capture_output=True, timeout=60, check=True)
        jpgs.append(j)
    res = merge.run_hdr(jpgs, tmp_path / "m.tif", align=False)
    out = subprocess.run(["exiftool", "-s3", "-Make", "-Model", "-DateTimeOriginal", "-Orientation#", res["output"]],
                         capture_output=True, text=True, timeout=60).stdout.split("\n")
    assert out[0] == "TestCam" and out[1] == "Base-b_mid"
    assert out[2].startswith("2024:05:06") and out[3] == "1"


# ----------------------------------------------------------------- panorama

def _pano_source(h=300, w=900, seed=3):
    rng = np.random.default_rng(seed)
    img = np.full((h, w, 3), 90, np.uint8)
    for _ in range(900):
        c = tuple(int(v) for v in rng.integers(30, 255, 3))
        cv2.circle(img, (int(rng.integers(0, w)), int(rng.integers(0, h))), int(rng.integers(3, 14)), c, -1)
    for _ in range(300):
        c = tuple(int(v) for v in rng.integers(30, 255, 3))
        x, y = int(rng.integers(0, w - 30)), int(rng.integers(0, h - 30))
        cv2.rectangle(img, (x, y), (x + int(rng.integers(5, 30)), y + int(rng.integers(5, 30))), c, 2)
    return cv2.GaussianBlur(img, (0, 0), 1.0)


@pytest.fixture
def frames(tmp_path):
    src = _pano_source()
    paths = []
    for i, x in enumerate((0, 250, 500)):
        p = tmp_path / f"p{i}.png"
        cv2.imwrite(str(p), src[:, x:x + 400])
        paths.append(p)
    return src, paths


def test_pano_stitches_and_crops(frames, tmp_path):
    src, paths = frames
    before = [p.read_bytes() for p in paths]
    res = merge.run_pano(paths, tmp_path / "pano.tif", scans=True)
    out = cv2.imread(res["output"])
    assert out is not None and 550 < out.shape[1] <= 910      # wider than one 400 px frame
    assert (out.max(axis=2) > 0).all()                        # cropped: no empty corners
    assert [p.read_bytes() for p in paths] == before


def test_pano_preview_only(frames, tmp_path):
    _, paths = frames
    res = merge.run_pano(paths, tmp_path / "pv.tif", scans=True, preview_only=True)
    assert res["preview_only"] and res["size"][0] > 0


def test_pano_fails_clearly_on_unrelated_photos(tmp_path):
    ps = []
    for i in range(2):
        p = tmp_path / f"u{i}.png"
        cv2.imwrite(str(p), _pano_source(200, 300, seed=50 + i))
        ps.append(p)
    with pytest.raises(merge.MergeError, match="overlap|match|camera"):
        merge.run_pano(ps, tmp_path / "x.tif")
    assert not (tmp_path / "x.tif").exists()


def test_pano_needs_two(frames):
    with pytest.raises(merge.MergeError, match="at least 2"):
        merge.run_pano(frames[1][:1])


def test_pano_cancel(frames, tmp_path):
    with pytest.raises(merge.MergeCancelled):
        merge.run_pano(frames[1], tmp_path / "c.tif", cancelled=lambda: True)


def test_largest_valid_rect_ignores_empty_corners():
    m = np.ones((100, 200), bool)
    m[:30, :40] = False
    m[70:, 150:] = False
    t, l, h, w = merge.largest_valid_rect(m)
    assert m[t:t + h, l:l + w].all()
    assert h * w >= 0.95 * 70 * 160


# ----------------------------------------------------------------- HTTP

def _wait(client, jid):
    for _ in range(300):
        st = client.get(f"/api/merge/status/{jid}").get_json()
        if st["state"] in ("done", "error", "cancelled"):
            return st
        time.sleep(0.1)
    return st


def test_api_runs_a_job_and_reports_progress(bracket, tmp_path, monkeypatch):
    import editor

    _, paths = bracket
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    client = editor.app.test_client()
    r = client.post("/api/merge", json={"kind": "hdr", "paths": [p.name for p in paths],
                                        "options": {"align": False}})
    assert r.status_code == 202, r.get_json()
    st = _wait(client, r.get_json()["id"])
    assert st["state"] == "done", st
    assert st["result"]["path"] == "a_dark-HDR.tif"
    assert (tmp_path / "a_dark-HDR.tif").exists()


def test_api_validates(bracket, tmp_path, monkeypatch):
    import editor

    _, paths = bracket
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    c = editor.app.test_client()
    assert c.post("/api/merge", json={"kind": "x", "paths": ["a", "b"]}).status_code == 400
    assert c.post("/api/merge", json={"kind": "hdr", "paths": [paths[0].name]}).status_code == 400
    assert c.post("/api/merge", json={"kind": "hdr", "paths": ["../etc/passwd", paths[0].name]}).status_code == 400
    assert c.post("/api/merge", json={"kind": "hdr", "paths": [paths[0].name] * 2}).status_code == 400
    assert c.get("/api/merge/status/nope").status_code == 404


def test_api_cancel(bracket, tmp_path, monkeypatch):
    import editor

    _, paths = bracket
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)

    def slow(kind, p, out, progress=None, cancelled=None, **o):
        while not cancelled():
            time.sleep(0.02)
        raise merge.MergeCancelled()

    monkeypatch.setattr(merge, "execute", slow)
    c = editor.app.test_client()
    jid = c.post("/api/merge", json={"kind": "hdr", "paths": [p.name for p in paths]}).get_json()["id"]
    assert c.post(f"/api/merge/cancel/{jid}").status_code == 200
    assert _wait(c, jid)["state"] == "cancelled"
    assert c.post(f"/api/merge/cancel/{jid}").status_code == 409
