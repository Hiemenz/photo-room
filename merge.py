"""
HDR merge and panorama stitching.

    photo-room merge hdr  a.CR2 b.CR2 c.CR2 -o merged.tif
    photo-room merge pano a.CR2 b.CR2 c.CR2 -o pano.tif

Both read their sources and never write to them: the result is a new TIFF
(``<first photo>-HDR.tif`` / ``-Pano.tif`` beside the first source unless ``-o``
says otherwise; an existing file is never overwritten, a numbered name is used).
The metadata of the base frame is copied across with exiftool; its saved Camera
Raw edits and embedded thumbnails are not, and Orientation is reset because the
decode is already upright.

HDR: every frame is decoded to linear light (raws through LibRaw with the
camera white balance, JPEG/PNG/TIFF by undoing the sRGB curve), the relative
exposure of each frame is measured from the pixels (EXIF is the fallback),
frames are aligned to the base frame with OpenCV's median-threshold-bitmap
aligner, and merged with a weight that trusts mid-tones and distrusts clipped
and noisy pixels. Output is a linear-light sRGB-primaries TIFF: 16-bit
(radiance scaled so the brightest pixel is 65535; the scale is stored in the
file's description) or 32-bit float with ``float_output``.

Panorama: OpenCV's Stitcher (features, bundle adjustment, seam finding,
multi-band blending). A preview-size pass runs first so a hopeless set fails in
seconds; the full pass then stitches at full size. The Stitcher works on 8-bit
data, so the result is an 8-bit sRGB-encoded TIFF, cropped to the largest
axis-aligned rectangle with no empty corners.

Memory is bounded: HDR accumulates frame by frame (about 52 bytes per pixel,
refused above ``PHOTO_ROOM_MERGE_MB``, default 3000, with a hint to use
``half``); panorama inputs are scaled down together when they exceed
``PHOTO_ROOM_PANO_MP`` megapixels (default 300). Long steps report progress and
check for cancellation between frames and stages.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Callable

import numpy as np

import exifpool

try:
    import cv2
except ImportError:  # the doctor reports this; merging needs it
    cv2 = None

RAW_SUFFIXES = (".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2", ".dng")
OUT_SUFFIXES = (".tif", ".tiff")
EXIFTOOL_TIMEOUT = 120
PROXY_EDGE = 1536
BYTES_PER_PIXEL = 52          # HDR working set per output pixel (see module doc)
PREVIEW_EDGE = 1200

Progress = Callable[[float, str], None]


class MergeError(RuntimeError):
    """A merge that cannot proceed; the message says what to do."""


class MergeCancelled(Exception):
    pass


def _noop(frac: float, msg: str) -> None:
    pass


class _Ctl:
    def __init__(self, progress: Progress | None, cancelled: Callable[[], bool] | None):
        self.progress = progress or _noop
        self._cancelled = cancelled or (lambda: False)

    def step(self, frac: float, msg: str) -> None:
        if self._cancelled():
            raise MergeCancelled()
        self.progress(max(0.0, min(1.0, frac)), msg)


def _need_cv2() -> None:
    if cv2 is None:
        raise MergeError("OpenCV is not installed (uv sync), so merging is unavailable")


def _env_mb(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


# ---------------------------------------------------------------- loading

def is_raw(path: Path) -> bool:
    return Path(path).suffix.lower() in RAW_SUFFIXES


def _srgb_to_linear(enc: np.ndarray) -> np.ndarray:
    enc = np.clip(enc, 0.0, 1.0, out=enc)
    return np.where(enc <= 0.04045, enc / 12.92, ((enc + 0.055) / 1.055) ** 2.4).astype(np.float32)


def load_linear(path: Path, half: bool = False) -> np.ndarray:
    """RGB float32 in [0, 1], linear light, sRGB primaries."""
    _need_cv2()
    path = Path(path)
    if is_raw(path):
        import rawpy

        import process

        with rawpy.imread(str(path)) as raw:
            lin = process.decode_linear(raw, half_size=half)
            crop = process.camera_crop(raw, scale=2 if half else 1)
        if crop:
            top, left, h, w = crop
            lin = lin[top:top + h, left:left + w]
        return (lin.astype(np.float32) / 65535.0)
    img = cv2.imread(str(path), cv2.IMREAD_COLOR | cv2.IMREAD_ANYDEPTH)
    if img is None:
        raise MergeError(f"Cannot read {path.name}: not a readable image")
    scale = 65535.0 if img.dtype == np.uint16 else 255.0
    rgb = np.ascontiguousarray(img[:, :, ::-1]).astype(np.float32) / scale
    lin = _srgb_to_linear(rgb)
    if half:
        lin = cv2.resize(lin, (lin.shape[1] // 2, lin.shape[0] // 2), interpolation=cv2.INTER_AREA)
    return lin


def load_srgb8(path: Path, max_edge: int | None = None, scale: float = 1.0, half: bool = False) -> np.ndarray:
    """BGR uint8 sRGB-encoded (what the Stitcher wants), optionally shrunk."""
    _need_cv2()
    path = Path(path)
    if is_raw(path):
        import rawpy

        import process

        with rawpy.imread(str(path)) as raw:
            lin = process.decode_linear(raw, half_size=half)
            crop = process.camera_crop(raw, scale=2 if half else 1)
        if crop:
            top, left, h, w = crop
            lin = lin[top:top + h, left:left + w]
        img = np.ascontiguousarray(process.linear16_to_u8(lin)[:, :, ::-1])
    else:
        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if img is None:
            raise MergeError(f"Cannot read {path.name}: not a readable image")
    h, w = img.shape[:2]
    f = scale
    if max_edge:
        f = min(f, max_edge / max(h, w))
    if f < 1.0:
        img = cv2.resize(img, (max(1, round(w * f)), max(1, round(h * f))), interpolation=cv2.INTER_AREA)
    return img


def exposure_from_exif(path: Path) -> float | None:
    """Relative exposure (time * ISO / f-number^2) from EXIF, or None."""
    try:
        res = exifpool.run(["-s3", "-n", "-ExposureTime", "-ISO", "-FNumber", str(path)],
                           timeout=EXIFTOOL_TIMEOUT)
        vals = [float(v) for v in res.stdout.split() if v.strip()]
    except Exception:
        return None
    if len(vals) != 3 or min(vals) <= 0:
        return None
    t, iso, n = vals
    return t * iso / (n * n)


# ---------------------------------------------------------------- HDR

def _luma_max(img: np.ndarray) -> np.ndarray:
    return img.max(axis=2)


def _proxy(img: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    f = min(1.0, PROXY_EDGE / max(h, w))
    if f >= 1.0:
        return img.copy()
    return cv2.resize(img, (round(w * f), round(h * f)), interpolation=cv2.INTER_AREA)


def _pair_ratio(dark: np.ndarray, bright: np.ndarray) -> float | None:
    """Median of bright/dark over pixels that are well exposed in both, or None."""
    a, b = _luma_max(dark), _luma_max(bright)
    ok = (a > 0.02) & (a < 0.9) & (b > 0.02) & (b < 0.9)
    if int(ok.sum()) < 500:
        return None
    return float(np.median(b[ok] / a[ok]))


def exposure_weight(x: np.ndarray, gain: float) -> np.ndarray:
    """
    Trust of a pixel whose brightest channel is ``x`` (linear, 0-1) in a frame
    that received ``gain`` times the base frame's light: zero when clipped,
    ramping up out of the noise floor, and scaled by gain (more light, less noise).
    """
    lo = np.clip((x - 0.01) / 0.19, 0.0, 1.0)
    hi = np.clip((0.97 - x) / 0.17, 0.0, 1.0)
    return (lo * hi * np.float32(gain)).astype(np.float32) + np.float32(1e-6) * (x < 0.97)


def _shift_mtb(base8: np.ndarray, img8: np.ndarray) -> tuple[int, int]:
    bits = int(max(3, min(6, np.log2(max(16, min(base8.shape[:2])) / 16))))   # pyramid depth ~ image size
    aligner = cv2.createAlignMTB(bits, 4, True)
    gray = lambda a: cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)  # noqa: E731  (MTB takes one channel)
    dx, dy = aligner.calculateShift(gray(base8), gray(img8))
    return int(dx), int(dy)


def _to8(img: np.ndarray) -> np.ndarray:
    enc = np.where(img <= 0.0031308, img * 12.92, 1.055 * np.power(np.maximum(img, 1e-9), 1 / 2.4) - 0.055)
    return np.ascontiguousarray((np.clip(enc, 0, 1) * 255 + 0.5).astype(np.uint8)[:, :, ::-1])


def _translate(img: np.ndarray, dx: int, dy: int, fill: float = 0.0) -> np.ndarray:
    m = np.float32([[1, 0, dx], [0, 1, dy]])
    return cv2.warpAffine(img, m, (img.shape[1], img.shape[0]), flags=cv2.INTER_NEAREST,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=fill)


def merge_hdr(paths: list[Path], *, align: bool = True, half: bool = False,
              progress: Progress | None = None,
              cancelled: Callable[[], bool] | None = None) -> dict:
    """
    Merge bracketed frames. Returns ``{"radiance": float32 HxWx3 (base frame = 1.0),
    "base": Path, "gains": [...], "shifts": [...], "size": (w, h)}``.
    """
    _need_cv2()
    paths = [Path(p) for p in paths]
    if len(paths) < 2:
        raise MergeError("HDR merge needs at least 2 photos at different exposures")
    ctl = _Ctl(progress, cancelled)
    n = len(paths)

    # Pass 1: small proxies for exposure, order and alignment; full frames are not kept.
    proxies, sizes = [], []
    for i, p in enumerate(paths):
        ctl.step(0.02 + 0.2 * i / n, f"Reading {p.name}")
        img = load_linear(p, half=half)
        sizes.append(img.shape[:2])
        proxies.append(_proxy(img))
        del img
    if len(set(sizes)) != 1:
        raise MergeError("The photos are different sizes, so they are not a bracketed set")
    h, w = sizes[0]
    need = h * w * BYTES_PER_PIXEL / 1048576
    limit = _env_mb("PHOTO_ROOM_MERGE_MB", 3000)
    if need > limit:
        raise MergeError(f"Merging {n} photos of {w}x{h} needs about {need:.0f} MB "
                         f"(limit {limit:.0f} MB). Use half-size (--half) or raise PHOTO_ROOM_MERGE_MB")

    ctl.step(0.24, "Measuring exposures")
    means = [float(np.mean(_luma_max(px))) for px in proxies]
    order = sorted(range(n), key=lambda i: means[i])
    rel = {order[0]: 1.0}
    for a, b in zip(order, order[1:]):
        r = _pair_ratio(proxies[a], proxies[b])
        if r is None:
            ea, eb = exposure_from_exif(paths[a]), exposure_from_exif(paths[b])
            if ea and eb:
                r = eb / ea
        if r is None or r <= 0:
            raise MergeError(f"Cannot tell how {paths[b].name} differs in exposure from "
                             f"{paths[a].name}: they share no well-exposed pixels. "
                             "Are they the same scene?")
        rel[b] = rel[a] * r
    base = order[n // 2]
    gains = [rel[i] / rel[base] for i in range(n)]   # light received relative to the base frame

    shifts = [(0, 0)] * n
    if align:
        base8 = _to8(proxies[base])
        shifts = []
        for i in range(n):
            ctl.step(0.28 + 0.1 * i / n, f"Aligning {paths[i].name}")
            if i == base:
                shifts.append((0, 0))
                continue
            dx, dy = _shift_mtb(base8, _to8(proxies[i]))   # MTB is exposure-invariant by design
            f = h / proxies[i].shape[0]
            shifts.append((round(dx * f), round(dy * f)))
        del base8
    del proxies

    # Pass 2: weighted accumulate, one frame at a time.
    num = np.zeros((h, w, 3), np.float32)
    den = np.zeros((h, w), np.float32)
    for k, i in enumerate(order):
        ctl.step(0.4 + 0.5 * k / n, f"Merging {paths[i].name}")
        img = load_linear(paths[i], half=half)
        valid = None
        dx, dy = shifts[i]
        if dx or dy:
            img = _translate(img, dx, dy)
            valid = _translate(np.ones((h, w), np.float32), dx, dy)
        for r0 in range(0, h, 512):
            sl = slice(r0, min(h, r0 + 512))
            wgt = exposure_weight(_luma_max(img[sl]), gains[i])
            if valid is not None:
                wgt *= valid[sl]
            num[sl] += img[sl] * (wgt / np.float32(gains[i]))[..., None]
            den[sl] += wgt
        del img, valid

    ctl.step(0.92, "Combining")
    ok = den > 0
    rad = np.zeros_like(num)
    rad[ok] = num[ok] / den[ok][:, None]
    # Pixels with no usable data anywhere (shifted-in borders): leave black.
    return {"radiance": rad, "base": paths[base], "gains": gains, "shifts": shifts, "size": (w, h)}


# ---------------------------------------------------------------- output

def unique_output(requested: Path) -> Path:
    """``requested``, or ``name-1.ext`` and so on: never an existing file."""
    requested = Path(requested)
    cand, k = requested, 0
    while cand.exists():
        k += 1
        cand = requested.with_name(f"{requested.stem}-{k}{requested.suffix}")
    return cand


def default_output(first: Path, kind: str) -> Path:
    return Path(first).with_name(f"{Path(first).stem}-{'HDR' if kind == 'hdr' else 'Pano'}.tif")


def _check_output(out: Path, sources: list[Path]) -> Path:
    out = Path(out)
    if out.suffix.lower() not in OUT_SUFFIXES:
        raise MergeError(f"Output must be a .tif or .tiff file (DNG output is not supported), not {out.suffix or out.name}")
    srcs = {p.resolve() for p in sources}
    if out.resolve() in srcs:
        raise MergeError("The output would overwrite one of the source photos")
    if not out.parent.is_dir():
        raise MergeError(f"Folder {out.parent} does not exist")
    return out


def copy_metadata(base: Path, out: Path, description: str = "") -> None:
    """Base frame's tags onto ``out``, minus edits, thumbnails and orientation."""
    args = ["-m", "-overwrite_original", "-TagsFromFile", str(base), "-all:all",
            "--IFD1:all", "--PreviewImage", "--ThumbnailImage", "--JpgFromRaw", "--OtherImage",
            "--XMP-crs:all", "--XMP-photoshop:History", "-Orientation#=1"]
    if description:
        args.append(f"-ImageDescription={description}")
    args.append(str(out))
    try:
        res = exifpool.run(args, timeout=EXIFTOOL_TIMEOUT)
    except Exception as exc:  # timeout, missing exiftool
        raise MergeError(f"Could not copy metadata to {out.name}: {exc}") from None
    if res.returncode not in (0, 1) and not out.exists():
        raise MergeError(f"exiftool failed on {out.name}: {res.stderr.strip()}")


def _write_atomic(out: Path, write: Callable[[str], bool]) -> Path:
    """Write via a hidden temp file in the same folder, then link into place without overwriting."""
    fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=".merge-", suffix=out.suffix)
    os.close(fd)
    try:
        if not write(tmp):
            raise MergeError(f"OpenCV could not write {out.name}")
        return _link_unique(Path(tmp), out)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _link_unique(tmp: Path, out: Path) -> Path:
    """Move ``tmp`` to ``out`` (or a numbered sibling) without ever replacing a file."""
    for _ in range(1000):
        target = unique_output(out)
        try:
            os.link(tmp, target)
        except FileExistsError:
            continue
        tmp.unlink(missing_ok=True)
        return target
    raise MergeError(f"Cannot find a free name near {out.name}")


def write_hdr(result: dict, out: Path, float_output: bool = False) -> tuple[Path, float]:
    rad = result["radiance"]
    if float_output:
        data, scale = rad.astype(np.float32), 1.0
    else:
        peak = float(max(1.0, rad.max()))
        data = (np.clip(rad / peak, 0, 1) * 65535.0 + 0.5).astype(np.uint16)
        scale = peak
    bgr = np.ascontiguousarray(data[:, :, ::-1])
    final = _write_atomic(out, lambda tmp: cv2.imwrite(tmp, bgr))
    return final, scale


def hdr_description(base: Path, scale: float, n: int, float_output: bool) -> str:
    s = "32-bit float" if float_output else f"16-bit, divide by 65535 then multiply by {scale:.4f} for radiance"
    return f"photo-room HDR merge of {n} frames, linear sRGB primaries, {s}; base frame {base.name}"


def run_hdr(paths, out: Path | None = None, *, align=True, half=False, float_output=False,
            progress: Progress | None = None, cancelled=None) -> dict:
    paths = [Path(p) for p in paths]
    ctl = _Ctl(progress, cancelled)
    dest = _check_output(out or default_output(paths[0], "hdr"), paths)
    result = merge_hdr(paths, align=align, half=half, progress=progress, cancelled=cancelled)
    ctl.step(0.94, "Writing")
    final, scale = write_hdr(result, dest, float_output)
    try:
        ctl.step(0.97, "Copying metadata")
        copy_metadata(result["base"], final, hdr_description(result["base"], scale, len(paths), float_output))
    except MergeCancelled:
        final.unlink(missing_ok=True)
        raise
    except Exception:
        final.unlink(missing_ok=True)   # a half-finished file must not appear in the Library
        raise
    ctl.step(1.0, "Done")
    return {"output": str(final), "base": str(result["base"]), "frames": len(paths),
            "scale": scale, "size": list(result["size"]), "kind": "hdr"}


# ---------------------------------------------------------------- panorama

def largest_valid_rect(valid: np.ndarray) -> tuple[int, int, int, int]:
    """
    Largest axis-aligned rectangle containing only True pixels, as
    (top, left, height, width). Maximal-rectangle-in-histogram on a shrunken
    mask, then refined at full size.
    """
    h, w = valid.shape
    f = min(1.0, 500 / max(h, w))
    small = valid if f >= 1.0 else cv2.resize(valid.astype(np.uint8), (max(1, int(w * f)), max(1, int(h * f))),
                                              interpolation=cv2.INTER_NEAREST).astype(bool)
    sh, sw = small.shape
    heights = np.zeros(sw, np.int32)
    best = (0, 0, 0, 0, 0)  # area, top, left, height, width
    for r in range(sh):
        heights = np.where(small[r], heights + 1, 0)
        stack: list[int] = []
        for c in range(sw + 1):
            cur = int(heights[c]) if c < sw else 0
            while stack and int(heights[stack[-1]]) >= cur:
                hh = int(heights[stack.pop()])
                left = stack[-1] + 1 if stack else 0
                area = hh * (c - left)
                if area > best[0]:
                    best = (area, r - hh + 1, left, hh, c - left)
            stack.append(c)
    if best[0] == 0:
        return 0, 0, h, w
    _, t, l, hh, ww = best
    if f < 1.0:
        t, l, hh, ww = int(t / f), int(l / f), int(hh / f), int(ww / f)
    t, l = max(0, t), max(0, l)
    hh, ww = min(hh, h - t), min(ww, w - l)
    # Shrink one pixel row/column at a time until the rectangle is clean at full size.
    for _ in range(2000):
        win = valid[t:t + hh, l:l + ww]
        if win.all() or hh < 2 or ww < 2:
            break
        bad_top, bad_bot = (~win[0]).sum(), (~win[-1]).sum()
        bad_l, bad_r = (~win[:, 0]).sum(), (~win[:, -1]).sum()
        worst = max(bad_top, bad_bot, bad_l, bad_r)
        if worst == 0:
            break
        if bad_top == worst:
            t, hh = t + 1, hh - 1
        elif bad_bot == worst:
            hh -= 1
        elif bad_l == worst:
            l, ww = l + 1, ww - 1
        else:
            ww -= 1
    return t, l, hh, ww


_STITCH_ERRORS = {
    1: "The photos do not overlap enough to find a panorama (need ~30% overlap and detail in it)",
    2: "Could not match the photos to each other. Check that they are one scene shot from one spot",
    3: "Could not solve the camera positions. Try fewer photos or more overlap",
}


def _stitch(images: list[np.ndarray], scans: bool, full: bool) -> np.ndarray:
    mode = cv2.Stitcher_SCANS if scans else cv2.Stitcher_PANORAMA
    st = cv2.Stitcher_create(mode)
    if full:
        try:
            st.setCompositingResol(-1.0)       # compose at the input size
        except Exception:
            pass
    status, pano = st.stitch(images)
    if status != 0 or pano is None:
        raise MergeError(_STITCH_ERRORS.get(int(status), f"Stitching failed (OpenCV status {status})"))
    return pano


def crop_to_valid(pano: np.ndarray) -> np.ndarray:
    """Drop the empty (pure black) borders the warp leaves, keeping the largest clean rectangle."""
    empty = (pano.max(axis=2) == 0).astype(np.uint8)
    if not empty.any():
        return pano
    # Only empty regions that touch the border are warp padding; real black pixels inside stay.
    n, labels = cv2.connectedComponents(empty, connectivity=4)
    edge = np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))
    pad = np.isin(labels, edge[edge != 0])
    valid = cv2.erode((~pad).astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
    t, l, hh, ww = largest_valid_rect(valid)
    return np.ascontiguousarray(pano[t:t + hh, l:l + ww])


def run_pano(paths, out: Path | None = None, *, scans: bool = False, half: bool = False,
             preview_only: bool = False, progress: Progress | None = None, cancelled=None) -> dict:
    _need_cv2()
    paths = [Path(p) for p in paths]
    if len(paths) < 2:
        raise MergeError("Panorama needs at least 2 overlapping photos")
    ctl = _Ctl(progress, cancelled)
    dest = _check_output(out or default_output(paths[0], "pano"), paths)
    n = len(paths)

    ctl.step(0.02, "Preview pass")
    small = []
    for i, p in enumerate(paths):
        ctl.step(0.02 + 0.15 * i / n, f"Reading {p.name} (preview)")
        small.append(load_srgb8(p, max_edge=PREVIEW_EDGE, half=True))
    ctl.step(0.2, "Matching previews")
    pano = _stitch(small, scans, full=False)
    del small
    scaled = None
    if not preview_only:
        ctl.step(0.3, "Reading full-size photos")
        # Pixel budget for all inputs together.
        budget = _env_mb("PHOTO_ROOM_PANO_MP", 300) * 1e6
        full, total = [], 0
        scale = 1.0
        for i, p in enumerate(paths):
            ctl.step(0.3 + 0.2 * i / n, f"Reading {p.name}")
            img = load_srgb8(p, half=half)
            if i == 0:
                total_est = img.shape[0] * img.shape[1] * n
                if total_est > budget:
                    scale = (budget / total_est) ** 0.5
                    scaled = scale
            if scale < 1.0:
                img = cv2.resize(img, (max(1, int(img.shape[1] * scale)), max(1, int(img.shape[0] * scale))),
                                 interpolation=cv2.INTER_AREA)
            full.append(img)
        ctl.step(0.55, "Stitching (this is the slow part; cancel takes effect after it)")
        pano = _stitch(full, scans, full=True)
        del full
    ctl.step(0.85, "Cropping to the valid area")
    pano = crop_to_valid(pano)
    ctl.step(0.9, "Writing")
    final = _write_atomic(dest, lambda tmp: cv2.imwrite(tmp, pano))
    base = paths[n // 2]
    try:
        ctl.step(0.95, "Copying metadata")
        copy_metadata(base, final, f"photo-room panorama of {n} frames, sRGB-encoded 8-bit; base frame {base.name}")
    except BaseException:
        final.unlink(missing_ok=True)
        raise
    ctl.step(1.0, "Done")
    return {"output": str(final), "base": str(base), "frames": n, "size": [pano.shape[1], pano.shape[0]],
            "kind": "pano", "preview_only": preview_only, "downscaled": scaled}


def execute(kind: str, paths, out=None, **opts) -> dict:
    if kind == "hdr":
        return run_hdr(paths, out, **{k: v for k, v in opts.items()
                                       if k in ("align", "half", "float_output", "progress", "cancelled")})
    if kind == "pano":
        return run_pano(paths, out, **{k: v for k, v in opts.items()
                                        if k in ("scans", "half", "preview_only", "progress", "cancelled")})
    raise MergeError(f"Unknown merge kind {kind!r} (use hdr or pano)")


def main(argv: list[str]) -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(prog="photo-room merge", description="Merge bracketed photos to HDR or stitch a panorama.")
    p.add_argument("kind", choices=["hdr", "pano"])
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("-o", "--output", type=Path, help=".tif/.tiff; default: beside the first photo")
    p.add_argument("--half", action="store_true", help="work at half size (less memory)")
    p.add_argument("--no-align", action="store_true", help="hdr: frames are tripod-steady")
    p.add_argument("--float", dest="float_output", action="store_true", help="hdr: 32-bit float TIFF")
    p.add_argument("--scans", action="store_true", help="pano: flat/scan mode instead of rotating camera")
    p.add_argument("--preview", action="store_true", help="pano: stop after the quick preview-size pass")
    a = p.parse_args(argv)

    def show(frac, msg):
        print(f"[{frac * 100:3.0f}%] {msg}", file=sys.stderr)

    try:
        if a.kind == "hdr":
            res = run_hdr(a.files, a.output, align=not a.no_align, half=a.half,
                          float_output=a.float_output, progress=show)
        else:
            res = run_pano(a.files, a.output, scans=a.scans, half=a.half,
                           preview_only=a.preview, progress=show)
    except MergeError as exc:
        print(f"photo-room merge: {exc}", file=sys.stderr)
        return 1
    print(res["output"])
    return 0
