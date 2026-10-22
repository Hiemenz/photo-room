"""
Grain, post-crop vignette, Point Color, Calibration and the extra sharpening controls.

Kept apart from editor.py (like ``colormix``) so the maths is testable without a raw file. All
settings are flat keys in the same adjustments dict as every other slider (``point_colors`` is the
one list), so presets, copy/paste, batch apply, history and the stage cache carry them:

    grain_amount, grain_size (25), grain_roughness (50)
    vignette_amount, vignette_midpoint (50), vignette_roundness, vignette_feather (50)
    cal_red_hue, cal_red_sat, cal_green_hue, cal_green_sat, cal_blue_hue, cal_blue_sat, cal_shadow_tint
    sharpen_radius (1.0), sharpen_detail (25), sharpen_masking
    point_colors: [{hue, sat, lum, hue_shift, sat_shift, lum_shift, range}, ...]  (up to 8)

Where each runs (editor.apply_adjustments): Calibration inside the tone stage, on linear light, before
white balance; Point Color after the colour mixer; sharpening radius / detail / masking in the detail
stage (``render_kernels.detail_stage``); grain and the vignette on the finished picture, after local
masks. Vignette and grain are positioned by absolute pixel coordinates in the *cropped output frame*
(``_frame_origin`` / ``_frame_size``), so a 100% tile is the same pixels the full frame would give.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

import fastops

try:
    import cv2
except ImportError:        # pragma: no cover - the NumPy paths cover it
    cv2 = None

F32 = np.float32

CONTROLS = [
    {"key": "grain_amount", "label": "Grain amount", "group": "grain", "min": 0, "max": 100, "step": 1, "default": 0},
    {"key": "grain_size", "label": "Grain size", "group": "grain", "min": 0, "max": 100, "step": 1, "default": 25},
    {"key": "grain_roughness", "label": "Grain roughness", "group": "grain", "min": 0, "max": 100, "step": 1, "default": 50},
    {"key": "vignette_amount", "label": "Vignette amount", "group": "vignette", "min": -100, "max": 100, "step": 1, "default": 0},
    {"key": "vignette_midpoint", "label": "Vignette midpoint", "group": "vignette", "min": 0, "max": 100, "step": 1, "default": 50},
    {"key": "vignette_roundness", "label": "Vignette roundness", "group": "vignette", "min": -100, "max": 100, "step": 1, "default": 0},
    {"key": "vignette_feather", "label": "Vignette feather", "group": "vignette", "min": 0, "max": 100, "step": 1, "default": 50},
    {"key": "cal_shadow_tint", "label": "Shadows tint", "group": "calibration", "min": -100, "max": 100, "step": 1, "default": 0},
    {"key": "cal_red_hue", "label": "Red primary hue", "group": "calibration", "min": -100, "max": 100, "step": 1, "default": 0},
    {"key": "cal_red_sat", "label": "Red primary saturation", "group": "calibration", "min": -100, "max": 100, "step": 1, "default": 0},
    {"key": "cal_green_hue", "label": "Green primary hue", "group": "calibration", "min": -100, "max": 100, "step": 1, "default": 0},
    {"key": "cal_green_sat", "label": "Green primary saturation", "group": "calibration", "min": -100, "max": 100, "step": 1, "default": 0},
    {"key": "cal_blue_hue", "label": "Blue primary hue", "group": "calibration", "min": -100, "max": 100, "step": 1, "default": 0},
    {"key": "cal_blue_sat", "label": "Blue primary saturation", "group": "calibration", "min": -100, "max": 100, "step": 1, "default": 0},
    {"key": "sharpen_radius", "label": "Sharpen radius", "group": "sharpen", "min": 0.5, "max": 3.0, "step": 0.1, "default": 1.0},
    {"key": "sharpen_detail", "label": "Sharpen detail", "group": "sharpen", "min": 0, "max": 100, "step": 1, "default": 25},
    {"key": "sharpen_masking", "label": "Sharpen masking", "group": "sharpen", "min": 0, "max": 100, "step": 1, "default": 0},
]
DEFAULTS = {c["key"]: c["default"] for c in CONTROLS}
RANGES = {c["key"]: (c["min"], c["max"]) for c in CONTROLS}

# Camera Raw (crs:) names. Grain "roughness" is crs:GrainFrequency.
XMP_TAGS = {
    "grain_amount": "GrainAmount", "grain_size": "GrainSize", "grain_roughness": "GrainFrequency",
    "vignette_amount": "PostCropVignetteAmount", "vignette_midpoint": "PostCropVignetteMidpoint",
    "vignette_roundness": "PostCropVignetteRoundness", "vignette_feather": "PostCropVignetteFeather",
    "cal_shadow_tint": "ShadowTint", "cal_red_hue": "RedHue", "cal_red_sat": "RedSaturation",
    "cal_green_hue": "GreenHue", "cal_green_sat": "GreenSaturation",
    "cal_blue_hue": "BlueHue", "cal_blue_sat": "BlueSaturation",
    "sharpen_radius": "SharpenRadius", "sharpen_detail": "SharpenDetail", "sharpen_masking": "SharpenEdgeMasking",
}
# Intensities that edit strength scales (shape, size and position parameters are not intensities).
STRENGTH_KEYS = ("grain_amount", "vignette_amount", "cal_shadow_tint", "cal_red_hue", "cal_red_sat",
                 "cal_green_hue", "cal_green_sat", "cal_blue_hue", "cal_blue_sat")

MAX_POINTS = 8
POINT_DEFAULTS = {"hue": 0.0, "sat": 50.0, "lum": 50.0, "hue_shift": 0.0, "sat_shift": 0.0,
                  "lum_shift": 0.0, "range": 50.0}
_POINT_RANGES = {"hue": (0, 360), "sat": (0, 100), "lum": (0, 100), "hue_shift": (-100, 100),
                 "sat_shift": (-100, 100), "lum_shift": (-100, 100), "range": (0, 100)}


def normalise_points(points) -> list[dict]:
    """A clean list of at most ``MAX_POINTS`` Point Color entries (bad entries are dropped)."""
    out = []
    for p in points if isinstance(points, (list, tuple)) else []:
        if not isinstance(p, dict):
            continue
        clean = {}
        for key, default in POINT_DEFAULTS.items():
            try:
                v = float(p.get(key, default))
            except (TypeError, ValueError):
                v = default
            if v != v or v in (float("inf"), float("-inf")):
                v = default
            lo, hi = _POINT_RANGES[key]
            clean[key] = min(hi, max(lo, v))
        out.append(clean)
        if len(out) >= MAX_POINTS:
            break
    return out


def points_active(adj: dict) -> list[dict]:
    """Points that change anything (a point with all shifts at 0 is a no-op)."""
    return [p for p in normalise_points(adj.get("point_colors"))
            if p["hue_shift"] or p["sat_shift"] or p["lum_shift"]]


def complete(adj: dict) -> dict:
    """Every key of this module, defaulted, for the UI and the history."""
    out = {k: adj.get(k, d) for k, d in DEFAULTS.items()}
    out["point_colors"] = normalise_points(adj.get("point_colors"))
    return out


def value(adj: dict, key: str) -> float:
    raw = adj.get(key)
    try:
        return float(DEFAULTS[key] if raw is None else raw)
    except (TypeError, ValueError):
        return float(DEFAULTS[key])


# ---------------------------------------------------------------------------
# XMP
# ---------------------------------------------------------------------------

def _point_string(p: dict) -> str:
    return ", ".join(f"{v:.6f}" for v in (
        p["hue"] / 360.0, p["sat"] / 100.0, p["lum"] / 100.0, p["hue_shift"] / 100.0,
        p["sat_shift"] / 100.0, p["lum_shift"] / 100.0, p["range"] / 100.0))


def _parse_point(text: str) -> dict | None:
    try:
        v = [float(t) for t in text.split(",")]
    except ValueError:
        return None
    if len(v) < 7:
        return None
    return normalise_points([{"hue": v[0] * 360.0, "sat": v[1] * 100.0, "lum": v[2] * 100.0,
                              "hue_shift": v[3] * 100.0, "sat_shift": v[4] * 100.0,
                              "lum_shift": v[5] * 100.0, "range": v[6] * 100.0}])[0]


def xmp_arguments(adj: dict) -> list[str]:
    """exiftool arguments for every setting here (always all, so old values are overwritten)."""
    args = []
    for key, tag in XMP_TAGS.items():
        v = value(adj, key)
        args.append(f"-XMP-crs:{tag}={v:.1f}" if key == "sharpen_radius" else f"-XMP-crs:{tag}={int(round(v))}")
    pts = normalise_points(adj.get("point_colors"))
    args.append("-XMP-crs:PointColors=")           # clear, then add each point in order
    args += [f"-XMP-crs:PointColors+={_point_string(p)}" for p in pts]
    return args


def read_tags() -> list[str]:
    return list(XMP_TAGS.values())


def parse_values(lines: list[str]) -> dict:
    """Non-default settings from ``read_tags()`` output (``-s3 -f``, one line per tag, in order)."""
    out: dict = {}
    for i, key in enumerate(XMP_TAGS):
        raw = lines[i].strip() if i < len(lines) else "-"
        if raw in ("-", ""):
            continue
        try:
            v = float(raw)
        except ValueError:
            continue
        if abs(v - DEFAULTS[key]) > 1e-9:
            lo, hi = RANGES[key]
            out[key] = min(hi, max(lo, v))
    return out


def read_from_file(path: Path, timeout: float) -> dict:
    """Settings stored in ``path`` (non-default only). Raises ``RuntimeError`` if exiftool fails."""
    def run(args: list[str]) -> str:
        try:
            res = subprocess.run(["exiftool"] + args, capture_output=True, text=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise RuntimeError(f"Could not read metadata ({exc.__class__.__name__}): {exc}") from exc
        if res.returncode != 0:
            raise RuntimeError(f"exiftool failed reading metadata: {res.stderr.strip() or res.returncode}")
        return res.stdout

    out = parse_values(run([f"-XMP-crs:{t}" for t in read_tags()] + ["-s3", "-f", str(path)]).split("\n"))
    # Its own call with a "|" separator: a point's own fields are comma separated.
    raw = run(["-sep", "|", "-XMP-crs:PointColors", "-s3", str(path)]).strip()
    points = [_parse_point(t) for t in raw.split("|")] if raw else []
    points = [p for p in points if p]
    if points:
        out["point_colors"] = points
    return out


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

CAL_HUE_DEG = 30.0           # a primary's hue slider at +-100 turns it this far round the grey axis
CAL_SAT_GAIN = 0.6           # at -100 the primary keeps 40% of its chroma, at +100 it gains 60%
CAL_TINT_STRENGTH = 0.18     # green gain change at Shadows tint = +-100, in the deepest shadows


def calibration_active(adj: dict) -> bool:
    return any(value(adj, k) for k in DEFAULTS if k.startswith("cal_"))


def calibration_matrix(adj: dict) -> np.ndarray:
    """
    3x3 matrix on linear light that moves the red, green and blue primaries.

    Each primary (a column) is split into its grey part and its chroma; the chroma is turned round the
    grey axis by the hue slider and scaled by the saturation slider. Rows are then rescaled so white
    stays exactly white (a calibration must not tint neutrals).
    """
    m = np.zeros((3, 3))
    for k, name in enumerate(("red", "green", "blue")):
        e = np.zeros(3)
        e[k] = 1.0
        grey = np.full(3, 1.0 / 3.0)
        chroma = e - grey
        theta = np.deg2rad(value(adj, f"cal_{name}_hue") / 100.0 * CAL_HUE_DEG)
        axis = np.ones(3) / np.sqrt(3.0)
        # Rodrigues rotation of the chroma vector about the grey axis (it is perpendicular to it)
        rot = chroma * np.cos(theta) + np.cross(axis, chroma) * np.sin(theta)
        gain = 1.0 + value(adj, f"cal_{name}_sat") / 100.0 * CAL_SAT_GAIN
        m[:, k] = grey + rot * gain
    row = m.sum(axis=1)
    return (m / row[:, None]).astype(F32)


def calibrate(lin: np.ndarray, adj: dict) -> np.ndarray:
    """Calibration on linear light (primaries, then the shadows tint). Returns ``lin`` when unset."""
    if not calibration_active(adj):
        return lin
    import render_kernels

    mat = calibration_matrix(adj)
    prim = not np.allclose(mat, np.eye(3), atol=1e-7)
    tint = value(adj, "cal_shadow_tint") / 100.0

    def rows(strip: np.ndarray) -> np.ndarray:
        out = strip
        if prim:
            out = cv2.transform(np.ascontiguousarray(out), mat) if render_kernels._cv_ok(out) else out @ mat.T
        if tint:
            w = render_kernels._smooth_mask(render_kernels.lightness(out), 0.45, 0.0)
            w *= F32(tint * CAL_TINT_STRENGTH)
            out = out.copy() if out is strip else out
            out[..., 1] *= (1.0 - w)                 # + = magenta (less green), as the Tint slider
            out[..., 0] *= (1.0 + w * F32(0.3))
            out[..., 2] *= (1.0 + w * F32(0.3))
        return np.maximum(out, 0.0)

    return fastops.map_rows(rows, np.ascontiguousarray(lin, dtype=F32))


# ---------------------------------------------------------------------------
# Point Color
# ---------------------------------------------------------------------------

POINT_HUE_SHIFT_DEG = 45.0
POINT_LUM_GAIN = 0.5


def _falloff(dist, half):
    t = np.clip(dist / F32(half), 0.0, 1.0)
    return 1.0 - t * t * (3.0 - 2.0 * t)


def apply_point_colors(img: np.ndarray, adj: dict) -> np.ndarray:
    """
    Move the hue, saturation and luminance of the colours near each picked point.

    A pixel is affected by a point in proportion to how close its hue, saturation and value are to the
    point's (one ``range`` slider widens all three windows). Exact no-op when no point has a shift.
    """
    pts = points_active(adj)
    if not pts:
        return img
    import colormix

    def rows(strip: np.ndarray) -> np.ndarray:
        h, s, v = colormix.rgb_to_hsv(np.ascontiguousarray(strip, dtype=F32))
        h, s, v = h.astype(F32), s.astype(F32), v.astype(F32)
        dh = np.zeros_like(h)
        ds = np.zeros_like(h)
        dv = np.zeros_like(h)
        for p in pts:
            r = p["range"] / 100.0
            hue_half, sv_half = 10.0 + 60.0 * r, 0.12 + 0.6 * r
            d = np.abs(h - F32(p["hue"]))
            d = np.minimum(d, 360.0 - d)
            w = _falloff(d, hue_half)
            w *= _falloff(np.abs(s - F32(p["sat"] / 100.0)), sv_half)
            w *= _falloff(np.abs(v - F32(p["lum"] / 100.0)), sv_half * 1.3)
            dh += w * F32(p["hue_shift"] / 100.0 * POINT_HUE_SHIFT_DEG)
            ds += w * F32(p["sat_shift"] / 100.0)
            dv += w * F32(p["lum_shift"] / 100.0 * POINT_LUM_GAIN)
        h2 = (h + dh) % F32(360.0)
        s2 = np.clip(s * (1.0 + ds), 0.0, 1.0).astype(F32)
        v2 = np.clip(v * (1.0 + dv), 0.0, 1.0).astype(F32)
        return np.clip(colormix.hsv_to_rgb(h2, s2, v2), 0.0, 1.0).astype(F32, copy=False)

    return fastops.map_rows(rows, np.ascontiguousarray(img, dtype=F32))


# ---------------------------------------------------------------------------
# Post-crop vignette and grain (positioned in the output frame)
# ---------------------------------------------------------------------------

def _frame(adj: dict, shape) -> tuple[int, int, int, int]:
    """(x0, y0, frame width, frame height) of this image inside the cropped output frame."""
    h, w = shape[:2]
    ox, oy = adj.get("_frame_origin") or (0, 0)
    fw, fh = adj.get("_frame_size") or (w, h)
    return int(ox), int(oy), int(fw), int(fh)


def vignette_active(adj: dict) -> bool:
    return bool(value(adj, "vignette_amount"))


def vignette_plane(adj: dict, shape) -> np.ndarray:
    """0..1 strength of the vignette at every pixel (0 in the middle, 1 in the corners at full feather)."""
    ox, oy, fw, fh = _frame(adj, shape)
    h, w = shape[:2]
    mid = value(adj, "vignette_midpoint") / 100.0
    rnd = value(adj, "vignette_roundness") / 100.0
    feather = value(adj, "vignette_feather") / 100.0

    hw, hh = fw / 2.0, fh / 2.0
    # +roundness pulls the ellipse towards a circle; -roundness squares it off (a larger exponent).
    t = max(rnd, 0.0)
    ax, ay = hw + (max(hw, hh) - hw) * t, hh + (max(hw, hh) - hh) * t
    p = 2.0 + 6.0 * max(-rnd, 0.0)

    def dist(dx, dy):
        return (np.abs(dx / ax) ** p + np.abs(dy / ay) ** p) ** (1.0 / p)

    xs = (np.arange(ox, ox + w, dtype=F32) + F32(0.5) - F32(hw))[None, :]
    ys = (np.arange(oy, oy + h, dtype=F32) + F32(0.5) - F32(hh))[:, None]
    d = dist(xs, ys).astype(F32) / F32(dist(hw, hh))         # 1.0 at the frame's corners
    start = mid * 0.9                                         # the effect begins inside the corners
    width = max((1.0 - start) * (0.08 + 0.92 * feather), 1e-3)
    x = np.clip((d - F32(start)) / F32(width), 0.0, 1.0)
    return x * x * (F32(3.0) - F32(2.0) * x)


def apply_vignette(img: np.ndarray, adj: dict) -> np.ndarray:
    """Darken (amount < 0) or lighten (amount > 0) towards the frame's edge. Returns ``img`` when unset."""
    if not vignette_active(adj):
        return img
    a = value(adj, "vignette_amount") / 100.0
    t = vignette_plane(adj, img.shape)
    t *= F32(abs(a))
    out = np.empty_like(img)

    def rows(i, j):
        tt = t[i:j, :, None]
        out[i:j] = img[i:j] * (1.0 - tt) if a < 0 else img[i:j] + (1.0 - img[i:j]) * tt

    fastops.for_rows(rows, img.shape[0], img.shape[1])
    return out


GRAIN_STD = 0.14            # standard deviation of the grain at Amount = 100, on display-coded values
GRAIN_SEED = 0x9E3779B1


def _hash01(ix: np.ndarray, iy: np.ndarray, seed: int) -> np.ndarray:
    """A deterministic value in [0, 1) for every integer lattice point."""
    h = (ix.astype(np.uint32) * np.uint32(0x85EBCA6B)) ^ (iy.astype(np.uint32) * np.uint32(0xC2B2AE35)) ^ np.uint32(seed)
    h ^= h >> np.uint32(16)
    h *= np.uint32(0x7FEB352D)
    h ^= h >> np.uint32(15)
    h *= np.uint32(0x846CA68B)
    h ^= h >> np.uint32(16)
    return (h >> np.uint32(8)).astype(F32) * F32(1.0 / (1 << 24))


def value_noise(x0: int, y0: int, w: int, h: int, cell: float, seed: int) -> np.ndarray:
    """Unit-variance smooth noise on a lattice of ``cell`` px, a pure function of absolute position."""
    u = (np.arange(x0, x0 + w, dtype=np.float64) + 0.5) / cell
    v = (np.arange(y0, y0 + h, dtype=np.float64) + 0.5) / cell
    iu, iv = np.floor(u).astype(np.int64), np.floor(v).astype(np.int64)
    fu, fv = (u - iu).astype(F32), (v - iv).astype(F32)
    fu = fu * fu * (F32(3.0) - F32(2.0) * fu)
    fv = fv * fv * (F32(3.0) - F32(2.0) * fv)
    u0, v0 = int(iu[0]), int(iv[0])
    nu, nv = int(iu[-1]) - u0 + 2, int(iv[-1]) - v0 + 2
    lat = _hash01(np.arange(u0, u0 + nu)[None, :], np.arange(v0, v0 + nv)[:, None], seed)
    lat = (lat - F32(0.5)) * F32(np.sqrt(12.0))                       # unit variance at the lattice
    cols = lat[:, iu - u0] * (1 - fu)[None, :] + lat[:, iu - u0 + 1] * fu[None, :]
    out = cols[iv - v0] * (1 - fv)[:, None] + cols[iv - v0 + 1] * fv[:, None]
    return out * F32(1.0 / 0.62)         # interpolation lowers the variance; bring it back near 1


def grain_plane(adj: dict, shape, edge: float) -> np.ndarray:
    """Zero-mean grain pattern (about unit variance) for the pixels of this image."""
    ox, oy, _fw, _fh = _frame(adj, shape)
    h, w = shape[:2]
    size = value(adj, "grain_size") / 100.0
    rough = value(adj, "grain_roughness") / 100.0
    cell = max(1.0, (0.4 + 2.6 * size) * edge / 3000.0)
    wgt = 0.8 * rough
    coarse = value_noise(ox, oy, w, h, cell, GRAIN_SEED)
    if wgt <= 0:
        return coarse
    fine = value_noise(ox, oy, w, h, max(0.5, cell * 0.5), GRAIN_SEED ^ 0x5BD1E995)
    return (coarse * F32(1.0 - wgt) + fine * F32(wgt)) * F32(1.0 / np.hypot(1.0 - wgt, wgt))


def apply_grain(img: np.ndarray, adj: dict) -> np.ndarray:
    """Film grain: one luminance pattern added to all channels, strongest in the midtones."""
    amount = value(adj, "grain_amount") / 100.0
    if amount <= 0:
        return img
    import render_kernels

    h, w = img.shape[:2]
    edge = float(adj.get("_detail_edge") or max(_frame(adj, img.shape)[2:]))
    noise = grain_plane(adj, img.shape, edge)
    noise *= F32(amount * GRAIN_STD)
    out = np.empty_like(img)

    def rows(i, j):
        lum = np.clip(render_kernels.luminance(img[i:j]), 0.0, 1.0)
        mid = 1.0 - F32(0.6) * (lum * F32(2.0) - F32(1.0)) ** 2           # 1 in the midtones, 0.4 at the ends
        out[i:j] = img[i:j] + (noise[i:j] * mid)[..., None]

    fastops.for_rows(rows, h, w)
    return out


def apply_finishing(img: np.ndarray, adj: dict) -> np.ndarray:
    """Vignette, then grain (grain on top so the vignette does not smooth it)."""
    return apply_grain(apply_vignette(img, adj), adj)


def finishing_active(adj: dict) -> bool:
    return vignette_active(adj) or bool(value(adj, "grain_amount"))
