"""
Image analysis behind the Develop panel's one-click helpers.

Pure numpy (cv2 when present); nothing here touches Flask or the filesystem,
so every function is unit-testable on a synthetic array. develop_api.py puts
HTTP endpoints on top and tile_server.py serves zoomed tiles.

What lives here
  * kelvin_from_camera / baseline_kelvin -- the camera white balance as a
    colour temperature (the as-shot Temp the UI shows).
  * wb_solve / auto_white_balance / wb_presets -- Temp/Tint that neutralise a
    colour, a whole scene, or a named light.
  * auto_tone -- Lightroom "Auto": Exposure, Contrast, Highlights, Shadows,
    Whites, Blacks and Vibrance.
  * histogram / clipping -- single-pass 256-bin histograms and the clipping
    overlay masks with per-channel percentages.

Colour space note: since the linear-light pipeline (docs/COLOUR-PIPELINE.md)
white balance and exposure are gains on linear light, applied before the sRGB
encode. The solvers here use the editor's own gain functions
(colour.temperature_gains / tint_gains) on linearised pixels, and auto tone
runs the editor's real tone stage on a small sample, so what they propose is
what the preview and the export render.
"""
from __future__ import annotations

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover - cv2 is a normal dependency
    cv2 = None

# Ranges of the Temp and Tint sliders (editor.CONTROLS).
TEMP_MIN, TEMP_MAX = 2000, 12000
TINT_MIN, TINT_MAX = -150, 150


def _ed():
    import editor  # deferred: editor imports Flask and the preview caches

    return editor


# ---------------------------------------------------------------------------
# Camera white balance -> Kelvin
# ---------------------------------------------------------------------------

# Used when a raw has no colour matrix (and in tests that fake rawpy): the
# red/blue multiplier ratio of a typical daylight (D65) shot.
FALLBACK_DAYLIGHT_RB = 2.0
FALLBACK_DAYLIGHT_K = 6500.0


def _mccamy(x: float, y: float) -> float:
    n = (x - 0.3320) / (0.1858 - y)
    return 449.0 * n ** 3 + 3525.0 * n ** 2 + 6823.3 * n + 5520.33


def kelvin_from_camera(cam_mul, xyz_matrix=None, daylight_mul=None) -> int:
    """
    Correlated colour temperature of the light the camera white-balanced for.

    ``cam_mul`` are the camera's white-balance multipliers (R, G, B[, G2]).
    The scene's neutral is 1/multiplier in camera RGB; the camera's
    XYZ->camera matrix (rawpy ``rgb_xyz_matrix``) takes it to XYZ, and McCamy's
    formula turns the chromaticity into Kelvin. This is how Adobe derives the
    as-shot Temp: LibRaw's own daylight multipliers come out at 6500 K (D65)
    for the CR2 sample, and a typical daylight shot reads 5000-6500 K.

    The previous mapping, ``5500 / (R/B)``, ran the wrong way round (a warm
    light needs a *smaller* red multiplier, so the Kelvin must grow with R/B)
    and put a daylight frame at ~3100 K.
    """
    mul = np.asarray(cam_mul, dtype=float)[:3]
    if not np.all(mul > 0):
        return 5500
    if xyz_matrix is not None:
        try:
            m = np.asarray(xyz_matrix, dtype=float)[:3, :3]
            if np.abs(m).sum() > 0 and abs(np.linalg.det(m)) > 1e-9:
                xyz = np.linalg.solve(m, 1.0 / mul)
                total = xyz.sum()
                if total > 0 and xyz[1] > 0:
                    kelvin = _mccamy(xyz[0] / total, xyz[1] / total)
                    if np.isfinite(kelvin):
                        return int(round(float(np.clip(kelvin, 2500, 10000))))
        except (np.linalg.LinAlgError, ValueError, IndexError):
            pass
    # No matrix: scale from the daylight multipliers (R/B grows with Kelvin).
    rb = mul[0] / mul[2]
    rb_day = FALLBACK_DAYLIGHT_RB
    if daylight_mul is not None:
        d = np.asarray(daylight_mul, dtype=float)
        if d.size >= 3 and d[0] > 0 and d[2] > 0:
            rb_day = d[0] / d[2]
    return int(round(float(np.clip(FALLBACK_DAYLIGHT_K * rb / rb_day, 2500, 10000))))


def baseline_kelvin(raw, cam_mul=None) -> int:
    """As-shot Kelvin for an open ``rawpy`` object (or a test double)."""
    if cam_mul is None:
        cam_mul = np.array(raw.camera_whitebalance[:4], dtype=float)
    daylight = getattr(raw, "daylight_whitebalance", None)
    matrix = getattr(raw, "rgb_xyz_matrix", None)
    try:
        return kelvin_from_camera(cam_mul, matrix, daylight)
    except Exception:
        return 5500


# ---------------------------------------------------------------------------
# White balance model (mirrors render_kernels.wb_exposure_gains)
# ---------------------------------------------------------------------------

def _colour():
    import colour

    return colour


def wb_gains(temp: float, tint: float, baseline: float) -> np.ndarray:
    """The linear-light R, G, B gains the editor applies for this Temp/Tint."""
    import render_kernels

    g = render_kernels.wb_exposure_gains(baseline, {"color_temp": temp, "tint": tint})
    return np.ones(3) if g is None else np.asarray(g, dtype=float)


def wb_solve_linear(lin, baseline: float) -> tuple[int, int]:
    """
    Temp and Tint that make a linear-light colour neutral under the editor's
    gains. Temp from a bisection on the red/blue gain ratio (monotonic in
    Kelvin, independent of Tint); Tint then closed-form on green against red.
    """
    colour = _colour()
    r, g, b = (max(float(v), 1e-5) for v in lin)
    want = b / r                                   # needed gain ratio R/B

    def ratio(t: float) -> float:
        gains = colour.temperature_gains(t, baseline)
        return float(gains[0] / gains[2])

    lo, hi = float(TEMP_MIN), float(TEMP_MAX)
    if want <= ratio(lo):
        temp = lo
    elif want >= ratio(hi):
        temp = hi
    else:
        for _ in range(40):
            mid = 0.5 * (lo + hi)
            if ratio(mid) < want:
                lo = mid
            else:
                hi = mid
        temp = 0.5 * (lo + hi)
    temp = int(round(temp))
    tg = colour.temperature_gains(temp, baseline) if abs(temp - baseline) > 1 else np.ones(3)
    # log2(G gain / R gain) grows linearly with Tint; solve for the value that
    # equalises green and red after the Temp gain.
    unit = colour.tint_gains(150.0)
    per_unit = float(np.log2(unit[1] / unit[0]))   # at Tint = 150
    need = float(np.log2((tg[0] * r) / (tg[1] * g)))
    tint = int(round(float(np.clip(150.0 * need / per_unit, TINT_MIN, TINT_MAX))))
    return temp, tint


def wb_solve(rgb, baseline: float) -> tuple[int, int]:
    """``wb_solve_linear`` for a pixel in the encoded (sRGB, 0..1) preview."""
    lin = _colour().to_linear(np.asarray(rgb, dtype=np.float32).reshape(1, 1, 3))
    return wb_solve_linear(lin.reshape(3), baseline)


# Lightroom's presets. As Shot and Auto depend on the photo.
_FIXED_PRESETS = [
    ("daylight", "Daylight", 5500, 10),
    ("cloudy", "Cloudy", 6500, 10),
    ("shade", "Shade", 7500, 10),
    ("tungsten", "Tungsten", 2850, 0),
    ("fluorescent", "Fluorescent", 3800, 21),
    ("flash", "Flash", 5500, 0),
]


def wb_presets(baseline: int, auto: tuple[int, int] | None = None) -> list[dict]:
    """All presets as Temp/Tint. ``auto`` is omitted from the list when None."""
    out = [{"id": "as_shot", "label": "As Shot", "color_temp": int(baseline), "tint": 0}]
    if auto is not None:
        out.append({"id": "auto", "label": "Auto", "color_temp": auto[0], "tint": auto[1]})
    out += [{"id": i, "label": label, "color_temp": t, "tint": n}
            for i, label, t, n in _FIXED_PRESETS]
    return out


# ---------------------------------------------------------------------------
# Auto white balance
# ---------------------------------------------------------------------------

def _sample(img: np.ndarray, edge: int) -> np.ndarray:
    """A strided sample whose long side is about ``edge`` pixels."""
    stride = max(1, int(max(img.shape[:2]) // edge))
    return np.ascontiguousarray(img[::stride, ::stride, :3], dtype=np.float32)


def _is_skin(px: np.ndarray, lum: np.ndarray, sat: np.ndarray) -> np.ndarray:
    """Pixels in the skin-tone region (orange hues, moderate saturation)."""
    r, g, b = px[:, 0], px[:, 1], px[:, 2]
    frac = (g - b) / np.maximum(r - b, 1e-4)
    return ((r >= g) & (g >= b) & (sat > 0.1) & (sat < 0.75)
            & (lum > 0.12) & (lum < 0.92)
            & (frac > 0.2) & (frac < 0.95))                   # orange to yellow-orange hues


def estimate_illuminant(img: np.ndarray) -> tuple[np.ndarray | None, float]:
    """
    (colour of the light as a linear-light RGB triple with G=1, confidence 0..1).

    Grey-world over trustworthy pixels (not clipped, not near black, strongly
    coloured objects and skin down-weighted so a red wall or a portrait does
    not tint the whole frame), blended with a white-patch estimate from the
    brightest unclipped near-neutral pixels (specular highlights / white
    objects) when it agrees with grey-world.
    """
    colour = _colour()
    px = _sample(img, 220).reshape(-1, 3)
    mx = px.max(axis=1)
    valid = (mx < 0.97) & (0.2126 * px[:, 0] + 0.7152 * px[:, 1] + 0.0722 * px[:, 2] > 0.04)
    if valid.sum() < 50:
        return None, 0.0
    lin = colour.to_linear(px.reshape(-1, 1, 3)).reshape(-1, 3)

    # Saturation and skin are judged on the picture as it would look once
    # corrected (a strong cast makes every pixel look saturated), so the
    # estimate is refined a few times: estimate, correct, re-weight.
    illum = np.ones(3)
    confidence = 0.0
    for _ in range(4):
        corr_lin = np.clip(lin / illum, 0.0, 1.0)
        enc = colour.to_encoded(corr_lin.reshape(-1, 1, 3)).reshape(-1, 3)
        cmx, cmn = enc.max(axis=1), enc.min(axis=1)
        lum = 0.2126 * enc[:, 0] + 0.7152 * enc[:, 1] + 0.0722 * enc[:, 2]
        sat = (cmx - cmn) / np.maximum(cmx, 1e-4)
        w = valid / (1.0 + (sat / 0.5) ** 2)
        w = np.where(_is_skin(enc, lum, sat), w * 0.12, w)
        total = float(w.sum())
        if total < 5.0:
            return None, 0.0
        mean = (corr_lin * w[:, None]).sum(axis=0) / total
        mean = mean / max(mean[1], 1e-6)
        illum = illum * mean
        illum = illum / illum[1]
        confidence = float(np.clip(total / len(px) * 1.5, 0.0, 1.0))
    grey_world = illum

    # Specular / white-patch: top 3 % of unclipped, low-saturation pixels.
    cand = valid & (sat < 0.3) & (mx < 0.99)
    if cand.sum() >= 30:
        cut = np.percentile(lum[cand], 97)
        top = cand & (lum >= cut)
        if top.sum() >= 5:
            patch = lin[top].mean(axis=0)
            patch = patch / max(patch[1], 1e-6)
            # Trust it only when it roughly agrees with grey-world: a coloured
            # bright object (a yellow sign) must not steer the result.
            if np.abs(np.log(patch / grey_world)).max() < 0.30:
                illum = np.exp(0.65 * np.log(grey_world) + 0.35 * np.log(patch))
    return illum, confidence


def auto_white_balance(img: np.ndarray, baseline: float) -> dict:
    """Temp/Tint that neutralise the scene (``img``: pre-WB render, 0..1)."""
    illum, confidence = estimate_illuminant(img)
    if illum is None:
        return {"color_temp": int(round(baseline)), "tint": 0, "confidence": 0.0}
    temp, tint = wb_solve_linear(illum, baseline)
    # Keep a runaway estimate (a frame that is all one colour) within reason.
    temp = int(np.clip(temp, baseline * 0.5, baseline * 2.0))
    temp = int(np.clip(temp, TEMP_MIN, TEMP_MAX))
    tint = int(np.clip(tint, -60, 60))
    return {"color_temp": temp, "tint": tint, "confidence": round(confidence, 3),
            "illuminant": [round(float(v), 4) for v in illum]}


# ---------------------------------------------------------------------------
# Histogram (256 bins, one pass)
# ---------------------------------------------------------------------------

def to_uint8(img: np.ndarray) -> np.ndarray:
    """0..1 float (or uint8) RGB to uint8 in one pass."""
    if img.dtype == np.uint8:
        return img
    if cv2 is not None:
        return cv2.convertScaleAbs(np.ascontiguousarray(img, dtype=np.float32), alpha=255.0)
    return (np.clip(img, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


def histogram(img: np.ndarray, bins: int = 256, stride: int = 2) -> dict:
    """
    Per-channel and luminance histograms, ``bins`` wide (a divisor of 256).

    Quantises to 8 bits once (``stride`` 2 samples every other pixel, as the
    editor's own histogram does -- indistinguishable at this size), then one
    cv2.calcHist per channel and one for luminance. About 3 ms on a 1400 px
    preview; without OpenCV a bincount fallback gives identical counts.
    """
    if stride > 1:
        img = img[::stride, ::stride]
    u8 = np.ascontiguousarray(to_uint8(img)[:, :, :3])
    if cv2 is not None:
        gray = cv2.cvtColor(u8, cv2.COLOR_RGB2GRAY)      # BT.601 luma, same as the bincount path
        counts = np.stack([cv2.calcHist([u8], [c], None, [256], [0, 256]).ravel() for c in range(3)]
                          + [cv2.calcHist([gray], [0], None, [256], [0, 256]).ravel()])
        counts = counts.astype(np.int64)
    else:
        flat = u8.reshape(-1, 3)
        lum = (77 * flat[:, 0].astype(np.uint32) + 150 * flat[:, 1] + 29 * flat[:, 2] + 128) >> 8
        counts = np.stack([np.bincount(flat[:, c], minlength=256) for c in range(3)]
                          + [np.bincount(lum, minlength=256)])
    if bins != 256:
        counts = counts.reshape(4, bins, 256 // bins).sum(axis=2)
    return {"r": counts[0].tolist(), "g": counts[1].tolist(),
            "b": counts[2].tolist(), "lum": counts[3].tolist(), "bins": bins,
            "stride": stride}


# ---------------------------------------------------------------------------
# Clipping
# ---------------------------------------------------------------------------

def _threshold(value: float) -> float:
    value = float(value)
    return value / 255.0 if value > 1.0 else value


def clipping(img: np.ndarray, shadow: float = 2 / 255, highlight: float = 253 / 255) -> dict:
    """
    Clipping masks and percentages on a rendered 0..1 image.

    A pixel is highlight-clipped when any channel is >= ``highlight`` and
    shadow-clipped when any channel is <= ``shadow``. Thresholds are 0..1
    fractions or 0..255 levels. ``masks`` are boolean H x W arrays;
    ``percent`` the share of pixels, overall and per channel.
    """
    lo, hi = _threshold(shadow), _threshold(highlight)
    if not 0.0 <= lo < hi <= 1.0:
        raise ValueError("Clipping thresholds must satisfy 0 <= shadow < highlight <= 1")
    lo_c = img[:, :, :3] <= lo
    hi_c = img[:, :, :3] >= hi
    n = float(img.shape[0] * img.shape[1])

    def pct(mask) -> float:
        return round(float(np.count_nonzero(mask)) / n * 100.0, 3)

    shadow_any = lo_c.any(axis=2)
    highlight_any = hi_c.any(axis=2)
    return {
        "masks": {"shadows": shadow_any, "highlights": highlight_any},
        "percent": {
            "shadows": pct(shadow_any),
            "highlights": pct(highlight_any),
            "shadows_channels": {c: pct(lo_c[:, :, i]) for i, c in enumerate("rgb")},
            "highlights_channels": {c: pct(hi_c[:, :, i]) for i, c in enumerate("rgb")},
        },
        "thresholds": {"shadow": lo, "highlight": hi},
    }


SHADOW_COLOR = (0, 90, 255)       # blue
HIGHLIGHT_COLOR = (255, 30, 30)   # red


def clipping_rgba(masks: dict) -> np.ndarray:
    """Transparent RGBA overlay: blue where shadows clip, red where highlights do."""
    shadows, highlights = masks["shadows"], masks["highlights"]
    out = np.zeros(shadows.shape + (4,), dtype=np.uint8)
    out[shadows] = (*SHADOW_COLOR, 255)
    out[highlights] = (*HIGHLIGHT_COLOR, 255)       # highlights win a tie
    return out


# ---------------------------------------------------------------------------
# Auto tone
# ---------------------------------------------------------------------------

TARGET_MEDIAN = 0.45          # encoded mid-grey of a pleasing frame (linear ~0.17)
CLIP_LIMIT = 0.005            # at most 0.5 % of pixels may clip after auto
TONE_KEYS = ("exposure", "contrast", "highlights", "shadows", "whites", "blacks", "vibrance")
HEADROOM = 2.0                # linear value Highlights can still pull back (~1 stop over white)


def _tone_eval(sample: np.ndarray, s: dict) -> np.ndarray:
    ed = _ed()
    return ed._stage_saturation(ed._stage_tone(sample, 5500, dict(s, edit_version=2)), s)


def _tone_stats(out: np.ndarray) -> dict:
    ed = _ed()
    lum = ed._luminance(out)
    p = np.percentile(lum, (0.5, 5, 50, 95, 99.5))
    mx = out.max(axis=2)
    mid = (lum > 0.1) & (lum < 0.9)
    chroma = np.abs(out - lum[:, :, None]).max(axis=2)
    return {
        "p005": float(p[0]), "p5": float(p[1]), "median": float(p[2]),
        "p95": float(p[3]), "p995": float(p[4]),
        "hi_clip": float((lum >= 0.985).mean()),
        "lo_clip": float((lum <= 0.012).mean()),
        "chroma": float(chroma[mid].mean()) if mid.any() else 0.0,
        "ch_hi_clip": float((mx >= 0.995).mean()),
    }


def _lin(v: float) -> float:
    """sRGB code value to linear light."""
    return float(_colour().srgb_decode_exact(np.float32(v)))


def auto_tone(img: np.ndarray) -> dict:
    """
    Lightroom-style Auto for ``img`` (encoded 0..1 RGB, white balance already applied).

    Deterministic: closed-form first guesses from the luminance percentiles
    (exposure in stops of linear light), then corrective passes that run the
    editor's own tone stage on a ~160 px sample and nudge the sliders until at
    most 0.5 % of pixels clip and the median sits near mid-grey. Tens of
    milliseconds on a preview.

    Returns {"settings": {...}, "before": stats, "after": stats}; settings use
    the editor's slider units, so applying them is one ordinary edit (one
    history step).
    """
    ed = _ed()
    colour = _colour()
    sample = _sample(img, 160)
    lum = ed._luminance(sample)
    lin_lum = ed._luminance(colour.to_linear(sample))
    before = _tone_stats(sample)

    s = {k: 0 for k in TONE_KEYS}
    s["exposure"] = 0.0

    # --- Exposure: median to mid-grey, but keep highlights recoverable ----
    med = max(float(np.median(lin_lum)), 1e-4)
    p995 = max(float(np.percentile(lin_lum, 99.5)), 1e-3)
    e = float(np.log2(_lin(TARGET_MEDIAN) / med))
    e = min(e, float(np.log2(HEADROOM / p995)))
    s["exposure"] = round(float(np.clip(e, -5.0, 5.0)) * 20) / 20

    gain = 2.0 ** s["exposure"]
    q98 = float(np.percentile(lin_lum, 98)) * gain
    q5 = float(np.percentile(lin_lum, 5)) * gain
    # --- Highlights / Shadows (stops of linear light) ----------------------
    s["highlights"] = -int(np.clip(np.log2(max(q98, 1e-4) / 0.75) / 1.2 * 100.0, 0, 100))
    q5_enc = float(colour.srgb_encode_exact(np.float32(min(q5, 1.0))))
    if q5_enc < 0.2:
        s["shadows"] = int(np.clip((0.2 - q5_enc) / 0.2 * 60.0, 0, 80))

    # --- Contrast from the spread that is left ----------------------------
    st = _tone_stats(_tone_eval(sample, s))
    span = st["p95"] - st["p5"]
    s["contrast"] = int(np.clip((0.66 - span) / 0.66 * 100.0, -25, 75))

    # --- Blacks / Whites anchor the ends ----------------------------------
    st = _tone_stats(_tone_eval(sample, s))
    s["blacks"] = int(np.clip((_lin(0.02) - _lin(st["p005"])) / 0.012 * 100.0, -60, 40))
    if st["p995"] > 0.88:
        stops = float(np.log2(_lin(0.97) / max(_lin(st["p995"]), 1e-4)))
        s["whites"] = int(np.clip(stops / 0.75 * 100.0 / 0.7, -40, 40))

    # --- Corrective passes: no clipping, centred median --------------------
    for _ in range(8):
        st = _tone_stats(_tone_eval(sample, s))
        changed = False
        if st["hi_clip"] > CLIP_LIMIT:
            if s["highlights"] > -100:
                s["highlights"] = max(-100, s["highlights"] - 15)
            elif s["whites"] > -60:
                s["whites"] -= 15
            else:
                s["exposure"] = round(s["exposure"] - 0.1, 2)
            changed = True
        if st["lo_clip"] > CLIP_LIMIT:
            if s["blacks"] < 40:
                s["blacks"] = min(40, s["blacks"] + 10)
            else:
                s["shadows"] = min(80, s["shadows"] + 10)
            changed = True
        if not changed and not 0.36 <= st["median"] <= 0.56:
            ratio = _lin(TARGET_MEDIAN) / max(_lin(st["median"]), 1e-4)
            s["exposure"] = round(float(np.clip(
                s["exposure"] + np.log2(ratio) * 0.7, -5.0, 5.0)) * 20) / 20
            changed = True
        if not changed:
            break

    # --- Vibrance from the colour that is left (skip greyscale) -----------
    st = _tone_stats(_tone_eval(sample, s))
    if st["chroma"] < 0.008:
        s["vibrance"] = 0
    else:
        s["vibrance"] = int(np.clip((0.14 - st["chroma"]) / 0.14 * 45.0, -10, 35))

    s["exposure"] = round(float(s["exposure"]), 2)
    for k in TONE_KEYS[1:]:
        s[k] = int(s[k])
    after = _tone_stats(_tone_eval(sample, s))
    return {"settings": s,
            "before": {k: round(v, 4) for k, v in before.items()},
            "after": {k: round(v, 4) for k, v in after.items()}}
