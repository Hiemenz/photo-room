"""
Fast implementations of the develop pipeline's heavy stages.

Each function computes exactly what the original NumPy code in ``editor.py``
did (the equivalence is pinned by ``tests/test_render_equivalence.py`` against
a frozen copy of the old code), but

* works on row strips, in parallel (see ``fastops``), so temporaries stay in
  the CPU cache and all cores are used;
* folds white balance, tint and exposure into one per-channel multiply and the
  four tonal-range masks into one added plane;
* applies the tone curve through float32 lookup tables instead of
  ``np.interp`` (which silently promotes to float64);
* does clarity / texture / dehaze / sharpen with the elementwise work done in
  strips and the Gaussian blurs done on a quantised, shrunken plane (as before).
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter

import colour
import tonecurve
from fastops import for_rows, map_rows, run_parallel

try:
    import cv2
except ImportError:        # pragma: no cover - the NumPy paths cover it
    cv2 = None

F32 = np.float32
_LUMA = (F32(0.2126), F32(0.7152), F32(0.0722))
_LUMA_ROW = np.array([[0.2126, 0.7152, 0.0722]], dtype=np.float32)


def _cv_ok(img: np.ndarray) -> bool:
    return (cv2 is not None and img.dtype == np.float32 and img.ndim == 3 and img.shape[2] == 3
            and img.flags.c_contiguous and img.shape[0] * img.shape[1] > 0)


def luminance(img: np.ndarray) -> np.ndarray:
    if _cv_ok(img):
        return cv2.transform(img, _LUMA_ROW).reshape(img.shape[:2])
    out = img[..., 0] * _LUMA[0]
    out += img[..., 1] * _LUMA[1]
    out += img[..., 2] * _LUMA[2]
    return out


# ---------------------------------------------------------------------------
# Tone: white balance, exposure, tonal range, contrast, curve
# ---------------------------------------------------------------------------

def tone_is_neutral(baseline_temp: float, adj: dict) -> bool:
    temp = float(adj.get("color_temp", baseline_temp))
    if temp > 0 and abs(temp - baseline_temp) > 1:
        return False
    if any(float(adj.get(k, 0)) for k in
           ("tint", "exposure", "shadows", "highlights", "blacks", "whites", "contrast")):
        return False
    return tonecurve.is_identity(adj.get("tone_curve"))


def _channel_gains_legacy(baseline_temp: float, adj: dict) -> np.ndarray | None:
    gains = [1.0, 1.0, 1.0]
    temp = float(adj.get("color_temp", baseline_temp))
    if temp > 0 and abs(temp - baseline_temp) > 1:
        ratio = temp / baseline_temp
        gains[0] *= ratio ** 0.45
        gains[2] *= (1.0 / ratio) ** 0.45
    tint = float(adj.get("tint", 0))
    if tint:
        gains[1] *= 1.0 - (tint / 150.0) * 0.18
        gains[0] *= 1.0 + (tint / 150.0) * 0.06
        gains[2] *= 1.0 + (tint / 150.0) * 0.06
    exposure = float(adj.get("exposure", 0))
    if exposure:
        e = 2.0 ** exposure
        gains = [g * e for g in gains]
    if gains == [1.0, 1.0, 1.0]:
        return None
    return np.array(gains, dtype=F32)


# (key, strength, mask weight at lum)
def _range_plane_legacy(lum: np.ndarray, adj: dict) -> np.ndarray | None:
    delta = None

    def add(term):
        nonlocal delta
        if delta is None:
            delta = term
        else:
            delta += term

    shadows = float(adj.get("shadows", 0))
    if shadows:
        m = np.clip(1.0 - lum / 0.5, 0.0, 1.0)
        m *= m
        m *= F32((shadows / 100.0) * 0.35)
        add(m)
    highlights = float(adj.get("highlights", 0))
    if highlights:
        m = np.clip((lum - 0.5) / 0.5, 0.0, 1.0)
        m *= m
        m *= F32((highlights / 100.0) * 0.35)
        add(m)
    blacks = float(adj.get("blacks", 0))
    if blacks:
        m = np.clip(1.0 - lum / 0.25, 0.0, 1.0)
        m *= m
        m *= F32((blacks / 100.0) * 0.20)
        add(m)
    whites = float(adj.get("whites", 0))
    if whites:
        m = np.clip((lum - 0.75) / 0.25, 0.0, 1.0)
        m *= m
        m *= F32((whites / 100.0) * 0.20)
        add(m)
    return delta


def _affine(strip: np.ndarray, scale, offset=0.0) -> np.ndarray:
    """``strip * scale + offset`` per channel (scale: scalar or 3 values), into a new array."""
    sc = np.broadcast_to(np.asarray(scale, dtype=np.float32), (3,))
    if _cv_ok(strip):
        m = np.zeros((3, 4), dtype=np.float32)
        m[range(3), range(3)] = sc
        m[:, 3] = offset
        return cv2.transform(strip, m)
    return strip * sc + np.float32(offset)


def _tone_rows_legacy(strip: np.ndarray, gains, adj: dict, curve) -> np.ndarray:
    img = _affine(strip, gains) if gains is not None else strip.copy()
    np.clip(img, 0.0, 1.0, out=img)

    wants_range = any(float(adj.get(k, 0)) for k in ("shadows", "highlights", "blacks", "whites"))
    if wants_range:
        delta = _range_plane_legacy(luminance(img), adj)
        img += delta[..., None]

    contrast = float(adj.get("contrast", 0))
    if contrast:
        k = 1.0 + contrast / 100.0
        img = _affine(img, k, 0.5 - 0.5 * k)

    np.clip(img, 0.0, 1.0, out=img)
    if curve is not None:
        curve(img)
    return img


def tone_stage_legacy(base: np.ndarray, baseline_temp: float, adj: dict) -> np.ndarray:
    """Edit version 1: the same controls applied to gamma-coded values (kept so old edits look the same)."""
    gains = _channel_gains_legacy(baseline_temp, adj)
    curve = tonecurve.curve_applier(adj.get("tone_curve"))
    return map_rows(lambda s: _tone_rows_legacy(s, gains, adj, curve), base)


# ---------------------------------------------------------------------------
# Tone, edit version 2: light is edited in linear light
# ---------------------------------------------------------------------------
# Exposure, white balance, Shadows, Highlights, Whites and Blacks are applied to
# linear-light values (decoded with the output space's exact transfer function),
# which is what makes +1 EV exactly twice the light. Contrast and the tone curve
# are display-referred in Camera Raw too, so they run on the re-encoded values.
# See docs/COLOUR-PIPELINE.md for every constant and where it differs from Lightroom.

RANGE_STOPS = {"shadows": 1.5, "highlights": 1.5, "whites": 0.75}
BLACKS_OFFSET = 0.012          # linear light added at Blacks = +100 to the darkest tones (encoded 0.10 in sRGB)


def wb_exposure_gains(baseline_temp: float, adj: dict) -> np.ndarray | None:
    """Linear-light RGB gains for temperature, tint and exposure (None when all are neutral)."""
    gains = np.ones(3)
    temp = float(adj.get("color_temp", baseline_temp))
    if temp > 0 and abs(temp - baseline_temp) > 1:
        gains = gains * colour.temperature_gains(temp, baseline_temp)
    tint = float(adj.get("tint", 0))
    if tint:
        gains = gains * colour.tint_gains(tint)
    exposure = float(adj.get("exposure", 0))
    if exposure:
        gains = gains * 2.0 ** exposure
    if np.all(gains == 1.0):
        return None
    return gains.astype(F32)


def _smooth_mask(e: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """0 at ``e <= lo`` rising to 1 at ``hi`` (squared ramp; the old masks' shape), or falling when hi < lo."""
    m = np.clip((e - F32(lo)) / F32(hi - lo), 0.0, 1.0)
    m *= m
    return m


def lightness(lin: np.ndarray) -> np.ndarray:
    """Display-referred lightness 0..1 the range masks are drawn on: sqrt of the linear luminance.

    A gamma-2 stand-in for the output curve (within a few percent of sRGB's), one cheap
    pass, and the same for every output space so the sliders mean the same thing everywhere.
    """
    y = np.clip(luminance(lin), 0.0, 1.0)
    return cv2.sqrt(y) if cv2 is not None and y.flags.c_contiguous else np.sqrt(y)


def range_adjust(lin: np.ndarray, adj: dict, space: str) -> np.ndarray:
    """
    Shadows, Highlights, Whites and Blacks on linear light (``lin`` may be modified).

    Shadows, Highlights and Whites are a number of stops scaled by the slider and
    by how much of the pixel's display-referred lightness falls in their range;
    all three channels get the same factor, so hue is kept. Blacks adds or removes
    a little light, weighted to the darkest tones (Blacks -100 crushes them to 0,
    +100 lifts them), because a gain cannot move black.
    """
    sh, hi, wh, bl = (float(adj.get(k, 0)) for k in ("shadows", "highlights", "whites", "blacks"))
    if not (sh or hi or wh or bl):
        return lin
    e = lightness(lin)
    stops = None

    def add(term):
        nonlocal stops
        if stops is None:
            stops = term
        else:
            stops += term

    if sh:
        add(_smooth_mask(e, 0.55, 0.0) * F32(sh / 100.0 * RANGE_STOPS["shadows"]))
    if hi:
        add(_smooth_mask(e, 0.45, 1.0) * F32(hi / 100.0 * RANGE_STOPS["highlights"]))
    if wh:
        add(_smooth_mask(e, 0.70, 1.0) * F32(wh / 100.0 * RANGE_STOPS["whites"]))
    if stops is not None:
        stops *= F32(np.log(2.0))
        gain = cv2.exp(stops) if cv2 is not None and stops.flags.c_contiguous else np.exp(stops)
        lin = lin * gain[..., None]
    if bl:
        lift = _smooth_mask(e, 0.35, 0.0) * F32(bl / 100.0 * BLACKS_OFFSET)
        lin = lin + lift[..., None]
    return lin


def tone_is_linear_neutral(baseline_temp: float, adj: dict) -> bool:
    """True when nothing in the adjustment needs linear light (Contrast and curve may still apply)."""
    return (wb_exposure_gains(baseline_temp, adj) is None
            and not any(float(adj.get(k, 0)) for k in ("shadows", "highlights", "whites", "blacks")))


def _tone_rows(strip: np.ndarray, gains, adj: dict, applier, space: str, linear: bool,
               lin_strip: np.ndarray | None = None) -> np.ndarray:
    if linear:
        lin = lin_strip if lin_strip is not None else colour.to_linear(strip, space)
        if gains is not None:
            lin = _affine(lin, gains)
        lin = range_adjust(lin, adj, space)
        img = colour.to_encoded(lin, space)
    else:
        img = np.clip(strip, 0.0, 1.0).astype(F32, copy=False)      # a new array
    if applier is not None:
        img = np.ascontiguousarray(img)
        applier(img)
    return img


def tone_stage(base: np.ndarray, baseline_temp: float, adj: dict, linear: np.ndarray | None = None) -> np.ndarray:
    """
    White balance, exposure, tonal ranges, contrast and curve on an *encoded* image.

    ``linear`` may carry ``colour.to_linear(base)`` when the caller has it cached
    (a slider drag then costs one re-encode instead of a decode and an encode).
    """
    if int(adj.get("edit_version", 2) or 2) < 2:
        return tone_stage_legacy(base, baseline_temp, adj)
    space = colour.normalise_space(adj.get("_space"))
    gains = wb_exposure_gains(baseline_temp, adj)
    applier = tonecurve.curve_applier(adj.get("tone_curve"), adj.get("contrast", 0))
    is_linear = not tone_is_linear_neutral(baseline_temp, adj)
    if linear is not None and is_linear:
        return map_rows(lambda s, l: _tone_rows(s, gains, adj, applier, space, True, l), base, linear)
    return map_rows(lambda s: _tone_rows(s, gains, adj, applier, space, is_linear), base)


def tone_stage_from_linear(lin: np.ndarray, baseline_temp: float, adj: dict) -> np.ndarray:
    """``tone_stage`` for an image that is already linear light (edit version 2 only); returns encoded."""
    space = colour.normalise_space(adj.get("_space"))
    if tone_is_linear_neutral(baseline_temp, adj):
        return tone_stage(colour.to_encoded(lin, space), baseline_temp, adj)
    gains = wb_exposure_gains(baseline_temp, adj)
    applier = tonecurve.curve_applier(adj.get("tone_curve"), adj.get("contrast", 0))
    return map_rows(lambda l: _tone_rows(l, gains, adj, applier, space, True, l), lin)


# ---------------------------------------------------------------------------
# Saturation / vibrance
# ---------------------------------------------------------------------------

def _saturation_rows(strip: np.ndarray, saturation: float, vibrance: float) -> np.ndarray:
    if not vibrance and _cv_ok(strip):
        # gray + (img - gray) * f is one 3x3 matrix: f*I + (1-f) * (luma row in every row)
        f = 1.0 + saturation / 100.0
        m = np.eye(3, dtype=np.float32) * np.float32(f) + np.float32(1.0 - f) * np.repeat(_LUMA_ROW, 3, axis=0)
        out = cv2.transform(strip, m)
        return np.clip(out, 0.0, 1.0, out=out)
    # out = gray + delta * factor = img + delta * (factor - 1), with delta = img - gray
    if _cv_ok(strip):
        delta = cv2.transform(strip, np.eye(3, dtype=np.float32) - np.repeat(_LUMA_ROW, 3, axis=0))
    else:
        delta = strip - luminance(strip)[..., None]
    extra = np.full(strip.shape[:2], saturation / 100.0, dtype=F32)
    if vibrance:
        mag = np.abs(delta)
        current = np.maximum(np.maximum(mag[..., 0], mag[..., 1]), mag[..., 2])
        np.clip(current, 0.0, 1.0, out=current)
        current *= F32(-vibrance / 100.0)
        current += F32(vibrance / 100.0)           # vibrance * (1 - current)
        extra += current
    delta *= extra[..., None]
    delta += strip
    return np.clip(delta, 0.0, 1.0, out=delta)


def saturation_stage(img: np.ndarray, adj: dict) -> np.ndarray:
    saturation = float(adj.get("saturation", 0))
    vibrance = float(adj.get("vibrance", 0))
    if not (saturation or vibrance):
        return np.clip(img, 0.0, 1.0)
    return map_rows(lambda s: _saturation_rows(s, saturation, vibrance), img)


# ---------------------------------------------------------------------------
# Blur (Pillow, quantised exactly as the original) and the detail stage
# ---------------------------------------------------------------------------

def _to_u8(plane: np.ndarray) -> np.ndarray:
    out = np.empty(plane.shape, dtype=np.uint8)

    def rows(a, b):
        t = plane[a:b] * F32(255.0)
        t += F32(0.5)
        np.clip(t, 0, 255, out=t)
        out[a:b] = t           # truncating cast, as the original astype(uint8)

    for_rows(rows, plane.shape[0], plane.shape[1], rows=128)
    return out


def blur_u8(plane: np.ndarray, radius: float) -> np.ndarray:
    """Gaussian blur of a 0..1 plane, returned as uint8 (value * 255)."""
    h, w = plane.shape
    img = Image.fromarray(_to_u8(plane))
    factor = 1
    if radius >= 8.0:
        factor = min(4, max(1, int(radius // 4)))
    if factor > 1:
        small = img.resize((max(1, w // factor), max(1, h // factor)), Image.BILINEAR)
        small = small.filter(ImageFilter.GaussianBlur(radius / factor))
        img = small.resize((w, h), Image.BILINEAR)
        return np.asarray(img)
    return _small_radius_blur(np.asarray(img), radius)


def _small_radius_blur(u8: np.ndarray, radius: float) -> np.ndarray:
    """Pillow's Gaussian on row strips with a halo (it only looks ``~3*radius`` away)."""
    h, w = u8.shape
    if h * w < 600_000:
        return np.asarray(Image.fromarray(u8).filter(ImageFilter.GaussianBlur(radius)))
    halo = int(radius * 6) + 8
    out = np.empty_like(u8)

    def rows(a, b):
        lo, hi = max(0, a - halo), min(h, b + halo)
        blurred = np.asarray(Image.fromarray(np.ascontiguousarray(u8[lo:hi]))
                             .filter(ImageFilter.GaussianBlur(radius)))
        out[a:b] = blurred[a - lo:b - lo]

    for_rows(rows, h, w, rows=256)
    return out


def blur(plane: np.ndarray, radius: float) -> np.ndarray:
    """Drop-in for ``editor._blur``: float32 0..1 result."""
    u8 = blur_u8(plane, radius)
    out = np.empty(plane.shape, dtype=F32)
    k = F32(1.0 / 255.0)

    def rows(a, b):
        np.multiply(u8[a:b], k, out=out[a:b], dtype=F32)

    for_rows(rows, plane.shape[0], plane.shape[1], rows=256)
    return out


def detail_stage(img: np.ndarray, adj: dict, blur_fn=None) -> np.ndarray:
    """Clarity, texture, dehaze, sharpness (same maths as the original)."""
    clarity = float(adj.get("clarity", 0))
    texture = float(adj.get("texture", 0))
    sharpness = float(adj.get("sharpness", 0))
    dehaze = float(adj.get("dehaze", 0))
    if not (clarity or texture or sharpness or dehaze):
        return img

    blur_fn = blur_fn or blur
    h, w = img.shape[:2]
    # ``_detail_edge``: the tile server renders a crop with the radii of the whole frame.
    edge = adj.get("_detail_edge") or max(h, w)
    lum = map_rows(luminance, img)
    cur = img            # replaced by an owned array on the first change

    def own():
        nonlocal cur
        if cur is img:
            cur = np.empty_like(img)
        return cur

    def add_detail(k, detail_plane, weight=None, need_lum=True):
        """cur = src + k * detail * weight, rows in parallel; refresh lum from the clipped result."""
        nonlocal lum
        src = cur
        dst = own()
        new_lum = np.empty_like(lum) if need_lum else lum
        kk = F32(k)

        def rows(a, b):
            d = detail_plane[a:b] * kk
            if weight is not None:
                d *= weight[a:b]
            t = np.add(src[a:b], d[..., None], out=dst[a:b])
            if need_lum:
                new_lum[a:b] = luminance(np.clip(t, 0.0, 1.0))

        for_rows(rows, h, w)
        lum = new_lum

    def difference(blurred):
        d = np.empty_like(lum)

        def rows(a, b):
            np.subtract(lum[a:b], blurred[a:b], out=d[a:b])

        for_rows(rows, h, w, rows=256)
        return d

    if clarity:
        detail = difference(blur_fn(lum, max(4.0, edge * 0.02)))
        mid = np.empty_like(lum)

        def mrows(a, b):
            t = np.clip(lum[a:b], 0.0, 1.0)
            t *= F32(2.0)
            t -= F32(1.0)
            t *= t
            np.subtract(F32(1.0), t, out=mid[a:b])

        for_rows(mrows, h, w, rows=256)
        add_detail((clarity / 100.0) * 0.8, detail, mid)

    if texture:
        detail = difference(blur_fn(lum, max(1.0, edge * 0.0025)))
        add_detail((texture / 100.0) * 0.9, detail)

    if dehaze:
        src = cur
        dark = np.empty(lum.shape, dtype=F32)

        def drows(a, b):
            c = np.clip(src[a:b], 0.0, 1.0)
            np.minimum(np.minimum(c[..., 0], c[..., 1]), c[..., 2], out=dark[a:b])

        for_rows(drows, h, w)
        veil = blur_fn(dark, max(6.0, edge * 0.03))
        dst = own()
        kv = F32((dehaze / 100.0) * 0.7)

        def vrows(a, b):
            v = veil[a:b] * kv
            np.clip(v, -0.85, 0.85, out=v)
            v = v[..., None]
            t = src[a:b] - v
            t /= np.maximum(1.0 - v, F32(0.15))
            dst[a:b] = t

        for_rows(vrows, h, w)

    if sharpness:
        detail = difference(blur_fn(lum, 1.2))
        add_detail((sharpness / 100.0) * 0.5, detail, need_lum=False)

    out = cur
    if out is img:
        return np.clip(img, 0.0, 1.0)
    np.clip(out, 0.0, 1.0, out=out)
    return out


# ---------------------------------------------------------------------------
# 8-bit <-> float conversion (strips, all cores)
# ---------------------------------------------------------------------------

def u8_to_float(arr: np.ndarray) -> np.ndarray:
    """uint8 image -> float32 0..1 (what ``asarray(float32) / 255`` did)."""
    k = F32(1.0 / 255.0)
    return map_rows(lambda s: s.astype(F32) * k, arr, dtype=F32)


def u16_to_float(arr: np.ndarray) -> np.ndarray:
    """uint16 image -> float32 0..1."""
    k = F32(1.0 / 65535.0)
    return map_rows(lambda s: s.astype(F32) * k, arr, dtype=F32)


def float_to_u8(arr: np.ndarray) -> np.ndarray:
    """float 0..1 -> uint8 with round-half-up, clipping first (``(a*255+.5).astype(uint8)``)."""
    def rows(s):
        t = s * F32(255.0)
        t += F32(0.5)
        np.clip(t, 0.0, 255.0, out=t)
        return t.astype(np.uint8)
    return map_rows(rows, arr, dtype=np.uint8)
