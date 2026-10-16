"""
Glue for the M8 develop features (spot removal, transform, lens correction).

editor.py only calls into this module, so the render path and the XMP code
each gain a couple of lines and the rest lives in spots.py, transform.py and
lens.py.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import lens
import spots
import transform

CONTROLS = transform.CONTROLS + lens.CONTROLS          # sliders
TOGGLES = lens.TOGGLES                                  # checkboxes
DEFAULTS = {**transform.DEFAULTS, **lens.DEFAULTS, "spots": []}

# Keys that describe one photograph's content rather than a look, so a
# preset never carries them (pasting spot positions onto another frame
# would paint blotches).
PRESET_EXCLUDED = {"spots"}


def complete(adj: dict) -> dict:
    """Every M8 key, defaulted, for the UI and the history."""
    out = {}
    for key, default in DEFAULTS.items():
        if key == "spots":
            out[key] = spots.normalise(adj.get("spots"))
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
    img = spots.apply(img, adj)
    if adj.get("_raw_frame"):
        return img
    return lens.apply(img, adj)


def apply_transform(img, adj: dict):
    return transform.apply(img, adj)


def frame_after_transform(width: int, height: int, adj: dict) -> tuple[int, int]:
    return transform.output_size(width, height, adj)


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
    """M8 settings stored in ``path`` (non-default only). Never raises."""
    out: dict = {}
    try:
        args = [f"-XMP-crs:{t}" for t in read_tags()] + ["-s3", "-f", str(path)]
        res = subprocess.run(["exiftool"] + args, capture_output=True, text=True, timeout=timeout)
        out.update(parse_values(res.stdout.strip().split("\n")))
    except Exception:
        pass
    try:
        res = subprocess.run(["exiftool"] + spots.read_arguments() + [str(path)],
                             capture_output=True, text=True, timeout=timeout)
        found = spots.parse_xmp(res.stdout)
        if found:
            out["spots"] = found
    except Exception:
        pass
    return out
