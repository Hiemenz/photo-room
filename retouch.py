"""
Glue for the M8 develop features (spot removal, transform, lens correction).

editor.py only calls into this module, so the render path and the XMP code
each gain a couple of lines and the rest lives in spots.py, transform.py and
lens.py.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import extras
import lens
import redeye
import spots
import transform

import numpy as np

CONTROLS = transform.CONTROLS + lens.CONTROLS          # sliders
TOGGLES = lens.TOGGLES                                  # checkboxes
DEFAULTS = {**transform.DEFAULTS, **lens.DEFAULTS, "spots": [], "redeye": [],
            "rot90": 0, "flip_h": False, "flip_v": False, "crop_ratio": ""}
ORIENT_KEYS = ("rot90", "flip_h", "flip_v")



def complete(adj: dict) -> dict:
    """Every M8 key, defaulted, for the UI and the history."""
    out = {}
    for key, default in DEFAULTS.items():
        if key == "spots":
            out[key] = spots.normalise(adj.get("spots"))
        elif key == "redeye":
            out[key] = redeye.normalise(adj.get("redeye"))
        elif key == "rot90":
            out[key] = extras.normalise_orient(adj)["rot90"]
        elif key == "crop_ratio":
            out[key] = extras.normalise_ratio(adj.get("crop_ratio"))
        elif isinstance(default, bool):
            out[key] = bool(adj.get(key, default))
        else:
            out[key] = adj.get(key, default)
    return out


def apply_corrections(img, adj: dict):
    """
    Spots, then lens corrections. Runs on the original frame, before
    transform / straighten / crop. ``_raw_frame`` (spot-editing view) shows
    the spots only, so click positions match the stored coordinates.
    """
    img = redeye.apply(spots.apply(img, adj), adj)
    if adj.get("_raw_frame"):
        return img
    return lens.apply(img, adj)


def orient(img, adj: dict):
    """Mirror, then turn clockwise by ``rot90`` quarter turns (a view when unchanged)."""
    o = extras.normalise_orient(adj)
    if o["flip_h"]:
        img = img[:, ::-1]
    if o["flip_v"]:
        img = img[::-1]
    if o["rot90"]:
        img = np.rot90(img, k=-o["rot90"])
    return np.ascontiguousarray(img) if o["flip_h"] or o["flip_v"] or o["rot90"] else img


def oriented_size(width: int, height: int, adj: dict) -> tuple[int, int]:
    """Frame size after ``orient``."""
    return (height, width) if extras.normalise_orient(adj)["rot90"] % 2 else (width, height)


def apply_transform(img, adj: dict):
    """Orientation first, then the perspective transform, so every later step sees the turned frame."""
    return transform.apply(orient(img, adj), adj)


def frame_after_transform(width: int, height: int, adj: dict) -> tuple[int, int]:
    return transform.output_size(*oriented_size(width, height, adj), adj)


def xmp_arguments(adj: dict, frame: tuple[float, float] | None = None) -> list[str]:
    args = []
    v = transform.values(adj)
    for key, tag in transform.XMP_TAGS.items():
        if key == "xf_rotate":
            args.append(f"-XMP-crs:{tag}={v[key]:.2f}")
        elif key in ("xf_x", "xf_y"):
            args.append(f"-XMP-crs:{tag}={v[key]:.2f}")
        else:
            args.append(f"-XMP-crs:{tag}={int(round(v[key]))}")
    args += lens.xmp_arguments(adj)
    args += spots.xmp_arguments(adj.get("spots"), frame)
    return args


_SCALAR_TAGS = {**transform.XMP_TAGS, **lens.XMP_TAGS}


def read_tags() -> list[str]:
    return list(_SCALAR_TAGS.values())


def parse_values(lines: list[str]) -> dict:
    """Rebuild the non-default M8 scalars from ``read_tags()`` output."""
    out = {}
    for i, key in enumerate(_SCALAR_TAGS):
        raw = lines[i].strip() if i < len(lines) else "-"
        if raw in ("-", ""):
            continue
        if key in lens.BOOL_KEYS:
            on = raw.lower() in ("1", "true", "yes")
            if on:
                out[key] = True
            continue
        try:
            v = float(raw)
        except ValueError:
            continue
        if abs(v - DEFAULTS[key]) > 1e-9:
            out[key] = v
    return out


def read_from_file(path: Path, timeout: float) -> dict:
    """M8 settings stored in ``path`` (non-default only).

    Raises ``RuntimeError`` if exiftool cannot read the file: "unreadable"
    must never look like "no settings", or the next save writes defaults over
    the real values.
    """
    def run(args: list[str]) -> str:
        try:
            res = subprocess.run(["exiftool"] + args, capture_output=True,
                                 text=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise RuntimeError(f"Could not read metadata ({exc.__class__.__name__}): {exc}") from exc
        if res.returncode != 0:
            raise RuntimeError(f"exiftool failed reading metadata: {res.stderr.strip() or res.returncode}")
        return res.stdout

    out: dict = {}
    out.update(parse_values(
        run([f"-XMP-crs:{t}" for t in read_tags()] + ["-s3", "-f", str(path)]).strip().split("\n")))
    found = spots.parse_xmp(run(spots.read_arguments() + [str(path)]))
    if found:
        out["spots"] = found
    return out
