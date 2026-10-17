#!/usr/bin/env python3
"""
Smart previews: a cached, render-ready 2048px decode of each raw.

``decode_for_analysis`` (half-size demosaic, camera white balance, 8-bit) is
the dominant cost of opening a photo in Develop. A smart preview stores the
result downscaled to 2048 px on the long edge, so the next open (and every
preview render, which works at <= 1400 px) starts from a ~25 ms JPEG decode
instead of a ~1 s demosaic.

The render code can call ``get(path, root)``; it returns the same tuple as
``process.decode_for_analysis`` -- (uint8 RGB, baseline temp in K, white
balance multipliers) -- at smart-preview resolution. ``peek`` returns None when
none is cached (never decodes), ``ensure`` builds it. Entries are keyed by
the raw's path, size and mtime and live in ``.photo-room-previews`` under the
library root; ``prune`` keeps the folder under a size budget (least recently
used first).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import threading
from pathlib import Path

import numpy as np
from PIL import Image

EDGE = 2048
QUALITY = 96
DIR_NAME = ".photo-room-previews"
MAX_BYTES = 2 * 1024**3

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _key(path: Path) -> str:
    st = path.stat()
    return hashlib.sha256(f"{path.resolve()}:{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()[:24]


def entry_paths(path: Path, root: Path) -> tuple[Path, Path]:
    k = _key(path)
    d = Path(root) / DIR_NAME
    return d / f"{k}.jpg", d / f"{k}.json"


def peek(path: Path, root: Path):
    """The cached decode, or None. Never touches the raw."""
    try:
        jpg, meta = entry_paths(path, root)
        info = json.loads(meta.read_text())
        rgb = np.asarray(Image.open(jpg).convert("RGB"))
        os.utime(jpg)                                  # recently used
        return rgb, int(info["baseline_temp"]), np.array(info["cam_mul"], dtype=float)
    except (OSError, ValueError, KeyError):
        return None


def ensure(path: Path, root: Path, decoder=None):
    """Build the smart preview if missing; returns the decode tuple."""
    hit = peek(path, root)
    if hit is not None:
        return hit
    if decoder is None:
        from process import decode_for_analysis as decoder
    jpg, meta = entry_paths(path, root)
    with _locks_guard:
        lock = _locks.setdefault(str(jpg), threading.Lock())
    with lock:
        hit = peek(path, root)
        if hit is not None:
            return hit
        rgb, temp, cam_mul = decoder(path)
        img = Image.fromarray(rgb)
        if max(img.size) > EDGE:
            img.thumbnail((EDGE, EDGE), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=QUALITY, subsampling=0)
        jpg.parent.mkdir(parents=True, exist_ok=True)
        _atomic(jpg, buf.getvalue())
        _atomic(meta, json.dumps({"baseline_temp": int(temp),
                                  "cam_mul": [float(x) for x in cam_mul]}).encode())
    return np.asarray(img), int(temp), np.asarray(cam_mul, dtype=float)


get = ensure


def _atomic(target: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def prune(root: Path, max_bytes: int = MAX_BYTES) -> int:
    """Delete least-recently-used entries until the folder fits; returns bytes freed."""
    d = Path(root) / DIR_NAME
    files = sorted(d.glob("*.jpg"), key=lambda p: p.stat().st_mtime) if d.is_dir() else []
    total = sum(p.stat().st_size for p in files)
    freed = 0
    for p in files:
        if total <= max_bytes:
            break
        size = p.stat().st_size
        p.unlink(missing_ok=True)
        p.with_suffix(".json").unlink(missing_ok=True)
        total -= size
        freed += size
    return freed
