"""
Edit versions: which colour maths an edit was made under, and how to upgrade it.

Version 1 (everything before this module) applied the sliders to gamma-coded,
BT.709-curved values. Version 2 applies the light controls in linear light
(docs/COLOUR-PIPELINE.md). The same slider numbers therefore look different, so

* every saved edit records its version (``xmp:CreatorTool`` = ``photo-room (edit vN)``
  in the sidecar/DNG, an ``edit_version`` key in virtual copies, history entries
  and presets),
* an edit with no recorded version that does something is version 1 and keeps being
  rendered with the version 1 maths, so it looks exactly as it did,
* ``upgrade`` rewrites it for version 2: the slider values are mapped analytically
  and then refined against the version 1 render of the photo itself, and the
  residual difference is reported so the user can judge it. Nothing changes until
  the user asks (the editor offers a one-click "Upgrade edit").

Edits written by Lightroom or Camera Raw are version 2: their numbers were made
for Adobe's linear-light renderer, which version 2 approximates.
"""
from __future__ import annotations

import copy
import re

import numpy as np

import colour

LEGACY = 1
CURRENT = 2

TOOL_PREFIX = "photo-room (edit v"
_TOOL_RE = re.compile(r"photo-room \(edit v(\d+)\)")
FOREIGN_TOOLS = ("Lightroom", "Camera Raw", "Photoshop", "Bridge")

# Linear gain of a gamma-space +1 on mid-tones: (2 ** e) ** ~2.2.
EXPOSURE_FACTOR = 2.2
LOCAL_TEMP_FACTOR = 1.66


def tool_label(version: int = CURRENT) -> str:
    return f"{TOOL_PREFIX}{int(version)})"


def version_from_tool(creator_tool: str | None) -> int | None:
    """The edit version a CreatorTool string declares; None when it is not ours."""
    if not creator_tool:
        return None
    m = _TOOL_RE.search(str(creator_tool))
    return int(m.group(1)) if m else None


def version_for_stored(creator_tool: str | None, has_edits: bool, has_crs_version: bool = False) -> int:
    """
    Version of settings read back from a file.

    Ours says so. Adobe's (or any ``crs:Version`` we did not write) are version 2.
    Anything else that carries edits predates versioning: version 1. A file with
    nothing in it is current (there is nothing to preserve).
    """
    declared = version_from_tool(creator_tool)
    if declared is not None:
        return declared
    if creator_tool and any(name in creator_tool for name in FOREIGN_TOOLS):
        return CURRENT
    if has_crs_version:
        return CURRENT
    return LEGACY if has_edits else CURRENT


# ---------------------------------------------------------------------------
# Parameter mapping
# ---------------------------------------------------------------------------

def _clip(v: float, lo: float, hi: float) -> float:
    return float(min(hi, max(lo, v)))


def _temperature_for_ratio(target_rb: float, baseline: float) -> float:
    """Kelvin whose red/blue gain ratio (version 2) equals ``target_rb``."""
    def rb(t):
        g = colour.temperature_gains(t, baseline)
        return g[0] / g[2]
    lo, hi = colour.TEMP_RANGE
    if (rb(hi) - target_rb) * (rb(lo) - target_rb) > 0:
        return hi if abs(rb(hi) - target_rb) < abs(rb(lo) - target_rb) else lo
    for _ in range(50):
        mid = (lo + hi) / 2.0
        if (rb(mid) - target_rb) * (rb(lo) - target_rb) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2.0


def map_parameters(adj: dict, baseline_temp: float) -> dict:
    """
    Version 1 slider values -> the closest analytic version 2 values (before any
    render-based refinement). Never touches crop, curves, HSL or detail.
    """
    out = copy.deepcopy(adj)
    if out.get("exposure"):
        out["exposure"] = round(_clip(float(out["exposure"]) * EXPOSURE_FACTOR, -5.0, 5.0), 2)
    temp = float(out.get("color_temp", baseline_temp) or baseline_temp)
    if baseline_temp and abs(temp - baseline_temp) > 1:
        # Version 1 multiplied red by ratio**0.45 and blue by its inverse in gamma
        # space, i.e. red/blue by ratio**(2 * 0.45 * 2.2) in light.
        ratio = temp / baseline_temp
        target = ratio ** (2 * 0.45 * 2.2)
        out["color_temp"] = int(round(_temperature_for_ratio(target, baseline_temp)))
    for m in out.get("masks") or []:
        local = m.get("adj") if isinstance(m, dict) else None
        if not local:
            continue
        if local.get("exposure"):
            local["exposure"] = round(_clip(float(local["exposure"]) * EXPOSURE_FACTOR, -4.0, 4.0), 2)
        if local.get("temp"):
            local["temp"] = round(_clip(float(local["temp"]) * LOCAL_TEMP_FACTOR, -100, 100))
    out["edit_version"] = CURRENT
    return out


# ---------------------------------------------------------------------------
# Render-based refinement
# ---------------------------------------------------------------------------

FIT_EDGE = 320
_FIT_KEYS = ("exposure", "contrast", "shadows", "highlights", "whites", "blacks")
_FIT_STEPS = {"exposure": (0.3, 0.1, 0.03), "contrast": (16, 6, 2), "shadows": (16, 6, 2),
              "highlights": (16, 6, 2), "whites": (16, 6, 2), "blacks": (16, 6, 2)}
_FIT_RANGE = {"exposure": (-5.0, 5.0)}
_DROP_FOR_FIT = ("crop", "straighten", "spots", "lens_profile", "ca_remove", "masks", "strength",
                 "clarity", "texture", "dehaze", "sharpness", "luminance_nr", "_raw_frame")


def _fit_view(adj: dict, version: int) -> dict:
    view = {k: v for k, v in adj.items() if k not in _DROP_FOR_FIT and not str(k).startswith("xf_")}
    view["edit_version"] = version
    return view


def _small(base: np.ndarray) -> np.ndarray:
    h, w = base.shape[:2]
    scale = FIT_EDGE / max(h, w)
    if scale >= 1.0:
        return np.ascontiguousarray(base)
    import cv2
    return cv2.resize(base, (max(8, round(w * scale)), max(8, round(h * scale))),
                      interpolation=cv2.INTER_AREA)


def _error(a: np.ndarray, b: np.ndarray) -> float:
    """Mean absolute difference in 8-bit levels, channels averaged."""
    return float(np.abs(a - b).mean() * 255.0)


def upgrade(adj: dict, baseline_temp: float, base: np.ndarray, render) -> tuple[dict, dict]:
    """
    Upgrade a version 1 edit to version 2, keeping its look.

    ``render(base, baseline_temp, adj) -> float image`` is the editor's
    ``apply_adjustments``; ``base`` is the photo's neutral preview. Returns
    ``(new_adjustments, report)`` with the mean difference in 8-bit levels
    between the old render and the new one before and after refinement.
    An edit that is already version 2 comes back unchanged.
    """
    version = int(adj.get("edit_version") or LEGACY)
    if version >= CURRENT:
        return dict(adj), {"upgraded": False, "reason": "already current"}

    mapped = map_parameters(adj, baseline_temp)
    small = _small(base)
    target = render(small, baseline_temp, _fit_view(adj, LEGACY))
    best = dict(mapped)
    loss = lambda cand: _error(render(small, baseline_temp, _fit_view(cand, CURRENT)), target)   # noqa: E731
    before = loss(best)
    current = before

    # Coordinate descent over the light controls the user actually set (exposure always).
    fit_keys = [k for k in _FIT_KEYS if k == "exposure" or float(adj.get(k, 0) or 0)]
    for level in range(3):
        for key in fit_keys:
            step = _FIT_STEPS[key][level]
            lo, hi = _FIT_RANGE.get(key, (-100.0, 100.0))
            for sign in (1, -1):
                while True:
                    cand = dict(best)
                    value = _clip(float(cand.get(key, 0) or 0) + sign * step, lo, hi)
                    if value == float(cand.get(key, 0) or 0):
                        break
                    cand[key] = round(value, 2) if key == "exposure" else int(round(value))
                    err = loss(cand)
                    if err < current - 1e-4:
                        best, current = cand, err
                    else:
                        break
    if "exposure" in best:
        best["exposure"] = round(float(best["exposure"]), 2)
    best["edit_version"] = CURRENT
    report = {"upgraded": True, "mean_difference_before": round(before, 2),
              "mean_difference_after": round(current, 2), "levels": "8-bit"}
    return best, report
