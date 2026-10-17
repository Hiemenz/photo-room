"""
Tiles of the fully rendered photo for zoom and 1:1 inspection.

The preview is a 1400 px render; checking sharpness or noise needs real
pixels. A tile is a fixed-size (512 px) square of the finished image at a
chosen zoom -- fit, 50 %, 100 % or 200 % of the output frame -- rendered with
the same ``editor.apply_adjustments`` the preview and the export use.

How a tile is made
  * Region mode (the common case): crop the source to the tile plus a halo,
    run the normal pipeline on that crop, and cut the tile out. The halo is
    wider than every local filter's reach (clarity, texture, dehaze blur,
    sharpen), and ``_detail_edge`` makes the filters use the radii of the
    whole frame, so neighbouring tiles agree and no seams show.
  * Frame mode (straighten, transform, lens, spots or local masks, which work
    in whole-frame coordinates): render the whole frame once at that zoom,
    keep it, and slice tiles from it. Refused above FRAME_MAX_PIXELS so a
    200 % view of a big file cannot exhaust a Pi's memory.

Rendered tiles go in a byte-bounded LRU keyed on the photo, a hash of the
settings, the zoom and the tile coordinates.
"""
from __future__ import annotations

import hashlib
import io
import json
import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

TILE_SIZE = 512
ZOOMS = {"fit": None, "50": 0.5, "100": 1.0, "200": 2.0}
FRAME_MAX_PIXELS = 12_000_000
TILE_CACHE_BYTES = 64 * 1024 * 1024
ALIGN = 12                       # lcm(1..4): the detail blur shrinks by 1-4x
SETTINGS_KEPT = 64

_lock = threading.RLock()        # one heavy render at a time (Pi)
_full: OrderedDict = OrderedDict()       # (path, mtime) -> (linear uint16, baseline)
_scaled: OrderedDict = OrderedDict()     # (path, mtime, scale) -> encoded float32
_frames: OrderedDict = OrderedDict()     # (path, mtime, hash, scale) -> uint8 (rendered)
_tiles: OrderedDict = OrderedDict()      # key -> bytes
_tile_bytes = 0
_settings: OrderedDict = OrderedDict()   # hash -> settings dict


def _ed():
    import editor

    return editor


def clear_caches() -> None:
    global _tile_bytes
    with _lock:
        for c in (_full, _scaled, _frames, _tiles, _settings):
            c.clear()
        _tile_bytes = 0


# ---------------------------------------------------------------------------
# Settings identity
# ---------------------------------------------------------------------------

def settings_hash(adj: dict) -> str:
    """Stable id of the settings that change pixels (private ``_`` keys ignored)."""
    clean = {k: v for k, v in adj.items() if not str(k).startswith("_")}
    blob = json.dumps(clean, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def remember_settings(adj: dict) -> str:
    key = settings_hash(adj)
    with _lock:
        _settings[key] = dict(adj)
        _settings.move_to_end(key)
        while len(_settings) > SETTINGS_KEPT:
            _settings.popitem(last=False)
    return key


def recall_settings(key: str) -> dict | None:
    with _lock:
        hit = _settings.get(key)
        if hit is not None:
            _settings.move_to_end(key)
        return dict(hit) if hit is not None else None


# ---------------------------------------------------------------------------
# Source pixels
# ---------------------------------------------------------------------------

def _stamp(path: Path) -> tuple:
    st = path.stat()
    return (str(path.resolve()), st.st_mtime_ns, st.st_size)


def load_full(path: Path) -> tuple[np.ndarray, int]:
    """
    (full-resolution linear-light uint16 RGB in sRGB primaries, as-shot Kelvin).

    The same decode the exporter starts from (camera white balance, default
    crop, gamma 1); one photo is kept.
    """
    key = _stamp(path)
    with _lock:
        if key in _full:
            _full.move_to_end(key)
            return _full[key]
    from process import RAW_SUFFIXES, baseline_temp_from_wb, camera_crop, decode_linear

    if path.suffix.lower() in RAW_SUFFIXES:
        import rawpy

        with rawpy.imread(str(path)) as raw:
            baseline = baseline_temp_from_wb(raw.camera_whitebalance, raw)[0]
            lin = decode_linear(raw)
            crop = camera_crop(raw, scale=1)
            if crop:
                top, left, h, w = crop
                lin = lin[top:top + h, left:left + w]
    else:
        import colour

        with Image.open(path) as src:
            enc = np.asarray(ImageOps.exif_transpose(src).convert("RGB"), dtype=np.float32) / 255.0
        lin = np.rint(np.clip(colour.to_linear(enc), 0, 1) * 65535.0).astype(np.uint16)
        baseline = 5500
    lin = np.ascontiguousarray(lin)
    with _lock:
        _full[key] = (lin, baseline)
        while len(_full) > 1:
            _full.popitem(last=False)
    return lin, baseline


def _encode(lin16: np.ndarray) -> np.ndarray:
    import colour

    return colour.linear16_to_encoded(lin16, "srgb")


def _resize(arr: np.ndarray, w: int, h: int, up: bool) -> np.ndarray:
    if cv2 is not None:
        return cv2.resize(arr, (w, h), interpolation=cv2.INTER_CUBIC if up else cv2.INTER_AREA)
    planes = [np.asarray(Image.fromarray(np.ascontiguousarray(arr[:, :, c]), "F").resize(
        (w, h), Image.BICUBIC if up else Image.BOX)) for c in range(3)]
    return np.stack(planes, axis=-1)


def _scaled_frame(path: Path, lin: np.ndarray, scale: float) -> np.ndarray:
    """The whole frame at ``scale`` <= 1 as encoded float32 (cached)."""
    key = _stamp(path) + (round(scale, 6),)
    with _lock:
        if key in _scaled:
            _scaled.move_to_end(key)
            return _scaled[key]
    h, w = lin.shape[:2]
    enc = _encode(lin)
    out = enc if scale == 1.0 else _resize(enc, max(1, round(w * scale)), max(1, round(h * scale)), up=False)
    out = np.ascontiguousarray(out, dtype=np.float32)
    del enc
    with _lock:
        _scaled[key] = out
        while len(_scaled) > 2:
            _scaled.popitem(last=False)
    return out


def _source_region(path: Path, lin: np.ndarray, scale: float,
                   x0: int, y0: int, x1: int, y1: int) -> np.ndarray:
    """Pixels [x0,x1) x [y0,y1) of the frame at ``scale``, encoded float32."""
    if scale < 1.0:
        return _scaled_frame(path, lin, scale)[y0:y1, x0:x1]
    if scale == 1.0:
        return _encode(lin[y0:y1, x0:x1])
    # Upscaling: resize only the source pixels the region needs.
    k = int(round(scale))
    h, w = lin.shape[:2]
    sx0, sy0 = x0 // k, y0 // k
    sx1, sy1 = min(w, -(-x1 // k) + 1), min(h, -(-y1 // k) + 1)
    big = _resize(_encode(lin[sy0:sy1, sx0:sx1]), (sx1 - sx0) * k, (sy1 - sy0) * k, up=True)
    return big[y0 - sy0 * k:y1 - sy0 * k, x0 - sx0 * k:x1 - sx0 * k]


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def _complex(adj: dict) -> bool:
    """Settings that work in whole-frame coordinates (no cropping to a tile)."""
    import retouch

    if adj.get("spots") or adj.get("masks") or float(adj.get("straighten", 0) or 0):
        return True
    return any(adj.get(k, d) != d for k, d in retouch.DEFAULTS.items() if k != "spots")


def _crop_rect(adj: dict, w: int, h: int) -> tuple[int, int, int, int]:
    """editor.apply_geometry's crop (canonical fractions), as (x0, y0, x1, y1) on a w x h frame."""
    crop = adj.get("crop")
    if not crop:
        return 0, 0, w, h
    left = float(np.clip(crop.get("left", 0.0), 0.0, 1.0))
    top = float(np.clip(crop.get("top", 0.0), 0.0, 1.0))
    right = float(np.clip(crop.get("right", 1.0), 0.0, 1.0))
    bottom = float(np.clip(crop.get("bottom", 1.0), 0.0, 1.0))
    x0, x1 = int(round(left * w)), int(round(right * w))
    y0, y1 = int(round(top * h)), int(round(bottom * h))
    if x1 - x0 >= 8 and y1 - y0 >= 8:
        return x0, y0, x1, y1
    return 0, 0, w, h


def geometry_size(adj: dict, w: int, h: int) -> tuple[int, int]:
    """
    Size of the frame after transform, Straighten (inscribed rectangle, per
    geometry.py) and Crop, for a w x h source. Matches what the pipeline renders.
    """
    import geometry
    import transform

    tw, th = transform.output_size(w, h, adj)
    angle = float(adj.get("straighten", 0.0) or 0.0)
    if angle:
        kw, kh = geometry.largest_inscribed_rect(tw, th, np.deg2rad(angle))
        kw, kh = int(kw) - 2, int(kh) - 2          # editor.apply_geometry trims 2 px
        if kw >= 8 and kh >= 8:
            tw, th = kw, kh
    x0, y0, x1, y1 = _crop_rect(adj, tw, th)
    return x1 - x0, y1 - y0


def _output_size(adj: dict, w: int, h: int, mode: str = "") -> tuple[int, int]:
    return geometry_size(adj, w, h)


def describe(path: Path, adj: dict, fit_edge: int | None = None) -> dict:
    """Image size and zoom levels (the metadata endpoint)."""
    ed = _ed()
    lin, baseline = load_full(path)
    h, w = lin.shape[:2]
    fit_edge = int(fit_edge or ed.PREVIEW_MAX_EDGE)
    mode = "frame" if _complex(adj) else "region"
    out_w, out_h = _output_size(adj, w, h, mode)
    fit = min(1.0, fit_edge / max(out_w, out_h))
    levels = []
    for name, scale in ZOOMS.items():
        s = fit if scale is None else scale
        lw, lh = geometry_size(adj, round(w * s), round(h * s))
        levels.append({"z": name, "scale": round(s, 6), "width": lw, "height": lh,
                       "cols": -(-lw // TILE_SIZE), "rows": -(-lh // TILE_SIZE),
                       "renderable": mode == "region" or lw * lh <= FRAME_MAX_PIXELS})
    return {"source": {"width": w, "height": h}, "output": {"width": out_w, "height": out_h},
            "tile_size": TILE_SIZE, "mode": mode, "exact": True,
            "baseline_temp": baseline, "fit_edge": fit_edge,
            "settings_id": remember_settings(adj), "levels": levels}


def resolve_zoom(z: str, out_w: int, out_h: int, fit_edge: int) -> float:
    z = str(z).strip().lower().rstrip("%")
    if z not in ZOOMS:
        raise ValueError(f"z must be one of {', '.join(ZOOMS)}")
    scale = ZOOMS[z]
    return min(1.0, fit_edge / max(out_w, out_h)) if scale is None else scale


def halo_px(adj: dict, edge: int) -> int:
    """How far the pipeline's local filters reach, in pixels at this zoom."""
    reach = 2.0                                              # sharpen blur (1.2 px sigma)
    if float(adj.get("clarity", 0) or 0):
        reach = max(reach, max(4.0, edge * 0.02) * 3)
    if float(adj.get("texture", 0) or 0):
        reach = max(reach, max(1.0, edge * 0.0025) * 3)
    if float(adj.get("dehaze", 0) or 0):
        reach = max(reach, max(6.0, edge * 0.03) * 3)
    return int(np.ceil(reach)) + 2 * ALIGN


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _align(v: int, down: bool) -> int:
    return (v // ALIGN) * ALIGN if down else -(-v // ALIGN) * ALIGN


def _to_u8(out: np.ndarray) -> np.ndarray:
    import render_kernels

    return render_kernels.float_to_u8(out)


def render_region(path: Path, adj: dict, scale: float, tx: int, ty: int,
                  tile: int | None = None) -> np.ndarray:
    """One tile in region mode: float pipeline on tile + halo, cut back to the tile."""
    tile = tile or TILE_SIZE
    ed = _ed()
    lin, baseline = load_full(path)
    h, w = lin.shape[:2]
    sw, sh = round(w * scale), round(h * scale)
    cx0, cy0, cx1, cy1 = _crop_rect(adj, sw, sh)
    ow, oh = cx1 - cx0, cy1 - cy0
    x0, y0 = tx * tile, ty * tile
    if tx < 0 or ty < 0 or x0 >= ow or y0 >= oh:
        raise LookupError("tile is outside the image")
    x1, y1 = min(ow, x0 + tile), min(oh, y0 + tile)

    edge = max(ow, oh)
    halo = halo_px(adj, edge)
    rx0, ry0 = max(0, _align(x0 - halo, True)), max(0, _align(y0 - halo, True))
    rx1, ry1 = min(ow, _align(x1 + halo, False)), min(oh, _align(y1 + halo, False))

    region = _source_region(path, lin, scale, cx0 + rx0, cy0 + ry0, cx0 + rx1, cy0 + ry1)
    base = np.ascontiguousarray(region, dtype=np.float32)
    a = {k: v for k, v in adj.items() if k != "crop"}
    a.setdefault("color_temp", baseline)
    a["_detail_edge"] = edge
    out = ed.apply_adjustments(base, baseline, a)
    return _to_u8(out[y0 - ry0:y1 - ry0, x0 - rx0:x1 - rx0])


def _frame(path: Path, adj: dict, scale: float) -> np.ndarray:
    key = _stamp(path) + (settings_hash(adj), round(scale, 6))
    with _lock:
        if key in _frames:
            _frames.move_to_end(key)
            return _frames[key]
    ed = _ed()
    lin, baseline = load_full(path)
    h, w = lin.shape[:2]
    sw, sh = round(w * scale), round(h * scale)
    if sw * sh > FRAME_MAX_PIXELS:
        raise MemoryError(
            f"This zoom needs a {sw}x{sh} frame ({sw * sh / 1e6:.0f} MP); settings that "
            f"use straighten, transform, lens, spots or masks render the whole frame, "
            f"capped at {FRAME_MAX_PIXELS / 1e6:.0f} MP. Use a smaller zoom.")
    if scale > 1.0:
        src = _resize(_encode(lin), sw, sh, up=True)
    else:
        src = _scaled_frame(path, lin, scale)
    a = dict(adj)
    a.setdefault("color_temp", baseline)
    a = ed.lens.attach_meta(path, a)
    out = _to_u8(ed.apply_adjustments(np.ascontiguousarray(src, dtype=np.float32), baseline, a))
    with _lock:
        _frames[key] = out
        while len(_frames) > 2:
            _frames.popitem(last=False)
    return out


def render_tile_array(path: Path, adj: dict, z: str, tx: int, ty: int,
                      fit_edge: int | None = None, tile: int | None = None) -> np.ndarray:
    tile = tile or TILE_SIZE
    ed = _ed()
    fit_edge = int(fit_edge or ed.PREVIEW_MAX_EDGE)
    lin, _ = load_full(path)
    h, w = lin.shape[:2]
    with _lock:
        if not _complex(adj):
            ow, oh = _output_size(adj, w, h, "region")
            scale = resolve_zoom(z, ow, oh, fit_edge)
            return render_region(path, adj, scale, tx, ty, tile)
        ow, oh = _output_size(adj, w, h, "frame")
        scale = resolve_zoom(z, ow, oh, fit_edge)
        frame = _frame(path, adj, scale)
        fh, fw = frame.shape[:2]
        x0, y0 = tx * tile, ty * tile
        if tx < 0 or ty < 0 or x0 >= fw or y0 >= fh:
            raise LookupError("tile is outside the image")
        return frame[y0:y0 + tile, x0:x0 + tile]


def get_tile(path: Path, adj: dict, z: str, tx: int, ty: int, fmt: str = "jpeg",
             fit_edge: int | None = None) -> tuple[bytes, str, bool]:
    """(encoded tile, mime type, served from cache). Raises LookupError / ValueError / MemoryError."""
    global _tile_bytes
    fmt = {"jpg": "jpeg"}.get(fmt, fmt)
    if fmt not in ("jpeg", "png"):
        raise ValueError("format must be jpeg or png")
    key = _stamp(path) + (settings_hash(adj), str(z), tx, ty, fmt, fit_edge or 0)
    with _lock:
        if key in _tiles:
            _tiles.move_to_end(key)
            return _tiles[key], f"image/{fmt}", True
    arr = render_tile_array(path, adj, z, tx, ty, fit_edge)
    buf = io.BytesIO()
    img = Image.fromarray(np.ascontiguousarray(arr))
    if fmt == "jpeg":
        img.save(buf, format="JPEG", quality=90)
    else:
        img.save(buf, format="PNG", compress_level=1)
    data = buf.getvalue()
    with _lock:
        _tiles[key] = data
        _tile_bytes += len(data)
        while _tile_bytes > TILE_CACHE_BYTES and len(_tiles) > 1:
            _, old = _tiles.popitem(last=False)
            _tile_bytes -= len(old)
    return data, f"image/{fmt}", False
