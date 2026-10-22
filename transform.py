"""
Perspective transform (vertical / horizontal / rotate / aspect / scale / offset)
and Upright line detection.

Everything is a flat key in the adjustments dict, like the colour mixer, so
presets, copy/paste, history and batch apply carry it with no extra code:

    xf_vertical    -100..100   positive widens the top edge
    xf_horizontal  -100..100   positive enlarges the right edge
    xf_rotate      -45..45     degrees, positive = clockwise (on top of Straighten)
    xf_aspect      -100..100   positive stretches horizontally, area preserved
    xf_scale        50..150    percent
    xf_x, xf_y     -100..100   shift the content

The maths is one 3x3 homography in a centred pixel frame. The result is
auto-cropped to the largest rectangle (original aspect ratio) that fits inside
the warped image, so no empty corners appear. It runs before Straighten and
Crop, which therefore compose on the already-transformed frame.
"""
from __future__ import annotations

import math

import numpy as np

try:  # Optional at import time so a broken OpenCV can't stop the editor.
    import cv2
except Exception:  # pragma: no cover - exercised only without OpenCV
    cv2 = None

TILT_DEGREES = 30.0      # tilt at a slider value of +-100
ASPECT_LOG2 = 0.5        # aspect slider at +-100 stretches by 2**+-0.5
OFFSET_FRACTION = 0.25   # offset slider at +-100 moves content 25% of the frame

CONTROLS = [
    {"key": "xf_vertical",   "label": "Vertical",   "min": -100, "max": 100, "step": 1,   "default": 0},
    {"key": "xf_horizontal", "label": "Horizontal", "min": -100, "max": 100, "step": 1,   "default": 0},
    {"key": "xf_rotate",     "label": "Rotate",     "min": -45,  "max": 45,  "step": 0.1, "default": 0.0},
    {"key": "xf_aspect",     "label": "Aspect",     "min": -100, "max": 100, "step": 1,   "default": 0},
    {"key": "xf_scale",      "label": "Scale",      "min": 50,   "max": 150, "step": 1,   "default": 100},
    {"key": "xf_x",          "label": "X offset",   "min": -100, "max": 100, "step": 1,   "default": 0},
    {"key": "xf_y",          "label": "Y offset",   "min": -100, "max": 100, "step": 1,   "default": 0},
]
DEFAULTS = {c["key"]: c["default"] for c in CONTROLS}
_LIMITS = {c["key"]: (c["min"], c["max"]) for c in CONTROLS}

# Camera Raw tag for each key.
XMP_TAGS = {
    "xf_vertical": "PerspectiveVertical",
    "xf_horizontal": "PerspectiveHorizontal",
    "xf_rotate": "PerspectiveRotate",
    "xf_aspect": "PerspectiveAspect",
    "xf_scale": "PerspectiveScale",
    "xf_x": "PerspectiveX",
    "xf_y": "PerspectiveY",
}

UPRIGHT_MODES = ("auto", "level", "vertical", "full")


def values(adj: dict) -> dict:
    """The transform parameters, defaulted and clamped to their slider range."""
    out = {}
    for key, default in DEFAULTS.items():
        try:
            v = float(adj.get(key, default))
        except (TypeError, ValueError):
            v = float(default)
        if not math.isfinite(v):
            v = float(default)
        lo, hi = _LIMITS[key]
        out[key] = min(max(v, lo), hi)
    return out


def is_identity(adj: dict) -> bool:
    v = values(adj)
    return all(abs(v[k] - d) < 1e-9 for k, d in DEFAULTS.items())


# ---------------------------------------------------------------------------
# Homography
# ---------------------------------------------------------------------------

def homography(w: float, h: float, adj: dict) -> np.ndarray:
    """
    3x3 matrix mapping source pixel coordinates to the (uncropped) output
    canvas of the same size. Identity when every parameter is neutral.
    """
    v = values(adj)
    f = float(max(w, h))                       # nominal focal length in pixels
    theta = math.radians(v["xf_vertical"] / 100.0 * TILT_DEGREES)
    phi = math.radians(v["xf_horizontal"] / 100.0 * TILT_DEGREES)
    rho = math.radians(v["xf_rotate"])

    k = np.array([[f, 0, 0], [0, f, 0], [0, 0, 1.0]])
    k_inv = np.linalg.inv(k)
    rx = np.array([[1, 0, 0],
                   [0, math.cos(theta), -math.sin(theta)],
                   [0, math.sin(theta), math.cos(theta)]])
    ry = np.array([[math.cos(phi), 0, math.sin(phi)],
                   [0, 1, 0],
                   [-math.sin(phi), 0, math.cos(phi)]])
    persp = k @ ry @ rx @ k_inv

    rz = np.array([[math.cos(rho), -math.sin(rho), 0],
                   [math.sin(rho), math.cos(rho), 0],
                   [0, 0, 1.0]])

    sx = 2.0 ** (v["xf_aspect"] / 100.0 * ASPECT_LOG2)
    s = v["xf_scale"] / 100.0
    affine = np.array([[s * sx, 0, v["xf_x"] / 100.0 * OFFSET_FRACTION * w],
                       [0, s / sx, v["xf_y"] / 100.0 * OFFSET_FRACTION * h],
                       [0, 0, 1.0]])

    to_centre = np.array([[1, 0, -w / 2.0], [0, 1, -h / 2.0], [0, 0, 1.0]])
    from_centre = np.array([[1, 0, w / 2.0], [0, 1, h / 2.0], [0, 0, 1.0]])
    return from_centre @ affine @ rz @ persp @ to_centre


def map_points(matrix: np.ndarray, pts) -> np.ndarray:
    """Apply a homography to an (N, 2) array of points."""
    pts = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
    hom = np.hstack([pts, np.ones((len(pts), 1))]) @ matrix.T
    return hom[:, :2] / hom[:, 2:3]


def corners_after(w: float, h: float, adj: dict) -> np.ndarray:
    """Where the four source corners (TL, TR, BR, BL) land on the canvas."""
    src = [(0, 0), (w, 0), (w, h), (0, h)]
    return map_points(homography(w, h, adj), src)


def _inside_convex(quad: np.ndarray, pts: np.ndarray, eps: float = 0.0) -> bool:
    """All of ``pts`` at least ``eps`` pixels inside the convex ``quad``."""
    x, y = quad[:, 0], quad[:, 1]
    area = float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))
    sign = 1.0 if area > 0 else -1.0
    n = len(quad)
    for i in range(n):
        a, b = quad[i], quad[(i + 1) % n]
        edge = b - a
        norm = float(np.hypot(*edge)) or 1.0
        for p in pts:
            cross = edge[0] * (p[1] - a[1]) - edge[1] * (p[0] - a[0])
            if cross * sign < eps * norm:
                return False
    return True


def plan(w: int, h: int, adj: dict) -> dict:
    """
    Work out the crop that hides the empty corners.

    Returns ``identity`` (nothing to do), or the crop rectangle on the canvas
    and the matrix that renders straight into it, so the image is resampled
    once. Falls back to an uncropped, edge-replicated canvas if no sensible
    rectangle exists (extreme offsets).
    """
    if is_identity(adj) or w < 8 or h < 8:
        return {"identity": True, "x0": 0, "y0": 0, "w": int(w), "h": int(h),
                "matrix": np.eye(3)}

    matrix = homography(w, h, adj)
    quad = corners_after(w, h, adj)
    if not np.all(np.isfinite(quad)):
        return {"identity": True, "x0": 0, "y0": 0, "w": int(w), "h": int(h),
                "matrix": np.eye(3)}

    centre = np.array([w / 2.0, h / 2.0])
    if not _inside_convex(quad, centre[None, :]):
        centre = quad.mean(axis=0)

    def fits(t: float) -> bool:
        hw, hh = t * w / 2.0, t * h / 2.0
        box = np.array([[centre[0] - hw, centre[1] - hh], [centre[0] + hw, centre[1] - hh],
                        [centre[0] + hw, centre[1] + hh], [centre[0] - hw, centre[1] + hh]])
        # One pixel of slack: bilinear sampling right on the edge blends in
        # the replicated border.
        return _inside_convex(quad, box, eps=1.0)

    if not fits(0.05):
        return {"identity": False, "x0": 0, "y0": 0, "w": int(w), "h": int(h),
                "matrix": matrix, "uncropped": True}

    lo, hi = 0.05, 1.0
    if fits(hi):
        lo = hi
    else:
        for _ in range(30):
            mid = (lo + hi) / 2.0
            if fits(mid):
                lo = mid
            else:
                hi = mid
    t = lo
    cw, ch = max(8, int(t * w)), max(8, int(t * h))
    x0 = int(round(centre[0] - cw / 2.0))
    y0 = int(round(centre[1] - ch / 2.0))
    shift = np.array([[1, 0, -x0], [0, 1, -y0], [0, 0, 1.0]])
    return {"identity": False, "x0": x0, "y0": y0, "w": cw, "h": ch,
            "matrix": shift @ matrix}


def output_size(w: int, h: int, adj: dict) -> tuple[int, int]:
    """Frame size after the transform, which Straighten and Crop then see."""
    p = plan(w, h, adj)
    return p["w"], p["h"]


def apply(img: np.ndarray, adj: dict) -> np.ndarray:
    """Warp ``img`` (float 0..1, HxWx3). Returns ``img`` itself when neutral."""
    if is_identity(adj) or cv2 is None:
        return img
    try:
        h, w = img.shape[:2]
        p = plan(w, h, adj)
        if p["identity"]:
            return img
        out = cv2.warpPerspective(
            np.ascontiguousarray(img, dtype=np.float32), p["matrix"],
            (p["w"], p["h"]), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        return out.reshape(p["h"], p["w"], -1).astype(np.float32, copy=False)
    except Exception:
        # A transform that fails must never take the editor down with it.
        return img


# ---------------------------------------------------------------------------
# Upright: line detection
# ---------------------------------------------------------------------------

DETECT_EDGE = 800   # lines are found on a copy shrunk to this long edge


def detect_lines(img: np.ndarray) -> np.ndarray:
    """
    Line segments as an (N, 4) array of x1, y1, x2, y2 in the coordinates of
    ``img``. Empty when OpenCV is missing or nothing convincing is found.
    """
    if cv2 is None:
        return np.zeros((0, 4))
    try:
        h, w = img.shape[:2]
        scale = min(1.0, DETECT_EDGE / float(max(h, w)))
        u8 = np.clip(img * 255.0 + 0.5, 0, 255).astype(np.uint8)
        if scale < 1.0:
            u8 = cv2.resize(u8, (max(8, int(w * scale)), max(8, int(h * scale))),
                            interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(u8, cv2.COLOR_RGB2GRAY)
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        edges = cv2.Canny(gray, 50, 150)
        min_len = 0.12 * min(gray.shape)
        found = cv2.HoughLinesP(edges, 1, np.pi / 360.0, threshold=40,
                                minLineLength=min_len, maxLineGap=6)
        if found is None:
            return np.zeros((0, 4))
        return found.reshape(-1, 4).astype(np.float64) / scale
    except Exception:
        return np.zeros((0, 4))


def _angles_len(lines: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    d = lines[:, 2:4] - lines[:, 0:2]
    return np.degrees(np.arctan2(d[:, 1], d[:, 0])), np.hypot(d[:, 0], d[:, 1])


def _split(lines: np.ndarray, tol: float = 25.0):
    """Near-horizontal and near-vertical segments."""
    if len(lines) == 0:
        return lines, lines
    ang, _ = _angles_len(lines)
    a = np.abs(((ang + 90.0) % 180.0) - 90.0)      # 0 = horizontal, 90 = vertical
    return lines[a < tol], lines[a > 90.0 - tol]


def _skew_cost(lines: np.ndarray, matrix: np.ndarray, vertical: bool) -> float:
    """Length-weighted squared sine of each line's deviation after ``matrix``."""
    if len(lines) == 0:
        return 0.0
    p = map_points(matrix, lines[:, 0:2])
    q = map_points(matrix, lines[:, 2:4])
    d = q - p
    length = np.hypot(d[:, 0], d[:, 1]) + 1e-9
    off = d[:, 0] if vertical else d[:, 1]
    return float(np.sum(length * (off / length) ** 2) / np.sum(length))


def _minimise(fn, lo: float, hi: float, coarse: float, fine: float) -> float:
    grid = np.arange(lo, hi + 1e-9, coarse)
    best = min(grid, key=fn)
    grid = np.arange(best - coarse, best + coarse + 1e-9, fine)
    return float(min(grid, key=fn))


MAX_GUIDES = 4      # Guided Upright takes up to four user-drawn lines


def guide_lines(guides, width: int, height: int) -> np.ndarray:
    """
    Pixel segments (N, 4) from normalised guides ``[{x1, y1, x2, y2}, ...]``.

    Malformed, non-finite and (near) zero-length guides are dropped; at most
    ``MAX_GUIDES`` are kept. Coordinates are clamped to the frame.
    """
    rows = []
    for g in list(guides or [])[:MAX_GUIDES]:
        try:
            x1, y1, x2, y2 = (float(g[k]) for k in ("x1", "y1", "x2", "y2"))
        except (TypeError, KeyError, ValueError):
            continue
        if not all(np.isfinite([x1, y1, x2, y2])):
            continue
        x1, x2 = (min(1.0, max(0.0, v)) * width for v in (x1, x2))
        y1, y2 = (min(1.0, max(0.0, v)) * height for v in (y1, y2))
        if np.hypot(x2 - x1, y2 - y1) >= 0.02 * min(width, height):
            rows.append((x1, y1, x2, y2))
    return np.array(rows, dtype=np.float64).reshape(-1, 4)


def upright(img: np.ndarray, mode: str = "auto", guides=None) -> dict:
    """
    Suggest transform slider values that straighten the picture.

    ``level`` rotates only; ``vertical`` also fixes converging verticals;
    ``full`` also fixes converging horizontals; ``auto`` picks whichever of
    those the detected lines support. Anything inconclusive returns the
    neutral values, so the button never makes a picture worse by guessing.

    ``guides`` (Guided Upright) replaces line detection with the user's own
    lines, normalised ``{x1, y1, x2, y2}`` in the frame ``img`` shows. Because
    the user chose them, two lines are enough to correct a direction.
    """
    if mode not in UPRIGHT_MODES:
        raise ValueError(f"unknown upright mode {mode!r}")
    h, w = img.shape[:2]
    result = dict(DEFAULTS)
    guided = guides is not None
    lines = guide_lines(guides, w, h) if guided else detect_lines(img)
    need = 2 if guided else 3
    horiz, vert = _split(lines)
    info = {"guided": guided, "lines": int(len(lines)), "horizontal": int(len(horiz)), "vertical": int(len(vert))}

    def build(**kw):
        a = dict(DEFAULTS)
        a.update(kw)
        return homography(w, h, a)

    # --- Level: whichever family of lines is better supported.
    rot = 0.0
    if len(horiz) >= 2 or len(vert) >= 2:
        use_h = len(horiz) >= len(vert)
        fam, is_vert = (horiz, False) if use_h else (vert, True)
        rot = _minimise(lambda r: _skew_cost(fam, build(xf_rotate=r), is_vert), -20, 20, 1.0, 0.1)
        # Only trust it if it genuinely reduced the skew.
        if _skew_cost(fam, build(xf_rotate=rot), is_vert) > 0.8 * _skew_cost(fam, build(), is_vert):
            rot = 0.0
    result["xf_rotate"] = round(rot, 1)
    if mode == "level":
        result["info"] = info
        return result

    # --- Vertical: converging verticals.
    def cost_v(v):
        return _skew_cost(vert, build(xf_rotate=rot, xf_vertical=v), True)

    if len(vert) >= need:
        v = _minimise(cost_v, -100, 100, 4.0, 0.5)
        if cost_v(v) < 0.7 * cost_v(0.0):
            result["xf_vertical"] = round(v, 1)

    # --- Horizontal: converging horizontals.
    if mode in ("full", "auto") and len(horiz) >= need:
        vv = result["xf_vertical"]

        def cost_h(hh):
            return _skew_cost(horiz, build(xf_rotate=rot, xf_vertical=vv, xf_horizontal=hh), False)

        hz = _minimise(cost_h, -100, 100, 4.0, 0.5)
        # Auto is conservative: horizontal correction must clearly help.
        needed = 0.5 if mode == "auto" else 0.7
        if cost_h(hz) < needed * cost_h(0.0):
            result["xf_horizontal"] = round(hz, 1)

    result["info"] = info
    return result
