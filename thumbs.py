#!/usr/bin/env python3
"""
Thumbnails for browsing a shoot.

Cameras embed a full-size JPEG preview in every raw file, so a thumbnail is
cut from that rather than demosaicing the sensor data. The preview is pulled
out with ``rawpy.extract_thumb()`` (libraw reads the embedded JPEG in about a
millisecond when the file is cached by the OS), falling back to
``exiftool -b -PreviewImage`` and, for files with no preview at all, to a
half-size decode supplied by the caller. The JPEG is decoded with PIL's DCT
scaling (``draft``), which skips most of the work for small sizes, and the
camera orientation is applied: from the preview's own EXIF if it has one,
else from the raw's orientation as libraw reports it.

Three sizes are cached on disk (160 for dense grids, 320 for the normal grid,
1024 for loupe/compare), each keyed by the source's path, size and mtime so a
thumbnail is built once, survives restarts, and a changed file is rebuilt
rather than served stale. Building a missing size never blocks other work:
``ThumbPool`` runs jobs on worker threads from a priority queue, so tiles
that are on screen are built before the rest of the library.
"""

from __future__ import annotations

import hashlib
import heapq
import io
import itertools
import os
import subprocess
import tempfile
import threading
from pathlib import Path

import numpy as np
from PIL import Image

try:                      # optional: without rawpy the exiftool route is used
    import rawpy
except ImportError:       # pragma: no cover
    rawpy = None

# Bounds a hang reading a file's embedded preview.
EXIFTOOL_TIMEOUT = 120

SIZES = (160, 320, 1024)
THUMB_EDGE = 320            # the default grid size
THUMB_QUALITY = 74
CACHE_DIR_NAME = ".photo-room-thumbs"

# EXIF orientation -> PIL transpose op (None = leave as is)
_ORIENT_OPS = {
    2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180, 4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE, 6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}
# libraw's "flip" value -> EXIF orientation
_FLIP_TO_ORIENT = {0: 1, 3: 3, 5: 8, 6: 6}


def _cache_key(path: Path) -> str:
    """Identity of a file's content, cheaply: path, size and mtime."""
    try:
        stat = path.stat()
        seed = f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        seed = str(path)
    return hashlib.sha256(seed.encode()).hexdigest()[:24]


def cache_path(path: Path, root: Path, size: int = THUMB_EDGE) -> Path:
    name = _cache_key(path) + ("" if size == THUMB_EDGE else f"_{size}")
    return root / CACHE_DIR_NAME / f"{name}.jpg"


def _embedded_preview(path: Path) -> tuple[bytes | None, int | None]:
    """
    The camera's own JPEG preview and, when the raw file knows it, its
    orientation (EXIF numbering). ``(None, None)`` when the file has no
    preview — some converted DNGs do not — so the caller can decode instead.
    """
    if rawpy is not None:
        try:
            with rawpy.imread(str(path)) as raw:
                th = raw.extract_thumb()
                if th.format == rawpy.ThumbFormat.JPEG and len(th.data) > 1024:
                    return bytes(th.data), _FLIP_TO_ORIENT.get(int(raw.sizes.flip), 1)
                if th.format == rawpy.ThumbFormat.BITMAP:
                    buf = io.BytesIO()
                    Image.fromarray(th.data).save(buf, format="JPEG", quality=90)
                    return buf.getvalue(), _FLIP_TO_ORIENT.get(int(raw.sizes.flip), 1)
        except Exception:
            pass          # not a raw libraw knows, or no thumbnail: try exiftool
    for tag in ("-PreviewImage", "-JpgFromRaw", "-ThumbnailImage"):
        result = subprocess.run(
            ["exiftool", "-b", tag, str(path)], capture_output=True, timeout=EXIFTOOL_TIMEOUT
        )
        if result.returncode == 0 and len(result.stdout) > 1024:
            return result.stdout, _exiftool_orientation(path)
    return None, None


def _from_embedded_preview(path: Path) -> bytes | None:
    return _embedded_preview(path)[0]


def _exiftool_orientation(path: Path) -> int | None:
    try:
        out = subprocess.run(["exiftool", "-n", "-s3", "-Orientation", str(path)],
                             capture_output=True, text=True, timeout=EXIFTOOL_TIMEOUT).stdout
        value = int(str(out).strip())
        return value if 1 <= value <= 8 else None
    except Exception:
        return None


def _open_preview(data: bytes, orientation: int | None, want: int) -> Image.Image:
    """Decode a preview at about ``want`` pixels (DCT scaling) with orientation applied."""
    img = Image.open(io.BytesIO(data))
    own = img.getexif().get(274)             # the preview's own orientation wins over the raw's
    if own in _ORIENT_OPS or own == 1:
        orientation = own
    img.draft("RGB", (want * 2, want * 2))   # never below 2x the target: keeps the resize sharp
    img = img.convert("RGB")
    op = _ORIENT_OPS.get(orientation or 1)
    return img.transpose(op) if op is not None else img


def _encode(img: Image.Image, size: int = THUMB_EDGE) -> bytes:
    img = img.convert("RGB")
    img.thumbnail((size, size), Image.LANCZOS, reducing_gap=2.0)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=THUMB_QUALITY if size <= 320 else 82)
    return buf.getvalue()


def build_sizes(path: Path, sizes=SIZES, decode_fallback=None) -> dict[int, bytes]:
    """Thumbnails of one raw at several sizes, extracting/decoding the source once."""
    sizes = sorted(set(sizes))
    data, orientation = _embedded_preview(path)
    if data:
        try:
            base = _open_preview(data, orientation, max(sizes))
            return {s: _encode(base.copy() if s != sizes[-1] else base, s) for s in reversed(sizes)}
        except Exception:
            pass  # a malformed preview is not a reason to give up on the file

    if decode_fallback is None:
        raise RuntimeError(f"No embedded preview in {path.name}")

    rgb = decode_fallback(path)
    if rgb.dtype != np.uint8:
        rgb = np.clip(rgb * 255.0 + 0.5, 0, 255).astype(np.uint8)
    base = Image.fromarray(rgb)
    return {s: _encode(base.copy(), s) for s in sizes}


def build(path: Path, decode_fallback=None, size: int = THUMB_EDGE) -> bytes:
    """Produce thumbnail bytes for one raw file."""
    return build_sizes(path, (size,), decode_fallback)[size]


def _store(cached: Path, data: bytes) -> None:
    try:
        cached.parent.mkdir(parents=True, exist_ok=True)
        # A unique temporary name per writer. A fixed one raced: the grid
        # lazy-loads in bursts, so two requests for the same photo both wrote
        # the same path and could promote interleaved bytes — which the
        # immutable cache header would then serve forever.
        fd, tmp_name = tempfile.mkstemp(dir=cached.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp_name, cached)
        except OSError:
            Path(tmp_name).unlink(missing_ok=True)
            raise
    except OSError:
        pass  # an unwritable library still browses, just without caching


def get(path: Path, root: Path, decode_fallback=None, size: int = THUMB_EDGE) -> bytes:
    """Return a thumbnail, building and caching it if needed."""
    cached = cache_path(path, root, size)
    if cached.exists():
        try:
            return cached.read_bytes()
        except OSError:
            pass

    data = build(path, decode_fallback, size)
    _store(cached, data)
    return data


def get_all(path: Path, root: Path, decode_fallback=None, sizes=SIZES) -> None:
    """Make sure every size is cached, extracting the preview only once."""
    missing = [s for s in sizes if not cache_path(path, root, s).exists()]
    if not missing:
        return
    for s, data in build_sizes(path, missing, decode_fallback).items():
        _store(cache_path(path, root, s), data)


def clear_cache(root: Path) -> int:
    """Delete every cached thumbnail. Returns how many were removed."""
    cache = root / CACHE_DIR_NAME
    if not cache.is_dir():
        return 0

    removed = 0
    for f in cache.glob("*.jpg"):
        try:
            f.unlink()
            removed += 1
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------------------
# Background generation
# ---------------------------------------------------------------------------

class ThumbPool:
    """
    Builds thumbnails on worker threads from a priority queue, so a large
    library fills in behind the UI instead of in front of it and the tiles on
    screen jump the queue.

    Priority 0 is "the browser is waiting for this" (``get_now``, ``want``),
    priority 1 is the background walk over the rest of the library. Request
    workers take only priority 0; background workers take whatever is first,
    so a long background queue can never starve the visible tiles. All lanes write the same on-disk cache and a per-file
    lock stops two of them building one file twice.
    """

    def __init__(self, root: Path, decode_fallback=None, background_workers: int = 2,
                 request_workers: int = 3):
        self.root = root
        self.decode_fallback = decode_fallback
        self._cv = threading.Condition()
        self._heap: list = []
        self._counter = itertools.count()
        self._stop = False
        self._lock = threading.Lock()
        self._file_locks: dict[str, threading.Lock] = {}
        self._queued: set[str] = set()
        self.total = 0
        self.done = 0
        self.failed = 0
        self._threads = []
        for i in range(request_workers + background_workers):
            t = threading.Thread(target=self._worker, args=(i < request_workers,), daemon=True,
                                 name=f"thumb-{'req' if i < request_workers else 'bg'}-{i}")
            t.start()
            self._threads.append(t)

    # -- queue --------------------------------------------------------------
    def _submit(self, prio: int, fn, *args):
        from concurrent.futures import Future
        fut: Future = Future()
        with self._cv:
            heapq.heappush(self._heap, (prio, next(self._counter), fn, args, fut))
            self._cv.notify_all()
        return fut

    def _worker(self, request_only: bool) -> None:
        while True:
            with self._cv:
                while True:
                    if self._stop:
                        return
                    pick = None
                    # request workers only ever take priority 0, so a long background queue
                    # cannot occupy them; the background workers help with whatever is first
                    if self._heap and (not request_only or self._heap[0][0] == 0):
                        pick = heapq.heappop(self._heap)
                    if pick:
                        break
                    self._cv.wait()
            _, _, fn, args, fut = pick
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn(*args))
            except BaseException as exc:
                fut.set_exception(exc)

    def shutdown(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()

    # -- one file -----------------------------------------------------------
    def _build(self, path: Path, size: int = THUMB_EDGE, all_sizes: bool = False) -> bytes:
        key = _cache_key(path)
        with self._lock:
            lock = self._file_locks.setdefault(key, threading.Lock())
        try:
            with lock:
                if all_sizes:
                    get_all(path, self.root, self.decode_fallback)
                    return b""
                return get(path, self.root, self.decode_fallback, size)
        finally:
            with self._lock:
                self._file_locks.pop(key, None)

    def get_now(self, path: Path, timeout: float = 120, size: int = THUMB_EDGE) -> bytes:
        """The thumbnail for a file the browser is waiting on (runs on a worker, ahead of the background)."""
        cached = cache_path(path, self.root, size)
        if cached.exists():
            try:
                return cached.read_bytes()
            except OSError:
                pass
        return self._submit(0, self._build, path, size).result(timeout=timeout)

    def want(self, paths, size: int = THUMB_EDGE) -> int:
        """Queue the tiles currently on screen at top priority; returns how many needed building."""
        n = 0
        for p in paths:
            p = Path(p)
            if not cache_path(p, self.root, size).exists():
                self._submit(0, self._build, p, size)
                n += 1
        return n

    # -- whole library ------------------------------------------------------
    def _background_job(self, path: Path) -> None:
        try:
            self._build(path, all_sizes=True)
        except Exception:
            with self._lock:
                self.failed += 1
        finally:
            with self._lock:
                self.done += 1
                self._queued.discard(str(path))

    def prefetch(self, paths) -> dict:
        """Queue files that have no cached thumbnail yet. Returns the progress."""
        with self._lock:
            if self.total and self.done >= self.total:      # previous batch finished
                self.total = self.done = self.failed = 0
            todo = []
            for p in paths:
                p = Path(p)
                if str(p) in self._queued or all(cache_path(p, self.root, s).exists() for s in SIZES):
                    continue
                self._queued.add(str(p))
                todo.append(p)
            self.total += len(todo)
        for p in todo:
            self._submit(1, self._background_job, p)
        return self.progress()

    def progress(self) -> dict:
        with self._lock:
            return {"total": self.total, "done": self.done, "failed": self.failed,
                    "active": self.done < self.total}
