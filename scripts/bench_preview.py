#!/usr/bin/env python3
"""
Micro-benchmark for the slider-drag render path (not run in CI).

    uv run python scripts/bench_preview.py [--runs 7]

"full" is the sharp 1400px render; "drag" is the smaller render used while a
slider is moving (the editor sends a sharp one when it settles). On code that
predates the drag render the two columns are the same full render.

Uses a synthetic 1400px photo (no raw file needed), warms the pipeline once,
then times a full preview render (composite + histogram + JPEG encode) for a
drag of each control: every tick changes only that one control.
"""
import argparse
import inspect
import statistics
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import editor  # noqa: E402


def synthetic_base(w=1400, h=933) -> np.ndarray:
    rng = np.random.default_rng(1)
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    img = np.stack([x / w, y / h, (x + y) / (w + h)], axis=2)
    img = 0.15 + 0.7 * img + rng.normal(0, 0.03, img.shape).astype(np.float32)
    return np.clip(img, 0, 1).astype(np.float32)


# (label, key, values to cycle through)
DRAGS = [
    ("exposure", "exposure", [0.1 * i for i in range(1, 40)]),
    ("contrast", "contrast", [2 * i for i in range(1, 40)]),
    ("saturation", "saturation", [2 * i for i in range(1, 40)]),
    ("HSL hue_red", "hue_red", [2 * i for i in range(1, 40)]),
    ("HSL sat_blue", "sat_blue", [2 * i for i in range(1, 40)]),
    ("grade_shadow_sat", "grade_shadow_sat", [2 * i for i in range(1, 40)]),
    ("clarity", "clarity", [2 * i for i in range(1, 40)]),
    ("sharpness", "sharpness", [3 * i for i in range(1, 40)]),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=7)
    args = ap.parse_args()

    base = synthetic_base()
    editor._load_base_render = lambda path: (base, 5500)
    photo = Path("/nonexistent/bench.CR2")
    # A realistic edited photo: several controls already off zero.
    start = {"exposure": 0.3, "contrast": 10, "highlights": -30, "shadows": 25,
             "saturation": 5, "clarity": 15, "sharpness": 40, "hue_orange": 8,
             "sat_orange": 10, "grade_highlight_hue": 40, "grade_highlight_sat": 20}

    editor.render_with_histogram(photo, dict(start))   # warm
    has_fast = "fast" in inspect.signature(editor.render_with_histogram).parameters
    kw = lambda fast: {"fast": fast} if has_fast else {}   # noqa: E731
    editor.render_with_histogram(photo, dict(start), **kw(True))
    print(f"{'drag of':<20}{'full median':>12}{'full max':>10}{'drag median':>13}{'drag max':>10}   (ms)")
    for label, key, values in DRAGS:
        row = []
        for fast in (False, True):
            times = []
            for v in values[: args.runs]:
                adj = dict(start)
                adj[key] = v
                t = time.perf_counter()
                editor.render_with_histogram(photo, adj, **kw(fast))
                times.append((time.perf_counter() - t) * 1000)
            row += [statistics.median(times), max(times)]
        print(f"{label:<20}{row[0]:>12.0f}{row[1]:>10.0f}{row[2]:>13.0f}{row[3]:>10.0f}")


if __name__ == "__main__":
    main()
