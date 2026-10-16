"""
Crash-safe, serialised file writes.

``atomic_write_text`` writes a uniquely named temp file in the target's
folder, fsyncs it, renames it over the target and fsyncs the folder, so a
crash or a power cut leaves either the old file or the new one, never a
half-written or zero-length one. ``lock_for`` hands out one re-entrant lock
per file so read-modify-write sequences from concurrent requests cannot
interleave.
"""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def lock_for(path) -> threading.RLock:
    key = os.path.abspath(os.fspath(path))
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.RLock()
        return lock


def atomic_write_text(path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def quarantine(path) -> None:
    """Move an unreadable file aside as <name>.bad instead of losing it."""
    path = Path(path)
    try:
        os.replace(path, path.with_name(path.name + ".bad"))
    except OSError:
        pass
