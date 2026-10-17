#!/usr/bin/env python3
"""
Benchmark the Library backend (not run in CI).

    uv run python scripts/bench_library.py
    uv run python scripts/bench_library.py --photos 2000 --real 100 --index-size 20000

Builds a synthetic library (hard-linked copies of a real raw sample plus tiny
synthetic DNGs whose EXIF/XMP is written with the real exiftool), drives the
real Flask app through its test client and prints median / p95 latencies.
It only uses endpoints that exist on both the old and the new backend, so the
same script gives the "before" and "after" numbers; sections that need new
modules are skipped when they are absent.

Needs exiftool on PATH. Everything is created under --work (default
~/.cache/photo-room-bench) and removed afterwards unless --keep is given.
"""
from __future__ import annotations

import argparse
import csv
import os
import random
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CAMERAS = ["Canon EOS 5D Mark IV", "Canon EOS R6", "NIKON Z 6", "SONY ILCE-7M3", "FUJIFILM X-T4"]
LENSES = ["EF24-70mm f/2.8L", "RF 85mm F1.2", "NIKKOR Z 50mm f/1.8", "FE 35mm F1.8"]
WORDS = ["beach", "sunset", "family", "portrait", "street", "mountain", "city", "night", "wedding", "dog"]
LABELS = ["", "", "", "Red", "Yellow", "Green", "Blue", "Purple"]


def stats(samples: list[float]) -> str:
    s = sorted(samples)
    p95 = s[min(len(s) - 1, int(len(s) * 0.95))]
    return f"median {statistics.median(s) * 1000:8.1f} ms   p95 {p95 * 1000:8.1f} ms   n={len(s)}"


def report(name: str, samples: list[float]) -> None:
    print(f"  {name:<44} {stats(samples)}", flush=True)


def timed(fn) -> float:
    t = time.perf_counter()
    fn()
    return time.perf_counter() - t


def _has(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Library fixture
# ---------------------------------------------------------------------------

def build_library(root: Path, sample: Path, photos: int, real: int) -> None:
    from PIL import Image

    rng = random.Random(7)
    root.mkdir(parents=True, exist_ok=True)
    for i in range(real):
        day = root / f"2024-05-{1 + i % 20:02d}"
        day.mkdir(exist_ok=True)
        dest = day / f"IMG_{i:05d}.CR2"
        try:
            os.link(sample, dest)           # same bytes, own path: costs no disk
        except OSError:
            shutil.copy(sample, dest)

    tiny = root.parent / "_tiny.tif"
    Image.new("RGB", (16, 16), (120, 130, 140)).save(tiny, format="TIFF")
    rows = []
    for i in range(photos - real):
        day = root / f"2024-06-{1 + i % 28:02d}"
        day.mkdir(exist_ok=True)
        dest = day / f"DSC_{i:05d}.dng"
        shutil.copy(tiny, dest)
        rows.append({
            "SourceFile": str(dest),
            "Model": rng.choice(CAMERAS),
            "LensModel": rng.choice(LENSES),
            "ISO": rng.choice([100, 200, 400, 800, 1600, 3200, 6400]),
            "DateTimeOriginal": f"2024:06:{1 + i % 28:02d} {i % 24:02d}:{i % 60:02d}:00",
            "Rating": rng.choice([0, 0, 0, 1, 2, 3, 4, 5]),
            "Label": rng.choice(LABELS),
            "Subject": ", ".join(rng.sample(WORDS, rng.randint(0, 3))),
        })
    tiny.unlink()
    if rows:
        csv_path = root.parent / "bench-meta.csv"
        with open(csv_path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        subprocess.run(
            ["exiftool", "-q", "-overwrite_original", f"-csv={csv_path}", "-sep", ", ",
             "-XMP-xmp:Rating<Rating", "-XMP-xmp:Label<Label", "-XMP-dc:Subject<Subject", "-r", str(root)],
            check=True, capture_output=True)
        csv_path.unlink()


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def bench_api(root: Path, quick: bool) -> None:
    import editor

    editor.LIBRARY_ROOT = root
    editor.app.config["TESTING"] = True
    client = editor.app.test_client()
    flush = None
    if _has("libindex"):
        import libindex
        flush = lambda: libindex.flush_all()    # noqa: E731

    print("\nLibrary listing", flush=True)
    t = timed(lambda: client.get("/api/library?limit=200"))
    print(f"  {'first /api/library (cold, nothing cached)':<44} {t * 1000:8.1f} ms", flush=True)
    report("/api/library warm, page 1", [timed(lambda: client.get("/api/library?limit=200")) for _ in range(15)])
    report("/api/library filter rating>=4 + sort date",
           [timed(lambda: client.get("/api/library?rating_min=4&sort=date&dir=desc&limit=200")) for _ in range(15)])
    report("/api/library text search 'canon'",
           [timed(lambda: client.get("/api/library?q=canon&limit=200")) for _ in range(15)])

    items = client.get("/api/library?limit=1000&offset=0").get_json()["items"]
    paths = [i["path"] for i in items]
    dngs = [p for p in paths if p.endswith(".dng")]
    raws = [i["path"] for i in client.get("/api/library?q=IMG_&limit=1000").get_json()["items"]
            if i["path"].upper().endswith(".CR2")]
    n = 15 if quick else 40
    rng = random.Random(3)

    print("\nWrites (latency until the response arrives)", flush=True)
    report("rate (dng)", [timed(lambda p=rng.choice(dngs), r=rng.randint(0, 5):
                                client.post("/api/rate", json={"path": p, "rating": r})) for _ in range(n)])
    if raws:
        report("rate (raw, writes a .xmp sidecar)", [timed(lambda p=raws[i % len(raws)], r=1 + i % 5:
                                                           client.post("/api/rate", json={"path": p, "rating": r}))
                                                     for i in range(n)])
    report("flag pick", [timed(lambda p=rng.choice(paths):
                               client.post("/api/flag", json={"path": p, "flag": "pick"})) for _ in range(n)])
    report("label", [timed(lambda p=rng.choice(paths), lb=rng.choice(["red", "blue", "none"]):
                           client.post("/api/label", json={"path": p, "label": lb})) for _ in range(n)])
    report("keywords add", [timed(lambda p=rng.choice(paths), w=rng.choice(WORDS):
                                  client.post("/api/keywords", json={"path": p, "add": [w]})) for _ in range(n)])
    report("rate 50 photos in a burst (total)", [timed(lambda: [
        client.post("/api/rate", json={"path": p, "rating": 3}) for p in paths[:50]])])
    if flush:
        t = timed(flush)
        print(f"  {'background flush of everything queued':<44} {t * 1000:8.1f} ms", flush=True)

    print("\nThumbnails (real raw files)", flush=True)
    if raws:
        sample = raws[:40]
        report("thumb cold, one by one", [timed(lambda p=p: client.get(f"/api/thumb?path={p}")) for p in sample])
        report("thumb warm (disk cache)", [timed(lambda p=p: client.get(f"/api/thumb?path={p}")) for p in sample])


def bench_index(size: int) -> None:
    """Pure query cost on a synthetic index of ``size`` photos (no files)."""
    import library

    rng = random.Random(11)
    items = []
    for i in range(size):
        d = f"2024-{1 + i % 12:02d}-{1 + i % 28:02d} {i % 24:02d}:00:00"
        items.append({"path": f"2024/{i % 365:03d}/IMG_{i:06d}.CR2", "name": f"IMG_{i:06d}.CR2",
                      "rating": rng.choice([-1, 0, 0, 1, 2, 3, 4, 5]), "label": rng.choice(LABELS),
                      "keywords": rng.sample(WORDS, rng.randint(0, 3)), "camera": rng.choice(CAMERAS),
                      "lens": rng.choice(LENSES), "iso": rng.choice([100, 400, 1600, 6400]), "date": d,
                      "flag": rng.choice(["none", "none", "pick"])})
    print(f"\nQuery engine on a synthetic {size}-photo index", flush=True)
    queries = {
        "all, sort name, page 1": ({}, "name", False),
        "rating>=4, sort date desc": ({"rating_min": "4"}, "date", True),
        "keyword + camera, sort rating": ({"keyword": "beach", "camera": CAMERAS[0]}, "rating", True),
        "text 'canon beach'": ({"q": "canon beach"}, "name", False),
        "date range + label": ({"date_from": "2024-03-01", "date_to": "2024-05-31", "label": "Red"}, "date", False),
    }
    if _has("libindex"):
        import libindex
        idx = libindex.Index.in_memory(items)
        for name, (crit, key, desc) in queries.items():
            cold = timed(lambda: idx.query(crit, key, desc, 0, 200))
            warm = [timed(lambda: idx.query(crit, key, desc, 200, 200)) for _ in range(10)]
            fresh = []
            for _ in range(5):
                idx.touch()                       # a write happened: caches are stale
                fresh.append(timed(lambda: idx.query(crit, key, desc, 0, 200)))
            print(f"  {name:<34} first {cold * 1000:7.1f} ms   next page {statistics.median(warm) * 1000:6.2f} ms"
                  f"   after a write {statistics.median(fresh) * 1000:7.1f} ms", flush=True)
    else:
        for name, (crit, key, desc) in queries.items():
            def run():
                m = library.filter_items(items, crit)
                m = library.sort_items(m, key, desc)
                return m[:200]
            samples = [timed(run) for _ in range(5)]
            print(f"  {name:<34} every request {statistics.median(samples) * 1000:7.1f} ms", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=Path, default=Path.home() / ".cache/photo-room-test-raw/IMG_9406.CR2")
    ap.add_argument("--photos", type=int, default=2000)
    ap.add_argument("--real", type=int, default=100, help="how many photos are copies of the real raw")
    ap.add_argument("--index-size", type=int, default=20000)
    ap.add_argument("--work", type=Path, default=Path.home() / ".cache/photo-room-bench")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--skip-api", action="store_true")
    a = ap.parse_args()

    if not a.skip_api:
        if a.work.exists():
            shutil.rmtree(a.work)
        a.work.mkdir(parents=True)
        root = a.work / "library"
        t = time.perf_counter()
        build_library(root, a.sample, a.photos, a.real)
        print(f"built {a.photos}-photo library in {time.perf_counter() - t:.1f}s ({a.real} real raws)", flush=True)
        try:
            bench_api(root, a.quick)
        finally:
            if _has("libindex"):
                import libindex
                libindex.shutdown_all()
            if not a.keep:
                shutil.rmtree(a.work, ignore_errors=True)
    bench_index(a.index_size)


if __name__ == "__main__":
    main()
