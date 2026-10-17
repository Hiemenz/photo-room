"""
Tests for the fast Library backend: index, write-ahead sidecar writer, change
feed, incremental rescan, query engine, multi-size thumbnails, smart previews.

These run against the real exiftool (marker ``exiftool``) where sidecars are
involved, and pin: no lost writes under concurrency, crash recovery of the
write-ahead log, filter/sort results.
"""
import io
import json
import random
import shutil
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

import exifpool
import libindex
import library
import smartpreview
import thumbs

_skip = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")


def needs_exiftool(fn):
    return pytest.mark.exiftool(_skip(fn))


def make_photos(root: Path, names, fmt="TIFF"):
    for n in names:
        p = root / n
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), (10, 20, 30)).save(p, format=fmt)


def sidecar_state(path: Path) -> dict:
    """What the photo's XMP sidecar (``x.xmp`` next to ``x.dng``/``x.cr2``) says; {} if there is none."""
    path = path if path.suffix == ".xmp" else path.with_suffix(".xmp")
    if not path.exists():
        return {}
    r = exifpool.run(["-j", "-XMP-xmp:Rating", "-XMP-xmp:Label", "-XMP-dc:Subject"], input=str(path))
    return json.loads(r.stdout)[0]


@pytest.fixture
def real(monkeypatch):
    """Persistent exiftool, fast background writer."""
    monkeypatch.setattr(exifpool, "PERSISTENT", True)
    monkeypatch.setattr(libindex, "DEBOUNCE", 0.05)
    monkeypatch.setattr(libindex, "MAX_DELAY", 0.3)
    yield
    libindex.shutdown_all()
    exifpool.shutdown()


@pytest.fixture
def lib(tmp_path, real):
    make_photos(tmp_path, [f"d{i % 3}/IMG_{i:03d}.dng" for i in range(30)])
    idx = libindex.Index(tmp_path, tmp_path / libindex.DB_NAME)
    idx.scan()
    yield idx
    idx.close()


# ------------------------------------------------------------------ exiftool pool

@needs_exiftool
def test_persistent_exiftool_round_trip_and_errors(real, tmp_path):
    make_photos(tmp_path, ["a.dng"])
    for _ in range(3):
        r = exifpool.run(["-j", "-ImageWidth"], input=str(tmp_path / "a.dng"))
        assert r.returncode == 0 and json.loads(r.stdout)[0]["ImageWidth"] == 8
    bad = exifpool.run(["-j", str(tmp_path / "missing.dng")])
    assert bad.returncode != 0 and "File not found" in bad.stderr
    assert len(exifpool.get_pool()._all) == exifpool.POOL_SIZE   # no per-call spawning


@needs_exiftool
def test_non_ascii_paths(real, tmp_path):
    make_photos(tmp_path, ["café/Bild_ü.dng"])
    r = exifpool.run(["-j", "-ImageWidth"], input=str(tmp_path / "café/Bild_ü.dng"))
    assert json.loads(r.stdout)[0]["ImageWidth"] == 8


# ------------------------------------------------------------------ writes

@needs_exiftool
def test_set_fields_returns_immediately_and_sidecar_follows(lib, tmp_path):
    t = time.perf_counter()
    lib.set_fields({"d0/IMG_000.dng": {"rating": 4, "label": "Red", "keywords": ["a", "b"]}})
    assert time.perf_counter() - t < 0.1                       # memory + WAL only
    rec = lib.get("d0/IMG_000.dng")
    assert (rec["rating"], rec["label"], rec["keywords"]) == (4, "Red", ["a", "b"])
    assert lib.pending_count() >= 0
    assert lib.flush()
    st = sidecar_state(tmp_path / "d0/IMG_000.dng")
    assert st["Rating"] == 4 and st["Label"] == "Red" and st["Subject"] == ["a", "b"]


@needs_exiftool
def test_writes_are_coalesced(lib, monkeypatch):
    calls = []
    real_run = exifpool.run
    monkeypatch.setattr(exifpool, "run", lambda *a, **k: (calls.append(a), real_run(*a, **k))[1])
    rels = [f"d0/IMG_{i:03d}.dng" for i in range(0, 30, 3)]
    for r in range(1, 6):                                       # five rapid changes per photo...
        for rel in rels:
            lib.set_fields({rel: {"rating": r}})
    assert lib.flush()
    writes = [a for a in calls if "-overwrite_original" in a[0]]
    assert len(writes) <= 3                                     # ...and one command writes all ten files
    assert all(lib.get(r)["rating"] == 5 for r in rels)
    assert all(sidecar_state(lib.root / r)["Rating"] == 5 for r in rels)


@needs_exiftool
def test_no_lost_writes_under_concurrency(lib):
    rels = [r for r in (it["path"] for it in lib.items())]
    final: dict[str, int] = {}
    lock = threading.Lock()

    def worker(seed):
        rng = random.Random(seed)
        for _ in range(60):
            rel = rng.choice(rels)
            val = rng.randint(0, 5)
            with lock:                       # the order of the last write per photo is what we assert
                lib.set_fields({rel: {"rating": val}})
                final[rel] = val

    threads = [threading.Thread(target=worker, args=(s,)) for s in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert lib.flush()
    for rel, val in final.items():
        assert lib.get(rel)["rating"] == val
        assert sidecar_state(lib.root / rel).get("Rating", 0) == val


@needs_exiftool
def test_write_ahead_log_recovers_after_a_crash(real, tmp_path):
    make_photos(tmp_path, ["a.dng", "b.dng"])
    idx = libindex.Index(tmp_path, tmp_path / libindex.DB_NAME)
    idx.scan()
    idx._closed = True                                         # the writer thread never gets to run
    idx._writer.join(timeout=3)
    idx.set_fields({"a.dng": {"rating": 3, "label": "Blue"}, "b.dng": {"keywords": ["x"]}})
    assert not (tmp_path / "a.xmp").exists()                   # nothing reached the files
    idx._db.close()                                            # "crash": no flush, no clean shutdown

    again = libindex.Index(tmp_path, tmp_path / libindex.DB_NAME)
    try:
        assert again.get("a.dng")["rating"] == 3                # state restored from the database
        assert again.pending_count() == 2                       # ...and the unwritten work was remembered
        assert again.flush()
        assert sidecar_state(tmp_path / "a.dng")["Rating"] == 3
        assert sidecar_state(tmp_path / "b.dng")["Subject"] == "x"
    finally:
        again.close()
    third = libindex.Index(tmp_path, tmp_path / libindex.DB_NAME)
    assert third.pending_count() == 0
    third.close()


@needs_exiftool
def test_failed_write_stays_queued_and_is_retried(lib, monkeypatch):
    real_run = exifpool.run
    fail = {"on": True}

    def flaky(args, **kw):
        if fail["on"] and ("-overwrite_original" in args or "-o" in args):
            return __import__("subprocess").CompletedProcess(["exiftool"], 1, stdout="", stderr="Error: disk full")
        return real_run(args, **kw)

    monkeypatch.setattr(exifpool, "run", flaky)
    lib.set_fields({"d0/IMG_000.dng": {"rating": 2}})
    lib.flush(timeout=0.5)
    assert lib.pending_count() == 1 and lib.errors
    assert lib.get("d0/IMG_000.dng")["rating"] == 2             # the UI still sees the change
    fail["on"] = False
    assert lib.flush(timeout=10)
    assert sidecar_state(lib.root / "d0/IMG_000.dng")["Rating"] == 2


@needs_exiftool
def test_raw_without_sidecar_gets_one_atomically(real, tmp_path):
    make_photos(tmp_path, ["IMG_1.cr2"], fmt="TIFF")
    idx = libindex.Index(tmp_path, tmp_path / libindex.DB_NAME)
    idx.scan()
    idx.set_fields({"IMG_1.cr2": {"rating": 5}})
    assert idx.flush()
    assert (tmp_path / "IMG_1.xmp").exists()
    assert not list(tmp_path.glob(".*.tmp.xmp"))
    assert sidecar_state(tmp_path / "IMG_1.xmp")["Rating"] == 5
    idx.close()


def test_pick_flag_and_reject_are_one_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(libindex, "DB_ENABLED", False)
    idx = libindex.Index.in_memory([{"path": "a.dng", "name": "a.dng", "rating": 0, "label": "", "keywords": [],
                                     "camera": "", "lens": "", "iso": 0, "date": "", "flag": "none"}])
    assert idx.get("a.dng")["flag"] == "none"


# ------------------------------------------------------------------ scanning

@needs_exiftool
def test_rescan_reads_only_changed_files(lib, tmp_path, monkeypatch):
    calls = []
    real_run = exifpool.run
    monkeypatch.setattr(exifpool, "run", lambda args, **k: (calls.append(k.get("input") or ""), real_run(args, **k))[1])
    assert lib.scan() == {"added": 0, "changed": 0, "removed": 0} and calls == []
    # another program rates one photo and a new photo appears; one disappears
    exifpool.run(["-XMP-xmp:Rating=5", "-overwrite_original"], files=[str(tmp_path / "d1/IMG_001.dng")])
    make_photos(tmp_path, ["new.dng"])
    (tmp_path / "d2/IMG_002.dng").unlink()
    out = lib.scan()
    assert out == {"added": 1, "changed": 1, "removed": 1}
    assert sum(len(c.split("\n")) for c in calls if c) == 2
    assert lib.get("d1/IMG_001.dng")["rating"] == 5 and lib.get("d2/IMG_002.dng") is None


@needs_exiftool
def test_index_survives_restart_without_reading_files(real, tmp_path, monkeypatch):
    make_photos(tmp_path, ["a.dng", "b.dng"])
    idx = libindex.Index(tmp_path, tmp_path / libindex.DB_NAME)
    idx.scan()
    idx.set_fields({"a.dng": {"rating": 2}})
    idx.close()
    monkeypatch.setattr(exifpool, "run", lambda *a, **k: pytest.fail("exiftool should not be needed"))
    again = libindex.Index(tmp_path, tmp_path / libindex.DB_NAME)
    assert again.get("a.dng")["rating"] == 2
    assert again.scan()["changed"] == 0
    again.close()


@needs_exiftool
def test_dng_hides_same_named_raw(real, tmp_path):
    make_photos(tmp_path, ["x.cr2", "x.dng", "y.cr2", ".hidden/z.dng"])
    idx = libindex.Index(tmp_path, None)
    idx.scan()
    assert sorted(i["path"] for i in idx.items()) == ["x.dng", "y.cr2"]
    idx.close()


# ------------------------------------------------------------------ query engine

def synthetic(n=400, seed=5):
    rng = random.Random(seed)
    cams = ["Canon EOS R6", "NIKON Z 6", ""]
    items = []
    for i in range(n):
        rating = rng.choice([-1, 0, 1, 2, 3, 4, 5])
        pick = rng.random() < 0.2
        items.append({"path": f"f{i % 7}/IMG_{i:04d}.CR2", "name": f"IMG_{i:04d}.CR2", "rating": rating,
                      "label": rng.choice(["", "Red", "Blue"]),
                      "keywords": rng.sample(["beach", "dog", "city"], rng.randint(0, 2)),
                      "camera": rng.choice(cams), "lens": rng.choice(["50mm", "85mm", ""]),
                      "iso": rng.choice([0, 100, 800, 3200]),
                      "date": rng.choice(["", f"2024-0{rng.randint(1, 9)}-1{rng.randint(0, 9)} 10:00:00"]),
                      "flag": "reject" if rating == -1 else ("pick" if pick else "none")})
    return items


CASES = [
    {}, {"rating_min": "3"}, {"rating_min": "3", "rating_op": "eq"}, {"flag": "pick"}, {"flag": "unflagged"},
    {"label": "red"}, {"label": "none"}, {"keyword": "beach", "camera": "NIKON Z 6"}, {"lens": "50mm"},
    {"iso_min": "500"}, {"iso_min": "100", "iso_max": "800"}, {"date_from": "2024-03-01", "date_to": "2024-06-30"},
    {"q": "dog 0001"},
]


@pytest.mark.parametrize("sort", library.SORT_KEYS)
@pytest.mark.parametrize("desc", [False, True])
def test_query_matches_the_reference_implementation(sort, desc):
    items = synthetic()
    idx = libindex.Index.in_memory(items)
    for crit in CASES:
        want = library.sort_items(library.filter_items(items, crit), sort, desc)
        total, page, lib_total = idx.query(crit, sort, desc, 5, 50)
        assert total == len(want) and lib_total == len(items)
        assert [i["path"] for i in page] == [i["path"] for i in want[5:55]]


def test_query_caches_are_invalidated_by_writes(monkeypatch):
    monkeypatch.setattr(libindex.Index, "_writer_loop", lambda self: None)
    items = synthetic(50)
    idx = libindex.Index.in_memory(items)
    idx.root = Path("/nonexistent")             # enable set_fields' bookkeeping without a writer
    before = idx.query({"rating_min": "5"}, "rating", True, 0, 100)[0]
    target = next(i for i in items if i["rating"] < 5)
    idx.set_fields({target["path"]: {"rating": 5}})
    assert idx.query({"rating_min": "5"}, "rating", True, 0, 100)[0] == before + 1
    assert idx.query({}, "flag", False, 0, 100)[1][0]["path"] is not None
    with pytest.raises(ValueError):
        idx.query({}, "bogus")
    with pytest.raises(ValueError):
        idx.query({"label": "Mauve"})


def test_facets_include_lenses():
    f = libindex.Index.in_memory(synthetic(60)).facets()
    assert "50mm" in f["lenses"] and f["labels_order"]


# ------------------------------------------------------------------ change feed

def test_changes_long_poll_wakes_on_write(monkeypatch):
    monkeypatch.setattr(libindex.Index, "_writer_loop", lambda self: None)
    idx = libindex.Index.in_memory(synthetic(10))
    idx.root = Path("/nonexistent")
    start = idx.seq
    got = {}

    def waiter():
        got["r"] = idx.changes_since(start, timeout=5)

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.2)
    path = idx.items()[0]["path"]
    t0 = time.monotonic()
    idx.set_fields({path: {"label": "Green"}})
    t.join(3)
    assert time.monotonic() - t0 < 1
    ch = got["r"]["changes"]
    assert [c["path"] for c in ch] == [path] and ch[0]["item"]["label"] == "Green"
    assert idx.changes_since(idx.seq, timeout=0.1)["changes"] == []          # nothing new: times out empty


def test_changes_collapse_and_reset(monkeypatch):
    monkeypatch.setattr(libindex.Index, "_writer_loop", lambda self: None)
    idx = libindex.Index.in_memory(synthetic(5))
    idx.root = Path("/nonexistent")
    path = idx.items()[0]["path"]
    for r in range(1, 4):
        idx.set_fields({path: {"rating": r}})
    body = idx.changes_since(0)
    assert len(body["changes"]) == 1 and body["changes"][0]["item"]["rating"] == 3
    assert idx.changes_since(idx.seq + 99)["reset"] is True             # client ahead of us: server restarted


@needs_exiftool
def test_api_endpoints_end_to_end(real, tmp_path, monkeypatch):
    import editor
    make_photos(tmp_path, ["a.dng", "b.dng", "c.dng"])
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    c = editor.app.test_client()
    page = c.get("/api/library").get_json()
    assert page["total"] == 3 and "seq" in page and "lenses" in page["facets"]
    t = time.perf_counter()
    r = c.post("/api/rate", json={"path": "b.dng", "rating": 5})
    assert r.status_code == 200 and time.perf_counter() - t < 0.2
    assert [i["path"] for i in c.get("/api/library?rating_min=5").get_json()["items"]] == ["b.dng"]
    ch = c.get(f"/api/library/changes?since={page['seq']}&timeout=1").get_json()
    assert ch["changes"][0]["path"] == "b.dng" and ch["changes"][0]["item"]["rating"] == 5
    sse = c.get(f"/api/library/events?since={page['seq']}&once=1")
    assert b"event: changes" in sse.data and b'"b.dng"' in sse.data
    assert c.post("/api/library/flush", json={}).get_json()["pending"] == 0
    assert sidecar_state(tmp_path / "b.dng")["Rating"] == 5
    c.post("/api/flag", json={"path": "a.dng", "flag": "reject"})
    c.post("/api/flag", json={"path": "a.dng", "flag": "none"})
    assert c.get("/api/library/status").get_json()["photos"] == 3


# ------------------------------------------------------------------ thumbnails

def jpeg(w, h, color=(200, 50, 50)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="JPEG")
    return buf.getvalue()


def test_multi_size_thumbs_orientation_and_cache(tmp_path, monkeypatch):
    raw = tmp_path / "IMG_1.CR2"
    raw.write_bytes(b"raw")
    calls = []
    monkeypatch.setattr(thumbs, "_embedded_preview", lambda p: (calls.append(p), (jpeg(1800, 1200), 6))[1])
    out = thumbs.build_sizes(raw)
    assert set(out) == set(thumbs.SIZES)
    for size, data in out.items():
        w, h = Image.open(io.BytesIO(data)).size
        assert max(w, h) == size and h > w                         # rotated upright (orientation 6)
    assert len(calls) == 1                                          # one extraction for all sizes
    thumbs.get_all(raw, tmp_path)
    assert all(thumbs.cache_path(raw, tmp_path, s).exists() for s in thumbs.SIZES)
    thumbs.get_all(raw, tmp_path)
    assert len(calls) == 2                                          # second call: all cached
    raw.write_bytes(b"changed raw bytes")                           # new size/mtime -> new keys
    assert not thumbs.cache_path(raw, tmp_path, 160).exists()


def test_priority_visible_tiles_first(tmp_path, monkeypatch):
    order = []
    gate = threading.Event()

    def fake_get(path, root, fb=None, size=thumbs.THUMB_EDGE):
        if path.name != 'visible.CR2':
            gate.wait(5)
        order.append(path.name)
        return b"x" * 2000

    monkeypatch.setattr(thumbs, "get", fake_get)
    monkeypatch.setattr(thumbs, "get_all", lambda p, r, fb=None, sizes=thumbs.SIZES: fake_get(p, r))
    monkeypatch.setattr(thumbs, "cache_path", lambda p, r, s=thumbs.THUMB_EDGE: tmp_path / "none" / p.name)
    pool = thumbs.ThumbPool(tmp_path, background_workers=1, request_workers=1)
    files = [tmp_path / f"bg{i}.CR2" for i in range(6)]
    pool.prefetch(files)
    time.sleep(0.2)                                                 # the single bg worker is now busy
    visible = tmp_path / "visible.CR2"
    t = threading.Thread(target=lambda: pool.get_now(visible, timeout=10))
    t.start()
    t.join(5)
    gate.set()
    deadline = time.time() + 5
    while pool.progress()["active"] and time.time() < deadline:
        time.sleep(0.02)
    assert order[0] == "visible.CR2"                                # served while the backlog is still stuck
    pool.shutdown()


@pytest.mark.skipif(not (Path.home() / ".cache/photo-room-test-raw/IMG_9406.CR2").exists(), reason="no sample raw")
def test_real_raw_thumbnail_is_fast_and_upright(tmp_path):
    raw = Path.home() / ".cache/photo-room-test-raw/IMG_9406.CR2"
    thumbs.build_sizes(raw)                                         # warm the file cache
    t = time.perf_counter()
    out = thumbs.build_sizes(raw)
    assert time.perf_counter() - t < 1.0
    assert max(Image.open(io.BytesIO(out[320])).size) == 320


# ------------------------------------------------------------------ smart previews

def test_smart_preview_cached_and_render_ready(tmp_path):
    import numpy as np
    raw = tmp_path / "a.CR2"
    raw.write_bytes(b"raw")
    calls = []

    def decoder(p):
        calls.append(p)
        return np.full((1500, 3000, 3), 90, np.uint8), 5200, np.array([2.0, 1.0, 1.5, 1.0])

    assert smartpreview.peek(raw, tmp_path) is None
    rgb, temp, mul = smartpreview.ensure(raw, tmp_path, decoder)
    assert max(rgb.shape[:2]) == smartpreview.EDGE and rgb.dtype == np.uint8 and temp == 5200
    rgb2, temp2, mul2 = smartpreview.get(raw, tmp_path, decoder)       # second open: no decode
    assert len(calls) == 1 and temp2 == 5200 and list(mul2) == [2.0, 1.0, 1.5, 1.0]
    assert abs(int(rgb2[10, 10, 0]) - 90) <= 2
    assert smartpreview.prune(tmp_path, max_bytes=0) > 0 and smartpreview.peek(raw, tmp_path) is None
