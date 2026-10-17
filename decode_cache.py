"""
On-disk cache of full-resolution raw decodes.

Demosaicing a 10 MP raw takes seconds and is identical every time for the same
file and decode parameters, so a re-export (another size, another format, a
changed slider) can skip it. Entries are uncompressed ``.npy`` files keyed by
the BLAKE2b hash of the raw file plus every decode parameter, so a changed
file, colour space, bit depth or LibRaw build can never hit a stale entry.

Disable with ``PHOTO_ROOM_DECODE_CACHE=0``; move with ``PHOTO_ROOM_CACHE_DIR``;
size cap ``PHOTO_ROOM_DECODE_CACHE_MB`` (default 3072, least recently used out).
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from pathlib import Path

import numpy as np

FORMAT_VERSION = "v1"
DEFAULT_MB = 3072
_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("PHOTO_ROOM_DECODE_CACHE", "1") != "0"


def cache_dir() -> Path:
    base = os.environ.get("PHOTO_ROOM_CACHE_DIR")
    root = Path(base) if base else Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "photo-room"
    return root / "decode"


def file_digest(path: Path) -> str:
    h = hashlib.blake2b(digest_size=20)
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 22):
            h.update(chunk)
    return h.hexdigest()


def cache_key(path: Path, params: dict) -> str:
    import rawpy

    ident = "|".join(f"{k}={params[k]}" for k in sorted(params))
    ident += f"|libraw={getattr(rawpy, 'libraw_version', '?')}|{FORMAT_VERSION}"
    return file_digest(path) + "-" + hashlib.blake2b(ident.encode(), digest_size=8).hexdigest()


def _limit_bytes() -> int:
    try:
        return int(float(os.environ.get("PHOTO_ROOM_DECODE_CACHE_MB", DEFAULT_MB)) * 1024 * 1024)
    except ValueError:
        return DEFAULT_MB * 1024 * 1024


def _evict(directory: Path, keep: Path) -> None:
    limit = _limit_bytes()
    files = [(p.stat().st_mtime, p.stat().st_size, p) for p in directory.glob("*.npy")]
    total = sum(s for _, s, _ in files)
    for _, size, p in sorted(files):
        if total <= limit:
            break
        if p != keep:
            p.unlink(missing_ok=True)
            total -= size


def get_or_decode(path: Path, params: dict, decode):
    """``decode()`` -> ndarray, memoised on disk. Returns (array, was_cached)."""
    if not enabled():
        return decode(), False
    try:
        directory = cache_dir()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = directory / (cache_key(path, params) + ".npy")
    except OSError:
        return decode(), False
    if target.exists():
        try:
            arr = np.load(target, mmap_mode="r")
            os.utime(target)
            return arr, True
        except (OSError, ValueError):
            target.unlink(missing_ok=True)
    arr = decode()
    try:
        with _lock:
            fd, tmp = tempfile.mkstemp(dir=directory, suffix=".part")
            with os.fdopen(fd, "wb") as fh:
                np.save(fh, arr)
            os.replace(tmp, target)
            _evict(directory, target)
    except OSError:
        pass
    return arr, False
