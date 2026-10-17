"""
Tone curve: a parametric curve plus a point curve (RGB master and R, G, B).

Stored on an adjustment dict as ``adj["tone_curve"]``::

    {"parametric": {"highlights": 0, "lights": 0, "darks": 0, "shadows": 0,
                    "split1": 25, "split2": 50, "split3": 75},
     "points": {"rgb": [[0, 0], [255, 255]], "red": ..., "green": ..., "blue": ...}}

Points are in Camera Raw's own 0-255 units so they round-trip through
crs:ToneCurvePV2012 (and the Red/Green/Blue variants) unchanged. The
parametric sliders map to crs:ParametricHighlights/Lights/Darks/Shadows and
the three split points to ParametricShadowSplit/MidtoneSplit/HighlightSplit.
"""

from __future__ import annotations

import json

import numpy as np

CHANNELS = ("rgb", "red", "green", "blue")
POINT_TAGS = {
    "rgb": "ToneCurvePV2012",
    "red": "ToneCurvePV2012Red",
    "green": "ToneCurvePV2012Green",
    "blue": "ToneCurvePV2012Blue",
}
# key -> (XMP tag, default)
PARAMETRIC_TAGS = {
    "highlights": ("ParametricHighlights", 0),
    "lights": ("ParametricLights", 0),
    "darks": ("ParametricDarks", 0),
    "shadows": ("ParametricShadows", 0),
    "split1": ("ParametricShadowSplit", 25),
    "split2": ("ParametricMidtoneSplit", 50),
    "split3": ("ParametricHighlightSplit", 75),
}
# Largest shift a +/-100 parametric slider makes to its region (of full scale).
PARAMETRIC_AMPLITUDE = 0.25
LUT_SIZE = 1024
FINE_SIZE = 65536   # resolution of the lookup used when a curve is applied to pixels (16-bit)
LINEAR = [[0, 0], [255, 255]]

PRESETS = {
    "Linear": LINEAR,
    "Medium Contrast": [[0, 0], [64, 52], [192, 204], [255, 255]],
    "Strong Contrast": [[0, 0], [64, 42], [192, 218], [255, 255]],
}


def default_curve() -> dict:
    return {
        "parametric": {k: d for k, (_, d) in PARAMETRIC_TAGS.items()},
        "points": {c: [list(p) for p in LINEAR] for c in CHANNELS},
    }


def normalise_points(points) -> list[list[float]]:
    """
    Clean a user-supplied point list: clamp to 0-255, sort by x, drop
    duplicate x values, force an anchor at x=0 and x=255, and make y
    non-decreasing so the curve can never invert tones.
    """
    clean: dict[float, float] = {}
    for p in points or []:
        try:
            x, y = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError):
            continue
        if x != x or y != y:  # NaN
            continue
        clean[min(255.0, max(0.0, x))] = min(255.0, max(0.0, y))
    clean.setdefault(0.0, 0.0)
    clean.setdefault(255.0, 255.0)
    xs = sorted(clean)
    ys = np.maximum.accumulate([clean[x] for x in xs])
    return [[x, float(y)] for x, y in zip(xs, ys)]


def normalise(tc) -> dict:
    """Fill defaults and clean every field of a tone-curve dict."""
    out = default_curve()
    if not isinstance(tc, dict):
        return out
    para = tc.get("parametric") or {}
    for key, (_, default) in PARAMETRIC_TAGS.items():
        try:
            out["parametric"][key] = float(para.get(key, default))
        except (TypeError, ValueError):
            pass
    for key in ("highlights", "lights", "darks", "shadows"):
        out["parametric"][key] = min(100.0, max(-100.0, out["parametric"][key]))
    # Splits must stay ordered with a little room between them.
    s1, s2, s3 = (min(99.0, max(1.0, out["parametric"][k])) for k in ("split1", "split2", "split3"))
    s2 = max(s2, s1 + 1.0)
    s3 = max(s3, s2 + 1.0)
    out["parametric"].update(split1=s1, split2=min(s2, 98.0), split3=min(s3, 99.0))
    pts = tc.get("points") or {}
    for c in CHANNELS:
        if c in pts:
            out["points"][c] = normalise_points(pts[c])
    return out


def is_identity(tc) -> bool:
    """True when the curve changes nothing (missing counts as identity)."""
    if not tc:
        return True
    tc = normalise(tc)
    p = tc["parametric"]
    if any(abs(p[k]) > 1e-9 for k in ("highlights", "lights", "darks", "shadows")):
        return False
    return all(_points_are_linear(tc["points"][c]) for c in CHANNELS)


def _points_are_linear(points) -> bool:
    return all(abs(x - y) < 1e-9 for x, y in points)


def interpolate(points, xs: np.ndarray) -> np.ndarray:
    """
    Monotone cubic (Fritsch-Carlson / PCHIP) interpolation through ``points``
    (0-255 units), evaluated at ``xs`` in 0-1 and returned in 0-1.

    The curve passes through every control point and never overshoots
    between them, so a monotone set of points gives a monotone curve.
    """
    pts = normalise_points(points)
    x = np.array([p[0] for p in pts]) / 255.0
    y = np.array([p[1] for p in pts]) / 255.0
    n = len(x)
    h = np.diff(x)
    delta = np.diff(y) / h
    m = np.zeros(n)
    if n == 2:
        m[:] = delta[0]
    else:
        m[0], m[-1] = delta[0], delta[-1]
        for i in range(1, n - 1):
            if delta[i - 1] * delta[i] <= 0:
                m[i] = 0.0
            else:
                w1, w2 = 2 * h[i] + h[i - 1], h[i] + 2 * h[i - 1]
                m[i] = (w1 + w2) / (w1 / delta[i - 1] + w2 / delta[i])
        # Keep end slopes from overshooting the first/last segment.
        for i, d in ((0, delta[0]), (n - 1, delta[-1])):
            if m[i] * d < 0:
                m[i] = 0.0
            elif abs(m[i]) > 3 * abs(d):
                m[i] = 3 * d
    xs = np.clip(xs, 0.0, 1.0)
    idx = np.clip(np.searchsorted(x, xs, side="right") - 1, 0, n - 2)
    t = (xs - x[idx]) / h[idx]
    t2, t3 = t * t, t * t * t
    out = (
        (2 * t3 - 3 * t2 + 1) * y[idx]
        + (t3 - 2 * t2 + t) * h[idx] * m[idx]
        + (-2 * t3 + 3 * t2) * y[idx + 1]
        + (t3 - t2) * h[idx] * m[idx + 1]
    )
    return np.clip(out, 0.0, 1.0)


def parametric_curve(para: dict, xs: np.ndarray) -> np.ndarray:
    """
    The parametric sliders as a curve over ``xs`` (0-1). Each slider is a
    triangular bump centred in its region (regions are cut by the three
    splits); both ends have zero weight so black and white stay put.
    """
    s1, s2, s3 = (para["split1"] / 100.0, para["split2"] / 100.0, para["split3"] / 100.0)
    c_sh, c_dk, c_li, c_hi = s1 / 2, (s1 + s2) / 2, (s2 + s3) / 2, (s3 + 1) / 2
    knots = [
        ("shadows", [0, c_sh, c_dk], [0, 1, 0]),
        ("darks", [c_sh, c_dk, c_li], [0, 1, 0]),
        ("lights", [c_dk, c_li, c_hi], [0, 1, 0]),
        ("highlights", [c_li, c_hi, 1.0], [0, 1, 0]),
    ]
    out = xs.copy()
    for key, kx, ky in knots:
        amount = para[key] / 100.0 * PARAMETRIC_AMPLITUDE
        if amount:
            out = out + amount * np.interp(xs, kx, ky, left=0.0, right=0.0)
    return np.clip(np.maximum.accumulate(out), 0.0, 1.0)


def build_luts(tc) -> np.ndarray:
    """Per-channel lookup tables, shape (3, LUT_SIZE), values 0-1."""
    tc = normalise(tc)
    xs = np.linspace(0.0, 1.0, LUT_SIZE)
    base = parametric_curve(tc["parametric"], xs)
    base = interpolate(tc["points"]["rgb"], base)
    luts = np.empty((3, LUT_SIZE))
    for i, c in enumerate(("red", "green", "blue")):
        luts[i] = interpolate(tc["points"][c], base)
    return luts


_applier_cache: dict = {}


def contrast_values(contrast: float, xs: np.ndarray) -> np.ndarray:
    """
    Camera Raw-style Contrast as an S-curve about the encoded mid-point, 0..1 -> 0..1.

    A logistic sigmoid whose steepness follows the slider (about 1.75x slope at the
    centre for +100); negative values are its inverse (flattening). Unlike a
    straight scale about 0.5 it never clips, so the ends of the range keep detail.
    """
    a = float(np.clip(contrast, -100.0, 100.0)) / 100.0
    xs = np.asarray(xs, dtype=np.float64)
    if abs(a) < 1e-6:
        return xs
    k = 6.5 * abs(a)
    lo, hi = 1.0 / (1.0 + np.exp(k / 2.0)), 1.0 / (1.0 + np.exp(-k / 2.0))
    if a > 0:
        return (1.0 / (1.0 + np.exp(-k * (xs - 0.5))) - lo) / (hi - lo)
    p = np.clip(xs * (hi - lo) + lo, 1e-12, 1 - 1e-12)
    return np.clip(0.5 + np.log(p / (1.0 - p)) / k, 0.0, 1.0)


def curve_applier(tc, contrast: float = 0.0):
    """
    Return ``apply(img)`` that applies the Contrast S-curve and then the curve to a
    float32 (..., 3) array in 0..1 *in place* (and returns it), or None when both
    are neutral.

    Same curve as ``np.interp`` through the ``LUT_SIZE`` table (to within 0.01 of an
    8-bit level at the 65 537-entry lookup), but in float32 with ``np.take``
    (np.interp promotes to float64).
    """
    contrast = float(contrast or 0.0)
    if is_identity(tc) and abs(contrast) < 1e-6:
        return None
    key = json.dumps([tc, contrast], sort_keys=True, default=str)
    hit = _applier_cache.get(key)
    if hit is not None:
        return hit
    # Tables sampled from the same 1024-point curve with np.interp, indexed by
    # the nearest entry: at most slope/131072 off the exact linear interpolation,
    # and two gathers per pixel cheaper than interpolating.
    fine = np.linspace(0.0, 1.0, FINE_SIZE + 1)
    pre = contrast_values(contrast, fine) if abs(contrast) >= 1e-6 else fine
    if is_identity(tc):
        tables = [pre.astype(np.float32)] * 3
    else:
        grid = np.linspace(0.0, 1.0, LUT_SIZE)
        luts = build_luts(tc)
        tables = [np.interp(pre, grid, luts[c]).astype(np.float32) for c in range(3)]

    def apply(img: np.ndarray) -> np.ndarray:
        try:
            import cv2
        except ImportError:
            cv2 = None
        planes = list(cv2.split(img)) if cv2 is not None and img.flags.c_contiguous and img.ndim == 3 else \
            [np.ascontiguousarray(img[..., c]) for c in range(3)]
        for c in range(3):
            pos = planes[c] * np.float32(FINE_SIZE)
            pos += np.float32(0.5)
            planes[c] = np.take(tables[c], pos.astype(np.int32))
        if cv2 is not None and img.flags.c_contiguous and img.ndim == 3:
            cv2.merge(planes, dst=img)
        else:
            for c in range(3):
                img[..., c] = planes[c]
        return img

    if len(_applier_cache) > 16:
        _applier_cache.clear()
    _applier_cache[key] = apply
    return apply


def apply_tone_curve(img: np.ndarray, tc) -> np.ndarray:
    """Apply the curve to an (H, W, 3) float image in 0-1. Returns ``img`` untouched if identity."""
    fn = curve_applier(tc)
    if fn is None:
        return img
    out = np.clip(np.asarray(img, dtype=np.float32), 0.0, 1.0)     # new array; interp clamped too
    return fn(out if out is not img else out.copy())


def scale_toward_identity(tc, strength: float):
    """Blend a curve toward neutral by ``strength`` (1.0 = unchanged)."""
    if is_identity(tc) or abs(strength - 1.0) < 1e-9:
        return tc
    tc = normalise(tc)
    for k in ("highlights", "lights", "darks", "shadows"):
        tc["parametric"][k] *= strength
    for c in CHANNELS:
        tc["points"][c] = [[x, x + (y - x) * strength] for x, y in tc["points"][c]]
    return normalise(tc)


# ---------------------------------------------------------------------------
# XMP
# ---------------------------------------------------------------------------

def _xmp_points(points) -> list[list[int]]:
    """Points as Camera Raw stores them: integer pairs on the 0..255 scale.

    Lightroom only ever writes whole numbers ("64, 52"); a fractional "90.50"
    is not part of its format. Rounding can land two points on one x, so the
    later one wins and the curve stays a function.
    """
    by_x: dict[int, int] = {}
    for x, y in points:
        by_x[min(255, max(0, int(round(x))))] = min(255, max(0, int(round(y))))
    return [[x, by_x[x]] for x in sorted(by_x)]


def xmp_arguments(tc) -> list[str]:
    """
    exiftool arguments writing the curve. A neutral curve clears the tags
    so reverting actually removes the curve from the file. Lists are written
    with a ``|`` separator because Camera Raw stores each point as one item.
    """
    tc = normalise(tc) if not is_identity(tc) else None
    args = ["-sep", "|"]
    for c in CHANNELS:
        tag = f"-XMP-crs:{POINT_TAGS[c]}"
        if tc is None or _points_are_linear(tc["points"][c]):
            args.append(f"{tag}=")
        else:
            args.append(tag + "=" + "|".join(f"{x}, {y}" for x, y in _xmp_points(tc["points"][c])))
    for key, (tag, default) in PARAMETRIC_TAGS.items():
        value = default if tc is None else tc["parametric"][key]
        args.append(f"-XMP-crs:{tag}={int(round(value))}")
    args.append("-XMP-crs:ToneCurveName2012=" + ("Custom" if tc else "Linear"))
    return args


def xmp_read_tags() -> list[str]:
    return list(POINT_TAGS.values()) + [t for t, _ in PARAMETRIC_TAGS.values()]


def parse_xmp(values: list[str]) -> dict | None:
    """
    Rebuild a tone-curve dict from exiftool output for ``xmp_read_tags()``
    (one line per tag, lists joined with ``|``). None if the file has no curve.
    """
    names = xmp_read_tags()
    raw = {n: (values[i].strip() if i < len(values) else "-") for i, n in enumerate(names)}
    tc = default_curve()
    for c in CHANNELS:
        text = raw[POINT_TAGS[c]]
        if text in ("-", ""):
            continue
        pts = []
        for item in text.split("|"):
            parts = [s for s in item.replace(",", " ").split() if s]
            if len(parts) == 2:
                try:
                    pts.append([float(parts[0]), float(parts[1])])
                except ValueError:
                    pass
        if len(pts) >= 2:
            tc["points"][c] = normalise_points(pts)
    for key, (tag, _) in PARAMETRIC_TAGS.items():
        text = raw[tag]
        if text in ("-", ""):
            continue
        try:
            tc["parametric"][key] = float(text)
        except ValueError:
            continue
    tc = normalise(tc)
    return None if is_identity(tc) else tc
