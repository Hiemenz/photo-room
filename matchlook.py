"""
Match reference look: derive an *editable* preset from a reference image.

No model and nothing hidden. The target's and the reference's histograms and colour
statistics are compared and the difference is written out as ordinary controls:

1. **Tone curve.** The luminance histogram of the target is matched to the reference's
   (CDF matching), smoothed, and reduced to a handful of curve points on the RGB curve.
2. **Per-channel curves.** After that curve, the red/green/blue histograms are matched
   again; only the part that differs between the channels (the colour cast, not the
   brightness already handled) becomes the red/green/blue curves, at reduced strength.
3. **Colour mix.** Overall saturation, then Saturation per hue band (the HSL mixer),
   from the ratio of mean saturation in the reference and the target.
4. **Grading.** What remains of the average colour in the shadows, midtones and
   highlights becomes the three grading wheels (hue and saturation).

Everything is computed on small (256 px) copies and returns plain slider values plus
a report that states what each step did and how much closer the match got
(distances measured by rendering the derived values through the real colour stages).
The reference is a statistical target, not a template: a photo with different content
(more sky, a different subject) can only be matched as far as the statistics allow, and
``amount`` in the preset panel scales the result.
"""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, ImageOps

import colormix
import render_kernels
import tonecurve

EDGE = 256
CURVE_KNOTS = (0, 16, 48, 96, 144, 192, 232, 255)
CHANNEL_KNOTS = (0, 64, 128, 192, 255)
CHANNEL_STRENGTH = 0.7
CHANNEL_CAP = 0.12            # largest per-channel offset from the RGB curve (fraction of full scale)
SAT_STRENGTH = 0.7
BAND_STRENGTH = 0.6
GRADE_STRENGTH = 0.8
GRADE_MAX_SAT = 60.0
MIN_BAND_COVERAGE = 0.015     # of the pixels (weighted) a band needs in both pictures
MAX_UPLOAD = 8 * 1024 * 1024

_SRGB = ImageCms.createProfile("sRGB")


class MatchError(ValueError):
    pass


def load_image(source, edge: int = EDGE) -> np.ndarray:
    """A float32 RGB array (0..1, sRGB-encoded, long edge ``edge``) from a path, bytes or file object."""
    try:
        if isinstance(source, (bytes, bytearray)):
            if len(source) > MAX_UPLOAD:
                raise MatchError("The reference image is larger than 8 MB")
            source = io.BytesIO(bytes(source))
        img = Image.open(source)
        img.load()
    except MatchError:
        raise
    except Exception:
        raise MatchError("The reference is not an image photo-room can read (use JPEG, PNG, TIFF or WebP)") from None
    img = ImageOps.exif_transpose(img)
    icc = img.info.get("icc_profile")
    img = img.convert("RGB")
    if icc:
        try:
            img = ImageCms.profileToProfile(img, ImageCms.ImageCmsProfile(io.BytesIO(icc)), _SRGB,
                                            outputMode="RGB")
        except Exception:
            pass                                          # an unreadable profile: treat as sRGB
    img.thumbnail((edge, edge), Image.LANCZOS)
    return np.asarray(img, dtype=np.float32) / 255.0


def downsize(rgb: np.ndarray, edge: int = EDGE) -> np.ndarray:
    h, w = rgb.shape[:2]
    scale = edge / max(h, w)
    if scale >= 1:
        return np.ascontiguousarray(rgb, dtype=np.float32)
    img = Image.fromarray((np.clip(rgb, 0, 1) * 255 + 0.5).astype(np.uint8))
    img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    return np.asarray(img, dtype=np.float32) / 255.0


def _luma(img: np.ndarray) -> np.ndarray:
    return img[..., 0] * 0.2126 + img[..., 1] * 0.7152 + img[..., 2] * 0.0722


def _cdf(values: np.ndarray, bins: int = 256) -> np.ndarray:
    hist, _ = np.histogram(np.clip(values, 0, 1), bins=bins, range=(0, 1))
    c = np.cumsum(hist).astype(np.float64)
    return c / max(c[-1], 1.0)


def match_table(src: np.ndarray, ref: np.ndarray, smooth: int = 9) -> np.ndarray:
    """256-entry table mapping target values (0..1 in 1/255 steps) onto the reference distribution."""
    cs, cr = _cdf(src), _cdf(ref)
    grid = np.linspace(0, 1, 256)
    # Inverse CDF of the reference: the first value whose cumulative share reaches ours.
    table = grid[np.minimum(np.searchsorted(cr, cs - 1e-12, side="left"), 255)]
    # Where the target has no pixels the mapping is undefined. Carry the *offset* from the
    # identity through from the nearest populated values, so an empty end of the range
    # keeps its tones in place instead of snapping them to the edge of the populated part.
    present = np.diff(np.concatenate([[0.0], cs])) > 0
    if present.any() and not present.all():
        offset = np.interp(grid, grid[present], (table - grid)[present])
        table = np.clip(grid + offset, 0, 1)
    pad = smooth // 2
    kernel = np.ones(smooth) / smooth
    padded = np.concatenate([np.full(pad, table[0]), table, np.full(pad, table[-1])])
    table = np.convolve(padded, kernel, mode="valid")
    return np.maximum.accumulate(np.clip(table, 0, 1))


def _knots(table: np.ndarray, xs, strength: float = 1.0) -> list[list[float]]:
    pts = []
    for x in xs:
        y = float(table[int(x)]) * 255.0
        pts.append([float(x), float(x) + (y - x) * strength])
    return tonecurve.normalise_points(pts)


def _hue_degrees_to_tint(hue: float) -> np.ndarray:
    tint = colormix.hsv_to_rgb(np.float32(hue), np.float32(1.0), np.float32(1.0))
    return np.asarray(tint, dtype=np.float64) - float(colormix._luma(np.asarray(tint, np.float32)[None, None, :])[0, 0])


def _best_wheel(offset: np.ndarray) -> tuple[float, float]:
    """(hue, saturation 0..100) whose grading tint best reproduces a zero-luma colour offset."""
    best, best_err = (0.0, 0.0), float(offset @ offset)
    for hue in range(0, 360, 3):
        t = _hue_degrees_to_tint(hue)
        k = float(offset @ t) / float(t @ t)
        if k <= 0:
            continue
        err = float(((offset - k * t) ** 2).sum())
        if err < best_err:
            best, best_err = (float(hue), k / colormix.GRADE_TINT_STRENGTH * 100.0), err
    return best[0], min(GRADE_MAX_SAT, best[1])


def render(target: np.ndarray, values: dict) -> np.ndarray:
    """The derived values through the real colour stages (curve, saturation, HSL, grading)."""
    img = tonecurve.apply_tone_curve(target, values.get("tone_curve"))
    img = render_kernels.saturation_stage(img, values)
    img = colormix.apply_hsl(img, values)
    return colormix.apply_grading(img, values)


def distance(a: np.ndarray, b: np.ndarray) -> dict:
    """How far two pictures are in the statistics this module matches (0 = identical)."""
    tone = float(np.abs(_cdf(_luma(a)) - _cdf(_luma(b))).mean())
    channels = float(np.abs(a.reshape(-1, 3).mean(0) - b.reshape(-1, 3).mean(0)).mean())
    sa, sb = _mean_sat(a), _mean_sat(b)
    return {"tone": round(tone, 4), "colour_cast": round(channels, 4), "saturation": round(abs(sa - sb), 4)}


def _mean_sat(img: np.ndarray) -> float:
    _, s, v = colormix.rgb_to_hsv(np.ascontiguousarray(img, dtype=np.float32))
    keep = v > 0.1
    return float(s[keep].mean()) if keep.any() else 0.0


def derive(target: np.ndarray, reference: np.ndarray) -> dict:
    """
    Derive adjustment values that move ``target`` toward ``reference``.

    Both are float RGB arrays (0..1, display encoded). Returns ``{"values", "report"}``.
    """
    t0, ref = downsize(target), downsize(reference)
    if min(t0.shape[:2]) < 8 or min(ref.shape[:2]) < 8:
        raise MatchError("Both pictures need to be at least 8 pixels on each side")
    values: dict = {}
    steps: list[str] = []

    # 1. Luminance curve.
    lum_table = match_table(_luma(t0), _luma(ref))
    rgb_pts = _knots(lum_table, CURVE_KNOTS)
    tc = tonecurve.default_curve()
    tc["points"]["rgb"] = rgb_pts
    t1 = tonecurve.apply_tone_curve(t0, tc)
    moved = max(abs(y - x) for x, y in rgb_pts)
    steps.append(f"Tone curve: {len(rgb_pts)} points, largest move {moved:.0f}/255")

    # 2. Per-channel curves: the colour-only part of per-channel histogram matching.
    tables = np.stack([match_table(t1[..., c], ref[..., c]) for c in range(3)])
    centre = tables.mean(axis=0)
    cast = False
    for c, name in enumerate(("red", "green", "blue")):
        delta = np.clip((tables[c] - centre) * CHANNEL_STRENGTH, -CHANNEL_CAP, CHANNEL_CAP)
        pts = []
        for x in CHANNEL_KNOTS:
            pts.append([float(x), float(x) + float(delta[int(x)]) * 255.0 if 0 < x < 255 else float(x)])
        pts = tonecurve.normalise_points(pts)
        if max(abs(y - x) for x, y in pts) >= 2.0:
            tc["points"][name] = pts
            cast = True
    if cast:
        t2 = tonecurve.apply_tone_curve(t0, tc)
        steps.append("Red/green/blue curves: removed the colour cast difference")
    else:
        t2 = t1
        steps.append("Red/green/blue curves: no cast difference worth correcting")
    values["tone_curve"] = tonecurve.normalise(tc)

    # 3. Colour mix: overall saturation, then per hue band.
    s_t, s_r = _mean_sat(t2), _mean_sat(ref)
    if s_t > 1e-4:
        sat = float(np.clip((s_r / s_t - 1.0) * 100.0 * SAT_STRENGTH, -80, 80))
        if abs(sat) >= 2:
            values["saturation"] = round(sat)
    t3 = render_kernels.saturation_stage(t2, values)
    steps.append(f"Saturation {values.get('saturation', 0):+d} (mean saturation {s_t:.2f} -> reference {s_r:.2f})"
                 if "saturation" in values else "Saturation: already close")
    band_notes = []
    h_t, s_tt, v_t = colormix.rgb_to_hsv(np.ascontiguousarray(t3))
    h_r, s_rr, v_r = colormix.rgb_to_hsv(np.ascontiguousarray(ref))

    def band_stats(h, s, v):
        keep = (s > 0.12) & (v > 0.1)
        w = colormix.band_weights(h[keep])                       # (n, 8)
        total = max(1, h.size)
        cover = w.sum(axis=0) / total
        mean = (w * s[keep][:, None]).sum(axis=0) / np.maximum(w.sum(axis=0), 1e-9)
        return cover, mean

    c_t, m_t = band_stats(h_t, s_tt, v_t)
    c_r, m_r = band_stats(h_r, s_rr, v_r)
    for i, band in enumerate(colormix.BANDS):
        if c_t[i] < MIN_BAND_COVERAGE or c_r[i] < MIN_BAND_COVERAGE or m_t[i] < 1e-4:
            continue
        amount = float(np.clip((m_r[i] / m_t[i] - 1.0) * 100.0 * BAND_STRENGTH, -60, 60))
        if abs(amount) >= 4:
            values[f"sat_{band}"] = round(amount)
            band_notes.append(f"{band} {values[f'sat_{band}']:+d}")
    steps.append("Colour mixer saturation: " + (", ".join(band_notes) if band_notes else "no band differs enough"))
    t4 = colormix.apply_hsl(t3, values)

    # 4. Grading: the average colour left in each tonal range, as a wheel.
    luma_t, luma_r = _luma(t4), _luma(ref)
    ws = colormix.grade_masks(luma_t, 50, 0)
    wr = colormix.grade_masks(luma_r, 50, 0)
    wheel_notes = []
    for name, wt, wrf in zip(("shadow", "mid", "highlight"), ws, wr):
        if wt.sum() < 0.01 * wt.size or wrf.sum() < 0.01 * wrf.size:
            continue
        mean_t = (t4 * wt[..., None]).sum((0, 1)) / wt.sum()
        mean_r = (ref * wrf[..., None]).sum((0, 1)) / wrf.sum()
        lum_t = float(mean_t @ [0.2126, 0.7152, 0.0722])
        lum_r = float(mean_r @ [0.2126, 0.7152, 0.0722])
        offset = ((mean_r - lum_r) - (mean_t - lum_t)) * GRADE_STRENGTH
        hue, sat = _best_wheel(offset)
        if sat >= 2:
            values[f"grade_{name}_hue"] = round(hue)
            values[f"grade_{name}_sat"] = round(sat)
            wheel_notes.append(f"{name} hue {round(hue)} sat {round(sat)}")
    steps.append("Grading wheels: " + (", ".join(wheel_notes) if wheel_notes else "no tint left to add"))

    before = distance(t0, ref)
    after_img = render(t0, values)
    after = distance(after_img, ref)
    return {"values": values,
            "report": {"steps": steps, "distance_before": before, "distance_after": after}}


def derive_from_files(target_path_or_array, reference_source) -> dict:
    target = (target_path_or_array if isinstance(target_path_or_array, np.ndarray)
              else load_image(Path(target_path_or_array)))
    reference = reference_source if isinstance(reference_source, np.ndarray) else load_image(reference_source)
    return derive(target, reference)
