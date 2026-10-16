"""
The one crop / straighten coordinate system, and the only code that converts it.

CANONICAL FORM (what ``adjustments["crop"]`` and ``["straighten"]`` mean
everywhere inside this program):

* ``straighten`` is degrees; the picture is rotated about the centre of the
  frame that comes out of the perspective transform (``transform``), then cut
  to the largest axis-aligned rectangle with the frame's proportions that fits
  inside the rotation (the "inscribed rectangle"), so no empty corners show.
* ``crop`` is ``{left, top, right, bottom}``, fractions 0..1 of THAT inscribed
  rectangle (or of the whole transformed frame when ``straighten`` is 0).
  This is exactly what the preview shows and what the crop tool drags.
* Mask coordinates are fractions of the picture AFTER crop (see ``masks``);
  spot coordinates are fractions of the original frame BEFORE any geometry
  (see ``spots``). Neither is touched by this module.

This canonical form is what the preview, history, snapshots, presets, virtual
copies (catalog JSON), batch apply and export all carry and consume.

The ONLY place a different coordinate system exists is the XMP file, because
Camera Raw / Lightroom express ``CropLeft..CropBottom`` as fractions of the
whole straightened (rotated, un-inscribed) frame. ``ui_to_xmp_crop`` is applied
when writing XMP and ``xmp_to_ui_crop`` when reading it back; they are exact
inverses, so ``read(write(adj)) == adj`` for every straighten angle.

With a straighten and no crop, the XMP crop is the inscribed rectangle itself
(so Lightroom trims the empty corners like the preview does) and reads back as
the full canonical rectangle 0..1.

Pixel note: the XMP conversion uses the exact (fractional) inscribed
rectangle, so it is resolution independent. ``editor.apply_geometry`` shrinks
that rectangle by two pixels to avoid the resampled border, which is a
rendering detail, not a coordinate change.
"""
from __future__ import annotations

import numpy as np

FULL = {"left": 0.0, "top": 0.0, "right": 1.0, "bottom": 1.0}
_SNAP = 2e-4        # XMP stores 5 decimals; values this near 0 or 1 are 0 or 1

# Keys describing one photograph's content (framing, perspective, where
# things are on this frame) rather than a look. They are never carried to a
# *different* photo by presets or batch apply. A virtual copy, history entry
# or snapshot is the same photograph, so those keep everything.
PHOTO_SPECIFIC_KEYS = frozenset({
    "crop", "straighten", "spots", "masks",
    "xf_vertical", "xf_horizontal", "xf_rotate", "xf_aspect",
    "xf_scale", "xf_x", "xf_y",
})
# Internal, derived values that must never be persisted or transferred.
PRIVATE_PREFIX = "_"


def without_photo_specific(adj: dict, drop_private: bool = False) -> dict:
    """``adj`` minus everything that only makes sense on its own photograph."""
    return {k: v for k, v in adj.items()
            if k not in PHOTO_SPECIFIC_KEYS
            and not (drop_private and k.startswith(PRIVATE_PREFIX))}


def largest_inscribed_rect(w: float, h: float, angle_rad: float) -> tuple[float, float]:
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


def inscribed_box(width: float, height: float, angle: float) -> tuple[float, float, float, float] | None:
    """(x0, y0, w, h) of the inscribed rectangle in the straightened frame, or
    None when there is nothing to convert (no angle, or a degenerate frame)."""
    if not angle or width <= 0 or height <= 0:
        return None
    keep_w, keep_h = largest_inscribed_rect(width, height, np.deg2rad(angle))
    if keep_w <= 0 or keep_h <= 0:
        return None
    return (width - keep_w) / 2.0, (height - keep_h) / 2.0, keep_w, keep_h


def ui_to_xmp_crop(crop: dict | None, angle: float, width: float, height: float) -> dict:
    """Canonical crop -> fractions of the whole straightened frame (XMP)."""
    crop = crop or FULL
    box = inscribed_box(width, height, angle)
    if box is None:
        return dict(crop)
    x0, y0, kw, kh = box
    return {
        "left": (x0 + float(crop.get("left", 0.0)) * kw) / width,
        "right": (x0 + float(crop.get("right", 1.0)) * kw) / width,
        "top": (y0 + float(crop.get("top", 0.0)) * kh) / height,
        "bottom": (y0 + float(crop.get("bottom", 1.0)) * kh) / height,
    }


def xmp_to_ui_crop(crop: dict | None, angle: float, width: float, height: float) -> dict | None:
    """Exact inverse of ``ui_to_xmp_crop``: XMP fractions -> canonical crop."""
    if crop is None:
        return None
    box = inscribed_box(width, height, angle)
    if box is None:
        return dict(crop)
    x0, y0, kw, kh = box

    def snap(v: float) -> float:
        v = min(1.0, max(0.0, v))
        return 0.0 if v < _SNAP else 1.0 if v > 1.0 - _SNAP else v

    return {
        "left": snap((float(crop.get("left", 0.0)) * width - x0) / kw),
        "right": snap((float(crop.get("right", 1.0)) * width - x0) / kw),
        "top": snap((float(crop.get("top", 0.0)) * height - y0) / kh),
        "bottom": snap((float(crop.get("bottom", 1.0)) * height - y0) / kh),
    }
