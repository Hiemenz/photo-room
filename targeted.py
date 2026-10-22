"""
Targeted adjustment tool: what is under the cursor.

The tool lets the photographer drag on the photo instead of on a slider. All
it needs from the server is which tone-curve input level and which colour-mixer
band the clicked pixel belongs to; the client then moves the curve point or
the band's slider while the mouse moves. Pure numpy, no Flask.
"""
from __future__ import annotations

import colorsys

import numpy as np

import colormix

LOW_SATURATION = 0.06      # below this the hue is noise: no band is offered


def band_for_hue(hue: float) -> tuple[int, str]:
    """Colour-mixer band whose centre is nearest (circularly) to ``hue`` degrees."""
    centres = np.array(colormix.BAND_CENTRES, dtype=np.float64)
    d = np.abs((centres - (hue % 360.0) + 180.0) % 360.0 - 180.0)
    i = int(np.argmin(d))
    return i, colormix.BANDS[i]


def sample(img: np.ndarray, x: float, y: float, radius: int = 2) -> dict:
    """
    Look at a rendered 0..1 RGB image around the point (x, y), both 0..1 of the frame.

    Returns the mean ``rgb`` (0..255), ``level`` (Rec. 709 luma on the
    encoded values, 0..255: the tone-curve input), ``hue`` degrees,
    ``saturation`` 0..1, and the colour-mixer ``band`` (None for greys).
    """
    h, w = img.shape[:2]
    if not (np.isfinite(x) and np.isfinite(y)):
        raise ValueError("x and y must be finite")
    cx = int(round(min(1.0, max(0.0, x)) * (w - 1)))
    cy = int(round(min(1.0, max(0.0, y)) * (h - 1)))
    r = max(0, int(radius))
    patch = img[max(0, cy - r):cy + r + 1, max(0, cx - r):cx + r + 1, :3].reshape(-1, 3)
    rgb = np.clip(patch.mean(axis=0), 0.0, 1.0)
    level = float(rgb @ np.array([0.2126, 0.7152, 0.0722]) * 255.0)
    hue, sat, _ = colorsys.rgb_to_hsv(*[float(v) for v in rgb])
    band = None
    if sat >= LOW_SATURATION:
        index, name = band_for_hue(hue * 360.0)
        band = {"index": index, "name": name}
    return {"rgb": [int(round(v * 255)) for v in rgb], "level": round(level, 1),
            "hue": round(hue * 360.0, 1), "saturation": round(sat, 3), "band": band}
