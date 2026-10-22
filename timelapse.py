"""
Timelapse: turn a sequence of photos into a video.

The frames are sorted by capture time and rendered one at a time through the
same pipeline the Export dialog uses (``exporter._render_array``), each saved
as a JPEG in a work folder, so memory stays at one frame however long the
sequence is. ffmpeg then encodes the folder. Without ffmpeg the frames are
kept and a clear error says how to install it.

Edits: by default every frame takes the saved edit of one *reference* frame
(the look is set once, on one photo); ``per_frame`` uses each frame's own edit.

Deflicker: each frame is rendered small once, its mean luminance measured, the
log-luminance series smoothed with a moving average, and the difference to the
smoothed value is applied as an exposure offset in stops (clamped), so a slow
change like a sunset survives and frame-to-frame flicker does not.

Stabilise: the shift between neighbouring frames is measured by phase
correlation on small greyscale copies; the path is smoothed and each frame's
crop window moves to cancel the jitter (with a little zoom so there is room).

Every output frame is cropped to the video's aspect and resized to its size.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image

import exporter
import render_kernels

ANALYSIS_EDGE = 192            # long edge of the measuring renders
MAX_DEFLICKER_STOPS = 2.0
STABILISE_ZOOM = 1.08          # crop room used to absorb the shifts
RENDER_OVERSAMPLE = 1.5        # render a little larger than the video so the crop window can move
FFMPEG_BASE_TIMEOUT = 300      # seconds, plus FFMPEG_PER_FRAME for each frame
FFMPEG_PER_FRAME = 2
VIDEO_CODECS = {".mp4": "libx264", ".m4v": "libx264", ".mov": "libx264",
                ".mkv": "libx264", ".webm": "libvpx-vp9"}


class FfmpegMissing(RuntimeError):
    """ffmpeg is not installed; the frames were still written."""


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def install_hint() -> str:
    import sys

    cmd = "brew install ffmpeg" if sys.platform == "darwin" else "sudo apt install ffmpeg"
    return f"ffmpeg was not found. Install it ({cmd}) to encode the video."


def parse_size(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d{2,5})\s*[xX*]\s*(\d{2,5})\s*", str(text))
    if not m:
        raise ValueError(f"size must look like 1920x1080, not {text!r}")
    w, h = int(m.group(1)) // 2 * 2, int(m.group(2)) // 2 * 2   # yuv420p needs even sides
    if min(w, h) < 16 or max(w, h) > 8192:
        raise ValueError("size must be between 16 and 8192 pixels on each side")
    return w, h


@dataclass
class TimelapseOptions:
    size: tuple[int, int] = (1920, 1080)
    fps: float = 24.0
    reference: Path | None = None      # whose saved edit every frame uses; None: the first frame
    per_frame: bool = False            # each frame keeps its own edit instead
    deflicker: bool = False
    deflicker_window: int = 9          # frames in the moving average (odd)
    stabilise: bool = False
    stabilise_window: int = 15
    quality: int = 95                  # JPEG quality of the intermediate frames
    crf: int = 18                      # x264 quality (lower is better)
    frames_only: bool = False
    overwrite: bool = False

    def validate(self) -> "TimelapseOptions":
        if isinstance(self.size, str):
            self.size = parse_size(self.size)
        w, h = (int(self.size[0]) // 2 * 2, int(self.size[1]) // 2 * 2)
        if min(w, h) < 16 or max(w, h) > 8192:
            raise ValueError("size must be between 16 and 8192 pixels on each side")
        self.size = (w, h)
        self.fps = float(self.fps)
        if not 0.1 <= self.fps <= 240:
            raise ValueError("fps must be between 0.1 and 240")
        self.deflicker_window = max(1, int(self.deflicker_window)) | 1
        self.stabilise_window = max(1, int(self.stabilise_window)) | 1
        if not 1 <= int(self.quality) <= 100:
            raise ValueError("quality must be between 1 and 100")
        if not 0 <= int(self.crf) <= 51:
            raise ValueError("crf must be between 0 and 51")
        return self


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

def capture_times(paths: list[Path]) -> dict[Path, float]:
    """
    Capture time of each photo as epoch seconds (sub-second where the camera
    records it), read with one exiftool call; the file's modified time stands in
    for a photo with no capture tag.
    """
    import json
    import calendar
    import time

    import exifpool
    from process import EXIFTOOL_TIMEOUT

    found: dict[str, float] = {}
    for i in range(0, len(paths), 200):
        chunk = [str(p) for p in paths[i:i + 200]]
        try:
            res = exifpool.run(["-j", "-q", "-DateTimeOriginal", "-SubSecTimeOriginal", "-CreateDate"],
                               timeout=EXIFTOOL_TIMEOUT, files=chunk)
            rows = json.loads(res.stdout or "[]")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            rows = []
        for row in rows:
            raw = str(row.get("DateTimeOriginal") or row.get("CreateDate") or "")
            try:
                t = calendar.timegm(time.strptime(raw[:19], "%Y:%m:%d %H:%M:%S"))
            except ValueError:
                continue
            sub = str(row.get("SubSecTimeOriginal") or "").strip()
            if sub.isdigit():
                t += float("0." + sub)
            found[row.get("SourceFile", "")] = t
    out = {}
    for p in paths:
        t = found.get(str(p))
        if t is None:
            try:
                t = p.stat().st_mtime
            except OSError:
                t = 0.0
        out[p] = t
    return out


def order_frames(paths: list[Path], times: dict[Path, float] | None = None) -> list[Path]:
    """Oldest first; ties (and unreadable times) fall back to the file name."""
    seen, unique = set(), []
    for p in paths:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    times = times if times is not None else capture_times(unique)
    return sorted(unique, key=lambda p: (times.get(p, 0.0), p.name))


# ---------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------

def mean_luminance(arr: np.ndarray) -> float:
    """Mean scene luminance of an encoded (display-referred) float RGB frame."""
    lin = np.clip(arr, 0.0, 1.0) ** 2.2
    lum = lin[..., 0] * 0.2126 + lin[..., 1] * 0.7152 + lin[..., 2] * 0.0722
    return float(lum.mean())


def _moving_average(values: np.ndarray, window: int) -> np.ndarray:
    half = window // 2
    out = np.empty_like(values, dtype=np.float64)
    for i in range(len(values)):
        out[i] = values[max(0, i - half):i + half + 1].mean(axis=0)
    return out


def deflicker_offsets(lums: list[float], window: int = 9, max_stops: float = MAX_DEFLICKER_STOPS) -> list[float]:
    """Exposure offset in stops that brings each frame to the smoothed brightness."""
    if not lums:
        return []
    logs = np.log2(np.maximum(np.asarray(lums, dtype=np.float64), 1e-6))
    target = _moving_average(logs, window)
    return [float(v) for v in np.clip(target - logs, -max_stops, max_stops)]


def _shift_between(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """(dx, dy) in pixels by which the content of ``b`` sits right/below that of ``a`` (phase correlation)."""
    win = np.outer(np.hanning(a.shape[0]), np.hanning(a.shape[1]))
    fa, fb = np.fft.rfft2((a - a.mean()) * win), np.fft.rfft2((b - b.mean()) * win)
    cross = np.conj(fa) * fb
    cross /= np.maximum(np.abs(cross), 1e-9)
    corr = np.fft.irfft2(cross, s=a.shape)
    y, x = np.unravel_index(int(np.argmax(corr)), corr.shape)

    def refine(line, i):    # parabolic sub-pixel peak
        n = len(line)
        l, c, r = line[(i - 1) % n], line[i], line[(i + 1) % n]
        d = l - 2 * c + r
        return 0.0 if abs(d) < 1e-12 else 0.5 * (l - r) / d

    dy = y + refine(corr[:, x], y)
    dx = x + refine(corr[y, :], x)
    h, w = a.shape
    if dy > h / 2:
        dy -= h
    if dx > w / 2:
        dx -= w
    return float(dx), float(dy)


def stabilise_offsets(grays: list[np.ndarray], window: int = 15) -> list[tuple[float, float]]:
    """
    Per-frame crop-window offset as a fraction of the frame (dx, dy) that cancels
    the jitter of the camera path around its moving average.
    """
    if not grays:
        return []
    h, w = grays[0].shape
    path = [(0.0, 0.0)]
    for prev, cur in zip(grays, grays[1:]):
        dx, dy = _shift_between(prev, cur)
        path.append((path[-1][0] + dx, path[-1][1] + dy))
    path = np.asarray(path)
    smooth = _moving_average(path, window)
    # Where the content sits minus where it should sit: the window follows the content.
    delta = path - smooth
    return [(float(dx / w), float(dy / h)) for dx, dy in delta]


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------

def crop_box(width: int, height: int, size: tuple[int, int], zoom: float = 1.0,
             shift: tuple[float, float] = (0.0, 0.0)) -> tuple[int, int, int, int]:
    """Largest window of the video's aspect (shrunk by ``zoom``), moved by ``shift`` but kept inside the frame."""
    aspect = size[0] / size[1]
    if width / height > aspect:
        ww, wh = height * aspect, float(height)
    else:
        ww, wh = float(width), width / aspect
    ww, wh = ww / zoom, wh / zoom
    cx = min(max(width / 2 + shift[0] * width, ww / 2), width - ww / 2)
    cy = min(max(height / 2 + shift[1] * height, wh / 2), height - wh / 2)
    left, top = int(round(cx - ww / 2)), int(round(cy - wh / 2))
    right, bottom = min(width, left + int(round(ww))), min(height, top + int(round(wh)))
    return max(0, left), max(0, top), right, bottom


def frame_image(arr: np.ndarray, size: tuple[int, int], zoom: float = 1.0,
                shift: tuple[float, float] = (0.0, 0.0)) -> Image.Image:
    img = Image.fromarray(render_kernels.float_to_u8(arr))
    box = crop_box(img.width, img.height, size, zoom, shift)
    return img.crop(box).resize(size, Image.LANCZOS)


# ---------------------------------------------------------------------------
# The build
# ---------------------------------------------------------------------------

class Cancelled(Exception):
    pass


def _frames_dir_for(out: Path) -> Path:
    base = out.parent / f"{out.stem}_frames"
    n, cand = 0, base
    while cand.exists() and any(cand.iterdir()):
        n += 1
        cand = out.parent / f"{base.name}-{n}"
    cand.mkdir(parents=True, exist_ok=True)
    return cand


def _encode(ffmpeg: str, frames: Path, out: Path, count: int, opts: TimelapseOptions) -> None:
    codec = VIDEO_CODECS[out.suffix.lower()]
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y" if opts.overwrite else "-n",
           "-framerate", f"{opts.fps:g}", "-i", str(frames / "frame_%06d.jpg"),
           "-c:v", codec, "-pix_fmt", "yuv420p", "-crf", str(opts.crf)]
    if codec == "libvpx-vp9":
        cmd += ["-b:v", "0"]
    else:
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(out))
    timeout = FFMPEG_BASE_TIMEOUT + FFMPEG_PER_FRAME * count
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg did not finish within {timeout}s and was stopped.") from None
    except OSError as exc:
        raise RuntimeError(f"ffmpeg could not be started: {exc}") from None
    if res.returncode != 0:
        out.unlink(missing_ok=True)
        tail = (res.stderr or "").strip().splitlines()[-3:]
        raise RuntimeError("ffmpeg failed: " + " | ".join(tail))


def build(paths: list[Path], out: Path, opts: TimelapseOptions,
          progress: Callable[[int, int, str], None] | None = None,
          cancel: Callable[[], bool] | None = None,
          times: dict[Path, float] | None = None,
          work_dir: Path | None = None) -> dict:
    """
    Render ``paths`` (any order) into the video ``out``. Returns a report:
    ``{"video", "frames", "frames_dir", "ffmpeg", "error", "offsets"}``. When
    ffmpeg is missing, or ``frames_only`` is set, the numbered JPEG frames are
    kept in ``<out stem>_frames`` and ``video`` is None.
    """
    opts = opts.validate()
    out = Path(out)
    if out.suffix.lower() not in VIDEO_CODECS:
        raise ValueError(f"output must end in {', '.join(VIDEO_CODECS)}")
    if out.exists() and not opts.overwrite and not opts.frames_only:
        raise ValueError(f"{out} already exists (use overwrite)")
    frames = order_frames([Path(p) for p in paths], times)
    if len(frames) < 2:
        raise ValueError("A timelapse needs at least two photos")
    out.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg = ffmpeg_path()
    keep_frames = opts.frames_only or ffmpeg is None
    n = len(frames)
    need_analysis = opts.deflicker or opts.stabilise
    steps = n * (2 if need_analysis else 1) + (0 if keep_frames else 1)
    done = 0

    def tick(label: str) -> None:
        nonlocal done
        if cancel and cancel():
            raise Cancelled()
        if progress:
            progress(done, steps, label)
        done += 1

    ref = Path(opts.reference) if opts.reference else frames[0]
    shared = None if opts.per_frame else exporter._saved_adjustments(ref)

    def adjustments(path: Path) -> dict:
        return dict(shared) if shared is not None else exporter._saved_adjustments(path)

    offsets = [0.0] * n
    shifts = [(0.0, 0.0)] * n
    if need_analysis:
        small = exporter.ExportOptions(format="jpeg", long_edge=ANALYSIS_EDGE, colorspace="srgb")
        lums, grays = [], []
        for p in frames:
            tick(f"Measuring {p.name}")
            arr, _ = exporter._render_array(p, small, adjustments=adjustments(p))
            lums.append(mean_luminance(arr))
            if opts.stabilise:
                grays.append((arr * np.array([0.299, 0.587, 0.114], dtype=np.float32)).sum(axis=2))
            del arr
        if opts.deflicker:
            offsets = deflicker_offsets(lums, opts.deflicker_window)
        if opts.stabilise:
            shifts = stabilise_offsets(grays, opts.stabilise_window)
        del grays

    workdir = _frames_dir_for(out) if keep_frames else Path(
        tempfile.mkdtemp(prefix=f".{out.stem}_frames-", dir=work_dir or out.parent))
    big = exporter.ExportOptions(format="jpeg", colorspace="srgb",
                                 long_edge=int(max(opts.size) * RENDER_OVERSAMPLE))
    zoom = STABILISE_ZOOM if opts.stabilise else 1.0
    result = {"video": None, "frames": n, "frames_dir": str(workdir) if keep_frames else None,
              "ffmpeg": ffmpeg is not None, "error": None, "offsets": offsets}
    try:
        for i, p in enumerate(frames, start=1):
            tick(f"Rendering {p.name}")
            adj = adjustments(p)
            if offsets[i - 1]:
                adj["exposure"] = max(-5.0, min(5.0, float(adj.get("exposure", 0.0)) + offsets[i - 1]))
            arr, icc = exporter._render_array(p, big, adjustments=adj)
            img = frame_image(arr, opts.size, zoom, shifts[i - 1])
            del arr
            img.save(workdir / f"frame_{i:06d}.jpg", "JPEG", quality=int(opts.quality),
                     subsampling=0, icc_profile=icc)
        if opts.frames_only:
            return result
        if ffmpeg is None:
            result["error"] = (install_hint() + f" The {n} frames were kept in {workdir}; "
                               f"encode them with: ffmpeg -framerate {opts.fps:g} -i "
                               f"{workdir}/frame_%06d.jpg -pix_fmt yuv420p out.mp4")
            return result
        tick("Encoding video")
        _encode(ffmpeg, workdir, out, n, opts)
        result["video"] = str(out)
        return result
    finally:
        if not keep_frames:
            shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Choosing the frames
# ---------------------------------------------------------------------------

def collection_paths(root: Path, collection: str) -> list[Path]:
    """The photos in a collection (by id or name), as files under ``root``."""
    import catalog
    import libindex

    root = Path(root).resolve()
    cols = catalog.list_collections(root)
    col = next((c for c in cols if c["id"] == collection or c.get("name") == collection), None)
    if col is None:
        raise ValueError(f"No such collection: {collection}")
    if col.get("smart"):
        idx = libindex.Index(root, writer=False)
        idx.scan()
        rels = [i["path"] for i in catalog.collection_items(col, idx.items())]
    else:
        rels = list(col.get("photos", []))
    out, seen = [], set()
    for rel in rels:
        src, _vid = catalog.split_id(rel)     # a virtual copy stands for its raw
        p = (root / src).resolve()
        if p not in seen and p.is_file() and root in p.parents:
            seen.add(p)
            out.append(p)
    return out


def folder_paths(folder: Path) -> list[Path]:
    import process

    return process.list_photos(Path(folder))


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="photo-room timelapse",
                                 description="Render a folder or collection into a timelapse video.")
    ap.add_argument("folder", help="photo folder (the library root when --collection is used)")
    ap.add_argument("--collection", help="only the photos of this collection (id or name)")
    ap.add_argument("-o", "--output", required=True, help="video file (.mp4, .mov, .mkv, .webm)")
    ap.add_argument("--fps", type=float, default=24.0)
    ap.add_argument("--size", default="1920x1080", help="WIDTHxHEIGHT; frames are cropped to this aspect")
    ap.add_argument("--reference", help="photo whose saved edit is applied to every frame (default: first)")
    ap.add_argument("--per-frame", action="store_true", help="use each frame's own edit instead")
    ap.add_argument("--deflicker", action="store_true", help="even out frame-to-frame brightness")
    ap.add_argument("--deflicker-window", type=int, default=9, help="frames in the smoothing window")
    ap.add_argument("--stabilise", "--stabilize", action="store_true", help="steady camera shake by cropping")
    ap.add_argument("--crf", type=int, default=18, help="x264 quality, lower is better")
    ap.add_argument("--frames-only", action="store_true", help="write numbered JPEG frames and skip ffmpeg")
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args(argv)

    folder = Path(args.folder).expanduser()
    try:
        opts = TimelapseOptions(
            size=parse_size(args.size), fps=args.fps,
            reference=Path(args.reference).expanduser().resolve() if args.reference else None,
            per_frame=args.per_frame, deflicker=args.deflicker, deflicker_window=args.deflicker_window,
            stabilise=args.stabilise, crf=args.crf, frames_only=args.frames_only,
            overwrite=args.overwrite).validate()
        paths = collection_paths(folder, args.collection) if args.collection else folder_paths(folder)
        if not paths:
            raise ValueError("No photos found")
        if ffmpeg_path() is None and not opts.frames_only:
            import sys
            print(install_hint() + " Writing the frames only.", file=sys.stderr)

        def show(done, total, label):
            print(f"[{done + 1}/{total}] {label}", flush=True)

        report = build(paths, Path(args.output).expanduser(), opts, progress=show)
    except (ValueError, RuntimeError, OSError) as exc:
        import sys
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if report["error"]:
        import sys
        print(f"error: {report['error']}", file=sys.stderr)
        return 3
    print(f"wrote {report['video'] or report['frames_dir']} ({report['frames']} frames)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
