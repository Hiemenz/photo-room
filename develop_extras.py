"""
Per-photo Develop state that is not a Camera Raw slider: Guided Upright lines,
the soft-proof setup and the reference photo.

Kept in a small JSON sidecar next to the photo (``IMG.CR2.develop.json``), like
the mask sidecar. Lightroom cannot read these, so they never go into the XMP.
A photo with nothing to remember has no sidecar.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import fsutil
import softproof
import transform

SIDECAR_SUFFIX = ".develop.json"
VERSION = 1


def sidecar_path(photo: Path) -> Path:
    return photo.with_name(photo.name + SIDECAR_SUFFIX)


def _unit(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return min(1.0, max(0.0, f)) if math.isfinite(f) else None


def normalise_guides(raw) -> list[dict]:
    out = []
    for g in (raw if isinstance(raw, list) else [])[:transform.MAX_GUIDES]:
        if not isinstance(g, dict):
            continue
        vals = [_unit(g.get(k)) for k in ("x1", "y1", "x2", "y2")]
        if None in vals or math.hypot(vals[2] - vals[0], vals[3] - vals[1]) < 0.02:
            continue
        out.append(dict(zip(("x1", "y1", "x2", "y2"), (round(v, 5) for v in vals))))
    return out


def normalise(raw) -> dict:
    """Only known keys survive, each coerced to a safe value."""
    raw = raw if isinstance(raw, dict) else {}
    out: dict = {}
    guides = normalise_guides(raw.get("upright_guides"))
    if guides:
        out["upright_guides"] = guides
    proof = softproof.normalise_settings(raw.get("softproof"))
    if proof != softproof.normalise_settings(None):
        out["softproof"] = proof
    ref = raw.get("reference")
    if isinstance(ref, str) and ref and len(ref) < 1024:
        out["reference"] = ref
    return out


def load(photo: Path) -> dict:
    try:
        data = json.loads(sidecar_path(photo).read_text())
    except (OSError, ValueError):
        return {}
    return normalise(data)


def save(photo: Path, extras) -> dict:
    clean = normalise(extras)
    target = sidecar_path(photo)
    if not clean:
        try:
            target.unlink()
        except OSError:
            pass
        return clean
    with fsutil.lock_for(target):
        fsutil.atomic_write_text(target, json.dumps({"version": VERSION, **clean}, separators=(",", ":")))
    return clean
