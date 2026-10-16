"""
HSL / colour mixer, black & white mixer and colour grading.

Kept apart from editor.py so the maths is testable without a raw file, and so
it merges cleanly beside the other develop panels.

All settings are flat keys in the same adjustments dict as every other slider,
which is why presets, copy/paste and batch apply carry them with no extra code:

    hue_red, sat_red, lum_red ... (8 bands)     -100..100
    gray_red ...                                -100..100  (B&W mixer)
    grade_{shadow,mid,highlight,global}_{hue,sat,lum}
    grade_blending (0..100, default 50), grade_balance (-100..100)
"""
from __future__ import annotations

import numpy as np

try:                      # optional: threaded, SIMD colour conversion
    import cv2
except ImportError:       # pragma: no cover - numpy path covers it
    cv2 = None

BANDS = ["red", "orange", "yellow", "green", "aqua", "blue", "purple", "magenta"]
# Band centres in degrees of hue. Uneven on purpose: it follows how Camera
# Raw spaces them, with more resolution through the warm colours.
BAND_CENTRES = [0.0, 30.0, 60.0, 120.0, 180.0, 240.0, 275.0, 320.0]

# How far a slider at +-100 moves a pixel sitting exactly on a band centre.
MAX_HUE_SHIFT_DEG = 30.0
LUM_GAIN = 0.5
# Pixels below this HSV saturation have no meaningful hue; the mixer fades out.
CHROMA_FLOOR = 0.12

GRADE_RANGES = ["shadow", "mid", "highlight", "global"]
GRADE_XMP_NAMES = {"shadow": "Shadow", "mid": "Midtone", "highlight": "Highlight", "global": "Global"}
GRADE_TINT_STRENGTH = 0.25
GRADE_LUM_GAIN = 0.5


def _controls() -> list[dict]:
    out = []
    for band in BANDS:
        label = band.capitalize()
        out.append({"key": f"hue_{band}", "label": f"{label} hue", "group": "hsl_hue", "band": band})
        out.append({"key": f"sat_{band}", "label": f"{label} saturation", "group": "hsl_sat", "band": band})
        out.append({"key": f"lum_{band}", "label": f"{label} luminance", "group": "hsl_lum", "band": band})
        out.append({"key": f"gray_{band}", "label": label, "group": "gray", "band": band})
    for c in out:
        c.update(min=-100, max=100, step=1, default=0)
    for rng in GRADE_RANGES:
        label = {"mid": "Midtones"}.get(rng, rng.capitalize() + ("s" if rng != "global" else ""))
        out.append({"key": f"grade_{rng}_hue", "label": f"{label} hue", "group": "grade",
                    "min": 0, "max": 360, "step": 1, "default": 0, "range": rng})
        out.append({"key": f"grade_{rng}_sat", "label": f"{label} saturation", "group": "grade",
                    "min": 0, "max": 100, "step": 1, "default": 0, "range": rng})
        out.append({"key": f"grade_{rng}_lum", "label": f"{label} luminance", "group": "grade",
                    "min": -100, "max": 100, "step": 1, "default": 0, "range": rng})
    out.append({"key": "grade_blending", "label": "Blending", "group": "grade",
                "min": 0, "max": 100, "step": 1, "default": 50})
    out.append({"key": "grade_balance", "label": "Balance", "group": "grade",
                "min": -100, "max": 100, "step": 1, "default": 0})
    return out


COLOR_CONTROLS = _controls()
COLOR_DEFAULTS = {c["key"]: c["default"] for c in COLOR_CONTROLS}
# Keys that must not be scaled by edit strength: an angle or a shape parameter
# is not an intensity.
UNSCALED_KEYS = {k for k in COLOR_DEFAULTS if k.endswith("_hue") and k.startswith("grade_")}
UNSCALED_KEYS |= {"grade_blending", "grade_balance"}


def xmp_tag(key: str) -> str:
    """Camera Raw (crs:) tag name for a colour key."""
    kind, _, rest = key.partition("_")
    if kind in ("hue", "sat", "lum"):
        prefix = {"hue": "HueAdjustment", "sat": "SaturationAdjustment", "lum": "LuminanceAdjustment"}[kind]
        return prefix + rest.capitalize()
    if kind == "gray":
        return "GrayMixer" + rest.capitalize()
    if key == "grade_blending":
        return "ColorGradeBlending"
    if key == "grade_balance":
        return "SplitToningBalance"
    _, rng, what = key.split("_")
    if rng in ("shadow", "highlight") and what in ("hue", "sat"):
        # Camera Raw kept the old Split Toning names for these four; the
        # "ColorGradeShadowHue" style names do not exist in the crs schema, so
        # exiftool silently drops them ("Tag ... is not defined").
        return f"SplitToning{GRADE_XMP_NAMES[rng]}{'Hue' if what == 'hue' else 'Saturation'}"
    return f"ColorGrade{GRADE_XMP_NAMES[rng]}{what.capitalize()}"


XMP_TAGS = {c["key"]: xmp_tag(c["key"]) for c in COLOR_CONTROLS}


def xmp_arguments(adj: dict) -> list[str]:
    """exiftool arguments for every colour setting (always all, so old values are overwritten)."""
    return [f"-XMP-crs:{tag}={int(round(float(adj.get(key, COLOR_DEFAULTS[key]))))}"
            for key, tag in XMP_TAGS.items()]


def parse_xmp_values(values: list[str]) -> dict:
    """Map exiftool -s3 output (in ``XMP_TAGS`` order) to non-default settings."""
    out: dict = {}
    if len(values) != len(XMP_TAGS):
        return out
    for key, raw in zip(XMP_TAGS, values):
        raw = raw.strip()
        if raw in ("-", ""):
            continue
        try:
            v = float(raw)
        except ValueError:
            continue
        if v != COLOR_DEFAULTS[key]:
            out[key] = v
    return out


# ---------------------------------------------------------------------------
# Colour space helpers
# ---------------------------------------------------------------------------

def rgb_to_hsv(img: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return hue (degrees 0..360), saturation, value for an RGB float image."""
    if cv2 is not None and img.ndim == 3 and img.dtype == np.float32:
        hsv = cv2.cvtColor(np.ascontiguousarray(img), cv2.COLOR_RGB2HSV)
        return hsv[..., 0], hsv[..., 1], hsv[..., 2]
    return _rgb_to_hsv_numpy(img)


def _rgb_to_hsv_numpy(img: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    r, g, b = img[..., 0], img[..., 1], img[..., 2]
    v = img.max(axis=-1)
    c = v - img.min(axis=-1)
    safe = np.where(c > 1e-9, c, 1.0)
    h = np.where(v == r, ((g - b) / safe) % 6.0,
                 np.where(v == g, (b - r) / safe + 2.0, (r - g) / safe + 4.0))
    h = np.where(c > 1e-9, h * 60.0, 0.0)
    s = np.where(v > 1e-9, c / np.where(v > 1e-9, v, 1.0), 0.0)
    return h.astype(np.float32), s.astype(np.float32), v.astype(np.float32)


def hsv_to_rgb(h: np.ndarray, s: np.ndarray, v: np.ndarray) -> np.ndarray:
    h = np.asarray(h)
    if cv2 is not None and h.ndim == 2 and h.dtype == np.float32:
        hsv = np.stack([h % np.float32(360.0), s, v], axis=-1).astype(np.float32, copy=False)
        return cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)
    return _hsv_to_rgb_numpy(h, s, v)


def _hsv_to_rgb_numpy(h: np.ndarray, s: np.ndarray, v: np.ndarray) -> np.ndarray:
    h6 = (np.asarray(h, dtype=np.float32) % 360.0) / 60.0
    def f(n):
        k = (n + h6) % 6.0
        return v - v * s * np.clip(np.minimum(k, 4.0 - k), 0.0, 1.0)
    return np.stack([f(5.0), f(3.0), f(1.0)], axis=-1)


def band_weights(hue: np.ndarray) -> np.ndarray:
    """
    Weight of each of the 8 bands at each hue, shape ``hue.shape + (8,)``.

    Raised-cosine hats between neighbouring band centres: each pixel falls
    between exactly two centres whose weights sum to 1, so the weights are a
    partition of unity everywhere and are continuous with a continuous first
    derivative (no hard edges).
    """
    hue = np.asarray(hue, dtype=np.float32) % 360.0
    centres = np.array(BAND_CENTRES + [360.0], dtype=np.float32)
    n = len(BAND_CENTRES)
    idx = np.clip(np.searchsorted(centres, hue, side="right") - 1, 0, n - 1)
    lo = centres[idx]
    span = centres[idx + 1] - lo
    t = (hue - lo) / span
    w_hi = 0.5 - 0.5 * np.cos(np.pi * t)
    out = np.zeros(hue.shape + (n,), dtype=np.float32)
    flat_lo = idx
    flat_hi = (idx + 1) % n
    np.put_along_axis(out, flat_lo[..., None], (1.0 - w_hi)[..., None], axis=-1)
    # Wrap-safe accumulate (hi band may equal lo only when n == 1).
    cur = np.take_along_axis(out, flat_hi[..., None], axis=-1)
    np.put_along_axis(out, flat_hi[..., None], cur + w_hi[..., None], axis=-1)
    return out


def _band_lookup(hue: np.ndarray):
    hue = np.asarray(hue, dtype=np.float32) % 360.0
    centres = np.array(BAND_CENTRES + [360.0], dtype=np.float32)
    n = len(BAND_CENTRES)
    idx = np.clip(np.searchsorted(centres, hue, side="right") - 1, 0, n - 1)
    t = (hue - centres[idx]) / (centres[idx + 1] - centres[idx])
    return idx, (idx + 1) % n, 0.5 - 0.5 * np.cos(np.pi * t)


def band_mix(hue: np.ndarray, *values: np.ndarray):
    """Weighted sum of per-band ``values`` at each hue (one result per vector).

    Same as ``band_weights(hue) @ values`` but touches only the two bands that
    overlap each pixel and shares the lookup between vectors, which is much
    faster on a full preview."""
    lo, hi, w = _band_lookup(hue)
    out = []
    for vec in values:
        v = np.asarray(vec, dtype=np.float32)
        out.append(v[lo] * (1.0 - w) + v[hi] * w)
    return out[0] if len(out) == 1 else tuple(out)


def _smoothstep(e0, e1, x):
    t = np.clip((x - e0) / max(e1 - e0, 1e-6), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _luma(img: np.ndarray) -> np.ndarray:
    return img[..., 0] * 0.2126 + img[..., 1] * 0.7152 + img[..., 2] * 0.0722


# ---------------------------------------------------------------------------
# Mixers
# ---------------------------------------------------------------------------

def _vec(adj: dict, prefix: str) -> np.ndarray:
    return np.array([float(adj.get(f"{prefix}_{b}", 0) or 0) for b in BANDS], dtype=np.float32)


HUE_STEPS = 3600     # hue lookup resolution: 0.1 degree


def _hue_index(hue: np.ndarray) -> np.ndarray:
    """Hue in degrees to an index into a ``HUE_STEPS`` table (0.1 degree)."""
    return (np.asarray(hue, dtype=np.float32) * np.float32(HUE_STEPS / 360.0) + np.float32(0.5)
            ).astype(np.int32) % HUE_STEPS


_HUE_GRID = np.arange(HUE_STEPS, dtype=np.float32) * np.float32(360.0 / HUE_STEPS)


def _band_table(vec: np.ndarray) -> np.ndarray:
    """A per-band vector expanded to a ``HUE_STEPS`` lookup table."""
    return band_mix(_HUE_GRID, vec)


class Analysis:
    """
    The part of the colour mixer that depends only on the pixels, not on the
    sliders: HSV, the hue-table index and the chroma fade. Computed once per
    input image and reused while a slider is dragged.
    """
    __slots__ = ("h", "s", "v", "idx", "chroma")

    def __init__(self, img: np.ndarray):
        self.h, self.s, self.v = rgb_to_hsv(img)
        self.idx = _hue_index(self.h)
        self.chroma = _smoothstep(0.0, CHROMA_FLOOR * 2, self.s).astype(np.float32)

    @property
    def nbytes(self) -> int:
        return sum(a.nbytes for a in (self.h, self.s, self.v, self.idx, self.chroma))


def has_hsl(adj: dict) -> bool:
    return any(_vec(adj, p).any() for p in ("hue", "sat", "lum"))


def apply_hsl(img: np.ndarray, adj: dict, analysis: "Analysis | None" = None) -> np.ndarray:
    """Per-band hue / saturation / luminance. Exact no-op when all are zero.

    ``analysis`` is the :class:`Analysis` of ``img`` when the caller has one
    cached; it never changes the result."""
    dh, ds, dl = _vec(adj, "hue"), _vec(adj, "sat"), _vec(adj, "lum")
    if not (dh.any() or ds.any() or dl.any()):
        return img
    a = analysis or Analysis(img)
    h, s, v, chroma = a.h, a.s, a.v, a.chroma
    sat_scale = 1.0 + _band_table(ds)[a.idx] * chroma / 100.0
    new_s = np.clip(s * sat_scale, 0.0, 1.0)
    if dh.any():
        hue_shift = _band_table(dh)[a.idx] * chroma * np.float32(MAX_HUE_SHIFT_DEG / 100.0)
        out = hsv_to_rgb(h + hue_shift, new_s, v)
    else:
        # Hue untouched: rescale each pixel's distance from its own value
        # instead of a full HSV round trip. Same result, a third of the work.
        ratio = np.divide(new_s, s, out=np.ones_like(s), where=s > 1e-9)
        out = v[..., None] - (v[..., None] - img) * ratio[..., None]
    if dl.any():
        gain = 1.0 + _band_table(dl)[a.idx] * chroma * np.float32(LUM_GAIN / 100.0)
        out = out * gain[..., None]
    return np.clip(out, 0.0, 1.0)


def apply_gray_mixer(img: np.ndarray, adj: dict) -> np.ndarray:
    """B&W mixer: brighten or darken each colour band before the grey conversion."""
    g = _vec(adj, "gray")
    if not g.any():
        return img
    h, s, _ = rgb_to_hsv(img)
    gain = 1.0 + band_mix(h, g) * _smoothstep(0.0, CHROMA_FLOOR * 2, s) / 100.0 * LUM_GAIN * 2.0
    return np.clip(img * gain[..., None], 0.0, 1.0)


def grade_masks(luma: np.ndarray, blending: float, balance: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Shadow, midtone and highlight weights; they sum to exactly 1 everywhere."""
    pivot = 0.5 - float(balance) / 100.0 * 0.25
    width = 0.15 + 0.35 * float(blending) / 100.0
    s = 1.0 - _smoothstep(pivot - width, pivot, luma)
    h = _smoothstep(pivot, pivot + width, luma)
    return s, 1.0 - s - h, h


def apply_grading(img: np.ndarray, adj: dict) -> np.ndarray:
    """Shadows / midtones / highlights / global colour wheels. Exact no-op when unset."""
    spec = {}
    for rng in GRADE_RANGES:
        hue = float(adj.get(f"grade_{rng}_hue", 0) or 0)
        sat = float(adj.get(f"grade_{rng}_sat", 0) or 0)
        lum = float(adj.get(f"grade_{rng}_lum", 0) or 0)
        if sat or lum:
            spec[rng] = (hue, sat, lum)
    if not spec:
        return img

    luma = _luma(img)
    s, m, h = grade_masks(luma, adj.get("grade_blending", 50), adj.get("grade_balance", 0))
    masks = {"shadow": s, "mid": m, "highlight": h, "global": np.ones_like(luma)}

    out = img.copy()
    gain = np.ones_like(luma)
    for rng, (hue, sat, lum) in spec.items():
        w = masks[rng]
        if sat:
            tint = hsv_to_rgb(np.float32(hue), np.float32(1.0), np.float32(1.0))
            tint = tint - float(_luma(tint))            # zero-luma offset keeps brightness
            out += (w * (sat / 100.0) * GRADE_TINT_STRENGTH)[..., None] * tint
        if lum:
            gain += w * (lum / 100.0) * GRADE_LUM_GAIN
    out *= gain[..., None]
    return np.clip(out, 0.0, 1.0)
