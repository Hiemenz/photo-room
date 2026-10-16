"""
Per-photo edit history and named snapshots.

Stored in a small JSON sidecar next to the photo (``IMG_0001.dng.photoroom.json``)
rather than in the XMP, so the develop settings Lightroom reads stay small and
nothing here can disturb them. The sidecar holds:

- ``original``: the as-imported (auto-computed) settings, what "Before" shows
  and what reset-to-import returns to
- ``entries``: committed edit steps, oldest first, each ``{id, label, ts, adjustments}``
- ``index``: which entry is the current state (jumping back moves it without
  deleting anything; recording a new step after a jump drops the abandoned future)
- ``snapshots``: named copies of the settings

Everything here is a pure function of the file on disk, so it is unit-testable
without Flask.
"""

from __future__ import annotations

import functools
import json
import os
import time
import uuid
from pathlib import Path

import fsutil

MAX_ENTRIES = 100
MAX_SNAPSHOTS = 50
SUFFIX = ".photoroom.json"

# Keys that describe the request, not the edit.
_TRANSIENT = ("_baseline_temp",)


def history_path(photo: Path) -> Path:
    return photo.with_name(photo.name + SUFFIX)


def _clean(adj: dict) -> dict:
    return {k: v for k, v in adj.items() if k not in _TRANSIENT}


def _empty() -> dict:
    return {"version": 1, "original": None, "entries": [], "index": -1, "snapshots": []}


def load(photo: Path) -> dict:
    """The photo's history, or an empty one if absent or unreadable."""
    try:
        data = json.loads(history_path(photo).read_text())
    except OSError:
        return _empty()
    except ValueError:
        fsutil.quarantine(history_path(photo))  # keep it for recovery
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    out = _empty()
    for key in out:
        if key in data:
            out[key] = data[key]
    if not isinstance(out["entries"], list):
        out["entries"] = []
    if not isinstance(out["snapshots"], list):
        out["snapshots"] = []
    n = len(out["entries"])
    out["index"] = max(-1, min(n - 1, out["index"] if isinstance(out["index"], int) else n - 1))
    return out


def _locked(fn):
    """Serialise a load-modify-save so concurrent requests cannot lose steps."""
    @functools.wraps(fn)
    def wrapper(photo, *a, **k):
        with fsutil.lock_for(history_path(photo)):
            return fn(photo, *a, **k)
    return wrapper


def save(photo: Path, data: dict) -> None:
    """Atomic write, so an interrupted save never leaves half a file."""
    fsutil.atomic_write_text(history_path(photo), json.dumps(data, separators=(",", ":")))


def _new_id() -> str:
    return uuid.uuid4().hex[:10]


@_locked
def set_original(photo: Path, adjustments: dict) -> dict:
    """Remember the as-imported state. Only the first call has any effect."""
    data = load(photo)
    if data["original"] is None:
        data["original"] = _clean(adjustments)
        save(photo, data)
    return data


@_locked
def record(photo: Path, label: str, adjustments: dict, now: float | None = None) -> tuple[dict, bool]:
    """
    Append a committed step. Returns (history, recorded).

    A step identical to the current one is ignored (a slider released where it
    started). Recording after jumping back discards the abandoned future, like
    undo/redo in any editor. The oldest steps fall off beyond MAX_ENTRIES.
    """
    data = load(photo)
    adj = _clean(adjustments)
    entries = data["entries"]
    if 0 <= data["index"] < len(entries) and entries[data["index"]]["adjustments"] == adj:
        return data, False
    del entries[data["index"] + 1:]
    entries.append({
        "id": _new_id(),
        "label": (label or "Edit")[:80],
        "ts": time.time() if now is None else now,
        "adjustments": adj,
    })
    if len(entries) > MAX_ENTRIES:
        del entries[: len(entries) - MAX_ENTRIES]
    data["index"] = len(entries) - 1
    save(photo, data)
    return data, True


@_locked
def jump(photo: Path, entry_id: str) -> tuple[dict, dict]:
    """Make an earlier (or later) step current. Returns (history, adjustments)."""
    data = load(photo)
    for i, entry in enumerate(data["entries"]):
        if entry["id"] == entry_id:
            data["index"] = i
            save(photo, data)
            return data, entry["adjustments"]
    raise KeyError(f"No such history step: {entry_id}")


def summary(data: dict) -> dict:
    """What the UI needs: no adjustment payloads, which can be large."""
    return {
        "entries": [{"id": e["id"], "label": e["label"], "ts": e["ts"]} for e in data["entries"]],
        "index": data["index"],
        "snapshots": [{"id": s["id"], "name": s["name"], "ts": s["ts"]} for s in data["snapshots"]],
        "has_original": data["original"] is not None,
    }


@_locked
def create_snapshot(photo: Path, name: str, adjustments: dict, now: float | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("A snapshot needs a name")
    data = load(photo)
    if len(data["snapshots"]) >= MAX_SNAPSHOTS:
        raise ValueError(f"At most {MAX_SNAPSHOTS} snapshots per photo")
    # Same name replaces, like presets.
    data["snapshots"] = [s for s in data["snapshots"] if s["name"] != name[:80]]
    data["snapshots"].append({
        "id": _new_id(), "name": name[:80],
        "ts": time.time() if now is None else now,
        "adjustments": _clean(adjustments),
    })
    save(photo, data)
    return data


def get_snapshot(photo: Path, snapshot_id: str) -> dict:
    for s in load(photo)["snapshots"]:
        if s["id"] == snapshot_id:
            return s["adjustments"]
    raise KeyError(f"No such snapshot: {snapshot_id}")


@_locked
def delete_snapshot(photo: Path, snapshot_id: str) -> tuple[dict, bool]:
    data = load(photo)
    kept = [s for s in data["snapshots"] if s["id"] != snapshot_id]
    removed = len(kept) != len(data["snapshots"])
    if removed:
        data["snapshots"] = kept
        save(photo, data)
    return data, removed
