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
import copy
import os
import re
import io
import json
from collections import OrderedDict
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
import rawpy
from flask import Flask, Response, jsonify, render_template, request, url_for
from PIL import Image, ImageFilter

try:                       # optional: only used for faster resizing
    import cv2
except ImportError:        # pragma: no cover
    cv2 = None

import catalog
import colour
import colormix
import editversion
import effects
import folders
import fsutil
import imgload
import security
import geometry
import masks
import libindex
import libapi
import kwapi
import metadata_api
import library
import lookxmp
import lut as lutmod
import profiles
import history
import lens
import quality
import render_kernels
import retouch
import style
import stagecache
import thumbs
import tonecurve
from geometry import largest_inscribed_rect as _largest_inscribed_rect
from tonecurve import apply_tone_curve
from process import (
    EXIFTOOL_TIMEOUT,
    RAW_SUFFIXES,
    compute_auto_adjustments,
    detect_foreign_edits,
    edit_target,
    is_dng,
    read_target,
    has_sidecar,
    list_photos,
    decode_for_analysis,
    decode_preview_linear,
    linear16_to_u8,
    camera_baseline_temp,
    photo_features,
    verify_xmp_written,
    extract_original_raw,
    get_embedded_original_name,
    strip_xmp_adjustments,
)


# Run as `python editor.py`, this file is module "__main__"; export_api and
# exporter do `import editor`, which would load a SECOND copy with its own
# LIBRARY_ROOT (the launch directory), caches and flags. Alias it first so
# every launch style shares one module, one app and one configuration.
if __name__ == "__main__":
    sys.modules.setdefault("editor", sys.modules["__main__"])

# Static assets (static/css, static/js) are resolved relative to this file, not
# the working directory, so the editor can be launched from anywhere.
app = Flask(
    __name__,
    static_folder=str(Path(__file__).resolve().parent / "static"),
    template_folder=str(Path(__file__).resolve().parent / "templates"),
)


@app.context_processor
def _asset_helper():
    """``asset('js/x.js')`` -> /static/js/x.js?v=<mtime>, so edits bust the cache."""
    def asset(path: str) -> str:
        try:
            version = int((Path(app.static_folder) / path).stat().st_mtime)
        except OSError:
            version = 0
        return url_for("static", filename=path, v=version)
    return {"asset": asset}


@app.after_request
def _cache_static(response):
    # Versioned URLs never change content, so they can be cached for good;
    # an unversioned request must revalidate (Flask sends an ETag).
    if request.endpoint == "static":
        if request.args.get("v"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
    return response


import export_api  # noqa: E402  (the Export dialog's endpoints)

app.register_blueprint(export_api.bp)

import develop_api  # noqa: E402  (auto tone / white balance / clipping / tiles)

app.register_blueprint(develop_api.bp)
app.register_blueprint(libapi.bp)
app.register_blueprint(kwapi.bp)
app.register_blueprint(metadata_api.bp)

import stacks_api  # noqa: E402  (library stacks)
app.register_blueprint(stacks_api.bp)
import cull_api  # noqa: E402  (smart culling and Auto edit)

app.register_blueprint(cull_api.bp)

import looks_api  # noqa: E402  (preset panel, LUT library, match reference look)

app.register_blueprint(looks_api.bp)

import profiles_api  # noqa: E402  (Profile browser: built-in profiles, .dcp / .icc import)

app.register_blueprint(profiles_api.bp)

import import_api  # noqa: E402  (drag-and-drop / button import, smart previews)

app.register_blueprint(import_api.bp)

import backup_api  # noqa: E402  (library backup / restore, scheduled reminder)

app.register_blueprint(backup_api.bp)
import quick_api  # noqa: E402  (Auto Sync and Quick Develop)

app.register_blueprint(quick_api.bp)
import map_api  # noqa: E402  (Map view: geotagged photos, clustered, offline)
app.register_blueprint(map_api.bp)
import folders_api  # noqa: E402  (Folders panel: tree, create, safe move / rename, undo)

app.register_blueprint(folders_api.bp)
import semantic_api  # noqa: E402  (optional on-device semantic search)

app.register_blueprint(semantic_api.bp)
import faces_api  # noqa: E402  (optional, opt-in Faces / People)

app.register_blueprint(faces_api.bp)
import merge_api  # noqa: E402  (Merge to HDR / Panorama jobs)

app.register_blueprint(merge_api.bp)
import timelapse_api  # noqa: E402  (Timelapse dialog)

app.register_blueprint(timelapse_api.bp)
import proof_api  # noqa: E402  (targeted adjustment, soft proofing, reference view, Develop sidecar)

app.register_blueprint(proof_api.bp)


app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
security.install(app)


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
        _stage_cache.drop_photo(oldest)
        _stage_cache.drop_photo(oldest + ":fast")
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

# Colour maths generation. 1 = the original gamma-space maths; 2 = linear light
# (docs/COLOUR-PIPELINE.md). Saved with every edit; an edit without it is version 1.
EDIT_VERSION = editversion.CURRENT

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
    # Noise reduction (render_kernels.denoise_stage) runs in the preview, the 100% tiles and the
    # export. Its radii scale with the frame size, so a small preview shows the same amount of
    # smoothing the full-size export does.
    {"key": "luminance_nr", "label": "Noise reduction", "min": 0, "max": 100, "step": 1, "default": 0},
    {"key": "color_nr", "label": "Colour noise reduction", "min": 0, "max": 100, "step": 1, "default": 0},
]

# Applied on top of the per-slider values rather than alongside them: strength
# scales the whole edit toward the untouched photo, and monochrome drops colour
# after every other adjustment has been composited.
GLOBAL_CONTROLS = [
    {"key": "strength", "label": "Edit strength", "min": 0, "max": 150, "step": 1, "default": 100},
]

HISTOGRAM_BINS = 256     # one bin per 8-bit level, like the /api/develop/histogram route

# HSL / B&W mixer and colour grading sliders (see colormix.py). Kept apart from
# CONTROLS so the main slider list stays short; the UI builds its own panel.
COLOR_CONTROLS = colormix.COLOR_CONTROLS
# Grain, post-crop vignette, Calibration and sharpening radius / detail / masking (effects.py);
# the UI builds its own panels from these, and Point Color is the list ``point_colors``.
EFFECT_CONTROLS = effects.CONTROLS

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

    # One demosaic to linear light: the float preview is encoded straight from
    # the 16-bit data (no 8-bit step), the 8-bit copy is for analysis only.
    lin16, baseline_temp, cam_mul = decode_preview_linear(path)
    rgb = linear16_to_u8(lin16)
    base = _fit_preview(colour.linear16_to_encoded(lin16, "srgb"))
    del lin16

    with _cache_lock:
        _decode_cache[key] = (rgb, baseline_temp, cam_mul)
        _base_cache[key] = (base, baseline_temp)
        _evict_if_needed()
    return base, baseline_temp


def _fit_preview(enc: np.ndarray) -> np.ndarray:
    """Shrink an encoded float image to PREVIEW_MAX_EDGE (never enlarging)."""
    h, w = enc.shape[:2]
    scale = PREVIEW_MAX_EDGE / max(h, w)
    if scale >= 1.0:
        return enc
    size = (max(1, round(w * scale)), max(1, round(h * scale)))
    if cv2 is not None:
        return cv2.resize(enc, size, interpolation=cv2.INTER_AREA)
    planes = [np.asarray(Image.fromarray(np.ascontiguousarray(enc[:, :, c]), "F").resize(size, Image.LANCZOS))
              for c in range(3)]
    return np.stack(planes, axis=-1)


def _luminance(img: np.ndarray) -> np.ndarray:
    return 0.2126 * img[:, :, 0] + 0.7152 * img[:, :, 1] + 0.0722 * img[:, :, 2]


def _blur(plane: np.ndarray, radius: float) -> np.ndarray:
    """
    Gaussian blur of a single 0..1 plane, via Pillow (quantised to 8 bits, wide
    radii computed on a shrunken copy). See ``render_kernels.blur``.
    """
    return render_kernels.blur(plane, radius)


def _rotate_float(img: np.ndarray, angle: float) -> np.ndarray:
    """
    Rotate ``angle`` degrees counter-clockwise about the centre, same size, bicubic,
    in float32 (no 8-bit step: a 16-bit export keeps its precision through Straighten).
    Pixels from outside the frame are black, as with Pillow's ``rotate``.
    """
    h, w = img.shape[:2]
    if cv2 is not None and img.dtype == np.float32 and img.ndim == 3:
        m = cv2.getRotationMatrix2D(((w - 1) / 2.0, (h - 1) / 2.0), angle, 1.0)
        out = cv2.warpAffine(np.ascontiguousarray(img), m, (w, h), flags=cv2.INTER_CUBIC,
                             borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        return np.clip(out, 0.0, 1.0, out=out)
    planes = [np.asarray(Image.fromarray(np.ascontiguousarray(img[..., c], dtype=np.float32), "F")
                         .rotate(angle, resample=Image.BICUBIC, expand=False)) for c in range(img.shape[2])]
    return np.clip(np.stack(planes, axis=-1), 0.0, 1.0)


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
        img = _rotate_float(img, angle)

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
    run only when their slider is off its default -- a blur per photo is the
    most expensive thing in the preview path. The maths lives in
    ``render_kernels.detail_stage`` (row strips, all cores).
    """
    # Denoise first so sharpening and clarity do not amplify the noise.
    return render_kernels.detail_stage(render_kernels.denoise_stage(img, adj), adj)


class StaleRender(Exception):
    """Raised between stages when a newer request has superseded this one."""


# Stage outputs for the preview (see stagecache.py). ~16MB per float32 stage
# image at 1400px, so 160MB holds the stages of a couple of photos.
STAGE_CACHE_BYTES = 160 * 1024 * 1024
_stage_cache = stagecache.StageCache(STAGE_CACHE_BYTES)


def _stage_geometry(base: np.ndarray, adj: dict) -> np.ndarray:
    # Geometry first, so the untouched reference the strength blend compares
    # against is the cropped picture rather than the whole frame. `base` must
    # stay pristine -- later stages work on copies.
    # Spots and lens corrections work on the original frame; the transform
    # then feeds Straighten and Crop (see retouch.py).
    base = retouch.apply_corrections(base, adj)
    if not adj.get("_raw_frame"):
        base = retouch.apply_transform(base, adj)
        base = apply_geometry(base, adj)
    if _is_legacy(adj):
        # Edit version 1: tuned on BT.709-coded data, so start from exactly that.
        base = colour.to_legacy(base, colour.normalise_space(adj.get("_space")))
    return base


def _stage_profile(img: np.ndarray, adj: dict, linear: bool) -> np.ndarray:
    """Camera profile (profiles.py): the base look, right after the camera-matrix conversion and
    before every edit. Part of the geometry stage's cache entry, so a slider drag never redoes it.
    A profile this machine lacks is skipped (``_without_missing_profile`` tells the user)."""
    pid = adj.get("camera_profile")
    if not pid or _is_legacy(adj):
        return img
    try:
        prof = profiles.get(pid)
    except (profiles.ProfileNotFound, profiles.ProfileError, OSError):
        return img
    return profiles.apply(img, prof, adj.get("_space"), linear=linear)


def _without_missing_profile(adj: dict) -> tuple[dict, list[str]]:
    pid = adj.get("camera_profile")
    if pid and not profiles.exists(pid):
        trimmed = dict(adj)
        trimmed["camera_profile"] = ""
        return trimmed, [f"The camera profile used by this photo ({pid}) is not in the profile library on "
                         "this computer, so it is not shown. Import the .dcp file again."]
    return adj, []


def _is_legacy(adj: dict) -> bool:
    """True for an edit made under the original gamma-space maths (edit_version 1)."""
    return int(adj.get("edit_version", EDIT_VERSION) or EDIT_VERSION) < EDIT_VERSION


def _stage_tone(base: np.ndarray, baseline_temp: int, adj: dict, linear=None) -> np.ndarray:
    # Calibration (effects.py) turns the camera primaries first, on linear light; then white balance,
    # exposure, tonal range, contrast and curve (render_kernels).
    if effects.calibration_active(adj) and not _is_legacy(adj):
        space = adj.get("_space")
        lin = linear if linear is not None else colour.to_linear(base, space)
        base = colour.to_encoded(effects.calibrate(lin, adj), space)
        linear = None
    return render_kernels.tone_stage(base, baseline_temp, adj, linear=linear)


def lut_amount(adj: dict) -> float:
    """The film-style intensity as 0..1 (0 when no LUT is set)."""
    if not adj.get("lut"):
        return 0.0
    try:
        return min(1.0, max(0.0, float(adj.get("lut_amount", 100)) / 100.0))
    except (TypeError, ValueError):
        return 1.0


def _stage_lut(img: np.ndarray, adj: dict, cache, cache_id, key) -> np.ndarray:
    """Apply the photo's film style at its intensity. Raises ``lut.LutNotFound`` if this machine lacks the LUT."""
    amount = lut_amount(adj)
    if amount <= 0:
        return img
    lut_id = adj["lut"]
    full = cache.get(cache_id, "lut", f"{key}|{lut_id}") if cache and key else None
    if full is None:
        full = lutmod.apply(img, lutmod.get(lut_id), adj.get("_space"), 1.0)
        if cache and key:
            cache.put(cache_id, "lut", f"{key}|{lut_id}", full)
    if amount >= 1.0:
        return full
    return img + (full - img) * np.float32(amount)


def _without_missing_lut(adj: dict) -> tuple[dict, list[str]]:
    """``adj`` minus a LUT this machine does not have, and a warning saying so (previews keep working)."""
    if lut_amount(adj) > 0 and not lutmod.exists(adj["lut"]):
        trimmed = dict(adj)
        trimmed["lut"] = ""
        return trimmed, [f"The LUT used by this photo ({adj['lut']}) is not in the LUT library on this "
                         "computer, so it is not shown. Import the .cube file again."]
    return adj, []


def _stage_saturation(img: np.ndarray, adj: dict) -> np.ndarray:
    return render_kernels.saturation_stage(img, adj)


def apply_adjustments(
    base: np.ndarray,
    baseline_temp: int,
    adj: dict,
    cache_id: str | None = None,
    should_abort=None,
) -> np.ndarray:
    """
    Composite slider values onto the neutral render.

    This approximates how Camera Raw will interpret the same XMP values; it is
    a preview for judging an edit, not a byte-exact match to Adobe's renderer.

    With a ``cache_id`` (one photo's identity) each stage's output is kept in
    the stage cache and a re-render resumes after the last stage whose
    settings are unchanged. ``should_abort`` is polled between stages and
    raises :class:`StaleRender` for a request that has been superseded.
    """
    # ``_linear_input``: ``base`` is already linear light (the exporter's 16-bit decode), which
    # saves a decode and an encode of the full frame. Stages that need display-coded pixels
    # (spots, lens, the old maths) get an encoded copy instead.
    linear_in = bool(adj.get("_linear_input"))
    if linear_in and (_is_legacy(adj) or not fast_ok(adj)):
        base = colour.to_encoded(base, adj.get("_space"))
        linear_in = False
    cache = _stage_cache if cache_id else None
    keys = stagecache.stage_keys(adj, baseline_temp) if cache else None
    S = stagecache

    def checkpoint():
        if should_abort is not None and should_abort():
            raise StaleRender()

    def lookup(stage):
        return cache.get(cache_id, stage, keys[stage]) if cache else None

    def keep(stage, value, source):
        # Pass-throughs and crops of the input (views of it) cost nothing to
        # recompute; anything else is stored, as an owning array so the cache's
        # byte count is honest.
        if cache and value is not source and not np.may_share_memory(value, source):
            if value.base is not None:
                value = value.copy()
            cache.put(cache_id, stage, keys[stage], value)
        return value

    # Resume from the latest cached stage; each stage below runs only if its
    # own output was not found.
    cached = {}
    for stage in (S.DETAIL, S.GRADE, S.MIX, S.SATURATION, S.TONE):
        hit = lookup(stage)
        if hit is not None:
            cached[stage] = hit
            break
    resume = next(iter(cached), -1)

    # Geometry output is also the reference for the strength blend.
    geo = lookup(S.GEOMETRY)
    if geo is None:
        checkpoint()
        geo = keep(S.GEOMETRY, _stage_profile(_stage_geometry(base, adj), adj, linear_in), base)

    if resume >= S.TONE:
        img = cached[resume]
    elif linear_in:
        checkpoint()
        lin_geo = effects.calibrate(geo, adj) if not _is_legacy(adj) else geo
        img = render_kernels.tone_stage_from_linear(lin_geo, baseline_temp, adj)
    else:
        checkpoint()
        linear = None
        if cache and not _is_legacy(adj) and not render_kernels.tone_is_linear_neutral(baseline_temp, adj):
            # The geometry output decoded to linear light only depends on the
            # geometry, so a slider drag decodes it once and re-encodes per move.
            linear = cache.get(cache_id, "linear", keys[S.GEOMETRY])
            if linear is None:
                linear = colour.to_linear(geo, adj.get("_space"))
                cache.put(cache_id, "linear", keys[S.GEOMETRY], linear)
        img = keep(S.TONE, _stage_tone(geo, baseline_temp, adj, linear), geo)
    if resume < S.SATURATION:
        checkpoint()
        img = keep(S.SATURATION, _stage_saturation(img, adj), img)

    # --- Colour mixer / grading ---
    # In black & white the HSL sliders give way to the B&W mixer, which
    # brightens or darkens each hue band ahead of the grey conversion.
    mono = bool(adj.get("monochrome"))
    if resume < S.MIX:
        checkpoint()
        if mono:
            mixed = colormix.apply_gray_mixer(img, adj)
        else:
            # The HSV analysis depends only on the input pixels, so it is kept
            # while an HSL slider is dragged.
            analysis = cache.get(cache_id, "hsv", keys[S.SATURATION]) if cache else None
            if analysis is None and colormix.has_hsl(adj):
                analysis = colormix.Analysis(img)
                if cache:
                    cache.put(cache_id, "hsv", keys[S.SATURATION], analysis)
            mixed = effects.apply_point_colors(colormix.apply_hsl(img, adj, analysis), adj)
        img = keep(S.MIX, mixed, img)
    if resume < S.GRADE:
        checkpoint()
        if not mono:
            img = keep(S.GRADE, colormix.apply_grading(img, adj), img)

    # --- Film style (LUT) then detail ---
    # A LUT is a display-referred look, so it runs on the finished colour edit and
    # before clarity/texture/sharpening (lut.py). With black & white on, colour is
    # drained after grading, so the LUT goes last instead (below) to keep its tints.
    if resume < S.DETAIL:
        checkpoint()
        if not mono:
            img = _stage_lut(img, adj, cache, cache_id, keys[S.GRADE] if keys else None)
        img = keep(S.DETAIL, _apply_detail(img, adj), img)

    checkpoint()
    # --- Local adjustments (masks.py): graduated, radial and brush masks ---
    img = masks.apply_masks(img, adj.get("masks"), blur=_blur, space=adj.get("_space"),
                            legacy=_is_legacy(adj))

    # --- Post-crop vignette and grain (effects.py): on the finished, cropped picture ---
    img = effects.apply_finishing(img, adj)

    # --- Edit strength ---
    # Blend the finished edit back toward the original. Above 100% it
    # extrapolates away from the original, exaggerating the same edit.
    strength = float(adj.get("strength", 100)) / 100.0
    if abs(strength - 1.0) > 1e-6:
        ref = colour.to_encoded(geo, adj.get("_space")) if linear_in else geo
        img = ref + (img - ref) * strength

    # --- Monochrome ---
    # Applied after the colour sliders so they act as channel mixers: warming
    # the temperature changes which tones render light or dark. The blend is
    # capped at fully grey -- extrapolating past it would swing the image back
    # into inverted colour rather than making it "more" black and white.
    if mono:
        # Applied in full regardless of strength, because the file stores it
        # as a boolean. Blending it produced a half-desaturated preview of a
        # photograph that was saved fully black and white.
        img = np.repeat(_luminance(img)[:, :, None], 3, axis=2)
        # Colour grading still applies to a monochrome image (split toning).
        img = colormix.apply_grading(img, adj)
        img = _stage_lut(img, adj, None, None, None)

    return np.clip(img, 0.0, 1.0)


# The histogram drawn 280px wide is indistinguishable whether it counts every
# pixel or one in four, and this runs on every slider move.
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


# While a slider is moving the preview renders from a smaller copy of the
# photo (about a third of the pixels) and a sharp render follows when it
# settles, as Lightroom does. Spots and lens corrections work on the original
# frame at its true scale, so they always get the full-size path.
FAST_EDGE = 800
_fast_cache: OrderedDict[str, tuple[np.ndarray, np.ndarray]] = OrderedDict()   # path -> (full, small)


def _fast_base(path: Path, base: np.ndarray) -> np.ndarray:
    key = str(path.resolve())
    with _cache_lock:
        hit = _fast_cache.get(key)
        if hit is not None and hit[0] is base:
            _fast_cache.move_to_end(key)
            return hit[1]
    h, w = base.shape[:2]
    size = (max(1, round(w * FAST_EDGE / max(h, w))), max(1, round(h * FAST_EDGE / max(h, w))))
    if cv2 is not None:
        small = cv2.resize(base, size, interpolation=cv2.INTER_AREA)
    else:
        img = Image.fromarray((base * 255.0 + 0.5).astype(np.uint8)).resize(size, Image.BILINEAR)
        small = np.asarray(img, dtype=np.float32) / 255.0
    with _cache_lock:
        _fast_cache[key] = (base, small)
        while len(_fast_cache) > MAX_CACHED_PHOTOS:
            _fast_cache.popitem(last=False)
    return small


def fast_ok(adj: dict) -> bool:
    return not (adj.get("spots") or adj.get("lens_profile") or adj.get("ca_remove")
                or adj.get("_raw_frame"))


def render_with_histogram(path: Path, adj: dict, quality: int = 82, should_abort=None,
                          fast: bool = False) -> dict:
    """Render the preview and its histogram from a single composite pass."""
    base, baseline_temp = _load_base_render(path)
    adj, warnings = _without_missing_lut(adj)
    adj, w2 = _without_missing_profile(adj)
    warnings = warnings + w2
    cache_id = str(path.resolve())
    if fast and fast_ok(adj) and max(base.shape[:2]) > FAST_EDGE * 1.25:
        base = _fast_base(path, base)
        cache_id += ":fast"
    out = apply_adjustments(base, baseline_temp, lens.attach_meta(path, adj),
                            cache_id=cache_id, should_abort=should_abort)
    img = Image.fromarray((out * 255.0 + 0.5).astype(np.uint8))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return {
        "preview": f"data:image/jpeg;base64,{encoded}",
        "histogram": compute_histogram(out),
        **({"warnings": warnings} if warnings else {}),
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
        if key in ("straighten", "luminance_nr", "color_nr"):
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
    for key in effects.STRENGTH_KEYS:
        if key in adj:
            out[key] = effects.DEFAULTS[key] + (float(adj[key]) - effects.DEFAULTS[key]) * strength
    if adj.get("point_colors"):
        out["point_colors"] = [{**p, **{k: p[k] * strength for k in ("hue_shift", "sat_shift", "lum_shift")}}
                               for p in effects.normalise_points(adj["point_colors"])]
    if adj.get("masks"):
        out["masks"] = masks.scale_for_strength(adj["masks"], strength)
    if adj.get("lut"):
        out["lut_amount"] = min(100.0, float(adj.get("lut_amount", 100)) * strength)
    return out


def crop_in_full_frame(
    crop: dict | None, angle: float, width: float, height: float
) -> dict:
    """Canonical crop -> Camera Raw's full-frame fractions (see ``geometry``)."""
    return geometry.ui_to_xmp_crop(crop, angle, width, height)


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


class MetadataReadError(RuntimeError):
    """exiftool could not read a file's metadata (as opposed to finding none).

    Reads that fail must never be mistaken for "no settings stored": the next
    save would then write defaults over the real edits.
    """


def _exiftool_read(args: list[str]) -> str:
    """stdout of a read-only exiftool call; any failure raises MetadataReadError."""
    try:
        result = subprocess.run(["exiftool"] + args, capture_output=True,
                                text=True, timeout=EXIFTOOL_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise MetadataReadError(f"Could not read metadata ({exc.__class__.__name__}): {exc}") from exc
    if result.returncode != 0:
        raise MetadataReadError(f"exiftool failed reading metadata: {result.stderr.strip() or result.returncode}")
    return result.stdout


def existing_process_version(path: Path) -> str | None:
    """The process version already declared in a file, if any."""
    target = read_target(path)
    if target != path and not target.exists():
        return None
    path = target
    raw = _exiftool_read(["-XMP-crs:ProcessVersion", "-s3", "-f", str(path)]).strip()
    return None if raw in ("-", "") else raw


def _xmp_arguments(adj: dict, process_version: str | None = None) -> list[str]:
    """The develop settings as exiftool arguments, without a target file.

    Shared by the DNG and the sidecar so the two cannot drift apart.
    """
    return [
        f"-XMP-xmp:CreatorTool={editversion.tool_label(int(adj.get('edit_version') or EDIT_VERSION))}",
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
        f"-XMP-crs:ColorNoiseReduction={int(adj.get('color_nr', 0))}",
        f"-XMP-crs:ConvertToGrayscale={'True' if adj.get('monochrome') else 'False'}",
    ] + (colormix.xmp_arguments(adj) + tonecurve.xmp_arguments(adj.get("tone_curve"))
         + retouch.xmp_arguments(adj, adj.get("_frame")) + effects.xmp_arguments(adj) + _mask_arguments(adj)
         + lookxmp.xmp_arguments(adj) + profiles.xmp_arguments(adj))


def _mask_arguments(adj: dict) -> list[str]:
    """Local-adjustment XMP, only when the caller says anything about masks.

    A save that carries no ``masks`` key (an older client, a batch of plain
    sliders) leaves whatever masks the file already has alone.
    """
    if "masks" not in adj:
        return []
    return masks.xmp_arguments(adj["masks"], adj.get("_frame_aspect"))


def _process_version_for(path: Path) -> str:
    """
    Which process version to declare when writing this file.

    A newer one already present is kept. Process version selects the
    rendering engine, so replacing 15.4 with 11.0 would silently change how
    an existing photograph looks — a downgrade from Process Version 6 to 5,
    not just a metadata edit.
    """
    # A failed read must stop the save: guessing 11.0 here would silently
    # downgrade a file that declares a newer process version.
    existing = existing_process_version(path)

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

    with fsutil.lock_for(target):
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
    # A tag name exiftool does not know is only a warning, and the value is
    # dropped. That is how the colour-grading hue/saturation of the shadows and
    # highlights went missing, so treat it as the failure it is.
    if "is not defined" in result.stderr:
        raise RuntimeError(f"exiftool dropped settings for {target.name}: {result.stderr.strip()}")
    return target


def save_adjustments(
    path: Path,
    adj: dict,
    frame_size: tuple[float, float] | None = None,
    sidecar: bool = False,
    source_size: tuple[float, float] | None = None,
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
    masks_full = adj.get("masks") if "masks" in adj else None
    aspect = _output_aspect(frame_size, adj)
    adj = effective_adjustments(adj)
    if aspect:
        adj["_frame_aspect"] = aspect
    if source_size:
        # Spot radii are fractions of the width; Camera Raw also wants the height.
        adj["_frame"] = tuple(source_size)

    angle = float(adj.get("straighten", 0) or 0)
    if angle and frame_size:
        adj = dict(adj)
        adj["crop"] = crop_in_full_frame(adj.get("crop"), angle, *frame_size)

    if edit_target(path) != path:
        # Not a DNG: the raw is never rewritten, the sidecar is the record.
        write_sidecar(path, adj)
        verify_xmp_written(edit_target(path),
                           {"ColorTemperature": str(int(adj.get("color_temp", 5500)))})
        _save_mask_sidecar(path, masks_full)
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
    _save_mask_sidecar(path, masks_full)


def _output_aspect(frame_size: tuple[float, float] | None, adj: dict) -> float | None:
    """Width / height of the cropped picture, for writing gradient masks to XMP."""
    if not frame_size or not frame_size[1]:
        return None
    aspect = frame_size[0] / frame_size[1]
    crop = adj.get("crop") or {}
    cw = float(crop.get("right", 1.0)) - float(crop.get("left", 0.0))
    ch = float(crop.get("bottom", 1.0)) - float(crop.get("top", 0.0))
    if cw > 0.01 and ch > 0.01:
        aspect *= cw / ch
    return aspect


def _save_mask_sidecar(path: Path, masks_full) -> None:
    """Full-fidelity masks beside the photo (None = the caller said nothing)."""
    if masks_full is not None:
        masks.save_sidecar(path, masks_full)


def read_saved_adjustments(
    path: Path,
    prefer_sidecar: bool = True,
    source_size: tuple[float, float] | None = None,
) -> dict | None:
    """
    Develop settings stored for this photo, with its local-adjustment masks.

    Returned in the canonical coordinates (see ``geometry``): the XMP crop is
    inverted back to a crop of the straightened preview. That needs the
    frame's proportions when the photo is straightened; ``source_size`` is the
    untransformed frame's size, or None to take it from the decoded preview.
    Raises ``MetadataReadError`` if the metadata cannot be read.
    """
    values, source = _read_saved_values(path, prefer_sidecar)
    found = _read_masks(path, source) if source is not None else []
    if found:
        values = dict(values or {})
        values["masks"] = found
    if values and values.get("crop") and float(values.get("straighten") or 0):
        if source_size is None:
            base, _ = _load_base_render(path)
            source_size = (base.shape[1], base.shape[0])
        frame = retouch.frame_after_transform(*source_size, values)
        values = dict(values)
        values["crop"] = geometry.xmp_to_ui_crop(
            values["crop"], float(values["straighten"]), *frame)
    return values


def _read_masks(photo: Path, source: Path) -> list[dict]:
    """Masks from the XMP in ``source``, or the richer JSON sidecar if it still agrees."""
    # A failed read raises: treating it as "no masks" would let the next save
    # erase them. Only a successful read that finds none returns [].
    from_xmp = masks.parse_exiftool_json(
        _exiftool_read(masks.xmp_read_args() + [str(source)]))
    return masks.reconcile(masks.load_sidecar(photo), from_xmp)


def _read_saved_values(path: Path, prefer_sidecar: bool = True) -> tuple[dict | None, Path | None]:
    """
    Return slider values already stored for this photo.

    When a Lightroom-style .xmp sidecar sits beside the file it is read in
    preference to the copy inside the DNG. Both were observed on a real
    shoot carrying different values for the same photograph — exposure
    +1.12 against +1.11, highlights -84 against -81 — and the sidecar is
    what Lightroom reads back when sidecar writing is enabled, so it is the
    one that matches what the photographer sees in their catalogue.
    """
    if not is_dng(path):
        sidecar = edit_target(path)
        return (_read_adjustments_from(sidecar), sidecar) if sidecar.exists() else (None, None)
    if prefer_sidecar:
        sidecar = path.with_suffix(".xmp")
        if sidecar.exists():
            values = _read_adjustments_from(sidecar)
            if values:
                return values, sidecar
    return _read_adjustments_from(path), path


def _read_adjustments_from(path: Path) -> dict | None:
    """Return slider values already stored in the file, or None if it has none."""
    out = _read_basic_adjustments_from(path)
    extra = retouch.read_from_file(path, EXIFTOOL_TIMEOUT)   # raises on a failed read
    # Grain, vignette, calibration, Point Color and sharpening detail have their own read (their own offsets).
    extra = {**extra, **effects.read_from_file(path, EXIFTOOL_TIMEOUT)}
    if extra:
        out = dict(out or {})
        out.update(extra)
    return out or None


def _read_basic_adjustments_from(path: Path) -> dict | None:
    tags = [
        "ColorTemperature", "Tint", "Exposure2012", "Contrast2012",
        "Highlights2012", "Shadows2012", "Whites2012", "Blacks2012",
        "Vibrance", "Saturation",
        "Clarity2012", "Texture", "Dehaze", "Sharpness",
        "LuminanceSmoothing", "ConvertToGrayscale",
        "CropAngle", "HasCrop", "CropLeft", "CropTop", "CropRight", "CropBottom",
    ]
    args = [f"-XMP-crs:{t}" for t in tags] + ["-XMP-xmp:CreatorTool", "-XMP-crs:Version",
                                               "-XMP-crs:LookUUID", "-XMP-crs:ColorNoiseReduction",
                                               "-XMP-crs:CameraProfile", "-XMP-crs:CameraProfileDigest",
                                               "-s3", "-f", str(path)]
    values = _exiftool_read(args).strip().split("\n")
    stamp = values[len(tags):len(tags) + 2]            # CreatorTool, crs:Version (absent = "-")
    look = lookxmp.parse(values[len(tags) + 2] if len(values) > len(tags) + 2 else None)
    color_nr = values[len(tags) + 3].strip() if len(values) > len(tags) + 3 else "-"   # after the stamp lines
    prof_raw = values[len(tags) + 4:len(tags) + 6]
    prof_raw += ["-"] * (2 - len(prof_raw))
    values = values[:len(tags)]
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
    if color_nr not in ("-", ""):
        try:
            out["color_nr"] = float(color_nr)
        except ValueError:
            pass
    curve = _read_tone_curve(path)
    if curve:
        out["tone_curve"] = curve
    if out:
        out.update(_read_color_adjustments(path))
        out.update(look)
        out.update(profiles.parse_xmp(prof_raw[0], prof_raw[1]))
        declared = _declared_edit_version(stamp)
        if declared:
            out["edit_version"] = declared
    return out or None


def _declared_edit_version(stamp: list[str]) -> int | None:
    """Edit version a file states: ours via CreatorTool, Adobe's (crs:Version) as current; else unknown."""
    tool = stamp[0].strip() if stamp else "-"
    crs_version = stamp[1].strip() if len(stamp) > 1 else "-"
    tool = "" if tool == "-" else tool
    declared = editversion.version_from_tool(tool)
    if declared:
        return declared
    if (tool and any(n in tool for n in editversion.FOREIGN_TOOLS)) or crs_version not in ("-", ""):
        return editversion.CURRENT
    return None


def _read_tone_curve(path: Path) -> dict | None:
    args = ["-sep", "|"] + [f"-XMP-crs:{t}" for t in tonecurve.xmp_read_tags()] + ["-s3", "-f", str(path)]
    # Not stripped: an empty line is a tag with no value, and dropping
    # leading ones would shift every later tag up.
    return tonecurve.parse_xmp(_exiftool_read(args).split("\n"))


def _read_color_adjustments(path: Path) -> dict:
    """HSL, B&W mixer and colour grading values stored in the file (non-default only)."""
    args = [f"-XMP-crs:{t}" for t in colormix.XMP_TAGS.values()] + ["-s3", "-f", str(path)]
    return colormix.parse_xmp_values(_exiftool_read(args).strip().split("\n"))



# ---------------------------------------------------------------------------
# Ratings
# ---------------------------------------------------------------------------

# XMP-xmp:Rating is the tag Lightroom, Bridge and most catalogue software
# read, and -1 is their convention for "rejected" — so marks made here show
# up wherever the photos are opened next.
REJECTED = -1


def read_rating(path: Path) -> int:
    if is_dng(path):
        # The sidecar wins when it carries a rating; else the DNG's own.
        side = edit_target(path)
        if side != path and side.exists():
            value = _read_rating_from(side)
            if value:
                return value
        return _read_rating_from(path)
    target = edit_target(path)
    if target != path and not target.exists():
        return 0
    return _read_rating_from(target)


def _read_rating_from(path: Path) -> int:
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
    with fsutil.lock_for(target):
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
    out_all = {str(p): 0 for p in paths}
    # (file to read, photo it belongs to). A DNG is read from its sidecar
    # AND itself; the sidecar's rating wins when it has one.
    jobs: list[tuple[str, str, bool]] = []
    for p in paths:
        t = edit_target(p)
        if is_dng(p):
            if t != p and t.exists():
                jobs.append((str(t), str(p), True))
            jobs.append((str(p), str(p), False))
        elif t == p or t.exists():
            jobs.append((str(t), str(p), True))
    if not jobs:
        return out_all
    inner = _read_ratings_raw([Path(j[0]) for j in jobs])
    for tgt, owner, is_side in jobs:
        rating = inner.get(tgt, 0)
        if is_side or not out_all[owner]:
            if rating or is_side:
                out_all[owner] = rating or out_all[owner]
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


# ---------------------------------------------------------------------------
# Virtual copies (catalog.py): a copy has its own id, "<raw>#vc-<hex>". The raw
# is rendered with the copy's stored settings; history for the copy lives in a
# sidecar named after a pseudo path that sits beside the raw but never exists.
# ---------------------------------------------------------------------------

def _virtual(raw_id) -> tuple[Path, dict] | None:
    """(source raw, store entry) when ``raw_id`` names a virtual copy, else None."""
    if not catalog.is_virtual_id(raw_id):
        return None
    try:
        entry = catalog.get_virtual(LIBRARY_ROOT, raw_id)
    except KeyError as exc:
        raise ValueError(exc.args[0]) from None
    return _safe_path(entry["source"]), entry


def _resolve_photo(raw_id) -> Path:
    """The raw file behind a photo id: a virtual copy's id resolves to its source.

    For routes that only read or analyse the raw (overlay, upright, spot
    suggestion, recover); routes that also need the copy's own settings use
    ``_virtual`` directly.
    """
    vc = _virtual(raw_id)
    return vc[0] if vc else _safe_path(raw_id)


def _history_key(source: Path, entry: dict) -> Path:
    return source.with_name(f"{source.name}.{entry['vid']}")


def _unpseudo(path: Path) -> Path:
    """The raw behind a virtual copy's history key (a real path passes through)."""
    stem, _, tail = path.name.rpartition(".")
    if stem and re.fullmatch(r"vc-[0-9a-f]{8}", tail):
        return path.with_name(stem)
    return path


def _split_virtual(payload: dict) -> list[str]:
    """Remove virtual-copy ids from the payload's path(s) and return them."""
    raw = payload.get("paths")
    if raw is None and "path" in payload:
        raw = [payload["path"]]
    if not isinstance(raw, list):
        return []
    vids = [r for r in raw if catalog.is_virtual_id(r)]
    if vids:
        payload.pop("path", None)
        payload["paths"] = [r for r in raw if not catalog.is_virtual_id(r)]
    return vids


@app.route("/")
def index():
    return render_template(
        "editor.html",
        controls=CONTROLS,
        global_controls=GLOBAL_CONTROLS,
        color_controls=COLOR_CONTROLS,
        mask_controls=masks.LOCAL_CONTROLS,
        m8_controls=retouch.CONTROLS,
        effect_controls=EFFECT_CONTROLS,
        m8_toggles=retouch.TOGGLES,
        curve_presets=tonecurve.PRESETS,
        curve_parametric=[{"key": k, "label": k.capitalize()} for k in ("highlights", "lights", "darks", "shadows")],
        root=str(LIBRARY_ROOT),
        raw_suffixes=sorted(RAW_SUFFIXES),
        token=app.config["PHOTOROOM_TOKEN"],
    )


@app.route("/api/files")
def api_files():
    files = [str(p.relative_to(LIBRARY_ROOT)) for p in list_photos(LIBRARY_ROOT)]
    return jsonify({"root": str(LIBRARY_ROOT), "files": files})


def adjustments_have_edits(adj: dict, baseline_temp: float | None = None) -> bool:
    """True when ``adj`` changes anything the colour maths touches (not framing, not noise reduction)."""
    skip = {"straighten", "luminance_nr", "color_nr"}
    for c in CONTROLS + GLOBAL_CONTROLS:
        key = c["key"]
        if key in skip or key not in adj:
            continue
        if key == "color_temp":
            if baseline_temp is not None and abs(float(adj[key]) - float(baseline_temp)) > 1:
                return True
            continue
        try:
            if abs(float(adj[key]) - float(c["default"])) > 1e-9:
                return True
        except (TypeError, ValueError):
            continue
    for key, default in colormix.COLOR_DEFAULTS.items():
        if key in adj and adj[key] != default:
            return True
    if adj.get("monochrome") or not tonecurve.is_identity(adj.get("tone_curve")):
        return True
    if lut_amount(adj) > 0:
        return True
    return bool(adj.get("masks"))


def resolve_edit_version(adj: dict, baseline_temp: float | None = None) -> dict:
    """
    ``adj`` with ``edit_version`` set. Settings that say nothing and do something
    were made before versions existed, so they are version 1 and keep their look.
    """
    if adj.get("edit_version"):
        return adj
    out = dict(adj)
    out["edit_version"] = (editversion.LEGACY if adjustments_have_edits(adj, baseline_temp)
                           else editversion.CURRENT)
    return out


def complete_adjustments(adj: dict, baseline_temp: int) -> dict:
    """Fill every control, colour key, crop and curve so the UI gets a full set."""
    adj = dict(adj)
    adj.setdefault("color_temp", baseline_temp)
    merged = {c["key"]: adj.get(c["key"], c["default"]) for c in CONTROLS + GLOBAL_CONTROLS}
    merged.update({k: adj.get(k, d) for k, d in colormix.COLOR_DEFAULTS.items()})
    merged["monochrome"] = bool(adj.get("monochrome", False))
    merged.update(lookxmp.complete(adj))
    merged["camera_profile"] = adj.get("camera_profile") or ""
    if adj.get("camera_profile_foreign"):
        merged["camera_profile_foreign"] = adj["camera_profile_foreign"]
    merged["edit_version"] = resolve_edit_version(adj, baseline_temp).get("edit_version", EDIT_VERSION)
    merged["crop"] = adj.get("crop")
    merged["tone_curve"] = tonecurve.normalise(adj.get("tone_curve"))
    merged["masks"] = masks.normalise_masks(adj.get("masks"))
    merged.update(retouch.complete(adj))
    merged.update(effects.complete(adj))
    return merged


@app.route("/api/open", methods=["POST"])
def api_open():
    """Open a file: return its preview plus whatever adjustments it already has."""
    try:
        payload = _json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    try:
        vc = _virtual(payload["path"])
        path = vc[0] if vc else _safe_path(payload["path"])
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400
    # A virtual copy keeps its history under a pseudo path and its settings,
    # rating and so on in the catalog; the raw itself is never written.
    hkey = _history_key(*vc) if vc else path

    try:
        # Decode first so the auto analysis below can reuse the result rather
        # than unpacking the sensor data a second time.
        _, baseline_temp = _load_base_render(path)

        saved = (vc[1].get("adjustments") or None) if vc else read_saved_adjustments(path)
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
            adj["edit_version"] = EDIT_VERSION

        merged = complete_adjustments(adj, baseline_temp)

        base, _ = _load_base_render(path)
        assessment = quality.assess(base)

        # The as-imported state: the auto (or styled) proposal, remembered the
        # first time the photo is opened. Drives Before and reset-to-import.
        hist = history.load(hkey)
        if hist["original"] is None:
            if source != "saved":
                imported = merged   # already the auto proposal
            else:
                with _cache_lock:
                    decoded = _decode_cache.get(str(path.resolve()))
                auto = compute_auto_adjustments(
                    path, decoded=decoded, style_model=get_style_model())
                auto = {k: v for k, v in auto.items() if k not in ("iso", "styled")}
                auto["edit_version"] = EDIT_VERSION
                imported = complete_adjustments(auto, baseline_temp)
            hist = history.set_original(hkey, imported)
        if not hist["entries"]:
            hist, _ = history.record(
                hkey, "Import" if source != "saved" else "Opened with saved edits", merged)

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
            "rating": vc[1].get("rating", 0) if vc else _indexed_rating(path),
            "virtual": bool(vc),
            "baseline_temp": baseline_temp,
            "foreign_editor": None if vc else detect_foreign_edits(path),
            "needs_upgrade": int(merged.get("edit_version") or EDIT_VERSION) < EDIT_VERSION,
            "has_sidecar": has_sidecar(path),
            "lens": lens.describe(path),
            "source_kind": "image" if imgload.is_image(path) else "raw",
        })
    except Exception as exc:
        return jsonify({"error": describe_error(exc)}), 500


# Slider drags send a rising sequence number per photo. A request that has been
# overtaken by a newer one is skipped (before it starts, or between stages),
# since the client is about to discard its image anyway. Renders run one at a
# time: on a Pi, two at once just make both slower.
_latest_seq: dict[str, int] = {}
_seq_lock = threading.Lock()
_render_lock = threading.Lock()


def _note_seq(key: str, seq: int) -> None:
    with _seq_lock:
        if seq > _latest_seq.get(key, 0):
            _latest_seq[key] = seq
        if len(_latest_seq) > 256:                # bounded: forget old photos
            for k in list(_latest_seq)[:128]:
                if k != key:
                    del _latest_seq[k]


def _render_if_current(path: Path, adj: dict, seq: int, fast: bool = False) -> dict:
    key = str(path.resolve())
    _note_seq(key, seq)

    def stale() -> bool:
        return _latest_seq.get(key, 0) > seq

    with _render_lock:
        if stale():
            return {"stale": True}
        try:
            return render_with_histogram(path, adj, should_abort=stale, fast=fast)
        except StaleRender:
            return {"stale": True}


@app.route("/api/preview", methods=["POST"])
def api_preview():
    try:
        payload = _json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    try:
        vc = _virtual(payload["path"])
        path = vc[0] if vc else _safe_path(payload["path"])
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        adj = payload.get("adjustments", {})
        if payload.get("raw_frame"):
            # Spot editing shows the uncropped frame so clicks map straight
            # onto the stored coordinates.
            adj = {**adj, "_raw_frame": True}
        seq = payload.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            return jsonify(render_with_histogram(path, adj))
        return jsonify(_render_if_current(path, adj, seq, bool(payload.get("fast"))))
    except Exception as exc:
        return jsonify({"error": describe_error(exc)}), 500


    try:
        payload = _json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    try:
        path = _resolve_photo(payload["path"])
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400
@app.route("/api/upgrade_edit", methods=["POST"])
def api_upgrade_edit():
    """
    Re-map an edit made under the old (version 1) colour maths so it keeps its look.

    Nothing is written: the upgraded settings come back with a fresh preview and a
    report of how far the new render is from the old one; saving is the user's call.
    """
    try:
        payload = _json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    try:
        path = _resolve_photo(payload["path"])
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        adj = {k: v for k, v in dict(payload.get("adjustments") or {}).items() if k != "_baseline_temp"}
        base, baseline_temp = _load_base_render(path)
        with _render_lock:
            upgraded, report = editversion.upgrade(
                resolve_edit_version(adj, baseline_temp), baseline_temp, base,
                lambda b, t, a: apply_adjustments(b, t, a))
            merged = complete_adjustments(upgraded, baseline_temp)
            rendered = render_with_histogram(path, merged)
        return jsonify({"adjustments": merged, "report": report, **rendered})
    except Exception as exc:
        return jsonify({"error": describe_error(exc)}), 500


@app.route("/api/mask_overlay", methods=["POST"])
def api_mask_overlay():
    """The selected mask as a red-tint PNG the size of the current preview (O key)."""
    try:
        payload = _json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    try:
        path = _resolve_photo(payload["path"])
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        adj = payload.get("adjustments", {})
        wanted = str(payload.get("mask_id", ""))
        found = [m for m in masks.normalise_masks(adj.get("masks")) if m["id"] == wanted]
        if not found:
            return jsonify({"error": "No such mask"}), 404
        base, _ = _load_base_render(path)
        # The frame the preview shows: spots and lens first, then transform, straighten, crop.
        framed = retouch.apply_transform(retouch.apply_corrections(base, adj), adj)
        geo = apply_geometry(framed, adj)
        shape = geo.shape[:2]
        # Range selections read the framed picture (before the other edits) so they show.
        png = masks.overlay_png(found[0], shape, img=geo if found[0].get("range") else None)
        return jsonify({"overlay": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
                        "width": shape[1], "height": shape[0]})
    except Exception as exc:
        return jsonify({"error": describe_error(exc)}), 500


@app.route("/api/upright", methods=["POST"])
def api_upright():
    """Suggest transform values from detected lines (Auto / Level / Vertical / Full)."""
    try:
        payload = _json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    try:
        path = _resolve_photo(payload["path"])
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400
    mode = payload.get("mode", "auto")
    if mode not in retouch.transform.UPRIGHT_MODES:
        return jsonify({"error": f"mode must be one of {list(retouch.transform.UPRIGHT_MODES)}"}), 400
    try:
        base, _ = _load_base_render(path)
        # Detect on the lens-corrected frame, before any transform, so the
        # result is what the sliders should be set to from neutral.
        adj = {k: v for k, v in (payload.get("adjustments") or {}).items()
               if not k.startswith("xf_")}
        framed = retouch.apply_corrections(base, lens.attach_meta(path, adj))
        guides = payload.get("guides")
        if guides is not None and not isinstance(guides, list):
            return jsonify({"error": "guides must be a list of {x1, y1, x2, y2}"}), 400
        found = retouch.transform.upright(framed, mode, guides)
        info = found.pop("info", {})
        return jsonify({"ok": True, "mode": mode, "values": found, "detected": info,
                        "message": ("No clear lines found; nothing changed"
                                    if all(found[k] == retouch.transform.DEFAULTS[k] for k in found)
                                    else f"{'Guided upright' if guides is not None else 'Upright'} ({mode}) from {info.get('lines', 0)} lines")})
    except Exception as exc:
        return jsonify({"error": describe_error(exc)}), 500


@app.route("/api/spot/suggest", methods=["POST"])
def api_spot_suggest():
    """Pick a clone/heal source for a spot placed at (x, y) with radius r."""
    try:
        payload = _json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    try:
        path = _resolve_photo(payload["path"])
        x, y = float(payload["x"]), float(payload["y"])
        r = float(payload.get("r", 0.02))
    except (ValueError, KeyError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        base, _ = _load_base_render(path)
        sx, sy = retouch.spots.suggest_source(base, min(max(x, 0.0), 1.0),
                                              min(max(y, 0.0), 1.0), r)
        return jsonify({"ok": True, "sx": sx, "sy": sy})
    except Exception as exc:
        return jsonify({"error": describe_error(exc)}), 500


def _save_frames(path: Path, chosen: dict):
    """(frame_size, source_size) save_adjustments needs for ``chosen`` on ``path``.

    Used by every writer (single save, batch apply) so they cannot disagree.
    """
    needs_frame = (float(chosen.get("straighten", 0) or 0) or chosen.get("masks")
                   or chosen.get("spots") or not retouch.transform.is_identity(chosen))
    if not needs_frame:
        return None, None
    base, _ = _load_base_render(path)
    source_size = (base.shape[1], base.shape[0])
    # Straighten and Crop act on the transformed frame.
    return retouch.frame_after_transform(*source_size, chosen), source_size


@app.route("/api/save", methods=["POST"])
def api_save():
    try:
        payload = _json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    try:
        vc = _virtual(payload["path"])
        path = vc[0] if vc else _safe_path(payload["path"])
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400

    if vc:
        chosen = payload.get("adjustments", {})
        if not isinstance(chosen, dict):
            return jsonify({"error": "adjustments must be an object"}), 400
        catalog.update_virtual(LIBRARY_ROOT, vc[1]["id"], adjustments=chosen)
        return jsonify({"ok": True, "message": f"Saved virtual copy of {path.name}",
                        "warning": None, "style": None})

    try:
        chosen = payload.get("adjustments", {})

        frame_size, source_size = _save_frames(path, chosen)

        save_adjustments(path, chosen, frame_size=frame_size, sidecar=WRITE_SIDECARS,
                         source_size=source_size)
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
    return camera_baseline_temp(path)


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


def _idx(fresh: bool = True) -> "libindex.Index":
    """The in-memory library index for the current root (scanned on first use)."""
    idx = libindex.get_index(LIBRARY_ROOT)
    if fresh:
        idx.ensure_fresh()
    return idx


def _set_fields(updates: dict[str, dict]) -> None:
    """Apply rating / label / keyword / pick changes through the index (sidecars follow in the background)."""
    idx = _idx()
    if any(idx.get(rel) is None for rel in updates):
        idx.scan()                    # a file that appeared since the last scan
    idx.set_fields(updates)


def _indexed_rating(path: Path) -> int:
    """A photo's rating without spawning exiftool when the index knows it."""
    rec = libindex.get_index(LIBRARY_ROOT).get(_rel(path))
    return rec["rating"] if rec is not None else read_rating(path)


@app.route("/api/ratings")
def api_ratings():
    """Every rating in the library, from the index."""
    return jsonify({"ratings": {it["path"]: it["rating"] for it in _idx().items()}})


@app.route("/api/rate", methods=["POST"])
def api_rate():
    try:
        payload = _json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    try:
        vc = _virtual(payload["path"])
        path = vc[0] if vc else _safe_path(payload["path"])
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        rating = int(payload.get("rating", 0))
        if vc:
            catalog.update_virtual(LIBRARY_ROOT, vc[1]["id"], rating=rating)
            return jsonify({"ok": True, "rating": rating, "message": f"{vc[1]['id']}: {rating}"})
        _set_fields({_rel(path): {"rating": rating}})
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
    sem_info = None
    try:
        offset = max(0, int(args.get("offset", 0)))
        limit = max(1, min(LIBRARY_MAX_PAGE, int(args.get("limit", LIBRARY_PAGE_SIZE))))
        criteria = {k: args.get(k, "") for k in library.FILTER_KEYS}
        idx = _idx()
        virtuals = catalog.virtual_items(LIBRARY_ROOT, idx.items()) if catalog.has_virtuals(LIBRARY_ROOT) else []
        stacked = catalog.has_stacks(LIBRARY_ROOT)
        folder = args.get("folder")
        semantic_rank = semantic_api.wants_ranking(args)     # `sem` / `similar`: rank the filtered set
        person = faces_api.wants_person(args)                # `person`: only photos showing that person
        if not virtuals and not args.get("collection") and folder is None and not stacked \
                and not semantic_rank and not person:
            total, page, library_total = idx.query(criteria, args.get("sort", "name"),
                                                   args.get("dir", "asc") == "desc", offset, limit)
            facets = idx.facets() if offset == 0 else None
        else:
            everything = idx.items() + virtuals
            matched = library.filter_items(everything, criteria)
            if args.get("collection"):
                matched = catalog.collection_items(
                    catalog.get_collection(LIBRARY_ROOT, args["collection"]), matched)
            if folder is not None:
                matched = [it for it in matched if folders.in_folder(
                    it["path"], folder.strip("/"), args.get("subfolders") == "1")]
            if person:
                matched = faces_api.filter_library(matched, args)
            matched = library.sort_items(
                matched, args.get("sort", "name"), args.get("dir", "asc") == "desc")
            if stacked and not semantic_rank:        # a similarity ranking shows every match, not stack tops
                matched = catalog.apply_stacks(LIBRARY_ROOT, matched, everything,
                                               args.get("stacks", "1") != "0")
            if semantic_rank:
                matched, sem_info = semantic_api.rank_library(matched, args)
            total, page, library_total = len(matched), matched[offset:offset + limit], len(everything)
            facets = library.facets(everything) if offset == 0 else None
    except KeyError as exc:
        return jsonify({"error": exc.args[0]}), 404
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": describe_error(exc)}), 500

    body = {
        "total": total,
        "library_total": library_total,
        "offset": offset,
        "items": page,
        "seq": idx.seq,
    }
    if facets is not None:
        body["facets"] = facets
    if sem_info is not None:
        body["semantic"] = sem_info
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
        vids = _split_virtual(payload)
        paths = _paths_from(payload) if (payload.get("paths") or not vids) else []
        flag = payload.get("flag")
        if flag not in library.FLAGS:
            raise ValueError(f"flag must be one of {', '.join(library.FLAGS)}")
        for vid in vids:
            entry = catalog.get_virtual(LIBRARY_ROOT, vid)
            rating = entry.get("rating", 0)
            if flag == "reject":
                if rating != REJECTED:
                    library.remember_pre_reject(LIBRARY_ROOT, {vid: rating})
                new_rating = REJECTED
            elif rating == REJECTED:
                new_rating = library.take_pre_reject(LIBRARY_ROOT, [vid])[vid]
            else:
                new_rating = rating
            catalog.update_virtual(
                LIBRARY_ROOT, vid, flag="pick" if flag == "pick" else "none",
                rating=new_rating)
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc.args[0] if isinstance(exc, KeyError) else exc)}), 400
    try:
        _flag_paths(paths, flag)
        n = len(paths) + len(vids)
        return jsonify({"ok": True, "flag": flag, "count": n, "message": f"{n} photo(s): {flag}"})
    except Exception as exc:
        return jsonify({"error": describe_error(exc)}), 500


def _flag_paths(paths: list[Path], flag: str) -> None:
    """pick / reject / none on real photos through the index (also used by the cull batch)."""
    idx = _idx()
    if any(idx.get(_rel(p)) is None for p in paths):
        idx.scan()
    current = idx.get_many(_rel(p) for p in paths)
    updates = {}
    for p in paths:
        rel = _rel(p)
        was_rejected = current.get(rel, {}).get("rating") == REJECTED
        change = {"pick": flag == "pick"}
        if flag == "reject" and not was_rejected:
            # Rejecting writes -1 over the stars; remember them first so
            # un-rejecting (or an accidental double X) gets them back.
            library.remember_pre_reject(LIBRARY_ROOT, {rel: current.get(rel, {}).get("rating", 0)})
            change["rating"] = REJECTED
        elif flag != "reject" and was_rejected:
            change["rating"] = library.take_pre_reject(LIBRARY_ROOT, [rel])[rel]
        updates[rel] = change
    idx.set_fields(updates)


@app.route("/api/label", methods=["POST"])
def api_label():
    """Set a colour label (or clear it with "none") on one or many photos."""
    payload, err = _json_or_error()
    if err:
        return err
    try:
        vids = _split_virtual(payload)
        paths = _paths_from(payload) if (payload.get("paths") or not vids) else []
        label = library.normalize_label(payload.get("label"))
        for vid in vids:
            catalog.update_virtual(LIBRARY_ROOT, vid, label=label)
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc.args[0] if isinstance(exc, KeyError) else exc)}), 400
    try:
        _set_fields({_rel(p): {"label": label} for p in paths})
    except Exception as exc:
        return jsonify({"error": describe_error(exc)}), 500
    return jsonify({"ok": True, "label": label, "count": len(paths) + len(vids),
                    "message": f"{len(paths) + len(vids)} photo(s): " + (label.lower() + " label" if label else "label cleared")})


@app.route("/api/keywords", methods=["POST"])
def api_keywords():
    """Add and/or remove keywords, or replace them with ``set``, on one or many photos."""
    payload, err = _json_or_error()
    if err:
        return err
    try:
        vids = _split_virtual(payload)
        paths = _paths_from(payload) if (payload.get("paths") or not vids) else []
        replace = payload.get("set")
        add = library.normalize_keywords(payload.get("add", []))
        remove = {k.lower() for k in library.normalize_keywords(payload.get("remove", []))}
        if replace is None and not add and not remove:
            raise ValueError("Nothing to change")
        for vid in vids:
            have = catalog.get_virtual(LIBRARY_ROOT, vid).get("keywords", [])
            new = library.normalize_keywords(replace) if replace is not None else \
                library.normalize_keywords([k for k in have if k.lower() not in remove] + add)
            catalog.update_virtual(LIBRARY_ROOT, vid, keywords=new)
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc.args[0] if isinstance(exc, KeyError) else exc)}), 400
    try:
        idx = _idx()
        if any(idx.get(_rel(p)) is None for p in paths):
            idx.scan()
        updates = {}
        for p in paths:
            if replace is not None:
                new = library.normalize_keywords(replace)
            else:
                have = (idx.get(_rel(p)) or {}).get("keywords", [])
                new = library.normalize_keywords([k for k in have if k.lower() not in remove] + add)
            updates[_rel(p)] = {"keywords": new}
        idx.set_fields(updates)
    except Exception as exc:
        return jsonify({"error": describe_error(exc)}), 500
    n = len(paths) + len(vids)
    return jsonify({"ok": True, "count": n, "message": f"Keywords updated on {n} photo(s)"})


# ---------------------------------------------------------------------------
# Collections, smart collections, virtual copies, compare/survey previews
# ---------------------------------------------------------------------------

def _all_items() -> list[dict]:
    items = _idx().items()
    return items + catalog.virtual_items(LIBRARY_ROOT, items)


def _collections_body() -> dict:
    counts = catalog.collection_counts(LIBRARY_ROOT, _all_items())
    cols = catalog.tree_order(catalog.list_collections(LIBRARY_ROOT))
    return {"collections": [{**c, "count": counts.get(c["id"], 0)} for c in cols],
            "target": catalog.target_id(LIBRARY_ROOT)}


def _catalog_call(fn):
    """Run a catalog mutation; map its errors onto HTTP statuses."""
    try:
        return fn()
    except KeyError as exc:
        return jsonify({"error": exc.args[0]}), 404
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": describe_error(exc)}), 500


def _known_ids(raw) -> list[str]:
    """Validate photo ids from the client: real photos or existing copies."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("No photos given")
    for r in raw:
        if not isinstance(r, str):
            raise ValueError("Photo ids must be strings")
        if catalog.is_virtual_id(r):
            catalog.get_virtual(LIBRARY_ROOT, r)
        else:
            _safe_path(r)
    return raw


@app.route("/api/collections")
def api_collections():
    return jsonify(_collections_body())


@app.route("/api/collections/create", methods=["POST"])
def api_collection_create():
    payload, err = _json_or_error()
    if err:
        return err

    def run():
        smart = bool(payload.get("smart"))
        photos = _known_ids(payload["photos"]) if payload.get("photos") else []
        col = catalog.create_collection(
            LIBRARY_ROOT, payload.get("name"), smart=smart, rules=payload.get("rules"),
            match=payload.get("match", "all"), photos=photos,
            parent=payload.get("parent") or None, is_set=bool(payload.get("set")))
        return jsonify({"ok": True, "collection": col, **_collections_body()})
    return _catalog_call(run)


@app.route("/api/collections/rename", methods=["POST"])
def api_collection_rename():
    payload, err = _json_or_error()
    if err:
        return err

    def run():
        catalog.rename_collection(LIBRARY_ROOT, str(payload.get("id", "")), payload.get("name"))
        return jsonify({"ok": True, **_collections_body()})
    return _catalog_call(run)


@app.route("/api/collections/delete", methods=["POST"])
def api_collection_delete():
    payload, err = _json_or_error()
    if err:
        return err

    def run():
        catalog.delete_collection(LIBRARY_ROOT, str(payload.get("id", "")))
        return jsonify({"ok": True, **_collections_body()})
    return _catalog_call(run)


@app.route("/api/collections/move", methods=["POST"])
def api_collection_move():
    """Put a collection or set inside a collection set (``parent`` empty: top level)."""
    payload, err = _json_or_error()
    if err:
        return err

    def run():
        catalog.move_collection(LIBRARY_ROOT, str(payload.get("id", "")), payload.get("parent") or None)
        return jsonify({"ok": True, **_collections_body()})
    return _catalog_call(run)


@app.route("/api/collections/target", methods=["POST"])
def api_collection_target():
    """Choose the Target Collection (``id`` empty: the Quick Collection)."""
    payload, err = _json_or_error()
    if err:
        return err

    def run():
        catalog.set_target(LIBRARY_ROOT, str(payload.get("id") or "") or None)
        return jsonify({"ok": True, **_collections_body()})
    return _catalog_call(run)


@app.route("/api/collections/target_toggle", methods=["POST"])
def api_collection_target_toggle():
    """B: add the photos to the Target Collection, or remove them if all are in it."""
    payload, err = _json_or_error()
    if err:
        return err

    def run():
        ids = _known_ids(payload.get("photos"))
        res = catalog.toggle_in_target(LIBRARY_ROOT, ids)
        return jsonify({"ok": True, "action": res["action"], "collection": res["collection"]["name"],
                        **_collections_body()})
    return _catalog_call(run)


@app.route("/api/collections/clear", methods=["POST"])
def api_collection_clear():
    """Empty a manual collection (typically the Quick Collection); the photos stay."""
    payload, err = _json_or_error()
    if err:
        return err

    def run():
        catalog.clear_collection(LIBRARY_ROOT, str(payload.get("id", "")))
        return jsonify({"ok": True, **_collections_body()})
    return _catalog_call(run)


@app.route("/api/collections/rules", methods=["POST"])
def api_collection_rules():
    payload, err = _json_or_error()
    if err:
        return err

    def run():
        catalog.update_rules(LIBRARY_ROOT, str(payload.get("id", "")),
                             payload.get("rules"), payload.get("match"))
        return jsonify({"ok": True, **_collections_body()})
    return _catalog_call(run)


@app.route("/api/collections/add", methods=["POST"])
def api_collection_add():
    payload, err = _json_or_error()
    if err:
        return err

    def run():
        ids = _known_ids(payload.get("photos"))
        catalog.add_photos(LIBRARY_ROOT, str(payload.get("id", "")), ids)
        return jsonify({"ok": True, **_collections_body()})
    return _catalog_call(run)


@app.route("/api/collections/remove", methods=["POST"])
def api_collection_remove():
    payload, err = _json_or_error()
    if err:
        return err

    def run():
        ids = payload.get("photos")
        if not isinstance(ids, list):
            raise ValueError("No photos given")
        catalog.remove_photos(LIBRARY_ROOT, str(payload.get("id", "")), ids)
        return jsonify({"ok": True, **_collections_body()})
    return _catalog_call(run)


def _current_adjustments(path: Path) -> dict:
    """What opening the photo would show: saved edits, else the auto proposal."""
    _, baseline_temp = _load_base_render(path)
    saved = read_saved_adjustments(path)
    if saved:
        return complete_adjustments(saved, baseline_temp)
    with _cache_lock:
        decoded = _decode_cache.get(str(path.resolve()))
    auto = compute_auto_adjustments(path, decoded=decoded, style_model=get_style_model())
    auto = {k: v for k, v in auto.items() if k not in ("iso", "styled")}
    auto["edit_version"] = EDIT_VERSION
    return complete_adjustments(auto, baseline_temp)


@app.route("/api/virtual/create", methods=["POST"])
def api_virtual_create():
    """Make a virtual copy: own settings, rating, label, keywords; no second raw."""
    payload, err = _json_or_error()
    if err:
        return err

    def run():
        src_id = catalog.split_id(str(payload.get("path", "")))[0]
        source = _safe_path(src_id)
        parent = catalog.get_virtual(LIBRARY_ROOT, payload["path"]) \
            if catalog.is_virtual_id(payload["path"]) else None
        if parent is not None:
            adj, meta = parent["adjustments"], parent
        else:
            adj = payload.get("adjustments")
            if not isinstance(adj, dict) or not adj:
                adj = _current_adjustments(source)
            meta = _idx().get(src_id) or library.read_metadata([source]).get(str(source), {})
            meta = {**meta, "flag": "pick" if meta.get("flag") == "pick" or library.get_pick(LIBRARY_ROOT, src_id) else "none"}
        rating = meta.get("rating", 0)
        entry = catalog.create_virtual(
            LIBRARY_ROOT, src_id, adj, rating=max(0, rating), label=meta.get("label", ""),
            keywords=meta.get("keywords", []), flag=meta.get("flag", "none"))
        return jsonify({"ok": True, "virtual": entry,
                        "message": f"Created virtual copy {entry['copy_no']} of {source.name}"})
    return _catalog_call(run)


@app.route("/api/virtual/delete", methods=["POST"])
def api_virtual_delete():
    """Delete a copy. Only the catalog entry and the copy's history file go; never the raw."""
    payload, err = _json_or_error()
    if err:
        return err

    def run():
        vid = str(payload.get("path", ""))
        if not catalog.is_virtual_id(vid):
            raise ValueError("Only virtual copies can be deleted here")
        entry = catalog.delete_virtual(LIBRARY_ROOT, vid)
        try:
            source = _safe_path(entry["source"])
            history.history_path(_history_key(source, entry)).unlink(missing_ok=True)
        except (ValueError, OSError):
            pass
        return jsonify({"ok": True, "message": f"Deleted virtual copy {entry['copy_no']}"})
    return _catalog_call(run)


@app.route("/api/view_preview", methods=["POST"])
def api_view_preview():
    """
    A preview of a photo (or virtual copy) as currently edited, for the compare
    view. Read-only: it opens no history and records nothing.
    """
    payload, err = _json_or_error()
    if err:
        return err

    def run():
        vc = _virtual(payload.get("path"))
        path = vc[0] if vc else _safe_path(payload.get("path", ""))
        adj = (vc[1].get("adjustments") or None) if vc else None
        if adj is None:
            adj = _current_adjustments(path)
        else:
            adj = complete_adjustments(adj, _load_base_render(path)[1])
        rendered = render_with_histogram(path, adj)
        return jsonify({"preview": rendered["preview"]})
    return _catalog_call(run)


_thumb_pool_instance: thumbs.ThumbPool | None = None
_thumb_pool_lock = threading.Lock()


def _thumb_pool() -> thumbs.ThumbPool:
    """The shared thumbnail workers, created on first use for the current library."""
    global _thumb_pool_instance
    with _thumb_pool_lock:
        if _thumb_pool_instance is None or _thumb_pool_instance.root != LIBRARY_ROOT:
            def fallback(p):
                rgb, _, _ = decode_for_analysis(p)
                return rgb
            _thumb_pool_instance = thumbs.ThumbPool(LIBRARY_ROOT, decode_fallback=fallback)
        return _thumb_pool_instance


@app.route("/api/thumbs/prefetch", methods=["POST"])
def api_thumbs_prefetch():
    """Start building every missing thumbnail in the background; returns progress."""
    try:
        _json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    try:
        return jsonify(_thumb_pool().prefetch([LIBRARY_ROOT / it['path'] for it in _idx().items() if it.get('kind') != 'video']))
    except Exception as exc:
        return jsonify({"error": describe_error(exc)}), 500


@app.route("/api/thumbs/want", methods=["POST"])
def api_thumbs_want():
    """Tell the workers which tiles are on screen now; they jump the queue. Returns at once."""
    payload, err = _json_or_error()
    if err:
        return err
    try:
        size = int(payload.get("size", thumbs.THUMB_EDGE))
        paths = [_safe_path(r) for r in payload.get("paths", [])[:500]]
        return jsonify({"queued": _thumb_pool().want(paths, size)})
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/thumbs/status")
def api_thumbs_status():
    return jsonify(_thumb_pool().progress())


@app.route("/api/thumb")
def api_thumb():
    """Serve a cached thumbnail for one file."""
    try:
        raw = request.args.get("path", "")
        vc = _virtual(raw)
        path = vc[0] if vc else _safe_path(raw)    # a copy shows its raw's thumbnail
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        size = int(request.args.get("size", thumbs.THUMB_EDGE))
        if size not in thumbs.SIZES:
            return jsonify({"error": f"size must be one of {', '.join(map(str, thumbs.SIZES))}"}), 400
        data = _thumb_pool().get_now(path, size=size)
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
    target_version = int(payload.get("edit_version") or EDIT_VERSION)
    if target_version < EDIT_VERSION:
        # Merging version 2 numbers into a version 1 edit would silently change how
        # the photo's existing sliders look.
        return jsonify({"error": "This photo's edit uses the old colour maths. Click "
                                 "Upgrade edit first, then apply the preset.",
                        "needs_upgrade": True}), 409
    if not preset.get("edit_version"):
        # Saved before edit versions: its numbers were tuned on the old maths.
        preset = editversion.map_parameters(
            preset, float(payload.get("baseline_temp") or 5500))
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
    vids = _split_virtual(payload)
    raw_paths = payload.get("paths", [])

    if not raw_paths and not vids:
        return jsonify({"error": "No files selected"}), 400

    applied: list[str] = []
    failed: list[dict] = []
    skipped: list[dict] = []
    allow_foreign = bool(payload.get("overwrite_foreign", False))
    include_geometry = bool(payload.get("include_geometry", False))

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

    # A virtual copy keeps its settings in the catalog (canonical coordinates,
    # same photograph), never in the raw's XMP.
    for vid in vids:
        try:
            entry = catalog.get_virtual(LIBRARY_ROOT, vid)
            incoming = (dict(adjustments) if include_geometry
                        else geometry.without_photo_specific(adjustments))
            existing = entry.get("adjustments") or {}
            if existing and int(existing.get("edit_version") or editversion.LEGACY) < int(
                    incoming.get("edit_version") or EDIT_VERSION):
                # Never merge version 2 numbers into a version 1 edit as if they were alike.
                existing = editversion.map_parameters(existing, float(
                    incoming.get("_baseline_temp") or source_baseline or 5500))
            catalog.update_virtual(LIBRARY_ROOT, vid, adjustments={**existing, **incoming})
            applied.append(vid)
        except Exception as exc:
            failed.append({"path": vid, "error": str(exc.args[0] if isinstance(exc, KeyError) else exc)})

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

            # Framing, perspective, spots and masks belong to the photo they
            # were made on; only the look travels (geometry.PHOTO_SPECIFIC_KEYS)
            # unless the caller explicitly asks for geometry too. Then each
            # target gets the same coordinate conversion as a single save.
            per_file = (dict(adjustments) if include_geometry
                        else geometry.without_photo_specific(adjustments))

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

            frame_size, source_size = _save_frames(path, per_file)
            save_adjustments(path, per_file, frame_size=frame_size, source_size=source_size)
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
        vc = _virtual(payload["path"])
        path = vc[0] if vc else _safe_path(payload["path"])
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        # Reset clears the whole XMP-crs group. On a Lightroom-managed file
        # that destroys develop settings this tool never wrote — the same
        # damage cmd_revert and batch apply already refuse to do.
        if not vc and not payload.get("overwrite_foreign", False):
            who = detect_foreign_edits(path)
            if who:
                return jsonify({
                    "error": f"{path.name} carries edits made in {who}. "
                             "Resetting would discard them.",
                    "foreign_editor": who,
                }), 409

        if not vc:
            strip_xmp_adjustments(path)
        _, baseline_temp = _load_base_render(path)
        neutral = {c["key"]: c["default"] for c in CONTROLS + GLOBAL_CONTROLS}
        neutral.update(colormix.COLOR_DEFAULTS)
        neutral["color_temp"] = baseline_temp
        neutral["monochrome"] = False
        neutral.update(lookxmp.complete({}))
        neutral["crop"] = None
        neutral["masks"] = []
        if not vc and is_dng(path) and edit_target(path) != path:
            # A DNG's embedded settings are never edited; an explicit neutral
            # sidecar overrides them instead.
            save_adjustments(path, neutral)
        if not vc:
            masks.delete_sidecar(path)    # a virtual copy keeps its masks in the catalog
        neutral.update(copy.deepcopy(retouch.DEFAULTS))
        neutral.update(effects.complete({}))
        if vc:
            catalog.update_virtual(LIBRARY_ROOT, vc[1]["id"], adjustments=neutral)
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
        vc = _virtual(payload["path"])
        return payload, (_history_key(*vc) if vc else _safe_path(payload["path"])), None
    except (ValueError, KeyError) as exc:
        return None, None, (jsonify({"error": str(exc)}), 400)


def _state_response(path: Path, adjustments: dict, hist: dict, **extra):
    """A full adjustment set rendered, as the client needs after a jump/apply."""
    path = _unpseudo(path)
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
        path = _resolve_photo(payload["path"])
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
    parser.add_argument(
        "--embed-dng", action="store_true",
        help="Write edits, ratings and labels INTO .dng files instead of a "
             ".xmp sidecar. Off by default: a DNG may be your only original.",
    )
    args = parser.parse_args()
    if args.embed_dng:
        os.environ["PHOTO_ROOM_EMBED_DNG"] = "1"

    global WRITE_SIDECARS
    WRITE_SIDECARS = args.write_sidecars

    root = Path(args.folder).expanduser().resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        sys.exit(1)
    LIBRARY_ROOT = root

    print(f"Editing {root}")
    import backup
    if backup.maybe_auto_backup(root):
        print("Scheduled library backup started.")
    if WRITE_SIDECARS:
        print("Saves will also write .xmp sidecars.")
    print(f"Open http://127.0.0.1:{args.port}")
    app.config["BOUND_PORT"] = args.port
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
