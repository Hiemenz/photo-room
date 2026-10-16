#!/usr/bin/env python3
"""
Local web editor for reviewing and adjusting converted photos.

Renders a live preview from the raw data as you move the sliders, then
writes the result back as XMP develop settings — the pixels in the file
are never modified, so every edit stays reversible.

Run:
  python editor.py ~/Pictures/dng/
"""

import argparse
import base64
import os
import io
import json
from collections import OrderedDict
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
import rawpy
from flask import Flask, Response, jsonify, render_template, request
from PIL import Image, ImageFilter

import colormix
import library
import history
import quality
import style
import thumbs
import tonecurve
from tonecurve import apply_tone_curve
from process import (
    EXIFTOOL_TIMEOUT,
    compute_auto_adjustments,
    detect_foreign_edits,
    edit_target,
    has_sidecar,
    list_photos,
    decode_for_analysis,
    photo_features,
    verify_xmp_written,
    extract_original_raw,
    get_embedded_original_name,
    strip_xmp_adjustments,
)


app = Flask(__name__)


# A page open in the same browser can post to this server. Without a check,
# a cross-origin form POST with Content-Type text/plain skips the CORS
# preflight entirely, and request.get_json(force=True) parses it regardless
# — so any site visited while the editor is running could reject
# photographs, strip develop settings, or overwrite a whole shoot.
@app.before_request
def _reject_cross_origin_writes():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None

    origin = request.headers.get("Origin")
    if origin is None:
        # Same-origin fetch from the editor's own page, or a command-line
        # client. Browsers attach Origin to every cross-origin request.
        return None

    allowed = {f"http://{request.host}", f"https://{request.host}"}
    if origin not in allowed:
        return jsonify({
            "error": "Refused: this request came from another site. "
                     "The editor only accepts changes from its own page.",
        }), 403
    return None


def _json_body():
    """
    The request body as JSON, accepting only a JSON content type.

    force=True parsed anything, which is what let a text/plain form POST
    through without a CORS preflight.
    """
    if not request.is_json:
        raise ValueError("Expected a JSON request body")
    return request.get_json(silent=True) or {}

# Rendering raw data is slow (~1-2s), so each file's neutral render is held in
# memory and every slider move re-composites from that cached base instead.
#
# Bounded, because the entries are large: a preview is about 16MB and the
# half-size decode behind it about 23MB, so roughly 38MB per photograph.
# Unbounded, browsing a 431-frame shoot would have held 16GB — measured at
# 1.3GB after only 40 photos. Eight is enough for arrow-key navigation and
# before/after comparison to stay instant while capping the cost near 300MB.
MAX_CACHED_PHOTOS = 8

_base_cache: OrderedDict[str, tuple[np.ndarray, int]] = OrderedDict()
# Raw decode results, reused so opening a file does not decode the sensor twice.
_decode_cache: OrderedDict[str, tuple[np.ndarray, int, np.ndarray]] = OrderedDict()
_cache_lock = threading.Lock()


def _evict_if_needed() -> None:
    """Drop the least recently used photos. Caller must hold the lock."""
    while len(_base_cache) > MAX_CACHED_PHOTOS:
        oldest, _ = _base_cache.popitem(last=False)
        _decode_cache.pop(oldest, None)
    # The decode cache can also be populated on its own, so bound it too.
    while len(_decode_cache) > MAX_CACHED_PHOTOS:
        _decode_cache.popitem(last=False)


def cache_stats() -> dict:
    with _cache_lock:
        return {
            "previews": len(_base_cache),
            "decodes": len(_decode_cache),
            "limit": MAX_CACHED_PHOTOS,
        }

LIBRARY_ROOT: Path = Path.cwd()
PREVIEW_MAX_EDGE = 1400

# Whether a save also writes a Lightroom-style .xmp beside the photo.
# Off unless --write-sidecars is passed: a sidecar is what Lightroom reads
# back, so writing one decides which application wins.
WRITE_SIDECARS = False

# Slider definitions shared with the front-end so the two can't drift apart.
CONTROLS = [
    {"key": "exposure",   "label": "Exposure",   "min": -5.0, "max": 5.0,   "step": 0.05, "default": 0.0},
    {"key": "contrast",   "label": "Contrast",   "min": -100, "max": 100,   "step": 1,    "default": 0},
    {"key": "highlights", "label": "Highlights", "min": -100, "max": 100,   "step": 1,    "default": 0},
    {"key": "shadows",    "label": "Shadows",    "min": -100, "max": 100,   "step": 1,    "default": 0},
    {"key": "whites",     "label": "Whites",     "min": -100, "max": 100,   "step": 1,    "default": 0},
    {"key": "blacks",     "label": "Blacks",     "min": -100, "max": 100,   "step": 1,    "default": 0},
    {"key": "color_temp", "label": "Temp",       "min": 2000, "max": 12000, "step": 50,   "default": 5500},
    {"key": "tint",       "label": "Tint",       "min": -150, "max": 150,   "step": 1,    "default": 0},
    {"key": "vibrance",   "label": "Vibrance",   "min": -100, "max": 100,   "step": 1,    "default": 0},
    {"key": "saturation", "label": "Saturation", "min": -100, "max": 100,   "step": 1,    "default": 0},
    {"key": "clarity",    "label": "Clarity",    "min": -100, "max": 100,   "step": 1,    "default": 0},
    {"key": "texture",    "label": "Texture",    "min": -100, "max": 100,   "step": 1,    "default": 0},
    {"key": "dehaze",     "label": "Dehaze",     "min": -100, "max": 100,   "step": 1,    "default": 0},
    {"key": "sharpness",  "label": "Sharpness",  "min": 0,    "max": 150,   "step": 1,    "default": 0},
    {"key": "straighten", "label": "Straighten",  "min": -15.0, "max": 15.0, "step": 0.1,  "default": 0.0},
    # Not shown in the preview, and deliberately so: the preview is a
    # half-size render, and downsampling already hides the noise this
    # removes. Previewing it there would misrepresent the full-resolution
    # result in both directions.
    {"key": "luminance_nr", "label": "Noise reduction", "min": 0, "max": 100, "step": 1, "default": 0},
]

# Applied on top of the per-slider values rather than alongside them: strength
# scales the whole edit toward the untouched photo, and monochrome drops colour
# after every other adjustment has been composited.
GLOBAL_CONTROLS = [
    {"key": "strength", "label": "Edit strength", "min": 0, "max": 150, "step": 1, "default": 100},
]

HISTOGRAM_BINS = 64

# HSL / B&W mixer and colour grading sliders (see colormix.py). Kept apart from
# CONTROLS so the main slider list stays short; the UI builds its own panel.
COLOR_CONTROLS = colormix.COLOR_CONTROLS

# Reloaded after each save so a new edit influences the next photo you open.
_style_model = None
_style_lock = threading.Lock()


def get_style_model():
    global _style_model
    with _style_lock:
        if _style_model is None:
            _style_model = style.load_model()
        return _style_model


def refresh_style_model():
    """Re-fit from the store. Failures must never block a save."""
    global _style_model
    try:
        model = style.load_model()
    except Exception:
        return
    with _style_lock:
        _style_model = model


# ---------------------------------------------------------------------------
# Raw rendering
# ---------------------------------------------------------------------------

def _load_base_render(path: Path) -> tuple[np.ndarray, int]:
    """
    Return (neutral RGB float array in 0..1, camera baseline temp in K).

    Rendered once per file with camera white balance and no auto brightening,
    so the slider values are applied against a predictable starting point.
    """
    key = str(path.resolve())
    with _cache_lock:
        if key in _base_cache:
            # Touch it so the photo being worked on is never the one evicted.
            _base_cache.move_to_end(key)
            if key in _decode_cache:
                _decode_cache.move_to_end(key)
            return _base_cache[key]

    rgb, baseline_temp, cam_mul = decode_for_analysis(path)

    img = Image.fromarray(rgb)
    img.thumbnail((PREVIEW_MAX_EDGE, PREVIEW_MAX_EDGE), Image.LANCZOS)
    base = np.asarray(img, dtype=np.float32) / 255.0

    with _cache_lock:
        _decode_cache[key] = (rgb, baseline_temp, cam_mul)
        _base_cache[key] = (base, baseline_temp)
        _evict_if_needed()
    return base, baseline_temp


def _luminance(img: np.ndarray) -> np.ndarray:
    return 0.2126 * img[:, :, 0] + 0.7152 * img[:, :, 1] + 0.0722 * img[:, :, 2]


def _blur(plane: np.ndarray, radius: float) -> np.ndarray:
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


def _largest_inscribed_rect(w: float, h: float, angle_rad: float) -> tuple[float, float]:
    """
    Biggest axis-aligned rectangle of the original proportions that fits
    inside a rotated w x h rectangle.

    Straightening otherwise leaves wedge-shaped gaps at the corners, so the
    result is cropped to this rather than showing them.
    """
    if w <= 0 or h <= 0:
        return 0.0, 0.0

    angle = abs(angle_rad) % np.pi
    if angle > np.pi / 2:
        angle = np.pi - angle

    sin_a, cos_a = abs(np.sin(angle)), abs(np.cos(angle))
    short, long_ = (w, h) if w < h else (h, w)

    if short <= 2.0 * sin_a * cos_a * long_ or abs(sin_a - cos_a) < 1e-10:
        # The limit is a single corner; the rectangle is half the short side.
        x = 0.5 * short
        wr, hr = (x / sin_a, x / cos_a) if w >= h else (x / cos_a, x / sin_a)
    else:
        cos_2a = cos_a * cos_a - sin_a * sin_a
        wr = (w * cos_a - h * sin_a) / cos_2a
        hr = (h * cos_a - w * sin_a) / cos_2a

    return max(0.0, wr), max(0.0, hr)


def apply_geometry(img: np.ndarray, adj: dict) -> np.ndarray:
    """
    Straighten and crop.

    Runs before the tonal work so that every downstream measurement — the
    histogram, the clipping readout, the local-contrast masks — describes
    the picture the photographer is actually keeping rather than material
    that has been cropped away.
    """
    angle = float(adj.get("straighten", 0.0))
    crop = adj.get("crop")

    if angle:
        h, w = img.shape[:2]
        as_u8 = np.clip(img * 255.0 + 0.5, 0, 255).astype(np.uint8)
        rotated = Image.fromarray(as_u8).rotate(
            angle, resample=Image.BICUBIC, expand=False
        )
        img = np.asarray(rotated, dtype=np.float32) / 255.0

        keep_w, keep_h = _largest_inscribed_rect(w, h, np.deg2rad(angle))
        # Round inward and drop a further pixel each side: the exact rectangle
        # touches the rotated edge, where bicubic resampling has already
        # blended in the empty background. Rounding outward leaves a black
        # line along the border.
        keep_w = int(keep_w) - 2
        keep_h = int(keep_h) - 2
        if keep_w >= 8 and keep_h >= 8:
            x0 = (w - keep_w) // 2
            y0 = (h - keep_h) // 2
            img = img[y0:y0 + keep_h, x0:x0 + keep_w]

    if crop:
        h, w = img.shape[:2]
        left = float(np.clip(crop.get("left", 0.0), 0.0, 1.0))
        top = float(np.clip(crop.get("top", 0.0), 0.0, 1.0))
        right = float(np.clip(crop.get("right", 1.0), 0.0, 1.0))
        bottom = float(np.clip(crop.get("bottom", 1.0), 0.0, 1.0))

        x0, x1 = int(round(left * w)), int(round(right * w))
        y0, y1 = int(round(top * h)), int(round(bottom * h))
        # A crop that inverts or collapses would produce an empty array and
        # break every downstream step, so ignore it rather than propagate it.
        if x1 - x0 >= 8 and y1 - y0 >= 8:
            img = img[y0:y1, x0:x1]

    return img


def _apply_detail(img: np.ndarray, adj: dict) -> np.ndarray:
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
    lum = _luminance(img)

    if clarity:
        # Large radius, weighted to the midtones: this is the "punch" control,
        # and applying it to highlights and shadows just produces halos.
        detail = lum - _blur(lum, max(4.0, edge * 0.02))
        midtones = 1.0 - (2.0 * np.clip(lum, 0.0, 1.0) - 1.0) ** 2
        img = img + (clarity / 100.0) * 0.8 * (detail * midtones)[:, :, None]
        lum = _luminance(np.clip(img, 0.0, 1.0))

    if texture:
        # Small radius: fine detail only, and deliberately not midtone-masked
        # so it can smooth skin as well as bring out fabric.
        detail = lum - _blur(lum, max(1.0, edge * 0.0025))
        img = img + (texture / 100.0) * 0.9 * detail[:, :, None]
        lum = _luminance(np.clip(img, 0.0, 1.0))

    if dehaze:
        # Dark-channel prior: haze lifts the darkest channel everywhere, so
        # the blurred per-pixel minimum estimates how much veil to remove.
        dark = np.min(np.clip(img, 0.0, 1.0), axis=2)
        veil = _blur(dark, max(6.0, edge * 0.03)) * (dehaze / 100.0) * 0.7
        veil = np.clip(veil, -0.85, 0.85)[:, :, None]
        img = (img - veil) / np.maximum(1.0 - veil, 0.15)

    if sharpness:
        detail = lum - _blur(lum, 1.2)
        img = img + (sharpness / 100.0) * 0.5 * detail[:, :, None]

    return np.clip(img, 0.0, 1.0)


def apply_adjustments(base: np.ndarray, baseline_temp: int, adj: dict) -> np.ndarray:
    """
    Composite slider values onto the neutral render.

    This approximates how Camera Raw will interpret the same XMP values; it is
    a preview for judging an edit, not a byte-exact match to Adobe's renderer.
    """
    # Geometry first, so the untouched reference the strength blend compares
    # against is the cropped picture rather than the whole frame. `base` must
    # stay pristine — the steps below mutate `img` in place.
    base = apply_geometry(base, adj)
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
    lum = _luminance(img)

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
    img = np.clip(apply_tone_curve(img, adj.get("tone_curve")), 0.0, 1.0)

    # --- Color ---
    saturation = float(adj.get("saturation", 0))
    vibrance = float(adj.get("vibrance", 0))
    if saturation or vibrance:
        gray = _luminance(img)[:, :, None]
        delta = img - gray
        factor = 1.0 + saturation / 100.0
        if vibrance:
            # Vibrance pushes muted colors harder than already-saturated ones.
            current_sat = np.clip(np.max(np.abs(delta), axis=2), 0.0, 1.0)[:, :, None]
            factor = factor + (vibrance / 100.0) * (1.0 - current_sat)
        img = gray + delta * factor

    img = np.clip(img, 0.0, 1.0)

    # --- Colour mixer / grading ---
    # In black & white the HSL sliders give way to the B&W mixer, which
    # brightens or darkens each hue band ahead of the grey conversion.
    mono = bool(adj.get("monochrome"))
    img = colormix.apply_gray_mixer(img, adj) if mono else colormix.apply_hsl(img, adj)
    if not mono:
        img = colormix.apply_grading(img, adj)

    # --- Detail ---
    img = _apply_detail(img, adj)

    # --- Edit strength ---
    # Blend the finished edit back toward the original. Above 100% it
    # extrapolates away from the original, exaggerating the same edit.
    strength = float(adj.get("strength", 100)) / 100.0
    if abs(strength - 1.0) > 1e-6:
        img = base + (img - base) * strength

    # --- Monochrome ---
    # Applied after the colour sliders so they act as channel mixers: warming
    # the temperature changes which tones render light or dark. The blend is
    # capped at fully grey — extrapolating past it would swing the image back
    # into inverted colour rather than making it "more" black and white.
    if adj.get("monochrome"):
        # Applied in full regardless of strength, because the file stores it
        # as a boolean. Blending it produced a half-desaturated preview of a
        # photograph that was saved fully black and white.
        img = np.repeat(_luminance(img)[:, :, None], 3, axis=2)
        # Colour grading still applies to a monochrome image (split toning).
        img = colormix.apply_grading(img, adj)

    return np.clip(img, 0.0, 1.0)


# A 64-bin histogram drawn 280px wide is indistinguishable whether it counts
# every pixel or one in four, and this runs on every slider move.
HISTOGRAM_STRIDE = 2


def compute_histogram(img: np.ndarray) -> dict:
    """Per-channel and luminance distributions for the preview display."""
    sample = img[::HISTOGRAM_STRIDE, ::HISTOGRAM_STRIDE]
    lum = _luminance(sample)

    # np.histogram sorts into arbitrary edges; with a fixed 0..1 range and
    # uniform bins, scaling to integer bin indices and counting is far
    # cheaper and gives identical results.
    def counts(plane: np.ndarray) -> list[int]:
        idx = np.clip(
            (plane.ravel() * HISTOGRAM_BINS).astype(np.int32), 0, HISTOGRAM_BINS - 1
        )
        return np.bincount(idx, minlength=HISTOGRAM_BINS).astype(int).tolist()

    out: dict[str, list] = {
        "r": counts(sample[:, :, 0]),
        "g": counts(sample[:, :, 1]),
        "b": counts(sample[:, :, 2]),
        "lum": counts(lum),
    }

    total = max(1, lum.size)
    out["clipped_shadows"] = round(float((lum <= 0.002).sum()) / total * 100, 2)
    out["clipped_highlights"] = round(float((lum >= 0.998).sum()) / total * 100, 2)
    return out


def render_preview(path: Path, adj: dict, quality: int = 82) -> str:
    """Render the adjusted preview and return it as a base64 data URI."""
    return render_with_histogram(path, adj, quality)["preview"]


def render_with_histogram(path: Path, adj: dict, quality: int = 82) -> dict:
    """Render the preview and its histogram from a single composite pass."""
    base, baseline_temp = _load_base_render(path)
    out = apply_adjustments(base, baseline_temp, adj)
    img = Image.fromarray((out * 255.0 + 0.5).astype(np.uint8))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return {
        "preview": f"data:image/jpeg;base64,{encoded}",
        "histogram": compute_histogram(out),
    }


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------

def effective_adjustments(adj: dict) -> dict:
    """
    Resolve the edit-strength blend into concrete slider values.

    Camera Raw has no notion of "strength", so what gets written is each
    slider scaled from its neutral point by that percentage — the numbers
    that reproduce what the preview showed.
    """
    strength = float(adj.get("strength", 100)) / 100.0
    neutral = {c["key"]: c["default"] for c in CONTROLS}

    out = dict(adj)
    if adj.get("tone_curve"):
        out["tone_curve"] = tonecurve.scale_toward_identity(adj["tone_curve"], strength)
    for control in CONTROLS:
        key = control["key"]
        if key not in adj:
            continue
        # Geometry is a framing decision, not an intensity — scaling it by
        # edit strength would slide the crop around as the slider moves.
        if key in ("straighten", "luminance_nr"):
            continue
        # Temperature scales from the camera's own white balance, not from the
        # generic 5500K default, or reducing strength would tint the photo.
        if key == "color_temp":
            # Without a baseline there is nothing to scale toward, so the
            # value is left alone rather than collapsing toward a generic
            # 5500K that would tint the photo.
            anchor = adj.get("_baseline_temp")
            if anchor is None:
                continue
        else:
            anchor = neutral[key]
        out[key] = anchor + (float(adj[key]) - anchor) * strength
    for key, default in colormix.COLOR_DEFAULTS.items():
        if key in adj and key not in colormix.UNSCALED_KEYS:
            out[key] = default + (float(adj[key]) - default) * strength
    return out


def crop_in_full_frame(
    crop: dict | None, angle: float, width: float, height: float
) -> dict:
    """
    Convert a crop expressed against the straightened preview into fractions
    of the whole straightened frame, which is what Camera Raw reads.

    The preview shows the largest rectangle that fits inside the rotation,
    so a crop dragged on screen is relative to that inset region. Writing
    those fractions unchanged selected a different part of the photograph —
    on a 300x200 frame straightened 10 degrees, a right edge dragged to 0.25
    meant 81px on screen and 75px in the file.

    With no crop at all this returns the inscribed rectangle itself, so a
    straighten-only edit trims the empty corners in Camera Raw exactly as
    the preview does, instead of writing a full 0..1 rect that shows them.
    """
    crop = crop or {"left": 0.0, "top": 0.0, "right": 1.0, "bottom": 1.0}
    if not angle or width <= 0 or height <= 0:
        return dict(crop)

    keep_w, keep_h = _largest_inscribed_rect(width, height, np.deg2rad(angle))
    if keep_w <= 0 or keep_h <= 0:
        return dict(crop)

    x0 = (width - keep_w) / 2.0
    y0 = (height - keep_h) / 2.0

    return {
        "left": (x0 + float(crop.get("left", 0.0)) * keep_w) / width,
        "right": (x0 + float(crop.get("right", 1.0)) * keep_w) / width,
        "top": (y0 + float(crop.get("top", 0.0)) * keep_h) / height,
        "bottom": (y0 + float(crop.get("bottom", 1.0)) * keep_h) / height,
    }


def _has_crop(adj: dict) -> bool:
    """
    True when the photo is actually cropped or rotated at all.

    Straightening alone counts: the rotation leaves empty wedges that the
    crop rectangle is what removes.
    """
    if float(adj.get("straighten", 0)) != 0:
        return True
    crop = adj.get("crop") or {}
    return (
        float(crop.get("left", 0.0)) > 1e-6
        or float(crop.get("top", 0.0)) > 1e-6
        or float(crop.get("right", 1.0)) < 1.0 - 1e-6
        or float(crop.get("bottom", 1.0)) < 1.0 - 1e-6
    )


# Process Version 5, which is what Camera Raw 11 writes for new images and
# the oldest version supporting every slider here — Texture and Dehaze
# included. Lightroom Classic currently writes 15.4 (Process Version 6).
DEFAULT_PROCESS_VERSION = "11.0"


def _version_tuple(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in str(value).strip().split("."))
    except (TypeError, ValueError):
        return ()


def existing_process_version(path: Path) -> str | None:
    """The process version already declared in a file, if any."""
    target = edit_target(path)
    if target != path and not target.exists():
        return None
    path = target
    result = subprocess.run(
        ["exiftool", "-XMP-crs:ProcessVersion", "-s3", "-f", str(path)],
        capture_output=True, text=True, timeout=EXIFTOOL_TIMEOUT,
    )
    raw = result.stdout.strip()
    return None if raw in ("-", "") else raw


def _xmp_arguments(adj: dict, process_version: str | None = None) -> list[str]:
    """The develop settings as exiftool arguments, without a target file.

    Shared by the DNG and the sidecar so the two cannot drift apart.
    """
    return [
        f"-XMP-crs:ProcessVersion={process_version or DEFAULT_PROCESS_VERSION}",
        "-XMP-crs:WhiteBalance=Custom",
        f"-XMP-crs:ColorTemperature={int(adj.get('color_temp', 5500))}",
        f"-XMP-crs:Tint={int(adj.get('tint', 0))}",
        f"-XMP-crs:Exposure2012={float(adj.get('exposure', 0)):.2f}",
        f"-XMP-crs:Contrast2012={int(adj.get('contrast', 0))}",
        f"-XMP-crs:Highlights2012={int(adj.get('highlights', 0))}",
        f"-XMP-crs:Shadows2012={int(adj.get('shadows', 0))}",
        f"-XMP-crs:Whites2012={int(adj.get('whites', 0))}",
        f"-XMP-crs:Blacks2012={int(adj.get('blacks', 0))}",
        f"-XMP-crs:Vibrance={int(adj.get('vibrance', 0))}",
        f"-XMP-crs:Saturation={int(adj.get('saturation', 0))}",
        f"-XMP-crs:Clarity2012={int(adj.get('clarity', 0))}",
        f"-XMP-crs:Texture={int(adj.get('texture', 0))}",
        f"-XMP-crs:Dehaze={int(adj.get('dehaze', 0))}",
        f"-XMP-crs:Sharpness={int(adj.get('sharpness', 0))}",
        f"-XMP-crs:CropAngle={float(adj.get('straighten', 0)):.2f}",
        f"-XMP-crs:HasCrop={'True' if _has_crop(adj) else 'False'}",
        f"-XMP-crs:CropLeft={float((adj.get('crop') or {}).get('left', 0.0)):.5f}",
        f"-XMP-crs:CropTop={float((adj.get('crop') or {}).get('top', 0.0)):.5f}",
        f"-XMP-crs:CropRight={float((adj.get('crop') or {}).get('right', 1.0)):.5f}",
        f"-XMP-crs:CropBottom={float((adj.get('crop') or {}).get('bottom', 1.0)):.5f}",
        f"-XMP-crs:LuminanceSmoothing={int(adj.get('luminance_nr', 0))}",
        f"-XMP-crs:ConvertToGrayscale={'True' if adj.get('monochrome') else 'False'}",
    ] + colormix.xmp_arguments(adj) + tonecurve.xmp_arguments(adj.get("tone_curve"))


def _process_version_for(path: Path) -> str:
    """
    Which process version to declare when writing this file.

    A newer one already present is kept. Process version selects the
    rendering engine, so replacing 15.4 with 11.0 would silently change how
    an existing photograph looks — a downgrade from Process Version 6 to 5,
    not just a metadata edit.
    """
    try:
        existing = existing_process_version(path)
    except Exception:
        return DEFAULT_PROCESS_VERSION

    if existing and _version_tuple(existing) > _version_tuple(DEFAULT_PROCESS_VERSION):
        return existing
    return DEFAULT_PROCESS_VERSION


def write_sidecar(path: Path, adj: dict) -> Path:
    """
    Mirror the develop settings into a Lightroom-style .xmp beside the photo.

    Written in place, so tags this tool does not manage — the tone curve, the
    lens profile, everything else Lightroom keeps there — survive untouched.
    An absent sidecar is created.
    """
    target = path.with_suffix(".xmp")
    args = _xmp_arguments(adj, process_version=_process_version_for(path))

    if target.exists():
        result = subprocess.run(
            ["exiftool"] + args + ["-overwrite_original", str(target)],
            capture_output=True, text=True, timeout=EXIFTOOL_TIMEOUT,
        )
    else:
        result = subprocess.run(
            ["exiftool"] + args + ["-o", str(target), os.devnull],
            capture_output=True, text=True, timeout=EXIFTOOL_TIMEOUT,
        )

    # Seeding a new sidecar from an empty source warns "no writable tags set"
    # while still creating the file correctly, so the file is the test.
    if not target.exists():
        raise RuntimeError(f"Could not write {target.name}: {result.stderr.strip()}")
    return target


def save_adjustments(
    path: Path,
    adj: dict,
    frame_size: tuple[float, float] | None = None,
    sidecar: bool = False,
) -> None:
    """
    Write the full slider set back as Camera Raw XMP develop settings.

    ``frame_size`` is the straightened frame's proportions, needed to express
    the crop in the coordinates Camera Raw uses. Only the aspect ratio
    matters, so the preview's dimensions are as good as the full file's.

    ``sidecar`` additionally mirrors the settings into a .xmp beside the
    photo. Off by default: a sidecar is what Lightroom reads back, so
    writing one changes which application wins, and that is the
    photographer's decision rather than a default.
    """
    adj = effective_adjustments(adj)

    angle = float(adj.get("straighten", 0) or 0)
    if angle and frame_size:
        adj = dict(adj)
        adj["crop"] = crop_in_full_frame(adj.get("crop"), angle, *frame_size)

    if edit_target(path) != path:
        # Not a DNG: the raw is never rewritten, the sidecar is the record.
        write_sidecar(path, adj)
        verify_xmp_written(edit_target(path),
                           {"ColorTemperature": str(int(adj.get("color_temp", 5500)))})
        return

    result = subprocess.run(
        ["exiftool"]
        + _xmp_arguments(adj, process_version=_process_version_for(path))
        + ["-overwrite_original", str(path)],
        capture_output=True, text=True, timeout=EXIFTOOL_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(f"exiftool failed: {result.stderr.strip()}")

    # exiftool has been seen reporting success while dropping white balance,
    # so confirm the tag is really in the file.
    verify_xmp_written(path, {"ColorTemperature": str(int(adj.get("color_temp", 5500)))})

    if sidecar:
        write_sidecar(path, adj)


def read_saved_adjustments(path: Path, prefer_sidecar: bool = True) -> dict | None:
    """
    Return slider values already stored for this photo.

    When a Lightroom-style .xmp sidecar sits beside the file it is read in
    preference to the copy inside the DNG. Both were observed on a real
    shoot carrying different values for the same photograph — exposure
    +1.12 against +1.11, highlights -84 against -81 — and the sidecar is
    what Lightroom reads back when sidecar writing is enabled, so it is the
    one that matches what the photographer sees in their catalogue.
    """
    if edit_target(path) != path:
        sidecar = edit_target(path)
        return _read_adjustments_from(sidecar) if sidecar.exists() else None
    if prefer_sidecar:
        sidecar = path.with_suffix(".xmp")
        if sidecar.exists():
            values = _read_adjustments_from(sidecar)
            if values:
                return values
    return _read_adjustments_from(path)


def _read_adjustments_from(path: Path) -> dict | None:
    """Return slider values already stored in the file, or None if it has none."""
    tags = [
        "ColorTemperature", "Tint", "Exposure2012", "Contrast2012",
        "Highlights2012", "Shadows2012", "Whites2012", "Blacks2012",
        "Vibrance", "Saturation",
        "Clarity2012", "Texture", "Dehaze", "Sharpness",
        "LuminanceSmoothing", "ConvertToGrayscale",
        "CropAngle", "HasCrop", "CropLeft", "CropTop", "CropRight", "CropBottom",
    ]
    args = [f"-XMP-crs:{t}" for t in tags] + ["-s3", "-f", str(path)]
    result = subprocess.run(["exiftool"] + args, capture_output=True, text=True, timeout=EXIFTOOL_TIMEOUT)
    values = result.stdout.strip().split("\n")
    if len(values) < len(tags) or all(v.strip() in ("-", "") for v in values):
        # No develop settings, but a curve alone is still an edit.
        curve = _read_tone_curve(path)
        return {"tone_curve": curve} if curve else None

    mapping = {
        "color_temp": 0, "tint": 1, "exposure": 2, "contrast": 3,
        "highlights": 4, "shadows": 5, "whites": 6, "blacks": 7,
        "vibrance": 8, "saturation": 9,
        "clarity": 10, "texture": 11, "dehaze": 12, "sharpness": 13,
        "luminance_nr": 14,
    }
    out: dict = {}
    grayscale = values[15].strip() if len(values) > 15 else "-"
    if grayscale not in ("-", ""):
        out["monochrome"] = grayscale.lower() == "true"

    def _number(index: int, fallback: float) -> float:
        raw = values[index].strip() if len(values) > index else "-"
        if raw in ("-", ""):
            return fallback
        try:
            return float(raw)
        except ValueError:
            return fallback

    out["straighten"] = _number(16, 0.0)
    if len(values) > 17 and values[17].strip().lower() == "true":
        out["crop"] = {
            "left": _number(18, 0.0),
            "top": _number(19, 0.0),
            "right": _number(20, 1.0),
            "bottom": _number(21, 1.0),
        }
    for key, idx in mapping.items():
        raw_value = values[idx].strip() if idx < len(values) else "-"
        if raw_value in ("-", ""):
            continue
        try:
            out[key] = float(raw_value)
        except ValueError:
            continue
    curve = _read_tone_curve(path)
    if curve:
        out["tone_curve"] = curve
    if out:
        out.update(_read_color_adjustments(path))
    return out or None


def _read_tone_curve(path: Path) -> dict | None:
    args = ["-sep", "|"] + [f"-XMP-crs:{t}" for t in tonecurve.xmp_read_tags()] + ["-s3", "-f", str(path)]
    result = subprocess.run(["exiftool"] + args, capture_output=True, text=True, timeout=EXIFTOOL_TIMEOUT)
    # Not stripped: an empty line is a tag with no value, and dropping
    # leading ones would shift every later tag up.
    return tonecurve.parse_xmp(result.stdout.split("\n"))


def _read_color_adjustments(path: Path) -> dict:
    """HSL, B&W mixer and colour grading values stored in the file (non-default only)."""
    args = [f"-XMP-crs:{t}" for t in colormix.XMP_TAGS.values()] + ["-s3", "-f", str(path)]
    result = subprocess.run(["exiftool"] + args, capture_output=True, text=True, timeout=EXIFTOOL_TIMEOUT)
    return colormix.parse_xmp_values(result.stdout.strip().split("\n"))



# ---------------------------------------------------------------------------
# Ratings
# ---------------------------------------------------------------------------

# XMP-xmp:Rating is the tag Lightroom, Bridge and most catalogue software
# read, and -1 is their convention for "rejected" — so marks made here show
# up wherever the photos are opened next.
REJECTED = -1


def read_rating(path: Path) -> int:
    target = edit_target(path)
    if target != path and not target.exists():
        return 0
    path = target
    result = subprocess.run(
        ["exiftool", "-XMP-xmp:Rating", "-s3", "-f", str(path)],
        capture_output=True, text=True, timeout=EXIFTOOL_TIMEOUT,
    )
    raw = result.stdout.strip()
    if raw in ("-", ""):
        return 0
    try:
        return max(REJECTED, min(5, int(float(raw))))
    except ValueError:
        return 0


def write_rating(path: Path, rating: int) -> None:
    rating = max(REJECTED, min(5, int(rating)))
    target = edit_target(path)
    if target != path and not target.exists():
        cmd = ["exiftool", f"-XMP-xmp:Rating={rating}", "-o", str(target), os.devnull]
    else:
        cmd = ["exiftool", f"-XMP-xmp:Rating={rating}", "-overwrite_original", str(target)]
    result = subprocess.run(
        cmd,
        capture_output=True, text=True, timeout=EXIFTOOL_TIMEOUT,
    )
    created_sidecar = "-o" in cmd
    if (created_sidecar and not target.exists()) or (
        not created_sidecar and result.returncode != 0
    ):
        raise RuntimeError(f"exiftool failed: {result.stderr.strip()}")


def read_ratings(paths: list[Path]) -> dict[str, int]:
    """
    Read many ratings in one exiftool invocation.

    One process per file costs about 80ms; a shoot of several hundred would
    take most of a minute before the library could even be listed.

    JSON output rather than bare values: given several files exiftool prints
    a "======== <path>" header before each one, so matching values to files
    by line position silently reports one photo's rating against another.
    The file list goes in on stdin because a few hundred paths can exceed
    the command-line length limit.
    """
    if not paths:
        return {}

    # Non-DNG raws keep their rating in a sidecar; read that instead, and
    # leave those without one at 0.
    originals = {str(edit_target(p)): str(p) for p in paths}
    out_all = {str(p): 0 for p in paths}
    targets = [edit_target(p) for p in paths
               if edit_target(p) == p or edit_target(p).exists()]
    if not targets:
        return out_all
    inner = _read_ratings_raw(targets)
    for tgt, rating in inner.items():
        out_all[originals.get(tgt, tgt)] = rating
    return out_all


def _read_ratings_raw(paths: list[Path]) -> dict[str, int]:
    result = subprocess.run(
        # -fast2 stops reading once past the metadata header, which is where
        # XMP lives in a DNG or CR2. Measured 1.4x faster over 665 real files
        # with byte-identical results, including files whose rating this tool
        # had written.
        ["exiftool", "-fast2", "-j", "-XMP-xmp:Rating", "-@", "-"],
        input="\n".join(str(p) for p in paths),
        capture_output=True,
        text=True,
        # Scales with the library: one pass over 665 files took 4 seconds.
        timeout=max(EXIFTOOL_TIMEOUT, len(paths) // 10),
    )

    out: dict[str, int] = {str(p): 0 for p in paths}
    try:
        entries = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return out

    by_resolved = {str(Path(p).resolve()): str(p) for p in paths}
    for entry in entries:
        source = entry.get("SourceFile")
        if not source:
            continue
        key = by_resolved.get(str(Path(source).resolve()), source)
        try:
            value = int(float(entry.get("Rating", 0)))
        except (TypeError, ValueError):
            value = 0
        out[key] = max(REJECTED, min(5, value))
    return out


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def describe_error(exc: Exception) -> str:
    """
    A readable message for the UI.

    rawpy raises with a bytes payload, so the naive str() of a decode failure
    reads b'Input/output error' — which tells a photographer nothing about
    which of their files is unreadable or why.
    """
    text = str(exc)
    if text.startswith(("b'", 'b"')) and text.endswith(("'", '"')):
        text = text[2:-1]
    text = text.strip() or exc.__class__.__name__

    lowered = text.lower()
    if "input/output" in lowered or "unexpected end" in lowered:
        return f"Could not read the image data ({text}) — the file may be truncated or corrupt"
    return text


def _safe_path(raw_path: str) -> Path:
    """Resolve a client-supplied path, refusing anything outside the library root."""
    candidate = (LIBRARY_ROOT / raw_path).resolve()
    root = LIBRARY_ROOT.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("Path is outside the library root")
    if not candidate.is_file():
        raise ValueError(f"No such file: {raw_path}")
    return candidate


@app.route("/")
def index():
    return render_template(
        "editor.html",
        controls=CONTROLS,
        global_controls=GLOBAL_CONTROLS,
        color_controls=COLOR_CONTROLS,
        curve_presets=tonecurve.PRESETS,
        curve_parametric=[{"key": k, "label": k.capitalize()} for k in ("highlights", "lights", "darks", "shadows")],
        root=str(LIBRARY_ROOT),
    )


@app.route("/api/files")
def api_files():
    files = [str(p.relative_to(LIBRARY_ROOT)) for p in list_photos(LIBRARY_ROOT)]
    return jsonify({"root": str(LIBRARY_ROOT), "files": files})


def complete_adjustments(adj: dict, baseline_temp: int) -> dict:
    """Fill every control, colour key, crop and curve so the UI gets a full set."""
    adj = dict(adj)
    adj.setdefault("color_temp", baseline_temp)
    merged = {c["key"]: adj.get(c["key"], c["default"]) for c in CONTROLS + GLOBAL_CONTROLS}
    merged.update({k: adj.get(k, d) for k, d in colormix.COLOR_DEFAULTS.items()})
    merged["monochrome"] = bool(adj.get("monochrome", False))
    merged["crop"] = adj.get("crop")
    merged["tone_curve"] = tonecurve.normalise(adj.get("tone_curve"))
    return merged


@app.route("/api/open", methods=["POST"])
def api_open():
    """Open a file: return its preview plus whatever adjustments it already has."""
    try:
        payload = _json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    try:
        path = _safe_path(payload["path"])
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        # Decode first so the auto analysis below can reuse the result rather
        # than unpacking the sensor data a second time.
        _, baseline_temp = _load_base_render(path)

        saved = read_saved_adjustments(path)
        if saved:
            source = "saved"
            adj = saved
        else:
            with _cache_lock:
                decoded = _decode_cache.get(str(path.resolve()))
            model = get_style_model()
            auto = compute_auto_adjustments(path, decoded=decoded, style_model=model)
            source = "styled" if auto.pop("styled", False) else "auto"
            adj = {k: v for k, v in auto.items() if k != "iso"}

        merged = complete_adjustments(adj, baseline_temp)

        base, _ = _load_base_render(path)
        assessment = quality.assess(base)

        # The as-imported state: the auto (or styled) proposal, remembered the
        # first time the photo is opened. Drives Before and reset-to-import.
        hist = history.load(path)
        if hist["original"] is None:
            if source != "saved":
                imported = merged   # already the auto proposal
            else:
                with _cache_lock:
                    decoded = _decode_cache.get(str(path.resolve()))
                auto = compute_auto_adjustments(
                    path, decoded=decoded, style_model=get_style_model())
                auto = {k: v for k, v in auto.items() if k not in ("iso", "styled")}
                imported = complete_adjustments(auto, baseline_temp)
            hist = history.set_original(path, imported)
        if not hist["entries"]:
            hist, _ = history.record(
                path, "Import" if source != "saved" else "Opened with saved edits", merged)

        rendered = render_with_histogram(path, merged)
        return jsonify({
            "history": history.summary(hist),
            "import_adjustments": complete_adjustments(hist["original"], baseline_temp),
            "preview": rendered["preview"],
            "quality": assessment,
            "histogram": rendered["histogram"],
            "adjustments": merged,
            "source": source,
            "has_original": get_embedded_original_name(path) is not None,
            "rating": read_rating(path),
            "baseline_temp": baseline_temp,
            "foreign_editor": detect_foreign_edits(path),
            "has_sidecar": has_sidecar(path),
        })
    except Exception as exc:
        return jsonify({"error": describe_error(exc)}), 500


@app.route("/api/preview", methods=["POST"])
def api_preview():
    try:
        payload = _json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    try:
        path = _safe_path(payload["path"])
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        adj = payload.get("adjustments", {})
        return jsonify(render_with_histogram(path, adj))
    except Exception as exc:
        return jsonify({"error": describe_error(exc)}), 500


@app.route("/api/save", methods=["POST"])
def api_save():
    try:
        payload = _json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    try:
        path = _safe_path(payload["path"])
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        chosen = payload.get("adjustments", {})

        frame_size = None
        if float(chosen.get("straighten", 0) or 0):
            base, _ = _load_base_render(path)
            frame_size = (base.shape[1], base.shape[0])

        save_adjustments(path, chosen, frame_size=frame_size, sidecar=WRITE_SIDECARS)
        learned = _record_for_learning(path, chosen)

        # Settings are written into the DNG, but a sidecar is read back in
        # preference to it. Without saying so, the edit appears to save and
        # then silently reverts the next time the photo is opened.
        # Only a concern when the sidecar is not being kept in step; with
        # sidecar writing on, what is read back is what was just written.
        warning = None
        if has_sidecar(path) and not WRITE_SIDECARS:
            warning = (
                f"Written into {path.name}, but {path.with_suffix('.xmp').name} "
                "sits beside it and is read first — reopening will show the "
                "sidecar's values, not these. Turn off sidecar writing in "
                "Lightroom, or edit this photo there."
            )

        return jsonify({
            "ok": True,
            "message": warning or f"Saved to {path.name}",
            "warning": warning,
            "style": learned,
        })
    except Exception as exc:
        return jsonify({"error": describe_error(exc)}), 500


def _baseline_temp_of(path: Path) -> int:
    """
    This file's own camera white balance, from the cache where possible.

    Batch apply needs one number per target and must not decode a whole
    shoot to get it, so a cached decode is reused when the photo has been
    opened; otherwise the raw header is read without rendering a preview.
    """
    key = str(path.resolve())
    with _cache_lock:
        cached = _decode_cache.get(key)
    if cached is not None:
        return cached[1]
    return decode_for_analysis(path)[1]


def _record_for_learning(path: Path, chosen: dict) -> dict | None:
    """
    Store this edit as a training example.

    Learning is a side effect of saving, never a precondition for it: if the
    store is unavailable the photo is still saved and the user is not blocked.
    """
    try:
        with _cache_lock:
            decoded = _decode_cache.get(str(path.resolve()))

        auto = compute_auto_adjustments(path, decoded=decoded)
        baseline_temp = float(auto.get("color_temp", 5500)) or 5500.0
        features = photo_features(path, decoded=decoded, iso=auto.get("iso"))

        effective = effective_adjustments(chosen)

        # Temperature is learned as a ratio so it transfers across scenes
        # lit differently, rather than as an absolute Kelvin value.
        auto_cmp = dict(auto)
        auto_cmp["temp_ratio"] = 1.0
        chosen_cmp = dict(effective)
        chosen_cmp["temp_ratio"] = float(effective.get("color_temp", baseline_temp)) / baseline_temp

        style.record_edit(path, features, auto_cmp, chosen_cmp)
        refresh_style_model()

        model = get_style_model()
        return {"samples": model.n_samples, "confidence": round(model.confidence, 2)}
    except Exception:
        return None


@app.route("/api/ratings")
def api_ratings():
    """Every rating in the library, in one pass."""
    paths = list_photos(LIBRARY_ROOT)
    raw = read_ratings(paths)
    root = LIBRARY_ROOT.resolve()
    return jsonify({
        "ratings": {str(p.relative_to(root)): raw.get(str(p), 0) for p in paths}
    })


@app.route("/api/rate", methods=["POST"])
def api_rate():
    try:
        payload = _json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    try:
        path = _safe_path(payload["path"])
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        rating = int(payload.get("rating", 0))
        write_rating(path, rating)
        library.update_cached(path, rating=rating)
        label = "rejected" if rating == REJECTED else (
            "unrated" if rating == 0 else f"{rating} star" + ("s" if rating > 1 else "")
        )
        return jsonify({"ok": True, "rating": rating, "message": f"{path.name}: {label}"})
    except Exception as exc:
        return jsonify({"error": describe_error(exc)}), 500


# ---------------------------------------------------------------------------
# Library module: flags, colour labels, keywords, filtered + sorted pages
# ---------------------------------------------------------------------------

LIBRARY_PAGE_SIZE = 200
LIBRARY_MAX_PAGE = 1000


def _paths_from(payload: dict) -> list[Path]:
    raw = payload.get("paths")
    if raw is None:
        raw = [payload["path"]]
    if not isinstance(raw, list) or not raw:
        raise ValueError("No photos given")
    return [_safe_path(r) for r in raw]


def _rel(path: Path) -> str:
    return path.relative_to(LIBRARY_ROOT.resolve()).as_posix()


@app.route("/api/library")
def api_library():
    """
    One page of the library, filtered and sorted.

    Never decodes a raw: metadata is one cached exiftool pass and thumbnails
    are fetched separately, lazily, by the browser.
    """
    args = request.args
    try:
        offset = max(0, int(args.get("offset", 0)))
        limit = max(1, min(LIBRARY_MAX_PAGE, int(args.get("limit", LIBRARY_PAGE_SIZE))))
        keys = ("rating_min", "rating_op", "flag", "label", "keyword",
                "camera", "date_from", "date_to", "q")
        criteria = {k: args.get(k, "") for k in keys}
        everything = library.build_items(LIBRARY_ROOT, list_photos(LIBRARY_ROOT))
        matched = library.filter_items(everything, criteria)
        matched = library.sort_items(
            matched, args.get("sort", "name"), args.get("dir", "asc") == "desc"
        )
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": describe_error(exc)}), 500

    body = {
        "total": len(matched),
        "library_total": len(everything),
        "offset": offset,
        "items": matched[offset:offset + limit],
    }
    if offset == 0:
        body["facets"] = library.facets(everything)
    return jsonify(body)


def _json_or_error():
    try:
        return _json_body(), None
    except ValueError as exc:
        return None, (jsonify({"error": str(exc)}), 415)


@app.route("/api/flag", methods=["POST"])
def api_flag():
    """Set pick / reject / none on one or many photos (reject = rating -1)."""
    payload, err = _json_or_error()
    if err:
        return err
    try:
        paths = _paths_from(payload)
        flag = payload.get("flag")
        if flag not in library.FLAGS:
            raise ValueError(f"flag must be one of {', '.join(library.FLAGS)}")
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        current = library.read_metadata(paths)
        for p in paths:
            was_rejected = current.get(str(p), {}).get("rating") == REJECTED
            if flag == "reject" and not was_rejected:
                write_rating(p, REJECTED)
                library.update_cached(p, rating=REJECTED)
            elif flag != "reject" and was_rejected:
                write_rating(p, 0)
                library.update_cached(p, rating=0)
        library.set_picks(LIBRARY_ROOT, [_rel(p) for p in paths], flag == "pick")
        return jsonify({"ok": True, "flag": flag, "count": len(paths),
                        "message": f"{len(paths)} photo(s): {flag}"})
    except Exception as exc:
        return jsonify({"error": describe_error(exc)}), 500


@app.route("/api/label", methods=["POST"])
def api_label():
    """Set a colour label (or clear it with "none") on one or many photos."""
    payload, err = _json_or_error()
    if err:
        return err
    try:
        paths = _paths_from(payload)
        label = library.normalize_label(payload.get("label"))
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400
    failed = []
    for p in paths:
        try:
            library.write_xmp(p, label=label)
            library.update_cached(p, label=label)
        except Exception as exc:
            failed.append(f"{p.name}: {describe_error(exc)}")
    if failed:
        return jsonify({"error": "; ".join(failed[:3])}), 500
    return jsonify({"ok": True, "label": label, "count": len(paths),
                    "message": f"{len(paths)} photo(s): " + (label.lower() + " label" if label else "label cleared")})


@app.route("/api/keywords", methods=["POST"])
def api_keywords():
    """Add and/or remove keywords, or replace them with ``set``, on one or many photos."""
    payload, err = _json_or_error()
    if err:
        return err
    try:
        paths = _paths_from(payload)
        replace = payload.get("set")
        add = library.normalize_keywords(payload.get("add", []))
        remove = {k.lower() for k in library.normalize_keywords(payload.get("remove", []))}
        if replace is None and not add and not remove:
            raise ValueError("Nothing to change")
        existing = library.read_metadata(paths)
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400
    failed = []
    for p in paths:
        try:
            if replace is not None:
                new = library.normalize_keywords(replace)
            else:
                have = existing.get(str(p), {}).get("keywords", [])
                new = library.normalize_keywords(
                    [k for k in have if k.lower() not in remove] + add
                )
            library.write_xmp(p, keywords=new)
            library.update_cached(p, keywords=new)
        except Exception as exc:
            failed.append(f"{p.name}: {describe_error(exc)}")
    if failed:
        return jsonify({"error": "; ".join(failed[:3])}), 500
    return jsonify({"ok": True, "count": len(paths), "message": f"Keywords updated on {len(paths)} photo(s)"})


@app.route("/api/thumb")
def api_thumb():
    """Serve a cached thumbnail for one file."""
    try:
        path = _safe_path(request.args.get("path", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    def fallback(p):
        rgb, _, _ = decode_for_analysis(p)
        return rgb

    try:
        data = thumbs.get(path, LIBRARY_ROOT, decode_fallback=fallback)
    except Exception as exc:
        return jsonify({"error": describe_error(exc)}), 500

    # Cached hard: the key already encodes the file's size and mtime, so a
    # changed photo produces a different URL rather than a stale image.
    return Response(data, mimetype="image/jpeg", headers={
        "Cache-Control": "public, max-age=31536000, immutable",
    })


@app.route("/api/presets", methods=["GET"])
def api_presets():
    return jsonify({"presets": style.list_presets()})


@app.route("/api/preset_save", methods=["POST"])
def api_preset_save():
    try:
        payload = _json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"error": "A preset needs a name"}), 400

    try:
        adjustments = dict(payload.get("adjustments", {}))
        # Record which white balance the preset was built against, so it can
        # be re-anchored when applied to a photo shot under different light.
        if payload.get("baseline_temp"):
            adjustments["_baseline_temp"] = payload["baseline_temp"]
        style.save_preset(name, adjustments)
        return jsonify({"ok": True, "presets": style.list_presets(),
                        "message": f"Saved preset '{name}'"})
    except Exception as exc:
        return jsonify({"error": describe_error(exc)}), 500


@app.route("/api/preset_get", methods=["POST"])
def api_preset_get():
    try:
        payload = _json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    try:
        preset = style.get_preset(
            payload.get("name", ""),
            baseline_temp=float(payload.get("baseline_temp") or 5500),
        )
    except Exception as exc:
        return jsonify({"error": describe_error(exc)}), 500

    if preset is None:
        return jsonify({"error": "No such preset"}), 404
    return jsonify({"adjustments": preset})


@app.route("/api/preset_delete", methods=["POST"])
def api_preset_delete():
    try:
        payload = _json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    name = payload.get("name", "")
    removed = style.delete_preset(name)
    return jsonify({"ok": removed, "presets": style.list_presets(),
                    "message": f"Deleted '{name}'" if removed else "No such preset"})


@app.route("/api/batch_apply", methods=["POST"])
def api_batch_apply():
    """
    Write one set of adjustments to many files.

    Each file is attempted independently so a single unwritable photo does
    not abandon the rest of the shoot; the response reports what failed.
    """
    try:
        payload = _json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    adjustments = payload.get("adjustments", {})
    raw_paths = payload.get("paths", [])

    if not raw_paths:
        return jsonify({"error": "No files selected"}), 400

    applied: list[str] = []
    failed: list[dict] = []
    skipped: list[dict] = []
    allow_foreign = bool(payload.get("overwrite_foreign", False))

    # How much warmer or cooler than its own camera the source photo was set.
    source_baseline = payload.get("baseline_temp")
    temp_ratio = None
    if source_baseline and adjustments.get("color_temp"):
        try:
            temp_ratio = float(adjustments["color_temp"]) / float(source_baseline)
        except (TypeError, ValueError, ZeroDivisionError):
            temp_ratio = None

    # Writing the same file twice is wasted work and inflates the count.
    seen: set[str] = set()
    raw_paths = [p for p in raw_paths if not (p in seen or seen.add(p))]

    for raw in raw_paths:
        try:
            path = _safe_path(raw)
        except ValueError as exc:
            failed.append({"path": raw, "error": str(exc)})
            continue

        try:
            if not allow_foreign:
                who = detect_foreign_edits(path)
                if who:
                    # Overwriting a whole shoot edited elsewhere is the most
                    # destructive thing this endpoint can do.
                    skipped.append({"path": raw, "editor": who})
                    continue

            per_file = dict(adjustments)

            # Temperature is camera-relative, so copying an absolute Kelvin
            # value onto a frame shot under different light shifts its colour.
            # Carry the source's warmth as a ratio and re-apply it here.
            #
            # This previously set _baseline_temp and relied on the strength
            # blend to do the work, which is a no-op at the default strength
            # of 100 — and it decoded every target file to compute a number
            # it then discarded.
            if temp_ratio is not None:
                target_baseline = _baseline_temp_of(path)
                per_file["color_temp"] = int(round(target_baseline * temp_ratio))
                per_file.pop("_baseline_temp", None)

            save_adjustments(path, per_file)
            applied.append(str(path.relative_to(LIBRARY_ROOT.resolve())))
        except Exception as exc:
            failed.append({"path": raw, "error": str(exc)})

    return jsonify({
        "ok": not failed and not skipped,
        "applied": applied,
        "failed": failed,
        "skipped": skipped,
        "message": (
            f"Applied to {len(applied)} file(s)"
            + (f", {len(failed)} failed" if failed else "")
            + (f", {len(skipped)} skipped (edited in other software)" if skipped else "")
        ),
    })


@app.route("/api/reset", methods=["POST"])
def api_reset():
    """Clear every stored adjustment, returning the file to a neutral render."""
    try:
        payload = _json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    try:
        path = _safe_path(payload["path"])
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        # Reset clears the whole XMP-crs group. On a Lightroom-managed file
        # that destroys develop settings this tool never wrote — the same
        # damage cmd_revert and batch apply already refuse to do.
        if not payload.get("overwrite_foreign", False):
            who = detect_foreign_edits(path)
            if who:
                return jsonify({
                    "error": f"{path.name} carries edits made in {who}. "
                             "Resetting would discard them.",
                    "foreign_editor": who,
                }), 409

        strip_xmp_adjustments(path)
        _, baseline_temp = _load_base_render(path)
        neutral = {c["key"]: c["default"] for c in CONTROLS + GLOBAL_CONTROLS}
        neutral.update(colormix.COLOR_DEFAULTS)
        neutral["color_temp"] = baseline_temp
        neutral["monochrome"] = False
        neutral["crop"] = None
        rendered = render_with_histogram(path, neutral)
        return jsonify({
            "ok": True,
            "adjustments": neutral,
            "preview": rendered["preview"],
            "histogram": rendered["histogram"],
            "message": f"Cleared all adjustments on {path.name}",
        })
    except Exception as exc:
        return jsonify({"error": describe_error(exc)}), 500


# ---------------------------------------------------------------------------
# History and snapshots (history.py). These never touch the photo itself: the
# state lives in a sidecar JSON, and the client saves to XMP separately.
# ---------------------------------------------------------------------------

def _history_request():
    """Parse the JSON body and resolve its path. Returns (payload, path, error_response)."""
    try:
        payload = _json_body()
    except ValueError as exc:
        return None, None, (jsonify({"error": str(exc)}), 415)
    try:
        return payload, _safe_path(payload["path"]), None
    except (ValueError, KeyError) as exc:
        return None, None, (jsonify({"error": str(exc)}), 400)


def _state_response(path: Path, adjustments: dict, hist: dict, **extra):
    """A full adjustment set rendered, as the client needs after a jump/apply."""
    _, baseline_temp = _load_base_render(path)
    merged = complete_adjustments(adjustments, baseline_temp)
    rendered = render_with_histogram(path, merged)
    return jsonify({
        "ok": True, "adjustments": merged, "preview": rendered["preview"],
        "histogram": rendered["histogram"], "history": history.summary(hist), **extra,
    })


@app.route("/api/history", methods=["POST"])
def api_history():
    _, path, err = _history_request()
    if err:
        return err
    return jsonify({"history": history.summary(history.load(path))})


@app.route("/api/history/record", methods=["POST"])
def api_history_record():
    payload, path, err = _history_request()
    if err:
        return err
    adj = payload.get("adjustments")
    if not isinstance(adj, dict):
        return jsonify({"error": "adjustments must be an object"}), 400
    try:
        hist, recorded = history.record(path, str(payload.get("label", "Edit")), adj)
    except OSError as exc:
        return jsonify({"error": describe_error(exc)}), 500
    return jsonify({"ok": True, "recorded": recorded, "history": history.summary(hist)})


@app.route("/api/history/jump", methods=["POST"])
def api_history_jump():
    payload, path, err = _history_request()
    if err:
        return err
    try:
        hist, adj = history.jump(path, str(payload.get("id", "")))
        return _state_response(path, adj, hist)
    except KeyError as exc:
        return jsonify({"error": exc.args[0]}), 404
    except Exception as exc:
        return jsonify({"error": describe_error(exc)}), 500


@app.route("/api/history/reset_to_import", methods=["POST"])
def api_history_reset_to_import():
    """Return to the as-imported settings, recorded as a step so it can be undone."""
    _, path, err = _history_request()
    if err:
        return err
    try:
        hist = history.load(path)
        if hist["original"] is None:
            return jsonify({"error": "No import state recorded; open the photo first"}), 404
        hist, _ = history.record(path, "Reset to import", hist["original"])
        return _state_response(path, hist["original"], hist)
    except Exception as exc:
        return jsonify({"error": describe_error(exc)}), 500


@app.route("/api/snapshots/create", methods=["POST"])
def api_snapshot_create():
    payload, path, err = _history_request()
    if err:
        return err
    adj = payload.get("adjustments")
    if not isinstance(adj, dict):
        return jsonify({"error": "adjustments must be an object"}), 400
    try:
        hist = history.create_snapshot(path, str(payload.get("name", "")), adj)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "history": history.summary(hist)})


@app.route("/api/snapshots/apply", methods=["POST"])
def api_snapshot_apply():
    payload, path, err = _history_request()
    if err:
        return err
    try:
        adj = history.get_snapshot(path, str(payload.get("id", "")))
        hist, _ = history.record(path, f"Snapshot: {payload.get('name') or 'applied'}", adj)
        return _state_response(path, adj, hist)
    except KeyError as exc:
        return jsonify({"error": exc.args[0]}), 404
    except Exception as exc:
        return jsonify({"error": describe_error(exc)}), 500


@app.route("/api/snapshots/delete", methods=["POST"])
def api_snapshot_delete():
    payload, path, err = _history_request()
    if err:
        return err
    hist, removed = history.delete_snapshot(path, str(payload.get("id", "")))
    return jsonify({"ok": removed, "history": history.summary(hist)})


@app.route("/api/recover", methods=["POST"])
def api_recover():
    """Write out the untouched original raw embedded at conversion time."""
    try:
        payload = _json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    try:
        path = _safe_path(payload["path"])
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        recovered = extract_original_raw(path, path.parent)
        return jsonify({"ok": True, "message": f"Recovered original → {recovered.name}"})
    except Exception as exc:
        return jsonify({"error": describe_error(exc)}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    global LIBRARY_ROOT

    parser = argparse.ArgumentParser(
        description="Local web editor for reviewing and adjusting converted photos."
    )
    parser.add_argument("folder", nargs="?", default=".", help="Folder of DNG/CR2 files to edit")
    parser.add_argument("-p", "--port", type=int, default=5000, help="Port (default: 5000)")
    parser.add_argument(
        "--write-sidecars", action="store_true",
        help="Also write a Lightroom-style .xmp beside each photo on save "
             "(off by default; a sidecar is what Lightroom reads back)",
    )
    args = parser.parse_args()

    global WRITE_SIDECARS
    WRITE_SIDECARS = args.write_sidecars

    root = Path(args.folder).expanduser().resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        sys.exit(1)
    LIBRARY_ROOT = root

    print(f"Editing {root}")
    if WRITE_SIDECARS:
        print("Saves will also write .xmp sidecars.")
    print(f"Open http://127.0.0.1:{args.port}")
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
