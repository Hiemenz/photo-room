"""
Spot removal: circular clone / heal patches.

A spot is a plain dict, stored under the ``spots`` key of the adjustments:

    {"x": 0.30, "y": 0.40,      destination centre, fractions of the frame
     "r": 0.02,                 radius, fraction of the frame WIDTH
     "sx": 0.45, "sy": 0.40,    source centre, fractions of the frame
     "feather": 50,             0..100, how soft the edge is
     "opacity": 100,            0..100
     "mode": "heal",            "clone" copies pixels, "heal" also matches
                                the destination's tone (Poisson blending)
     "shape": "circle"}         "circle" or "square" (r is then the half side)

Everything is normalised, so the same list repairs the half-size preview and
the full-resolution export identically. Spots apply to the original frame,
before lens correction, transform, straighten and crop; that is why moving
the crop never moves a repair.

Rendering is deterministic: no random numbers are involved.
"""
from __future__ import annotations

import math

import numpy as np

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

MAX_SPOTS = 64
MIN_RADIUS = 0.002
MAX_RADIUS = 0.25
MODES = ("clone", "heal")
SHAPES = ("circle", "square")


def _num(value, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def normalise(spots) -> list[dict]:
    """Clean a list of spots from the client or a file; drop anything unusable."""
    if not isinstance(spots, (list, tuple)):
        return []
    out = []
    for raw in spots[:MAX_SPOTS]:
        if not isinstance(raw, dict) or "x" not in raw or "y" not in raw:
            continue
        x = min(max(_num(raw["x"], -1.0), 0.0), 1.0)
        y = min(max(_num(raw["y"], -1.0), 0.0), 1.0)
        if _num(raw["x"], -1.0) < 0 or _num(raw["y"], -1.0) < 0:
            continue
        r = min(max(_num(raw.get("r"), 0.02), MIN_RADIUS), MAX_RADIUS)
        mode = raw.get("mode") if raw.get("mode") in MODES else "heal"
        out.append({
            "x": x, "y": y, "r": r,
            "sx": min(max(_num(raw.get("sx"), min(x + 3 * r, 1.0)), 0.0), 1.0),
            "sy": min(max(_num(raw.get("sy"), y), 0.0), 1.0),
            "feather": min(max(_num(raw.get("feather"), 50.0), 0.0), 100.0),
            "opacity": min(max(_num(raw.get("opacity"), 100.0), 0.0), 100.0),
            "mode": mode,
            "shape": raw.get("shape") if raw.get("shape") in SHAPES else "circle",
        })
    return out


def geometry(spot: dict, w: int, h: int) -> dict:
    """
    Pixel geometry for one spot on a ``w`` x ``h`` frame: integer destination
    and source centres and the radius. The source centre is pulled inward so
    the whole source disc lies inside the frame.
    """
    r = max(2, int(round(spot["r"] * w)))
    r = min(r, max(2, min(w, h) // 2 - 1))
    cx = int(round(spot["x"] * (w - 1)))
    cy = int(round(spot["y"] * (h - 1)))
    sx = int(round(spot["sx"] * (w - 1)))
    sy = int(round(spot["sy"] * (h - 1)))
    sx = min(max(sx, r), w - 1 - r)
    sy = min(max(sy, r), h - 1 - r)
    return {"cx": cx, "cy": cy, "sx": sx, "sy": sy, "r": r}


def _alpha(size: int, r: int, feather: float, opacity: float, shape: str = "circle") -> np.ndarray:
    """Soft disc (or square) mask, (size, size), centred; 1 inside, fading over the feather."""
    c = (size - 1) / 2.0
    yy, xx = np.mgrid[0:size, 0:size]
    if shape == "square":
        d = np.maximum(np.abs(xx - c), np.abs(yy - c))
    else:
        d = np.hypot(xx - c, yy - c)
    inner = r * (1.0 - feather / 100.0)
    t = np.clip((r - d) / max(r - inner, 1e-6), 0.0, 1.0)
    t = t * t * (3.0 - 2.0 * t)                     # smoothstep
    t[d > r] = 0.0
    return (t * (opacity / 100.0)).astype(np.float32)


def _heal(src: np.ndarray, dst: np.ndarray, r: int, shape: str = "circle") -> np.ndarray | None:
    """
    Poisson-blend ``src`` into ``dst`` (both float 0..1 patches) inside a disc
    (or square) of radius ``r``. None if OpenCV is unavailable or refuses the patch.
    """
    if cv2 is None:
        return None
    size = dst.shape[0]
    mask = np.zeros((size, size), np.uint8)
    if shape == "square":
        m = size // 2
        k = max(1, r - 1)
        mask[m - k:m + k + 1, m - k:m + k + 1] = 255
    else:
        cv2.circle(mask, (size // 2, size // 2), max(1, r - 1), 255, -1)
    try:
        s8 = np.clip(src * 255.0 + 0.5, 0, 255).astype(np.uint8)
        d8 = np.clip(dst * 255.0 + 0.5, 0, 255).astype(np.uint8)
        out = cv2.seamlessClone(s8, d8, mask, (size // 2, size // 2), cv2.NORMAL_CLONE)
    except Exception:
        return None
    return out.astype(np.float32) / 255.0


def apply_spot(img: np.ndarray, spot: dict) -> None:
    """Apply one spot to ``img`` in place."""
    h, w = img.shape[:2]
    if w < 16 or h < 16:
        return
    g = geometry(spot, w, h)
    r, cx, cy, sx, sy = g["r"], g["cx"], g["cy"], g["sx"], g["sy"]
    pad = 2
    half = r + pad
    size = 2 * half + 1

    # The destination window must fit in the frame; clip the disc if the spot
    # sits against an edge by shifting the window and keeping the centre.
    x0, y0 = cx - half, cy - half
    if x0 < 0 or y0 < 0 or x0 + size > w or y0 + size > h:
        # Edge spot: plain clone is robust and needs no window of its own.
        _clone_clipped(img, spot, g)
        return

    sx0, sy0 = sx - half, sy - half
    if sx0 < 0 or sy0 < 0 or sx0 + size > w or sy0 + size > h:
        sx0 = min(max(sx0, 0), w - size)
        sy0 = min(max(sy0, 0), h - size)

    dst = img[y0:y0 + size, x0:x0 + size]
    src = img[sy0:sy0 + size, sx0:sx0 + size].copy()
    alpha = _alpha(size, r, spot["feather"], spot["opacity"], spot.get("shape", "circle"))

    patch = None
    if spot["mode"] == "heal":
        patch = _heal(src, dst.copy(), r, spot.get("shape", "circle"))
    if patch is None:
        patch = src
    dst[:] = dst * (1.0 - alpha[:, :, None]) + patch * alpha[:, :, None]


def _clone_clipped(img: np.ndarray, spot: dict, g: dict) -> None:
    """Clone for a spot whose window crosses the frame edge."""
    h, w = img.shape[:2]
    r, cx, cy, sx, sy = g["r"], g["cx"], g["cy"], g["sx"], g["sy"]
    size = 2 * r + 1
    alpha_full = _alpha(size, r, spot["feather"], spot["opacity"], spot.get("shape", "circle"))
    x0, y0 = cx - r, cy - r
    ax0, ay0 = max(0, -x0), max(0, -y0)
    ax1, ay1 = size - max(0, x0 + size - w), size - max(0, y0 + size - h)
    dx0, dy0 = x0 + ax0, y0 + ay0
    dx1, dy1 = x0 + ax1, y0 + ay1
    if dx1 <= dx0 or dy1 <= dy0:
        return
    s_x0, s_y0 = sx - r + ax0, sy - r + ay0
    src = img[s_y0:s_y0 + (dy1 - dy0), s_x0:s_x0 + (dx1 - dx0)].copy()
    alpha = alpha_full[ay0:ay1, ax0:ax1, None]
    dst = img[dy0:dy1, dx0:dx1]
    if src.shape != dst.shape:
        return
    dst[:] = dst * (1.0 - alpha) + src * alpha


def apply(img: np.ndarray, adj: dict) -> np.ndarray:
    """Return ``img`` with every spot applied (a copy; ``img`` is untouched)."""
    spots = normalise(adj.get("spots"))
    if not spots:
        return img
    out = np.array(img, dtype=np.float32, copy=True)
    for spot in spots:
        try:
            apply_spot(out, spot)
        except Exception:
            continue      # one bad spot must not lose the rest
    return out


# ---------------------------------------------------------------------------
# Choosing a source automatically
# ---------------------------------------------------------------------------

def suggest_source(img: np.ndarray, x: float, y: float, r: float) -> tuple[float, float]:
    """
    Pick a source centre for a spot at (``x``, ``y``): the nearby patch whose
    surroundings look most like the destination's, preferring close ones.

    Candidates sit on rings around the spot; each is scored by how well the
    ring of pixels just outside its disc matches the ring outside the
    destination (mean colour and local contrast). Deterministic.
    """
    h, w = img.shape[:2]
    scale = min(1.0, 400.0 / max(h, w))
    small = img
    if scale < 1.0 and cv2 is not None:
        small = cv2.resize(img, (max(16, int(w * scale)), max(16, int(h * scale))),
                           interpolation=cv2.INTER_AREA)
    sh, sw = small.shape[:2]
    rad = max(2.0, r * sw)
    cx, cy = x * (sw - 1), y * (sh - 1)

    def ring_stats(px: float, py: float):
        x0, x1 = int(max(0, px - 2.2 * rad)), int(min(sw, px + 2.2 * rad + 1))
        y0, y1 = int(max(0, py - 2.2 * rad)), int(min(sh, py + 2.2 * rad + 1))
        win = small[y0:y1, x0:x1]
        gy, gx = np.mgrid[y0:y1, x0:x1]
        d = np.hypot(gx - px, gy - py)
        ring = (d > 1.15 * rad) & (d < 2.2 * rad)
        if ring.sum() < 8:
            return None
        px_vals = win[ring]
        return px_vals.mean(axis=0), px_vals.std(axis=0).mean()

    target = ring_stats(cx, cy)
    best, best_score = None, float("inf")
    if target is not None:
        for dist in (2.6, 3.4, 4.5, 6.0):
            for k in range(16):
                ang = 2.0 * math.pi * k / 16.0
                px = cx + math.cos(ang) * dist * rad
                py = cy + math.sin(ang) * dist * rad
                if px < rad or py < rad or px > sw - 1 - rad or py > sh - 1 - rad:
                    continue
                stats = ring_stats(px, py)
                if stats is None:
                    continue
                score = (float(np.abs(stats[0] - target[0]).sum())
                         + float(abs(stats[1] - target[1])) + 0.02 * dist)
                if score < best_score:
                    best, best_score = (px, py), score
    if best is None:
        # Nothing fits (tiny frame, spot in a corner): step sideways.
        dx = 3.0 * rad if cx + 3.0 * rad < sw - 1 else -3.0 * rad
        best = (cx + dx, cy)
    return (min(max(best[0] / (sw - 1), 0.0), 1.0), min(max(best[1] / (sh - 1), 0.0), 1.0))


# ---------------------------------------------------------------------------
# Visualise spots and the dust finder
# ---------------------------------------------------------------------------

ANALYSIS_EDGE = 1600
MAX_DUST = 40


def _analysis_gray(img: np.ndarray) -> np.ndarray:
    """Float grayscale copy of ``img``, at most ``ANALYSIS_EDGE`` px on the long side."""
    gray = np.asarray(img, dtype=np.float32)
    gray = gray.mean(axis=2) if gray.ndim == 3 else gray
    h, w = gray.shape
    scale = min(1.0, ANALYSIS_EDGE / max(h, w))
    if scale < 1.0 and cv2 is not None:
        gray = cv2.resize(gray, (max(16, int(w * scale)), max(16, int(h * scale))),
                          interpolation=cv2.INTER_AREA)
    return np.ascontiguousarray(gray)


def visualise(img: np.ndarray, threshold: float = 50.0) -> np.ndarray:
    """
    The "Visualize Spots" map: a white page where fine detail shows as dark
    marks, so dust and sensor spots stand out. ``threshold`` 0..100; higher
    shows fainter marks. Returns a uint8 (h, w) array of the analysis size.
    """
    gray = _analysis_gray(img)
    thr = min(max(_num(threshold, 50.0), 0.0), 100.0) / 100.0
    if cv2 is not None:
        local = cv2.GaussianBlur(gray, (0, 0), max(2.0, gray.shape[1] / 300.0))
    else:  # pragma: no cover
        local = np.full_like(gray, float(gray.mean()))
    diff = np.abs(gray - local)
    full_scale = 0.20 * (1.0 - 0.95 * thr) + 0.005       # difference that reads as solid black
    out = 1.0 - np.clip(diff / full_scale, 0.0, 1.0)
    return (out * 255.0 + 0.5).astype(np.uint8)


def find_dust(img: np.ndarray, sensitivity: float = 50.0, limit: int = 24) -> list[dict]:
    """
    Propose spots for small dark blobs (sensor dust, specks): a black-hat
    filter finds marks smaller than a dust-sized kernel, connected blobs of the
    right size and roundness become candidates, strongest first. Each is a
    normalised spot dict WITHOUT a source (the caller picks one with
    ``suggest_source``). Deterministic; ``sensitivity`` 0..100.
    """
    if cv2 is None:
        return []
    gray = _analysis_gray(img)
    h, w = gray.shape
    if w < 64 or h < 64:
        return []
    sens = min(max(_num(sensitivity, 50.0), 0.0), 100.0) / 100.0
    k = int(min(41, max(7, round(w * 0.024)))) | 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    smooth = cv2.GaussianBlur(gray, (0, 0), 0.8)
    hat = cv2.morphologyEx(smooth, cv2.MORPH_BLACKHAT, kernel)
    thr = 0.14 - 0.12 * sens                               # 0.14 (strict) .. 0.02 (loose)
    mask = (hat > thr).astype(np.uint8)
    n, labels, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
    max_area = np.pi * (k / 2.0) ** 2
    found = []
    for i in range(1, n):
        bx, by, bw, bh, area = (int(v) for v in stats[i])
        if area < 5 or area > max_area:
            continue
        if max(bw, bh) > 2.2 * min(bw, bh) or area < 0.4 * bw * bh:
            continue                                       # a hair or a scratch, not a speck
        if bx <= 1 or by <= 1 or bx + bw >= w - 1 or by + bh >= h - 1:
            continue
        strength = float(hat[labels == i].mean()) * math.sqrt(area)
        radius = (math.sqrt(area / math.pi) * 1.5 + 2.0) / w
        found.append((strength, float(cents[i][0]) / (w - 1), float(cents[i][1]) / (h - 1), radius))
    found.sort(key=lambda t: (-t[0], t[2], t[1]))
    out = []
    for _, x, y, r in found[:max(1, min(int(limit), MAX_DUST))]:
        out.append({"x": round(x, 5), "y": round(y, 5), "r": round(min(max(r, 0.004), 0.05), 5)})
    return out


# ---------------------------------------------------------------------------
# XMP (crs:RetouchAreas)
# ---------------------------------------------------------------------------

def _f(v: float) -> str:
    return f"{v:.5f}".rstrip("0").rstrip(".") or "0"


def xmp_arguments(spots, frame: tuple[float, float] | None = None) -> list[str]:
    """
    exiftool argument writing ``crs:RetouchAreas``; an empty list clears it.

    Camera Raw stores each spot as a circle mask (centre + radii) plus the
    source as an absolute X and an offset in Y. The radius is a fraction of
    the width, so SizeY is scaled by the aspect ratio when ``frame`` is known.
    """
    spots = normalise(spots)
    if not spots:
        return ["-XMP-crs:RetouchAreas="]
    aspect = (frame[0] / frame[1]) if frame and frame[0] > 0 and frame[1] > 0 else 1.0
    items = []
    for s in spots:
        items.append(
            "{SpotType=%s,SourceState=sourceSetExplicitly,Method=%s,SourceX=%s,OffsetY=%s,"
            "Opacity=%s,Feather=%s,Seed=1,Masks=[{What=Mask/EllipseGradient,MaskValue=1,"
            "X=%s,Y=%s,SizeX=%s,SizeY=%s,Angle=0,Feather=%s}]}" % (
                s["mode"], "gaussian" if s["mode"] == "clone" else "healing",
                _f(s["sx"]), _f(s["sy"] - s["y"]),
                _f(s["opacity"] / 100.0), _f(s["feather"] / 100.0),
                _f(s["x"]), _f(s["y"]), _f(s["r"]), _f(s["r"] * aspect),
                _f(s["feather"] / 100.0)))
    return ["-XMP-crs:RetouchAreas=[" + ",".join(items) + "]"]


def read_arguments() -> list[str]:
    return ["-XMP-crs:RetouchAreas", "-struct", "-json"]


def parse_xmp(json_text: str) -> list[dict]:
    """Rebuild spots from ``exiftool -struct -json`` output (empty if none)."""
    import json
    try:
        data = json.loads(json_text)
        areas = data[0].get("RetouchAreas") or []
    except (ValueError, IndexError, AttributeError, TypeError):
        return []
    if isinstance(areas, dict):
        areas = [areas]
    spots = []
    for a in areas:
        try:
            mask = (a.get("Masks") or [{}])
            mask = mask[0] if isinstance(mask, list) else mask
            x, y = float(mask["X"]), float(mask["Y"])
            spots.append({
                "x": x, "y": y, "r": float(mask.get("SizeX", 0.02)),
                "sx": float(a.get("SourceX", x)), "sy": y + float(a.get("OffsetY", 0.0)),
                "feather": float(a.get("Feather", 0.5)) * 100.0,
                "opacity": float(a.get("Opacity", 1.0)) * 100.0,
                "mode": "clone" if str(a.get("SpotType", "heal")).lower() == "clone" else "heal",
            })
        except (KeyError, TypeError, ValueError):
            continue
    return normalise(spots)
