"""
Settings Camera Raw XMP cannot hold, kept in a JSON sidecar beside the photo.

``<photo>.extras.json`` carries what only this program understands, plus the
full-fidelity copy of what the XMP can only approximate:

* ``spots``      every spot with its ``shape`` (the XMP stores circles only)
* ``redeye``     red-eye / pet-eye corrections (no XMP equivalent)
* ``rot90``, ``flip_h``, ``flip_v``   90-degree turns and mirrors
* ``crop_ratio`` the crop's locked aspect ratio ("" free, "orig", "w:h")

The XMP stays the source Lightroom reads; this file is read back on top of it
(see ``reconcile_spots`` for when the XMP wins) and removed when nothing is
left to say, like the masks sidecar.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import fsutil
import redeye
import spots

SIDECAR_SUFFIX = ".extras.json"
KEYS = ("spots", "redeye", "rot90", "flip_h", "flip_v", "crop_ratio")
_RATIO = re.compile(r"^\d{1,4}(?:\.\d{1,4})?:\d{1,4}(?:\.\d{1,4})?$")


def normalise_ratio(value) -> str:
    """"" (free), "orig" or a positive "w:h" string; anything else is free."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if value == "orig":
        return value
    if _RATIO.match(value):
        a, b = value.split(":")
        if float(a) > 0 and float(b) > 0:
            return value
    return ""


def normalise_orient(adj: dict) -> dict:
    """The orientation keys, cleaned: rot90 in 0..3, flips as bools."""
    try:
        quarter = int(adj.get("rot90") or 0) % 4
    except (TypeError, ValueError):
        quarter = 0
    return {"rot90": quarter, "flip_h": bool(adj.get("flip_h")), "flip_v": bool(adj.get("flip_v"))}


def collect(adj: dict) -> dict:
    """Non-default extras from a full adjustment set (the sidecar's content)."""
    out: dict = {}
    sp = spots.normalise(adj.get("spots"))
    if any(s.get("shape") != "circle" for s in sp):
        out["spots"] = sp
    eyes = redeye.normalise(adj.get("redeye"))
    if eyes:
        out["redeye"] = eyes
    for key, value in normalise_orient(adj).items():
        if value:
            out[key] = value
    ratio = normalise_ratio(adj.get("crop_ratio"))
    if ratio:
        out["crop_ratio"] = ratio
    return out


def sidecar_path(photo: Path) -> Path:
    return photo.with_name(photo.name + SIDECAR_SUFFIX)


def delete_sidecar(photo: Path) -> None:
    try:
        sidecar_path(photo).unlink()
    except OSError:
        pass


def save_sidecar(photo: Path, adj: dict) -> None:
    """Write the extras of ``adj`` beside the photo (or remove the file when none)."""
    data = collect(adj)
    if not data:
        delete_sidecar(photo)
        return
    target = sidecar_path(photo)
    with fsutil.lock_for(target):
        fsutil.atomic_write_text(
            target, json.dumps({"version": 1, **data}, separators=(",", ":")))


def load_sidecar(photo: Path) -> dict:
    """The stored extras, cleaned; {} if there is no usable file."""
    try:
        raw = json.loads(sidecar_path(photo).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return collect(raw)


def reconcile_spots(sidecar_spots: list[dict], xmp_spots: list[dict]) -> list[dict]:
    """
    The sidecar's spots (with shapes) win while the XMP still lists the same
    number of spots; if the counts differ, Lightroom edited the XMP afterwards
    and the XMP is the truth.
    """
    if sidecar_spots and len(sidecar_spots) == len(xmp_spots):
        return sidecar_spots
    return xmp_spots


def merge_into(values: dict | None, stored: dict) -> dict | None:
    """``values`` (read from the XMP) with the sidecar's ``stored`` extras on top."""
    if not stored:
        return values
    out = dict(values or {})
    if "spots" in stored:
        out["spots"] = reconcile_spots(stored["spots"], out.get("spots") or [])
    for key in KEYS[1:]:
        if key in stored:
            out[key] = stored[key]
    return out
