"""
Red-eye and pet-eye correction.

An eye is a plain dict, stored under the ``redeye`` key of the adjustments:

    {"x": 0.40, "y": 0.30,      centre, fractions of the frame
     "r": 0.015,                radius, fraction of the frame WIDTH
     "kind": "red",             "red" (human flash) or "pet" (green/yellow/blue eyeshine)
     "amount": 100,             0..100, strength of the correction
     "darken": 50}              0..100, how dark the pupil is made

Like spots, everything is normalised and applied to the original frame before
lens correction, transform, straighten and crop, so the half-size preview, the
tiles and the full-resolution export repair the same pixels. No random numbers.
"""
from __future__ import annotations

import math

import numpy as np

MAX_EYES = 16
MIN_RADIUS = 0.002
MAX_RADIUS = 0.1
KINDS = ("red", "pet")


def _num(value, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def normalise(eyes) -> list[dict]:
    """Clean a list of eyes from the client or a file; drop anything unusable."""
    if not isinstance(eyes, (list, tuple)):
        return []
    out = []
    for raw in eyes[:MAX_EYES]:
        if not isinstance(raw, dict) or "x" not in raw or "y" not in raw:
            continue
        x, y = _num(raw["x"], -1.0), _num(raw["y"], -1.0)
        if x < 0 or y < 0:
            continue
        out.append({
            "x": min(x, 1.0), "y": min(y, 1.0),
            "r": min(max(_num(raw.get("r"), 0.015), MIN_RADIUS), MAX_RADIUS),
            "kind": raw.get("kind") if raw.get("kind") in KINDS else "red",
            "amount": min(max(_num(raw.get("amount"), 100.0), 0.0), 100.0),
            "darken": min(max(_num(raw.get("darken"), 50.0), 0.0), 100.0),
        })
    return out


def _smooth(t: np.ndarray) -> np.ndarray:
    t = np.clip(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def apply_eye(img: np.ndarray, eye: dict) -> None:
    """Correct one eye in ``img`` (float RGB 0..1) in place."""
    h, w = img.shape[:2]
    if w < 16 or h < 16:
        return
    r = max(2, int(round(eye["r"] * w)))
    cx, cy = int(round(eye["x"] * (w - 1))), int(round(eye["y"] * (h - 1)))
    x0, x1 = max(0, cx - r), min(w, cx + r + 1)
    y0, y1 = max(0, cy - r), min(h, cy + r + 1)
    if x1 <= x0 or y1 <= y0:
        return
    win = img[y0:y1, x0:x1]
    yy, xx = np.mgrid[y0:y1, x0:x1]
    d = np.hypot(xx - cx, yy - cy)
    fade = _smooth((r - d) / max(0.35 * r, 1.0))             # soft outer edge
    amount = eye["amount"] / 100.0
    dark = eye["darken"] / 100.0
    red, grn, blu = win[..., 0], win[..., 1], win[..., 2]
    top = win.max(axis=2)
    if eye["kind"] == "pet":
        sat = (top - win.min(axis=2)) / np.maximum(top, 1e-4)
        score = _smooth((sat - 0.30) / 0.30) * _smooth((top - 0.25) / 0.25)
        lum = 0.2126 * red + 0.7152 * grn + 0.0722 * blu
        target = (0.03 + 0.5 * lum * (1.0 - dark))[..., None] * np.ones(3, np.float32)
        wgt = (score * fade * amount)[..., None]
        win[:] = win * (1.0 - wgt) + target * wgt
        return
    other = np.maximum(grn, blu)
    score = _smooth(((red - other) / np.maximum(red, 1e-4) - 0.25) / 0.35) * _smooth((red - 0.10) / 0.15)
    wgt = (score * fade * amount)[..., None]
    fixed = win.copy()
    fixed[..., 0] = np.minimum(red, 0.5 * (grn + blu))
    fixed *= (1.0 - 0.6 * dark)
    win[:] = win * (1.0 - wgt) + fixed * wgt


def apply(img: np.ndarray, adj: dict) -> np.ndarray:
    """Return ``img`` with every eye corrected (a copy; ``img`` is untouched)."""
    eyes = normalise(adj.get("redeye"))
    if not eyes or img.ndim != 3 or img.shape[2] < 3:
        return img
    out = np.array(img, dtype=np.float32, copy=True)
    for eye in eyes:
        try:
            apply_eye(out, eye)
        except Exception:
            continue          # one bad eye must not lose the rest
    return out
