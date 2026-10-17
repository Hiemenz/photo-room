#!/usr/bin/env python3
"""
Per-stage render benchmark (not run in CI).

    uv run python scripts/bench_render.py [--raw FILE] [--runs 3]

Times every stage of the develop pipeline at preview size (1400 px) and at full
resolution on a real raw (default: the CC0 CR2 cached by tests/test_real_raw_e2e.py;
a synthetic 10 MP image when absent): decode, resize, white balance + tone
+ curve, saturation, HSL, grading, detail (clarity/texture/dehaze/sharpen),
8-bit conversion, JPEG and PNG encode, and the exiftool copy. Each cell is the
best of N runs, "wall / cpu" in ms. Run from an older checkout for the "before"
column. CPU time is the figure to trust on a shared machine.
"""
import argparse
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import colormix  # noqa: E402
import editor  # noqa: E402

DEFAULT_RAW = Path.home() / ".cache" / "photo-room-test-raw" / "IMG_9406.CR2"
ADJ = {"exposure": 0.3, "contrast": 15, "shadows": 20, "highlights": -20, "saturation": 10, "vibrance": 10,
       "clarity": 20, "texture": 15, "sharpness": 30, "dehaze": 10, "hue_red": 10, "sat_blue": -10,
       "grade_shadow_sat": 20, "grade_shadow_hue": 240, "color_temp": 6500,
       "tone_curve": {"points": {"rgb": [[0, 0], [128, 140], [255, 255]]}}}


def best(fn, runs):
    w = c = 1e9
    for _ in range(runs):
        c0, t0 = time.process_time(), time.perf_counter()
        r = fn()
        w, c = min(w, time.perf_counter() - t0), min(c, time.process_time() - c0)
    return r, w * 1000, c * 1000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()
    rows = {}

    def rec(stage, size, w, c):
        rows.setdefault(stage, {})[size] = (w, c)

    full_u8 = None
    if args.raw.exists():
        import rawpy
        with rawpy.imread(str(args.raw)) as raw:
            full_u8, w, c = best(lambda: raw.postprocess(use_camera_wb=True, no_auto_bright=True, output_bps=8), 1)
            rec("decode (full, 8-bit)", "full", w, c)
            _, w, c = best(lambda: raw.postprocess(use_camera_wb=True, no_auto_bright=True, output_bps=8, half_size=True), args.runs)
            rec("decode (half-size, preview)", "preview", w, c)
    else:
        rng = np.random.default_rng(0)
        full_u8 = rng.integers(0, 255, (2602, 3906, 3), dtype=np.uint8)
    adj = editor.effective_adjustments(dict(ADJ))
    dest_dir = Path(tempfile.mkdtemp(prefix="bench-render-", dir=os.environ.get("TMPDIR")))

    for size in ("preview", "full"):
        if size == "preview":
            img = Image.fromarray(full_u8)
            base, w, c = best(lambda: np.asarray(img.resize((1400, round(1400 * full_u8.shape[0] / full_u8.shape[1])),
                                                            Image.LANCZOS), dtype=np.float32) / 255.0, args.runs)
            rec("resize to 1400 px (Lanczos)", size, w, c)
        else:
            base, w, c = best(lambda: np.asarray(full_u8, dtype=np.float32) / 255.0, args.runs)
            rec("8-bit -> float", size, w, c)
        x, w, c = best(lambda: editor._stage_tone(base, 5000, adj), args.runs); rec("white balance+exposure+tone+curve", size, w, c)
        x, w, c = best(lambda: editor._stage_saturation(x, adj), args.runs); rec("saturation/vibrance", size, w, c)
        x, w, c = best(lambda: colormix.apply_hsl(x, adj), args.runs); rec("HSL", size, w, c)
        x, w, c = best(lambda: colormix.apply_grading(x, adj), args.runs); rec("colour grading", size, w, c)
        x, w, c = best(lambda: editor._apply_detail(x, adj), args.runs); rec("clarity/texture/dehaze/sharpen", size, w, c)
        _, w, c = best(lambda: editor.masks.apply_masks(x, [{"type": "linear", "x": .5, "y": .5, "angle": 0, "feather": 50,
                                                           "adj": {"exposure": -0.5, "clarity": 20}}], blur=editor._blur), 1)
        rec("one graduated mask (exposure+clarity)", size, w, c)
        u8, w, c = best(lambda: (np.clip(x, 0, 1) * 255.0 + 0.5).astype(np.uint8), args.runs); rec("float -> 8-bit", size, w, c)
        pil = Image.fromarray(u8)
        _, w, c = best(lambda: pil.save(io.BytesIO(), "JPEG", quality=92, subsampling=0), args.runs); rec("JPEG encode q92", size, w, c)
        _, w, c = best(lambda: pil.save(io.BytesIO(), "PNG", compress_level=1), 1); rec("PNG encode level 1", size, w, c)
        _, w, c = best(lambda: pil.save(io.BytesIO(), "PNG"), 1); rec("PNG encode level 6 (old default)", size, w, c)
        if size == "full" and shutil.which("exiftool") and args.raw.exists():
            jpg = dest_dir / "o.jpg"
            pil.save(jpg, "JPEG", quality=92)
            _, w, c = best(lambda: subprocess.run(["exiftool", "-TagsFromFile", str(args.raw), "-all:all", "--ICC_Profile:all",
                                                   "-overwrite_original", str(jpg)], capture_output=True), 1)
            rec("exiftool metadata copy (JPEG)", size, w, c)
    shutil.rmtree(dest_dir, ignore_errors=True)
    print(f"load average {os.getloadavg()[0]:.1f}; cells are best-of-{args.runs}: wall ms / cpu ms")
    print(f"{'stage':40}{'1400 px':>20}{'full-res':>22}")
    for stage, cells in rows.items():
        fmt = lambda k: "%7.0f / %-7.0f" % cells[k] if k in cells else " " * 15
        print(f"{stage:40}{fmt('preview'):>20}{fmt('full'):>22}")


if __name__ == "__main__":
    main()
