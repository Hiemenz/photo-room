"""
FROZEN copy of the original (pre-optimisation) NumPy render code.

Do not "improve" this file: the equivalence tests compare the fast pipeline
(render_kernels / colormix / tonecurve) against it. Copied verbatim from
commit bf866d8 (editor.py, colormix.py, tonecurve.py), only renamed with a
``legacy_`` prefix where names would clash.
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter

import colormix
from colormix import (CHROMA_FLOOR, GRADE_LUM_GAIN, GRADE_RANGES, GRADE_TINT_STRENGTH,
                      LUM_GAIN, MAX_HUE_SHIFT_DEG, _band_table, _hue_index, _luma,
                      _smoothstep, _vec, grade_masks, hsv_to_rgb, rgb_to_hsv)
from tonecurve import LUT_SIZE, build_luts, is_identity


class LegacyAnalysis:
    __slots__ = ("h", "s", "v", "idx", "chroma")

    def __init__(self, img):
        self.h, self.s, self.v = rgb_to_hsv(img)
        self.idx = _hue_index(self.h)
        self.chroma = _smoothstep(0.0, CHROMA_FLOOR * 2, self.s).astype(np.float32)


def legacy_luminance(img: np.ndarray) -> np.ndarray:
    return 0.2126 * img[:, :, 0] + 0.7152 * img[:, :, 1] + 0.0722 * img[:, :, 2]


def legacy_blur(plane: np.ndarray, radius: float) -> np.ndarray:
    """
    Gaussian blur of a single 0..1 plane, via Pillow.

    Pillow's implementation is a separable box approximation in C, which is
    far faster than anything hand-rolled in numpy at preview resolution and
    accurate enough for a local-contrast mask.
    """
    # Pillow's Gaussian only accepts 8-bit modes, so the plane is quantised
    # for the blur. That is fine here — every caller uses the result as a
    # smooth mask, never as image data.
    #
    # Pillow's C implementation was measured against a numpy cumulative-sum
    # box blur at preview size and was roughly three times faster, so the
    # quantisation round trip is worth paying.
    h, w = plane.shape
    as_u8 = np.clip(plane * 255.0 + 0.5, 0, 255).astype(np.uint8)
    img = Image.fromarray(as_u8)

    # A wide blur removes exactly the detail that full resolution preserves,
    # so it is computed on a shrunken copy and scaled back up.
    factor = 1
    if radius >= 8.0:
        factor = min(4, max(1, int(radius // 4)))

    if factor > 1:
        small = img.resize((max(1, w // factor), max(1, h // factor)), Image.BILINEAR)
        small = small.filter(ImageFilter.GaussianBlur(radius / factor))
        img = small.resize((w, h), Image.BILINEAR)
    else:
        img = img.filter(ImageFilter.GaussianBlur(radius))

    return np.asarray(img, dtype=np.float32) / 255.0


def legacy_apply_detail(img: np.ndarray, adj: dict) -> np.ndarray:
    """
    Clarity, texture, dehaze and sharpening.

    All four are local-contrast operations, so they share the blur work and
    run only when their slider is off its default — a blur per photo is the
    most expensive thing in the preview path.
    """
    clarity = float(adj.get("clarity", 0))
    texture = float(adj.get("texture", 0))
    sharpness = float(adj.get("sharpness", 0))
    dehaze = float(adj.get("dehaze", 0))

    if not (clarity or texture or sharpness or dehaze):
        return img

    # Radii scale with the image so the effect looks the same at any preview
    # size rather than getting coarser as the picture gets larger.
    edge = max(img.shape[0], img.shape[1])
    lum = legacy_luminance(img)

    if clarity:
        # Large radius, weighted to the midtones: this is the "punch" control,
        # and applying it to highlights and shadows just produces halos.
        detail = lum - legacy_blur(lum, max(4.0, edge * 0.02))
        midtones = 1.0 - (2.0 * np.clip(lum, 0.0, 1.0) - 1.0) ** 2
        img = img + (clarity / 100.0) * 0.8 * (detail * midtones)[:, :, None]
        lum = legacy_luminance(np.clip(img, 0.0, 1.0))

    if texture:
        # Small radius: fine detail only, and deliberately not midtone-masked
        # so it can smooth skin as well as bring out fabric.
        detail = lum - legacy_blur(lum, max(1.0, edge * 0.0025))
        img = img + (texture / 100.0) * 0.9 * detail[:, :, None]
        lum = legacy_luminance(np.clip(img, 0.0, 1.0))

    if dehaze:
        # Dark-channel prior: haze lifts the darkest channel everywhere, so
        # the blurred per-pixel minimum estimates how much veil to remove.
        dark = np.min(np.clip(img, 0.0, 1.0), axis=2)
        veil = legacy_blur(dark, max(6.0, edge * 0.03)) * (dehaze / 100.0) * 0.7
        veil = np.clip(veil, -0.85, 0.85)[:, :, None]
        img = (img - veil) / np.maximum(1.0 - veil, 0.15)

    if sharpness:
        detail = lum - legacy_blur(lum, 1.2)
        img = img + (sharpness / 100.0) * 0.5 * detail[:, :, None]

    return np.clip(img, 0.0, 1.0)


def legacy_stage_tone(base: np.ndarray, baseline_temp: int, adj: dict) -> np.ndarray:
    img = base.copy()

    # --- White balance ---
    temp = float(adj.get("color_temp", baseline_temp))
    if temp > 0 and abs(temp - baseline_temp) > 1:
        ratio = temp / baseline_temp
        img[:, :, 0] *= ratio ** 0.45
        img[:, :, 2] *= (1.0 / ratio) ** 0.45

    tint = float(adj.get("tint", 0))
    if tint:
        img[:, :, 1] *= 1.0 - (tint / 150.0) * 0.18
        img[:, :, 0] *= 1.0 + (tint / 150.0) * 0.06
        img[:, :, 2] *= 1.0 + (tint / 150.0) * 0.06

    # --- Exposure ---
    exposure = float(adj.get("exposure", 0))
    if exposure:
        img *= 2.0 ** exposure

    img = np.clip(img, 0.0, 1.0)

    # --- Tonal range ---
    lum = legacy_luminance(img)

    shadows = float(adj.get("shadows", 0))
    if shadows:
        mask = np.clip(1.0 - lum / 0.5, 0.0, 1.0) ** 2
        img += (shadows / 100.0) * 0.35 * mask[:, :, None]

    highlights = float(adj.get("highlights", 0))
    if highlights:
        mask = np.clip((lum - 0.5) / 0.5, 0.0, 1.0) ** 2
        img += (highlights / 100.0) * 0.35 * mask[:, :, None]

    blacks = float(adj.get("blacks", 0))
    if blacks:
        mask = np.clip(1.0 - lum / 0.25, 0.0, 1.0) ** 2
        img += (blacks / 100.0) * 0.20 * mask[:, :, None]

    whites = float(adj.get("whites", 0))
    if whites:
        mask = np.clip((lum - 0.75) / 0.25, 0.0, 1.0) ** 2
        img += (whites / 100.0) * 0.20 * mask[:, :, None]

    contrast = float(adj.get("contrast", 0))
    if contrast:
        img = (img - 0.5) * (1.0 + contrast / 100.0) + 0.5

    img = np.clip(img, 0.0, 1.0)

    # --- Tone curve ---
    return np.clip(legacy_apply_tone_curve(img, adj.get("tone_curve")), 0.0, 1.0)


def legacy_stage_saturation(img: np.ndarray, adj: dict) -> np.ndarray:
    saturation = float(adj.get("saturation", 0))
    vibrance = float(adj.get("vibrance", 0))
    if saturation or vibrance:
        gray = legacy_luminance(img)[:, :, None]
        delta = img - gray
        factor = 1.0 + saturation / 100.0
        if vibrance:
            # Vibrance pushes muted colors harder than already-saturated ones.
            current_sat = np.clip(np.max(np.abs(delta), axis=2), 0.0, 1.0)[:, :, None]
            factor = factor + (vibrance / 100.0) * (1.0 - current_sat)
        img = gray + delta * factor

    return np.clip(img, 0.0, 1.0)


def legacy_apply_hsl(img: np.ndarray, adj: dict, analysis: None = None) -> np.ndarray:
    """Per-band hue / saturation / luminance. Exact no-op when all are zero.

    ``analysis`` is the :class:`Analysis` of ``img`` when the caller has one
    cached; it never changes the result."""
    dh, ds, dl = _vec(adj, "hue"), _vec(adj, "sat"), _vec(adj, "lum")
    if not (dh.any() or ds.any() or dl.any()):
        return img
    a = analysis or LegacyAnalysis(img)
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


def legacy_apply_grading(img: np.ndarray, adj: dict) -> np.ndarray:
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


def legacy_apply_tone_curve(img: np.ndarray, tc) -> np.ndarray:
    """Apply the curve to an (H, W, 3) float image in 0-1. Returns ``img`` untouched if identity."""
    if is_identity(tc):
        return img
    luts = build_luts(tc)
    grid = np.linspace(0.0, 1.0, LUT_SIZE)
    out = np.empty_like(img)
    for i in range(3):
        out[:, :, i] = np.interp(img[:, :, i], grid, luts[i])
    return out
