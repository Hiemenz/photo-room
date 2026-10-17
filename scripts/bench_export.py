#!/usr/bin/env python3
"""
End-to-end export benchmark (not run in CI).

    uv run python scripts/bench_export.py [--raw FILE] [--runs 3] [--profile light|heavy]

Exports one real raw (default: the CC0 Canon CR2 that tests/test_real_raw_e2e.py
caches under ~/.cache/photo-room-test-raw) to JPEG and PNG at full resolution,
including the exiftool metadata copy. Prints wall time and CPU time of the best
run for each of: cold (decode cache empty) and warm (decode cache hit).

Run it from a checkout of an older commit to get the "before" column; options
that older code lacks are skipped. The machine may be shared: CPU time is the
load-independent figure, wall time the one you feel.
"""
import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import exporter  # noqa: E402
from exporter import ExportOptions  # noqa: E402

DEFAULT_RAW = Path.home() / ".cache" / "photo-room-test-raw" / "IMG_9406.CR2"

PROFILES = {
    "light": {"exposure": 0.3, "contrast": 15, "saturation": 10, "color_temp": 5800},
    "heavy": {"exposure": 0.3, "contrast": 15, "shadows": 20, "highlights": -20, "saturation": 10,
              "vibrance": 10, "clarity": 20, "texture": 15, "sharpness": 30, "dehaze": 10,
              "hue_red": 10, "sat_blue": -10, "grade_shadow_sat": 20, "grade_shadow_hue": 240,
              "tone_curve": {"points": {"rgb": [[0, 0], [128, 140], [255, 255]]}}},
}


def timed(fn):
    c, t = time.process_time(), time.perf_counter()
    fn()
    return time.perf_counter() - t, time.process_time() - c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--profile", choices=PROFILES, default="heavy")
    ap.add_argument("--formats", default="jpeg,png")
    args = ap.parse_args()
    if not args.raw.exists():
        sys.exit(f"raw sample not found: {args.raw} (see tests/test_real_raw_e2e.py)")

    work = Path(tempfile.mkdtemp(prefix="bench-export-", dir=os.environ.get("TMPDIR")))
    cache = work / "cache"
    os.environ["PHOTO_ROOM_CACHE_DIR"] = str(cache)
    src = work / args.raw.name
    shutil.copy(args.raw, src)
    adj = PROFILES[args.profile]
    exporter._saved_adjustments = lambda path, *a, **k: dict(adj)
    print(f"load average {os.getloadavg()[0]:.1f} on {os.cpu_count()} cores; profile={args.profile}")
    print(f"{'format':8}{'decode':8}{'wall s':>9}{'cpu s':>9}")
    for fmt in args.formats.split(","):
        for label in ("cold", "warm"):
            best = None
            for _ in range(args.runs):
                if label == "cold":
                    shutil.rmtree(cache, ignore_errors=True)
                    os.environ["PHOTO_ROOM_DECODE_CACHE"] = "0"     # older code ignores it anyway
                else:
                    os.environ["PHOTO_ROOM_DECODE_CACHE"] = "1"
                out = work / f"out-{fmt}-{label}"
                shutil.rmtree(out, ignore_errors=True)
                opts = ExportOptions(format=fmt)
                r = timed(lambda: exporter.export_photo(src, out, opts))
                best = r if best is None or r[0] < best[0] else best
            if label == "warm" and not hasattr(exporter, "decode_cache"):
                continue
            print(f"{fmt:8}{label:8}{best[0]:9.2f}{best[1]:9.2f}")
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
