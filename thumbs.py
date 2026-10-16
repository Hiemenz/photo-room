#!/usr/bin/env python3
"""
Thumbnails for browsing a shoot.

Cameras embed a full-size JPEG preview in every raw file, so a thumbnail
can be pulled from that rather than demosaicing the sensor data — about
five times faster on a 5D Mark IV frame (80ms against 400ms). Across a
431-frame shoot that is the difference between half a minute and three.

Results are cached on disk and keyed by the source file's size and
modification time, so a thumbnail is built once and survives restarts,
and a file that changes is regenerated rather than served stale.
"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

# Bounds a hang reading a file's embedded preview.
EXIFTOOL_TIMEOUT = 120

THUMB_EDGE = 260
THUMB_QUALITY = 74
CACHE_DIR_NAME = ".photo-room-thumbs"


def _cache_key(path: Path) -> str:
    """Identity of a file's content, cheaply: path, size and mtime."""
    try:
        stat = path.stat()
        seed = f"{path.resolve()}:{stat.st_size}:{int(stat.st_mtime)}"
    except OSError:
        seed = str(path)
    return hashlib.sha256(seed.encode()).hexdigest()[:24]


def cache_path(path: Path, root: Path) -> Path:
    return root / CACHE_DIR_NAME / f"{_cache_key(path)}.jpg"


def _from_embedded_preview(path: Path) -> bytes | None:
    """
    Pull the camera's own JPEG preview out of the raw file.

    Returns None rather than raising when a file has no preview — some
    converted DNGs do not — so the caller can fall back to decoding.
    """
    for tag in ("-PreviewImage", "-JpgFromRaw", "-ThumbnailImage"):
        result = subprocess.run(
            ["exiftool", "-b", tag, str(path)], capture_output=True, timeout=EXIFTOOL_TIMEOUT
        )
        if result.returncode == 0 and len(result.stdout) > 1024:
            return result.stdout
    return None


def _encode(img: Image.Image) -> bytes:
    img = img.convert("RGB")
    img.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=THUMB_QUALITY)
    return buf.getvalue()


def build(path: Path, decode_fallback=None) -> bytes:
    """
    Produce thumbnail bytes for one raw file.

    ``decode_fallback`` is called only when the file carries no usable
    embedded preview; it should return an RGB array.
    """
    embedded = _from_embedded_preview(path)
    if embedded:
        try:
            return _encode(Image.open(io.BytesIO(embedded)))
        except Exception:
            pass  # a malformed preview is not a reason to give up on the file

    if decode_fallback is None:
        raise RuntimeError(f"No embedded preview in {path.name}")

    rgb = decode_fallback(path)
    if rgb.dtype != np.uint8:
        rgb = np.clip(rgb * 255.0 + 0.5, 0, 255).astype(np.uint8)
    return _encode(Image.fromarray(rgb))


def get(path: Path, root: Path, decode_fallback=None) -> bytes:
    """Return a thumbnail, building and caching it if needed."""
    cached = cache_path(path, root)
    if cached.exists():
        try:
            return cached.read_bytes()
        except OSError:
            pass

    data = build(path, decode_fallback)

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

    return data


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
