"""
Local adjustments (M7): graduated filter, radial filter and adjustment brush.

Kept apart from editor.py so the geometry is testable without a raw file and
so it merges cleanly beside the other develop features. editor.py touches it in
three places only: ``apply_masks`` in the render path, ``xmp_arguments`` /
``parse_xmp`` when saving and loading, and a few lines in the API.

A photo has a list of masks under the ``masks`` key of the ordinary adjustments
dict, which is why presets, copy/paste, history and batch apply carry them with
no extra code. Each mask is plain JSON::

    {"id": "a1b2c3d4", "name": "Sky", "type": "linear", "invert": false,
     "enabled": true, "adj": {"exposure": -0.7, "saturation": 12},
     ...geometry...}

Geometry, all in coordinates normalised to the *cropped and straightened*
picture (0..1, y down), so it is the same at preview and full resolution:

    linear   x, y        centre of the transition
             angle       degrees; 0 applies the effect at the top and fades
                         it downward, positive turns the gradient clockwise
             feather     0..100, width of the transition as a share of the
                         picture's extent along the gradient
    radial   x, y        centre
             rx, ry      radii as fractions of the width and height
             angle       rotation in degrees
             feather     0..100, how much of the radius is a soft edge
             (effect is inside the ellipse; ``invert`` swaps that)
    brush    strokes     list of {size, feather, flow, density, erase, pts}
             size is a radius as a fraction of the picture's longer edge;
             pts is a flat [x0, y0, x1, y1, ...] list - strokes are kept as
             compact stroke data, never as a bitmap, and rasterised on demand.

    range    no geometry: the whole picture, to be narrowed by a ``range``.

Any mask may carry a ``range`` that multiplies its shape by a tonal or colour
selection read from the picture itself::

    "range": {"lum": {"lo": 0.0, "hi": 0.4, "smooth": 0.3},
              "color": {"hue": 210, "tol": 30, "smooth": 0.3, "sat_min": 0.1}}

``lum`` keeps pixels whose luminance is within lo..hi (0..1, display coded)
with soft edges of ``smooth`` (0..1); ``color`` keeps hues within ``tol``
degrees of ``hue`` and saturated above ``sat_min``. Both are optional.

``op`` combines a mask with the one before it: "new" (default) starts its own
adjustment; "add", "subtract" and "intersect" fold the mask's shape into the
group started by the previous "new" mask, whose sliders the whole group uses.

The mask value is a float 0..1 per pixel; a local adjustment is rendered over
the whole picture and blended through the mask, so outside the mask the
pixels are bit-for-bit what they were.
"""
from __future__ import annotations

import fsutil
import json
import math
import os
import threading
import uuid
from collections import OrderedDict
from pathlib import Path

import numpy as np

MAX_MASKS = 16
MAX_STROKES = 200
MAX_POINTS = 2000            # points per stroke
MAX_DABS = 3000              # dabs per stroke while rasterising
MASK_TYPES = ("linear", "radial", "brush", "range")
GEOMETRY_TYPES = ("linear", "radial", "brush")     # the kinds Camera Raw XMP can carry
COMBINE_OPS = ("new", "add", "subtract", "intersect")

# (key, label, min, max, step, XMP tag)
_LOCAL = [
    ("exposure",   "Exposure",   -4.0, 4.0,   0.05, "LocalExposure2012"),
    ("contrast",   "Contrast",   -100, 100, 1, "LocalContrast2012"),
    ("highlights", "Highlights", -100, 100, 1, "LocalHighlights2012"),
    ("shadows",    "Shadows",    -100, 100, 1, "LocalShadows2012"),
    ("temp",       "Temp",       -100, 100, 1, "LocalTemperature"),
    ("tint",       "Tint",       -100, 100, 1, "LocalTint"),
    ("saturation", "Saturation", -100, 100, 1, "LocalSaturation"),
    ("clarity",    "Clarity",    -100, 100, 1, "LocalClarity2012"),
    ("texture",    "Texture",    -100, 100, 1, "LocalTexture"),
    ("dehaze",     "Dehaze",     -100, 100, 1, "LocalDehaze"),
    ("sharpness",  "Sharpness",  -100, 100, 1, "LocalSharpness"),
    ("whites",     "Whites",     -100, 100, 1, "LocalWhites2012"),
    ("blacks",     "Blacks",     -100, 100, 1, "LocalBlacks2012"),
    ("hue",        "Hue",        -100, 100, 1, "LocalHue"),
    ("noise",      "Noise reduction", 0, 100, 1, "LocalLuminanceNoise"),
]
LOCAL_CONTROLS = [{"key": k, "label": lab, "min": lo, "max": hi, "step": st, "default": 0}
                  for k, lab, lo, hi, st, _ in _LOCAL]
LOCAL_LIMITS = {k: (lo, hi) for k, _, lo, hi, _, _ in _LOCAL}
LOCAL_XMP_TAGS = {k: tag for k, _, _, _, _, tag in _LOCAL}

HUE_SHIFT_DEGREES = 45.0     # local Hue +-100 turns the colours by this much

BRUSH_DEFAULTS = {"size": 0.04, "feather": 50.0, "flow": 100.0, "density": 100.0}


# ---------------------------------------------------------------------------
# Normalisation (everything that arrives from the API or a file goes through
# here, so the rest of the module can trust its input)
# ---------------------------------------------------------------------------

def _num(value, default: float, lo: float, hi: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return min(hi, max(lo, v))


def _wrap_angle(value) -> float:
    a = _num(value, 0.0, -1e6, 1e6)
    return round(((a + 180.0) % 360.0) - 180.0, 3)


def _clean_text(value, default: str) -> str:
    text = "".join(ch for ch in str(value or "") if ch.isalnum() or ch in " _-.")
    return text.strip()[:40] or default


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def _stroke(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    pts = raw.get("pts")
    if not isinstance(pts, (list, tuple)):
        return None
    flat: list[float] = []
    for v in pts[: MAX_POINTS * 2]:
        flat.append(round(_num(v, 0.0, -0.5, 1.5), 4))
    if len(flat) % 2:
        flat.pop()
    if not flat:
        return None
    return {
        "size": round(_num(raw.get("size"), BRUSH_DEFAULTS["size"], 0.001, 0.5), 4),
        "feather": round(_num(raw.get("feather"), BRUSH_DEFAULTS["feather"], 0, 100), 2),
        "flow": round(_num(raw.get("flow"), BRUSH_DEFAULTS["flow"], 1, 100), 2),
        "density": round(_num(raw.get("density"), BRUSH_DEFAULTS["density"], 0, 100), 2),
        "erase": bool(raw.get("erase", False)),
        "pts": flat,
    }


def _range(raw) -> dict:
    """A clean range block ({} when it selects everything)."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    lum = raw.get("lum")
    if isinstance(lum, dict):
        lo = _num(lum.get("lo"), 0.0, 0.0, 1.0)
        hi = _num(lum.get("hi"), 1.0, 0.0, 1.0)
        if lo > hi:
            lo, hi = hi, lo
        if lo > 0.0 or hi < 1.0:
            out["lum"] = {"lo": round(lo, 4), "hi": round(hi, 4),
                          "smooth": round(_num(lum.get("smooth"), 0.2, 0.0, 1.0), 3)}
    col = raw.get("color")
    if isinstance(col, dict) and col.get("hue") is not None:
        out["color"] = {"hue": round(_num(col.get("hue"), 0.0, -1e6, 1e6) % 360.0, 2),
                        "tol": round(_num(col.get("tol"), 30.0, 1.0, 180.0), 2),
                        "smooth": round(_num(col.get("smooth"), 0.3, 0.0, 1.0), 3),
                        "sat_min": round(_num(col.get("sat_min"), 0.0, 0.0, 1.0), 3)}
    return out


def normalise_mask(raw, seen_ids: set | None = None) -> dict | None:
    """A clean copy of one mask, or None if it is not a usable mask."""
    if not isinstance(raw, dict) or raw.get("type") not in MASK_TYPES:
        return None
    seen = seen_ids if seen_ids is not None else set()
    mid = "".join(ch for ch in str(raw.get("id") or "") if ch.isalnum())[:16]
    if not mid or mid in seen:
        mid = new_id()
    seen.add(mid)

    kind = raw["type"]
    out: dict = {
        "id": mid,
        "name": _clean_text(raw.get("name"), kind.capitalize()),
        "type": kind,
        "invert": bool(raw.get("invert", False)),
        "enabled": bool(raw.get("enabled", True)),
    }
    op = raw.get("op")
    if op in COMBINE_OPS and op != "new":
        out["op"] = op
    rng = _range(raw.get("range"))
    if rng:
        out["range"] = rng
    adj = {}
    src = raw.get("adj") if isinstance(raw.get("adj"), dict) else {}
    for key, (lo, hi) in LOCAL_LIMITS.items():
        v = _num(src.get(key), 0.0, lo, hi)
        if v:
            adj[key] = round(v, 3)
    out["adj"] = adj

    if kind == "linear":
        out.update(x=round(_num(raw.get("x"), 0.5, -0.5, 1.5), 4),
                   y=round(_num(raw.get("y"), 0.5, -0.5, 1.5), 4),
                   angle=_wrap_angle(raw.get("angle")),
                   feather=round(_num(raw.get("feather"), 50.0, 0, 100), 2))
    elif kind == "radial":
        out.update(x=round(_num(raw.get("x"), 0.5, -0.5, 1.5), 4),
                   y=round(_num(raw.get("y"), 0.5, -0.5, 1.5), 4),
                   rx=round(_num(raw.get("rx"), 0.25, 0.005, 1.5), 4),
                   ry=round(_num(raw.get("ry"), 0.25, 0.005, 1.5), 4),
                   angle=_wrap_angle(raw.get("angle")),
                   feather=round(_num(raw.get("feather"), 50.0, 0, 100), 2))
    elif kind == "brush":
        strokes = raw.get("strokes") if isinstance(raw.get("strokes"), list) else []
        cleaned = [s for s in (_stroke(s) for s in strokes[:MAX_STROKES]) if s]
        out["strokes"] = cleaned
    return out


def normalise_masks(raw) -> list[dict]:
    """A clean list of masks (at most ``MAX_MASKS``, unique ids). Never raises."""
    if not isinstance(raw, (list, tuple)):
        return []
    seen: set = set()
    out = []
    for item in raw:
        m = normalise_mask(item, seen)
        if m:
            out.append(m)
        if len(out) >= MAX_MASKS:
            break
    return out


def has_effect(mask: dict) -> bool:
    """False for a mask that cannot change a pixel (disabled or all sliders zero)."""
    if not mask.get("enabled", True):
        return False
    return any(float(mask.get("adj", {}).get(k, 0) or 0) != 0 for k in LOCAL_LIMITS)


def groups(masks: list[dict]) -> list[list[dict]]:
    """
    Masks grouped by adjustment: each "new" mask (or a combiner with nothing
    before it) starts a group and the add / subtract / intersect masks that
    follow it fold into that group. ``masks`` must be normalised.
    """
    out: list[list[dict]] = []
    for m in masks:
        if m.get("op", "new") != "new" and out:
            out[-1].append(m)
        else:
            out.append([m])
    return out


def scale_for_strength(masks, strength: float) -> list[dict]:
    """Scale every local slider by the edit-strength factor (see editor.effective_adjustments)."""
    out = normalise_masks(masks)
    for m in out:
        m["adj"] = {k: round(v * strength, 3) for k, v in m["adj"].items()}
        m["adj"] = {k: v for k, v in m["adj"].items() if v}
    return out


# ---------------------------------------------------------------------------
# Mask geometry
# ---------------------------------------------------------------------------

def _smoothstep(t: np.ndarray) -> np.ndarray:
    t = np.clip(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _axes(shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    h, w = shape
    return (np.arange(w, dtype=np.float32) + 0.5), (np.arange(h, dtype=np.float32) + 0.5)


def linear_mask(shape: tuple[int, int], m: dict) -> np.ndarray:
    """Graduated filter: 1 on the full side, 0 on the far side, smooth between."""
    h, w = shape
    px, py = _axes(shape)
    theta = math.radians(float(m.get("angle", 0.0)))
    nx, ny = -math.sin(theta), math.cos(theta)
    s = (px - float(m["x"]) * w)[None, :] * nx + (py - float(m["y"]) * h)[:, None] * ny
    extent = abs(w * nx) + abs(h * ny)
    width = max(float(m.get("feather", 50.0)) / 100.0 * extent, 1.0)
    return (1.0 - _smoothstep(s / width + 0.5)).astype(np.float32)


def radial_mask(shape: tuple[int, int], m: dict) -> np.ndarray:
    """Radial filter: 1 inside the ellipse, soft edge over the outer ``feather`` of the radius."""
    h, w = shape
    px, py = _axes(shape)
    dx = (px - float(m["x"]) * w)[None, :]
    dy = (py - float(m["y"]) * h)[:, None]
    phi = math.radians(float(m.get("angle", 0.0)))
    c, s = math.cos(phi), math.sin(phi)
    a = max(float(m["rx"]) * w, 1.0)
    b = max(float(m["ry"]) * h, 1.0)
    r = np.sqrt(((dx * c + dy * s) / a) ** 2 + ((-dx * s + dy * c) / b) ** 2)
    soft = max(float(m.get("feather", 50.0)) / 100.0, 1.0 / min(a, b))
    soft = min(soft, 1.0)
    return (1.0 - _smoothstep((r - (1.0 - soft)) / soft)).astype(np.float32)


def _dab_positions(pts: np.ndarray, radius: float) -> np.ndarray:
    """Evenly spaced dab centres along a polyline (pixels), deterministic."""
    if len(pts) == 1:
        return pts
    seg = np.hypot(*np.diff(pts, axis=0).T)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    total = float(cum[-1])
    if total <= 1e-9:
        return pts[:1]
    step = max(radius * 0.25, 0.5, total / MAX_DABS)
    dist = np.append(np.arange(0.0, total, step), total)
    return np.stack([np.interp(dist, cum, pts[:, 0]), np.interp(dist, cum, pts[:, 1])], axis=1)


def stroke_coverage(shape: tuple[int, int], stroke: dict) -> np.ndarray:
    """One stroke's opacity 0..1: flow builds up under overlapping dabs, density caps it."""
    h, w = shape
    radius = max(float(stroke["size"]) * max(h, w), 0.5)
    flat = np.asarray(stroke["pts"], dtype=np.float64).reshape(-1, 2)
    pts = flat * np.array([w, h], dtype=np.float64)
    flow = float(stroke["flow"]) / 100.0
    feather = float(stroke["feather"]) / 100.0
    inner = 1.0 - feather
    acc = np.zeros((h, w), dtype=np.float32)
    for cx, cy in _dab_positions(pts, radius):
        x0, x1 = max(0, int(math.floor(cx - radius))), min(w, int(math.ceil(cx + radius)) + 1)
        y0, y1 = max(0, int(math.floor(cy - radius))), min(h, int(math.ceil(cy + radius)) + 1)
        if x0 >= x1 or y0 >= y1:
            continue
        xs = (np.arange(x0, x1, dtype=np.float32) + 0.5 - np.float32(cx))[None, :]
        ys = (np.arange(y0, y1, dtype=np.float32) + 0.5 - np.float32(cy))[:, None]
        d = np.sqrt(xs * xs + ys * ys) / np.float32(radius)
        if feather <= 0.0:
            profile = (d <= 1.0).astype(np.float32)
        else:
            profile = 1.0 - _smoothstep((d - inner) / max(1.0 - inner, 1e-6))
        win = acc[y0:y1, x0:x1]
        win[:] = 1.0 - (1.0 - win) * (1.0 - flow * profile)
    return np.minimum(acc, float(stroke["density"]) / 100.0)


_raster_cache: "OrderedDict[str, np.ndarray]" = OrderedDict()
_raster_lock = threading.Lock()
_RASTER_CACHE_SIZE = 8


def brush_mask(shape: tuple[int, int], m: dict) -> np.ndarray:
    """Rasterise a brush mask's strokes in order (erase strokes subtract)."""
    key = json.dumps([shape, m.get("strokes", [])], sort_keys=True, separators=(",", ":"))
    with _raster_lock:
        hit = _raster_cache.get(key)
        if hit is not None:
            _raster_cache.move_to_end(key)
            return hit
    total = np.zeros(shape, dtype=np.float32)
    for stroke in m.get("strokes", []):
        cov = stroke_coverage(shape, stroke)
        total = total * (1.0 - cov) if stroke.get("erase") else 1.0 - (1.0 - total) * (1.0 - cov)
    total = np.clip(total, 0.0, 1.0).astype(np.float32)
    total.setflags(write=False)
    with _raster_lock:
        _raster_cache[key] = total
        while len(_raster_cache) > _RASTER_CACHE_SIZE:
            _raster_cache.popitem(last=False)
    return total


def mask_array(m: dict, shape: tuple[int, int]) -> np.ndarray:
    """The mask as a float32 (H, W) array in 0..1, inversion applied."""
    kind = m.get("type")
    if kind == "linear":
        arr = linear_mask(shape, m)
    elif kind == "radial":
        arr = radial_mask(shape, m)
    elif kind == "brush":
        arr = brush_mask(shape, m)
    elif kind == "range":
        return np.ones(shape, dtype=np.float32)
    else:
        raise ValueError(f"unknown mask type {kind!r}")
    return (1.0 - arr) if m.get("invert") else arr


def _hue_sat(img: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """HSV hue (degrees) and saturation of an RGB float image."""
    mx, mn = img.max(axis=2), img.min(axis=2)
    d = mx - mn
    safe = np.where(d > 1e-6, d, 1.0)
    r, g, b = img[:, :, 0], img[:, :, 1], img[:, :, 2]
    hue = np.where(mx == r, ((g - b) / safe) % 6.0,
                   np.where(mx == g, (b - r) / safe + 2.0, (r - g) / safe + 4.0)) * 60.0
    sat = np.where(mx > 1e-6, d / np.maximum(mx, 1e-6), 0.0)
    return np.where(d > 1e-6, hue, 0.0).astype(np.float32), sat.astype(np.float32)


def range_array(rng: dict, img: np.ndarray) -> np.ndarray:
    """The luminance / colour range selection of ``img`` as a float32 (H, W) array in 0..1."""
    out = np.ones(img.shape[:2], dtype=np.float32)
    lum = rng.get("lum")
    if lum:
        L = _luma(img)
        s = float(lum["smooth"]) * 0.5
        if s > 0.0:
            low = _smoothstep((L - (float(lum["lo"]) - s)) / s) if lum["lo"] > 0 else 1.0
            high = 1.0 - _smoothstep((L - float(lum["hi"])) / s) if lum["hi"] < 1 else 1.0
        else:
            low = (L >= float(lum["lo"])).astype(np.float32)
            high = (L <= float(lum["hi"])).astype(np.float32)
        out = out * low * high
    col = rng.get("color")
    if col:
        hue, sat = _hue_sat(np.clip(img, 0.0, 1.0))
        dist = np.abs((hue - float(col["hue"]) + 180.0) % 360.0 - 180.0)
        soft = max(float(col["smooth"]) * 90.0, 1e-3)
        near = 1.0 - _smoothstep((dist - float(col["tol"])) / soft)
        out = out * near * _smoothstep((sat - float(col["sat_min"])) / 0.05)
    return out.astype(np.float32)


def member_array(m: dict, img: np.ndarray) -> np.ndarray:
    """One mask's shape times its range selection, read from ``img``."""
    arr = mask_array(m, img.shape[:2])
    if m.get("range"):
        arr = arr * range_array(m["range"], img)
    return arr


def group_array(group: list[dict], img: np.ndarray) -> np.ndarray | None:
    """The combined mask of a group (see ``groups``), or None when nothing is enabled."""
    arr = None
    for i, m in enumerate(group):
        if not m.get("enabled", True):
            continue
        a = member_array(m, img)
        op = m.get("op", "new") if i else "new"
        if arr is None:
            arr = a
        elif op == "add":
            arr = arr + a - arr * a
        elif op == "subtract":
            arr = arr * (1.0 - a)
        elif op == "intersect":
            arr = arr * a
    return arr


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _luma(img: np.ndarray) -> np.ndarray:
    return 0.2126 * img[:, :, 0] + 0.7152 * img[:, :, 1] + 0.0722 * img[:, :, 2]


def _default_blur(plane: np.ndarray, radius: float) -> np.ndarray:
    from editor import _blur           # lazy: editor imports this module
    return _blur(plane, radius)


def _hue_rotate(img: np.ndarray, degrees: float) -> np.ndarray:
    """Turn the colours around the grey axis by ``degrees`` (luminance-preserving rotation)."""
    a = math.radians(degrees)
    c, s = math.cos(a), math.sin(a)
    third, root = 1.0 / 3.0, math.sqrt(1.0 / 3.0)
    m = np.array([
        [c + (1 - c) * third, third * (1 - c) - root * s, third * (1 - c) + root * s],
        [third * (1 - c) + root * s, c + third * (1 - c), third * (1 - c) - root * s],
        [third * (1 - c) - root * s, third * (1 - c) + root * s, c + third * (1 - c)],
    ], dtype=np.float32)
    return np.clip(img @ m.T, 0.0, 1.0)


def _apply_light_legacy(img: np.ndarray, g) -> np.ndarray:
    """Edit version 1: the light sliders added to gamma-coded values."""
    out = img.copy()
    temp, tint = g("temp"), g("tint")
    if temp:
        out[:, :, 0] *= 1.0 + 0.3 * temp / 100.0
        out[:, :, 2] *= 1.0 - 0.3 * temp / 100.0
    if tint:
        out[:, :, 1] *= 1.0 - (tint / 100.0) * 0.18
        out[:, :, 0] *= 1.0 + (tint / 100.0) * 0.06
        out[:, :, 2] *= 1.0 + (tint / 100.0) * 0.06
    if g("exposure"):
        out *= 2.0 ** g("exposure")
    out = np.clip(out, 0.0, 1.0)

    lum = _luma(out)
    if g("shadows"):
        out += (g("shadows") / 100.0) * 0.35 * (np.clip(1.0 - lum / 0.5, 0.0, 1.0) ** 2)[:, :, None]
    if g("highlights"):
        out += (g("highlights") / 100.0) * 0.35 * (np.clip((lum - 0.5) / 0.5, 0.0, 1.0) ** 2)[:, :, None]
    if g("contrast"):
        out = (out - 0.5) * (1.0 + g("contrast") / 100.0) + 0.5
    return np.clip(out, 0.0, 1.0)


def _apply_light(img: np.ndarray, g, space: str) -> np.ndarray:
    """White balance, exposure, tonal ranges and contrast, with the global kernel."""
    import colour
    import render_kernels
    adj = {"exposure": g("exposure"), "shadows": g("shadows"), "highlights": g("highlights"),
           "whites": g("whites"), "blacks": g("blacks"), "contrast": g("contrast"),
           "tint": g("tint") * 1.5, "_space": space, "edit_version": 2}
    # A mask's Temp is relative (-100..+100), not Kelvin: express it as gains of the
    # same kind as the global slider by moving the white point symmetrically.
    out = np.ascontiguousarray(img, dtype=np.float32)
    temp = g("temp")
    if temp:
        lin = colour.to_linear(out, space) * colour.local_temperature_gains(temp).astype(np.float32)
        out = colour.to_encoded(lin, space)
    return render_kernels.tone_stage(out, 5500, {**adj, "color_temp": 5500})


def apply_local(img: np.ndarray, adj: dict, blur=None, space: str = "srgb", legacy: bool = False) -> np.ndarray:
    """
    Render one mask's sliders over the whole picture (the mask blends it later).

    The light controls (temp, tint, exposure, shadows, highlights, whites,
    blacks) run in linear light exactly like the global sliders, through the
    same kernel, so a local +1 stop is a global +1 stop; ``legacy`` keeps the
    original gamma-space maths for edits saved before edit version 2.
    ``img`` is not modified.
    """
    blur = blur or _default_blur
    g = lambda k: float(adj.get(k, 0) or 0)           # noqa: E731
    if legacy:
        out = _apply_light_legacy(img, g)
    else:
        out = _apply_light(img, g, space)

    if g("hue"):
        out = _hue_rotate(out, g("hue") / 100.0 * HUE_SHIFT_DEGREES)
    if g("saturation"):
        gray = _luma(out)[:, :, None]
        out = gray + (out - gray) * (1.0 + g("saturation") / 100.0)
        out = np.clip(out, 0.0, 1.0)

    if g("noise"):
        import render_kernels
        amount = min(max(g("noise"), 0.0), 100.0)
        out = render_kernels.denoise_stage(np.ascontiguousarray(out, dtype=np.float32),
                                           {"luminance_nr": amount, "color_nr": amount})
    clarity, texture, dehaze, sharp = g("clarity"), g("texture"), g("dehaze"), g("sharpness")
    if clarity or texture or dehaze or sharp:
        edge = max(out.shape[0], out.shape[1])
        lum = _luma(out)
        if clarity:
            detail = lum - blur(lum, max(4.0, edge * 0.02))
            mid = 1.0 - (2.0 * np.clip(lum, 0.0, 1.0) - 1.0) ** 2
            out = out + (clarity / 100.0) * 0.8 * (detail * mid)[:, :, None]
            lum = _luma(np.clip(out, 0.0, 1.0))
        if texture:
            detail = lum - blur(lum, max(1.0, edge * 0.0025))
            out = out + (texture / 100.0) * 0.9 * detail[:, :, None]
            lum = _luma(np.clip(out, 0.0, 1.0))
        if dehaze:
            dark = np.min(np.clip(out, 0.0, 1.0), axis=2)
            veil = np.clip(blur(dark, max(6.0, edge * 0.03)) * (dehaze / 100.0) * 0.7, -0.85, 0.85)[:, :, None]
            out = (out - veil) / np.maximum(1.0 - veil, 0.15)
        if sharp:
            detail = lum - blur(lum, 1.2)
            out = out + (sharp / 100.0) * 0.5 * detail[:, :, None]
        out = np.clip(out, 0.0, 1.0)
    return out


def apply_masks(img: np.ndarray, masks, blur=None, space: str = "srgb", legacy: bool = False) -> np.ndarray:
    """
    Apply every mask in order. Returns ``img`` itself, untouched, when no mask
    can change a pixel (no masks, all sliders zero, or an empty mask).
    """
    if not masks:
        return img
    out = img
    for group in groups(normalise_masks(masks)):
        if not has_effect(group[0]):
            continue
        arr = group_array(group, out)
        if arr is None or not arr.any():
            continue
        local = apply_local(out, group[0]["adj"], blur, space=space, legacy=legacy)
        out = np.clip(out + (local - out) * arr[:, :, None], 0.0, 1.0)
    return out


def overlay_png(m: dict, shape: tuple[int, int], opacity: float = 0.55, img: np.ndarray | None = None) -> bytes:
    """
    The mask as a red RGBA PNG (alpha = mask * opacity) for the O-key overlay.
    ``img`` (the picture at ``shape``) lets a range selection show; without it
    only the shape is drawn.
    """
    import io
    from PIL import Image
    clean = normalise_mask(m) or m
    arr = member_array(clean, img) if img is not None and img.shape[:2] == tuple(shape) else mask_array(clean, shape)
    rgba = np.zeros(shape + (4,), dtype=np.uint8)
    rgba[..., 0], rgba[..., 1], rgba[..., 2] = 255, 40, 40
    rgba[..., 3] = (np.clip(arr, 0.0, 1.0) * opacity * 255.0 + 0.5).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Camera Raw XMP
#
# Graduated and radial masks are written as crs:MaskGroupBasedCorrections
# (What=Mask/Gradient, What=Mask/CircularGradient) and brush masks as
# crs:PaintBasedCorrections (What=Mask/Brush), the structures Lightroom reads.
# Nothing in the XMP can carry the original ordering across the two lists, the
# feather model or the brush density of erase strokes, so full-fidelity masks
# also live in a JSON sidecar next to the photo; see ``load_sidecar``.
# ---------------------------------------------------------------------------

DEFAULT_ASPECT = 1.5
SIDECAR_SUFFIX = ".masks.json"
XMP_TAGS = ("MaskGroupBasedCorrections", "PaintBasedCorrections")


def _f(v: float) -> str:
    text = f"{float(v):.5f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _local_fields(adj: dict) -> list[str]:
    return [f"{tag}={_f(adj[k])}" for k, tag in LOCAL_XMP_TAGS.items() if adj.get(k)]


def _gradient_points(m: dict, aspect: float) -> tuple[float, float, float, float]:
    """(ZeroX, ZeroY, FullX, FullY) for a linear mask on a picture of the given aspect."""
    theta = math.radians(float(m["angle"]))
    nx, ny = -math.sin(theta), math.cos(theta)
    extent = abs(aspect * nx) + abs(ny)
    half = max(float(m["feather"]) / 100.0 * extent, 0.002) / 2.0
    cx, cy = float(m["x"]) * aspect, float(m["y"])
    return ((cx + nx * half) / aspect, cy + ny * half,
            (cx - nx * half) / aspect, cy - ny * half)


def _correction(m: dict, masks_fields: list[str]) -> str:
    fields = ["What=Correction", "CorrectionAmount=1", "CorrectionActive=True",
              f"CorrectionName={m['name']}"] + _local_fields(m["adj"])
    return "{" + ",".join(fields) + ",CorrectionMasks=[" + ",".join(masks_fields) + "]}"


def _mask_fields(m: dict, aspect: float) -> str:
    base = ["MaskActive=True", "MaskBlendMode=0", f"MaskInverted={'True' if m['invert'] else 'False'}",
            "MaskValue=1"]
    if m["type"] == "linear":
        zx, zy, fx, fy = _gradient_points(m, aspect)
        return "{" + ",".join(["What=Mask/Gradient"] + base +
                              [f"ZeroX={_f(zx)}", f"ZeroY={_f(zy)}", f"FullX={_f(fx)}", f"FullY={_f(fy)}"]) + "}"
    return "{" + ",".join(["What=Mask/CircularGradient"] + base + [
        f"Left={_f(m['x'] - m['rx'])}", f"Top={_f(m['y'] - m['ry'])}",
        f"Right={_f(m['x'] + m['rx'])}", f"Bottom={_f(m['y'] + m['ry'])}",
        f"Angle={_f(m['angle'])}", "Midpoint=50", f"Feather={_f(m['feather'])}",
        "Roundness=0", "Flipped=True"]) + "}"


def _brush_correction(m: dict) -> str:
    strokes = []
    for s in m["strokes"]:
        dabs = []
        for i in range(0, len(s["pts"]), 2):
            dabs.append(f"{'m' if i == 0 else 'd'} {_f(s['pts'][i])} {_f(s['pts'][i + 1])}")
        strokes.append("{" + ",".join([
            "What=Mask/Brush",
            f"MaskValue={_f(0 if s['erase'] else s['density'] / 100.0)}",
            f"Radius={_f(s['size'])}", f"Flow={_f(s['flow'] / 100.0)}",
            f"CenterWeight={_f(1.0 - s['feather'] / 100.0)}",
            "Dabs=[" + ",".join(dabs) + "]"]) + "}")
    fields = ["CorrectionAmount=1", f"CorrectionName={m['name']}"] + _local_fields(m["adj"])
    return "{" + ",".join(fields) + ",CorrectionMasks=[" + ",".join(strokes) + "]}"


def _xmp_masks(masks) -> list[dict]:
    """The masks that make it into the XMP: effective ones with some geometry."""
    # Range selections and combined (add / subtract / intersect) masks only live in the JSON sidecar.
    return [g[0] for g in groups(normalise_masks(masks))
            if g[0].get("enabled", True) and any(g[0]["adj"].values())
            and g[0]["type"] in GEOMETRY_TYPES and (g[0]["type"] != "brush" or g[0]["strokes"])]


def xmp_arguments(masks, aspect: float | None = None) -> list[str]:
    """exiftool arguments for the masks. Always both tags, so stale masks are cleared."""
    aspect = float(aspect) if aspect and aspect > 0 else DEFAULT_ASPECT
    group, paint = [], []
    for m in _xmp_masks(masks):
        if m["type"] == "brush":
            paint.append(_brush_correction(m))
        else:
            group.append(_correction(m, [_mask_fields(m, aspect)]))
    return [
        "-XMP-crs:MaskGroupBasedCorrections=" + ("[" + ",".join(group) + "]" if group else ""),
        "-XMP-crs:PaintBasedCorrections=" + ("[" + ",".join(paint) + "]" if paint else ""),
    ]


def xmp_counts(masks) -> tuple[int, int, int]:
    """(linear, radial, brush) counts as the XMP will hold them."""
    eff = _xmp_masks(masks)
    return tuple(sum(1 for m in eff if m["type"] == t) for t in GEOMETRY_TYPES)  # type: ignore[return-value]


def _truthy(v) -> bool:
    return v is True or str(v).strip().lower() == "true"


def _as_list(v) -> list:
    return v if isinstance(v, list) else ([] if v in (None, "") else [v])


def _local_from(corr: dict) -> dict:
    return {k: corr[tag] for k, tag in LOCAL_XMP_TAGS.items() if tag in corr}


def parse_xmp(data: dict | None, aspect: float | None = None) -> list[dict]:
    """
    Masks from exiftool ``-struct -json`` output (a dict holding
    MaskGroupBasedCorrections and/or PaintBasedCorrections). Unsupported mask
    kinds (AI, range, colour) are skipped.
    """
    aspect = float(aspect) if aspect and aspect > 0 else DEFAULT_ASPECT
    out: list = []
    data = data or {}
    for corr in _as_list(data.get("MaskGroupBasedCorrections")):
        if not isinstance(corr, dict):
            continue
        for sub in _as_list(corr.get("CorrectionMasks"))[:1]:
            if not isinstance(sub, dict):
                continue
            what = str(sub.get("What", ""))
            base = {"name": corr.get("CorrectionName"), "adj": _local_from(corr),
                    "invert": _truthy(sub.get("MaskInverted", False))}
            try:
                if what == "Mask/Gradient":
                    zx, zy, fx, fy = (float(sub[k]) for k in ("ZeroX", "ZeroY", "FullX", "FullY"))
                    dx, dy = (zx - fx) * aspect, zy - fy
                    w = math.hypot(dx, dy)
                    if w <= 1e-9:
                        continue
                    nx, ny = dx / w, dy / w
                    extent = abs(aspect * nx) + abs(ny)
                    out.append({**base, "type": "linear",
                                "x": (zx + fx) / 2.0, "y": (zy + fy) / 2.0,
                                "angle": math.degrees(math.atan2(-nx, ny)),
                                "feather": w / extent * 100.0})
                elif what == "Mask/CircularGradient":
                    left, top, right, bottom = (float(sub[k]) for k in ("Left", "Top", "Right", "Bottom"))
                    out.append({**base, "type": "radial",
                                "x": (left + right) / 2.0, "y": (top + bottom) / 2.0,
                                "rx": abs(right - left) / 2.0, "ry": abs(bottom - top) / 2.0,
                                "angle": float(sub.get("Angle", 0) or 0),
                                "feather": float(sub.get("Feather", 50) or 0)})
            except (KeyError, TypeError, ValueError):
                continue
    for corr in _as_list(data.get("PaintBasedCorrections")):
        if not isinstance(corr, dict):
            continue
        strokes = []
        for sub in _as_list(corr.get("CorrectionMasks")):
            if not isinstance(sub, dict):
                continue
            pts: list[float] = []
            for dab in _as_list(sub.get("Dabs")):
                parts = str(dab).split()
                if len(parts) >= 3:
                    try:
                        pts += [float(parts[1]), float(parts[2])]
                    except ValueError:
                        pass
            if not pts:
                continue
            value = float(sub.get("MaskValue", 1) or 0)
            strokes.append({"size": sub.get("Radius", BRUSH_DEFAULTS["size"]),
                            "flow": float(sub.get("Flow", 1) or 0) * 100.0,
                            "feather": (1.0 - float(sub.get("CenterWeight", 0.5) or 0)) * 100.0,
                            "density": value * 100.0 if value else 100.0,
                            "erase": value == 0, "pts": pts})
        if strokes:
            out.append({"type": "brush", "name": corr.get("CorrectionName"),
                        "adj": _local_from(corr), "strokes": strokes})
    return normalise_masks(out)


def xmp_read_args() -> list[str]:
    return ["-struct", "-json"] + [f"-XMP-crs:{t}" for t in XMP_TAGS]


def parse_exiftool_json(text: str, aspect: float | None = None) -> list[dict]:
    """Parse the stdout of an exiftool call built with ``xmp_read_args``."""
    try:
        rows = json.loads(text)
        row = rows[0] if isinstance(rows, list) and rows else {}
    except (ValueError, TypeError):
        return []
    return parse_xmp(row if isinstance(row, dict) else {}, aspect)


# ---------------------------------------------------------------------------
# JSON sidecar: the full-fidelity copy
# ---------------------------------------------------------------------------

def sidecar_path(photo: Path) -> Path:
    return photo.with_name(photo.name + SIDECAR_SUFFIX)


def save_sidecar(photo: Path, masks) -> None:
    """Write the masks beside the photo (or remove the file when there are none)."""
    target = sidecar_path(photo)
    clean = normalise_masks(masks)
    if not clean:
        delete_sidecar(photo)
        return
    with fsutil.lock_for(target):
        fsutil.atomic_write_text(
            target, json.dumps({"version": 1, "masks": clean}, separators=(",", ":")))


def load_sidecar(photo: Path) -> list[dict]:
    try:
        data = json.loads(sidecar_path(photo).read_text())
    except (OSError, ValueError):
        return []
    return normalise_masks(data.get("masks") if isinstance(data, dict) else None)


def delete_sidecar(photo: Path) -> None:
    try:
        sidecar_path(photo).unlink()
    except OSError:
        pass


def reconcile(sidecar_masks: list[dict], xmp_masks: list[dict]) -> list[dict]:
    """
    Choose between the full-fidelity sidecar and what the XMP says.

    The sidecar wins while the XMP still agrees with it on how many masks of
    each kind exist. If they differ, something else (Lightroom) edited the
    XMP after we wrote it, and the XMP is the truth.
    """
    if sidecar_masks and xmp_counts(sidecar_masks) == xmp_counts(xmp_masks):
        return sidecar_masks
    return xmp_masks
