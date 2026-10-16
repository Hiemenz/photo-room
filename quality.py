#!/usr/bin/env python3
"""
Frame assessment, for culling a shoot.

Deliberately conservative about what it calls a problem. A verdict is only
returned for failures that are unambiguous from the pixels — a frame that
is entirely blown, entirely black, or carries no tonal range at all. Those
cannot be recovered by any adjustment, so flagging them is safe.

Sharpness is reported as a number and never as a verdict. Measured against
a real 431-frame polo shoot, the sharpest and softest-scoring frames
differed by 24x, and the lowest-scoring frame was a perfectly sharp
photograph of a horse on smooth grass. The metric tracks how much detail a
scene contains, not whether the photographer got focus — so it is useful
for ranking frames within one shoot and actively misleading as a threshold.
"""

from __future__ import annotations

import numpy as np
from PIL import Image


# Above this fraction of the frame at the top or bottom of the range, no
# exposure adjustment recovers anything: the sensel values are gone.
UNRECOVERABLE_FRACTION = 0.5

# Standard deviation of luminance below which a frame carries no image.
FLAT_CONTRAST = 0.01


def _luminance(img: np.ndarray) -> np.ndarray:
    return 0.2126 * img[:, :, 0] + 0.7152 * img[:, :, 1] + 0.0722 * img[:, :, 2]


def _laplacian(plane: np.ndarray) -> np.ndarray:
    """Four-neighbour Laplacian — the usual basis for a focus measure."""
    return (
        -4.0 * plane[1:-1, 1:-1]
        + plane[:-2, 1:-1] + plane[2:, 1:-1]
        + plane[1:-1, :-2] + plane[1:-1, 2:]
    )


# Sharpness is measured at a fixed working height. A Laplacian's magnitude
# depends on the pixel grid it is computed over, so without this the same
# photograph scores differently from a full-size decode than from a preview
# and the numbers cannot be compared between the editor and the CLI.
SHARPNESS_HEIGHT = 700


def sharpness(lum: np.ndarray, tiles: int = 8) -> float:
    """
    Detail in the most detailed region of the frame.

    Taking the peak tile rather than the whole frame stops a sharp subject
    against smooth grass or sky from scoring as low as a genuinely soft
    picture. It narrows the spread across a real shoot from roughly 24x to
    6x, but does not remove it — which is why this is a number to sort by,
    not a pass/fail test.
    """
    if abs(lum.shape[0] - SHARPNESS_HEIGHT) > 2:
        # Proper area resampling, not striding: dropping pixels aliases high
        # frequencies straight into the measurement, which is exactly what
        # this metric is reading.
        h, w = lum.shape
        target_w = max(8, int(round(w * SHARPNESS_HEIGHT / h)))
        as_u8 = np.clip(lum * 255.0 + 0.5, 0, 255).astype(np.uint8)
        resized = Image.fromarray(as_u8).resize(
            (target_w, SHARPNESS_HEIGHT), Image.BOX
        )
        lum = np.asarray(resized, dtype=np.float32) / 255.0

    detail = _laplacian(lum)
    h, w = detail.shape
    if h < tiles or w < tiles:
        return float(detail.var()) * 1e4

    th, tw = h // tiles, w // tiles
    best = 0.0
    for i in range(tiles):
        for j in range(tiles):
            tile = detail[i * th:(i + 1) * th, j * tw:(j + 1) * tw]
            if tile.size:
                best = max(best, float(tile.var()))
    return best * 1e4


def assess(rgb: np.ndarray) -> dict:
    """
    Describe a rendered frame.

    ``verdict`` is None whenever the frame is merely unusual rather than
    definitively unusable — the photographer decides those.
    """
    img = rgb.astype(np.float32) / 255.0 if rgb.dtype == np.uint8 else rgb
    lum = _luminance(img)

    clipped_high = float((lum >= 0.99).mean())
    clipped_low = float((lum <= 0.01).mean())
    contrast = float(lum.std())

    verdict = None
    reason = None
    if clipped_high >= UNRECOVERABLE_FRACTION:
        verdict = "blown"
        reason = f"{clipped_high:.0%} of the frame is at pure white"
    elif clipped_low >= UNRECOVERABLE_FRACTION:
        verdict = "black"
        reason = f"{clipped_low:.0%} of the frame is at pure black"
    elif contrast < FLAT_CONTRAST:
        verdict = "flat"
        reason = "the frame carries almost no tonal range"

    return {
        "clipped_high": round(clipped_high * 100, 2),
        "clipped_low": round(clipped_low * 100, 2),
        "contrast": round(contrast, 4),
        "sharpness": round(sharpness(lum), 2),
        "verdict": verdict,
        "reason": reason,
        "recoverable": verdict is None,
    }


def rank_by_sharpness(assessments: dict[str, dict]) -> list[tuple[str, float, float]]:
    """
    Order frames by sharpness relative to the rest of the shoot.

    Returns (name, score, percentile). Comparing within one shoot is the
    only way this number means anything: the same subject shot wide and
    tight produces very different absolute values.
    """
    usable = [(n, a["sharpness"]) for n, a in assessments.items() if a["recoverable"]]
    if not usable:
        return []

    scores = sorted(s for _, s in usable)
    out = []
    for name, score in usable:
        below = sum(1 for s in scores if s < score)
        out.append((name, score, round(below / len(scores) * 100, 1)))
    return sorted(out, key=lambda r: r[1])
