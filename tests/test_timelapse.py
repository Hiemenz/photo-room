"""Timelapse: ordering, deflicker, stabilise, framing, ffmpeg handling (fake ffmpeg, synthetic frames)."""

import json
import os
import stat
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import exporter
import timelapse
from timelapse import TimelapseOptions


def make_frames(folder: Path, levels, size=(160, 120)):
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, v in enumerate(levels):
        p = folder / f"IMG_{i:04d}.jpg"
        Image.new("RGB", size, (v, v, v)).save(p, "JPEG", quality=95)
        paths.append(p)
    return paths


@pytest.fixture(autouse=True)
def no_sidecars(monkeypatch):
    """Edits come from a dict the test controls; exiftool is not needed."""
    edits = {}
    monkeypatch.setattr(exporter, "_saved_adjustments",
                        lambda path, source_size=None: dict(edits.get(Path(path).name, edits.get("*", {}))))
    return edits


@pytest.fixture
def fake_ffmpeg(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "ffmpeg.log"
    script = bindir / "ffmpeg"
    script.write_text(
        "#!/usr/bin/env python3\nimport sys, json, glob\n"
        "a = sys.argv[1:]\nout = a[-1]\n"
        "pat = a[a.index('-i') + 1]\n"
        f"open({str(log)!r}, 'a').write(json.dumps({{'args': a, 'frames': len(glob.glob(pat.replace('%06d', '*')))}}) + '\\n')\n"
        "open(out, 'wb').write(b'video')\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return log


@pytest.fixture(autouse=True)
def jpeg_library(monkeypatch):
    """The real library lists raws only; these synthetic frames are JPEGs."""
    monkeypatch.setattr(timelapse, "folder_paths", lambda folder: sorted(Path(folder).glob("*.jpg")))


@pytest.fixture
def no_ffmpeg(monkeypatch):
    monkeypatch.setattr(timelapse.shutil, "which", lambda name: None)


def times_for(paths):
    return {p: float(i) for i, p in enumerate(paths)}


# -- ordering ---------------------------------------------------------------

def test_frames_sort_by_capture_time_not_name(tmp_path):
    a, b, c = make_frames(tmp_path, [10, 20, 30])
    assert timelapse.order_frames([a, b, c], {a: 300.0, b: 100.0, c: 200.0}) == [b, c, a]


def test_capture_time_reads_exif_or_falls_back_to_the_file(tmp_path):
    (p,) = make_frames(tmp_path, [10])
    assert timelapse.capture_times([p])[p] > 0


# -- deflicker --------------------------------------------------------------

def test_deflicker_evens_out_flicker_but_keeps_a_trend():
    lums = [0.2 * (1.5 ** (0.1 * i)) for i in range(40)]          # slow ramp
    flickered = [v * (1.4 if i % 2 else 0.7) for i, v in enumerate(lums)]
    offs = timelapse.deflicker_offsets(flickered, window=9)
    fixed = [v * 2 ** o for v, o in zip(flickered, offs)]
    interior = slice(5, 35)
    assert np.std(np.diff(np.log2(fixed)[interior])) < 0.5 * np.std(np.diff(np.log2(flickered)[interior]))
    assert fixed[35] > fixed[5]                                    # the ramp survives


def test_deflicker_offsets_are_clamped():
    offs = timelapse.deflicker_offsets([0.5, 0.0001, 0.5], window=3, max_stops=1.0)
    assert max(abs(o) for o in offs) <= 1.0


def test_build_applies_deflicker_as_exposure(tmp_path, fake_ffmpeg, monkeypatch):
    paths = make_frames(tmp_path / "in", [100, 160, 100, 160, 100])
    seen = []
    real = exporter._render_array

    def spy(path, opts, adjustments=None):
        seen.append((path.name, opts.long_edge, (adjustments or {}).get("exposure", 0.0)))
        return real(path, opts, adjustments=adjustments)

    monkeypatch.setattr(exporter, "_render_array", spy)
    opts = TimelapseOptions(size=(64, 36), deflicker=True, deflicker_window=5)
    report = timelapse.build(paths, tmp_path / "o.mp4", opts, times=times_for(paths))
    final = {name: exp for name, edge, exp in seen if edge != timelapse.ANALYSIS_EDGE}
    assert final["IMG_0000.jpg"] > 0 > final["IMG_0001.jpg"]       # dark frame lifted, bright one pulled down
    assert len(report["offsets"]) == 5


# -- stabilise --------------------------------------------------------------

def test_stabilise_offsets_follow_the_jitter():
    rng = np.random.default_rng(1)
    world = rng.random((140, 200)).astype(np.float32)
    shifts = [(0, 0), (4, -3), (-3, 2), (5, 4), (-4, -2), (2, 3), (0, 0)]
    # the window moves by (dx, dy) over the world, so the content moves by (-dx, -dy)
    grays = [world[20 + dy:100 + dy, 30 + dx:150 + dx] for dx, dy in shifts]
    offs = timelapse.stabilise_offsets(grays, window=7)
    got = np.array([(o[0] * 120, o[1] * 80) for o in offs])
    want = -np.array(shifts, dtype=float)
    want = want - want.mean(axis=0)
    for axis in (0, 1):
        assert np.corrcoef(got[:, axis], want[:, axis])[0, 1] > 0.9


def test_crop_box_has_the_video_aspect_and_stays_inside():
    l, t, r, b = timelapse.crop_box(300, 200, (160, 90), zoom=1.1, shift=(0.5, -0.5))
    assert 0 <= l < r <= 300 and 0 <= t < b <= 200
    assert abs((r - l) / (b - t) - 160 / 90) < 0.05


def test_stabilise_build_runs(tmp_path, no_ffmpeg):
    paths = make_frames(tmp_path / "in", [90, 100, 110, 120])
    report = timelapse.build(paths, tmp_path / "t.mp4",
                             TimelapseOptions(size=(48, 32), stabilise=True, frames_only=True),
                             times=times_for(paths))
    assert len(list(Path(report["frames_dir"]).glob("*.jpg"))) == 4


# -- build ------------------------------------------------------------------

def test_build_encodes_with_ffmpeg_and_cleans_up(tmp_path, fake_ffmpeg):
    paths = make_frames(tmp_path / "in", [50, 100, 150])
    out = tmp_path / "out" / "t.mp4"
    report = timelapse.build(paths, out, TimelapseOptions(size=(64, 36), fps=12), times=times_for(paths))
    assert out.read_bytes() == b"video" and report["video"] == str(out)
    call = json.loads(fake_ffmpeg.read_text().splitlines()[0])
    assert call["frames"] == 3
    assert call["args"][call["args"].index("-framerate") + 1] == "12"
    assert "libx264" in call["args"] and "yuv420p" in call["args"]
    assert list(out.parent.iterdir()) == [out]       # no leftover work folder


def test_frames_are_the_requested_size_and_in_time_order(tmp_path, no_ffmpeg):
    paths = make_frames(tmp_path / "in", [30, 120, 220], size=(200, 100))
    times = {paths[0]: 3.0, paths[1]: 2.0, paths[2]: 1.0}      # names ascend, capture times descend
    report = timelapse.build(paths, tmp_path / "t.mp4", TimelapseOptions(size=(80, 80)), times=times)
    frames = sorted(Path(report["frames_dir"]).glob("frame_*.jpg"))
    assert [f.name for f in frames] == ["frame_000001.jpg", "frame_000002.jpg", "frame_000003.jpg"]
    assert Image.open(frames[0]).size == (80, 80)
    means = [np.asarray(Image.open(f)).mean() for f in frames]
    assert means[0] > means[1] > means[2]


def test_missing_ffmpeg_is_a_clear_error_and_keeps_the_frames(tmp_path, no_ffmpeg):
    paths = make_frames(tmp_path / "in", [50, 100])
    report = timelapse.build(paths, tmp_path / "t.mp4", TimelapseOptions(size=(32, 32)), times=times_for(paths))
    assert report["video"] is None and "ffmpeg was not found" in report["error"]
    assert len(list(Path(report["frames_dir"]).glob("*.jpg"))) == 2


def test_frames_only_never_runs_ffmpeg(tmp_path, fake_ffmpeg):
    paths = make_frames(tmp_path / "in", [50, 100])
    report = timelapse.build(paths, tmp_path / "t.mp4", TimelapseOptions(size=(32, 32), frames_only=True),
                             times=times_for(paths))
    assert report["error"] is None and report["frames_dir"] and not fake_ffmpeg.exists()


def test_reference_edit_applies_to_every_frame_unless_per_frame(tmp_path, no_sidecars):
    paths = make_frames(tmp_path / "in", [100, 100, 100])
    no_sidecars["IMG_0000.jpg"] = {"exposure": 1.0}
    no_sidecars["IMG_0001.jpg"] = {"exposure": -1.0}
    out = tmp_path / "o"
    shared = timelapse.build(paths, out / "a.mp4", TimelapseOptions(size=(32, 32), frames_only=True),
                             times=times_for(paths))
    each = timelapse.build(paths, out / "b.mp4", TimelapseOptions(size=(32, 32), per_frame=True, frames_only=True),
                           times=times_for(paths))

    def mean(rep, n):
        return np.asarray(Image.open(Path(rep["frames_dir"]) / f"frame_{n:06d}.jpg")).mean()

    assert abs(mean(shared, 1) - mean(shared, 2)) < 2         # same look on all frames
    assert mean(each, 1) > mean(each, 2) + 20                 # own edits differ


def test_ffmpeg_timeout_is_bounded_and_reported(tmp_path, monkeypatch, fake_ffmpeg):
    import subprocess

    paths = make_frames(tmp_path / "in", [50, 100])

    def hangs(cmd, **kw):
        assert kw["timeout"] > 0
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(timelapse.subprocess, "run", hangs)
    with pytest.raises(RuntimeError, match="did not finish"):
        timelapse.build(paths, tmp_path / "t.mp4", TimelapseOptions(size=(32, 32)), times=times_for(paths))


def test_validation():
    assert timelapse.parse_size("1921x1081") == (1920, 1080)
    with pytest.raises(ValueError):
        timelapse.parse_size("big")
    with pytest.raises(ValueError):
        TimelapseOptions(fps=0).validate()


def test_needs_two_frames_and_a_video_extension(tmp_path):
    (p,) = make_frames(tmp_path / "in", [1])
    with pytest.raises(ValueError, match="at least two"):
        timelapse.build([p], tmp_path / "t.mp4", TimelapseOptions(), times={p: 0.0})
    with pytest.raises(ValueError, match="must end in"):
        timelapse.build([p, p], tmp_path / "t.gif", TimelapseOptions())


def test_cancel_stops_the_build(tmp_path, fake_ffmpeg):
    paths = make_frames(tmp_path / "in", [50, 100, 150])
    with pytest.raises(timelapse.Cancelled):
        timelapse.build(paths, tmp_path / "t.mp4", TimelapseOptions(size=(32, 32)),
                        times=times_for(paths), cancel=lambda: True)
    assert not (tmp_path / "t.mp4").exists()


def test_cli(tmp_path, fake_ffmpeg, monkeypatch):
    make_frames(tmp_path / "shoot", [40, 80, 120])
    monkeypatch.setattr(timelapse, "capture_times", lambda paths: {p: float(i) for i, p in enumerate(paths)})
    out = tmp_path / "x.mp4"
    code = timelapse.main([str(tmp_path / "shoot"), "-o", str(out), "--fps", "10", "--size", "64x36"])
    assert code == 0 and out.exists()


def test_cli_without_ffmpeg_exits_nonzero_with_frames(tmp_path, no_ffmpeg, capsys, monkeypatch):
    make_frames(tmp_path / "shoot", [40, 80])
    monkeypatch.setattr(timelapse, "capture_times", lambda paths: {p: float(i) for i, p in enumerate(paths)})
    code = timelapse.main([str(tmp_path / "shoot"), "-o", str(tmp_path / "x.mp4"), "--size", "64x36"])
    assert code == 3 and "ffmpeg was not found" in capsys.readouterr().err
    assert (tmp_path / "x_frames" / "frame_000001.jpg").exists()


def test_collection_paths(tmp_path):
    import catalog

    make_frames(tmp_path, [10, 20, 30])
    col = catalog.create_collection(tmp_path, "Sunset")
    catalog.add_photos(tmp_path, col["id"], ["IMG_0000.jpg", "IMG_0002.jpg"])
    assert [p.name for p in timelapse.collection_paths(tmp_path, "Sunset")] == ["IMG_0000.jpg", "IMG_0002.jpg"]
    with pytest.raises(ValueError):
        timelapse.collection_paths(tmp_path, "nope")


def test_api_runs_a_job(tmp_path, monkeypatch, fake_ffmpeg):
    import time

    import editor

    root = tmp_path / "lib"
    make_frames(root, [50, 100, 150])
    monkeypatch.setattr(editor, "LIBRARY_ROOT", root)
    monkeypatch.setattr(timelapse, "capture_times", lambda paths: {p: float(i) for i, p in enumerate(paths)})
    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        assert c.get("/api/timelapse/defaults").get_json()["ffmpeg"] is True
        res = c.post("/api/timelapse", json={"scope": "folder", "dest": str(tmp_path / "out"),
                                             "name": "t.mp4", "size": "64x36"})
        assert res.status_code == 202, res.get_json()
        job = res.get_json()["id"]
        for _ in range(100):
            st = c.get(f"/api/timelapse/status/{job}").get_json()
            if st["state"] != "running":
                break
            time.sleep(0.1)
        assert st["state"] == "done" and (tmp_path / "out" / "t.mp4").exists()
        bad = c.post("/api/timelapse", json={"scope": "selected", "paths": [], "dest": str(tmp_path / "out")})
        assert bad.status_code == 400
