"""
Stage cache for the preview pipeline.

A slider drag changes one control, yet the preview used to redo every stage
from the neutral render. Each stage of ``editor.apply_adjustments`` now stores
its output under a key built from the settings of every stage up to and
including itself, so a re-render restarts at the first stage whose settings
changed and reuses everything upstream.

Correctness rests on one rule: a stage's key must include every setting that
can influence its output or anything upstream of it. ``stage_keys`` guards that
by building the first stage's key from "everything except the settings owned
by later stages", so a setting nobody listed here can only cause a cache miss,
never a stale image.

Memory is bounded by bytes (``MAX_BYTES``), evicting least recently used.
"""
from __future__ import annotations

import json
import threading
from collections import OrderedDict

import effects
import profiles

# Stage order. Index = position in the pipeline.
GEOMETRY, TONE, SATURATION, MIX, GRADE, DETAIL, FINAL = range(7)
STAGE_NAMES = ["geometry", "tone", "saturation", "mix", "grade", "detail", "final"]

_BANDS = ["red", "orange", "yellow", "green", "aqua", "blue", "purple", "magenta"]
_RANGES = ["shadow", "mid", "highlight", "global"]

_CALIBRATION = frozenset(k for k in effects.DEFAULTS if k.startswith("cal_"))
_SHARPEN = frozenset(k for k in effects.DEFAULTS if k.startswith("sharpen_"))
_FINISHING = frozenset(k for k in effects.DEFAULTS if k.startswith(("grain_", "vignette_")))

# Settings owned by each stage after geometry. Geometry owns everything else
# (spots, lens, transform, straighten, crop, ...).
STAGE_FIELDS: dict[int, frozenset] = {
    TONE: frozenset({"color_temp", "tint", "exposure", "shadows", "highlights",
                     "blacks", "whites", "contrast", "tone_curve"} | _CALIBRATION),
    SATURATION: frozenset({"saturation", "vibrance"}),
    MIX: frozenset({"monochrome", "point_colors"}
                   | {f"{p}_{b}" for p in ("hue", "sat", "lum", "gray") for b in _BANDS}),
    GRADE: frozenset({"monochrome", "grade_blending", "grade_balance"}
                     | {f"grade_{r}_{c}" for r in _RANGES for c in ("hue", "sat", "lum")}),
    # The LUT runs at the start of the detail stage (on the finished colour edit), so its
    # settings belong to DETAIL and to everything after it. Its own sub-result is cached
    # under the GRADE key, so the intensity slider only re-blends.
    DETAIL: frozenset({"clarity", "texture", "dehaze", "sharpness", "lut", "lut_amount",
                       "luminance_nr", "color_nr"} | _SHARPEN),
    # Vignette and grain run after the local masks and are not stored per stage; their keys are here so
    # every setting that reaches a pixel is owned by a stage (and never lands in the geometry key).
    FINAL: frozenset({"masks", "strength", "monochrome", "grade_blending", "grade_balance", "lut", "lut_amount"}
                     | _FINISHING
                     | {f"grade_{r}_{c}" for r in _RANGES for c in ("hue", "sat", "lum")}),
}
# Settings that never reach a preview pixel (none today: noise reduction is part of DETAIL).
IGNORED: frozenset = frozenset()


def _dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))


def stage_keys(adj: dict, baseline_temp) -> list[str]:
    """Cumulative cache key for every stage (see module docstring)."""
    owned = set(IGNORED)
    for fields in STAGE_FIELDS.values():
        owned |= fields
    # The camera profile (a geometry-level setting: it sits right after the camera matrix) is in the
    # dict dump above by id; its revision also covers an imported file being replaced or deleted.
    keys = [_dumps({k: v for k, v in adj.items() if k not in owned}) + f"|bt={baseline_temp}"
            + f"|cp={profiles.revision(adj.get('camera_profile'))}"]
    for stage in range(TONE, FINAL + 1):
        fields = STAGE_FIELDS[stage]
        part = {k: adj[k] for k in sorted(fields) if k in adj}
        keys.append(keys[-1] + "|" + STAGE_NAMES[stage] + _dumps(part))
    return keys


class StageCache:
    """Byte-bounded LRU of stage outputs, keyed (photo, stage, key)."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self._items: OrderedDict[tuple, tuple[object, int]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, photo: str, stage, key: str):
        with self._lock:
            item = self._items.get((photo, stage, key))
            if item is None:
                self.misses += 1
                return None
            self._items.move_to_end((photo, stage, key))
            self.hits += 1
            return item[0]

    def put(self, photo: str, stage, key: str, value) -> None:
        size = int(value.nbytes)
        if size > self.max_bytes:
            return
        if hasattr(value, "flags"):
            value.flags.writeable = False    # a shared array must never be mutated
        with self._lock:
            old = self._items.pop((photo, stage, key), None)
            if old:
                self._bytes -= old[1]
            self._items[(photo, stage, key)] = (value, size)
            self._bytes += size
            while self._bytes > self.max_bytes and self._items:
                _, (_, sz) = self._items.popitem(last=False)
                self._bytes -= sz

    def drop_photo(self, photo: str) -> None:
        with self._lock:
            for k in [k for k in self._items if k[0] == photo]:
                self._bytes -= self._items.pop(k)[1]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0

    def stats(self) -> dict:
        with self._lock:
            return {"entries": len(self._items), "bytes": self._bytes,
                    "limit": self.max_bytes, "hits": self.hits, "misses": self.misses}
