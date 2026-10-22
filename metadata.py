#!/usr/bin/env python3
"""
Metadata panel: a read-only EXIF/IPTC readout, editable descriptive and rights
fields, and named metadata presets.

Where the editable fields live, so Lightroom and Bridge see what this tool writes:

* title             XMP-dc:Title
* caption           XMP-dc:Description
* creator           XMP-dc:Creator
* copyright         XMP-dc:Rights
* copyright_status  XMP-xmpRights:Marked   (copyrighted = True, public_domain = False,
                                            unknown = tag absent)
* usage_terms       XMP-xmpRights:UsageTerms

Writes go to the photo's ``.xmp`` sidecar through ``library.write_target_args``
(exiftool writes a temp file and renames it over the sidecar, under the
per-file lock), so a raw is never opened for writing. Every write returns the
sidecar's previous values, which is all an undo needs: writing them back
restores the sidecar's earlier state (an empty value removes the tag).

Presets are named sets of those fields, kept in one JSON file in the library
root beside the other state (``.photo-room-library.json`` and friends) and
written atomically. Applying a preset writes only the fields the preset fills
in, so a copyright preset leaves titles and captions alone.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import exifpool
import fsutil
import library

FIELDS = ("title", "caption", "creator", "copyright", "copyright_status", "usage_terms")
STATUSES = ("unknown", "copyrighted", "public_domain")
MAX_LEN = 2000
MAX_PRESETS = 100
MAX_NAME = 60
STORE_NAME = ".photo-room-metadata-presets.json"

_TAGS = {
    "title": "XMP-dc:Title",
    "caption": "XMP-dc:Description",
    "creator": "XMP-dc:Creator",
    "copyright": "XMP-dc:Rights",
    "usage_terms": "XMP-xmpRights:UsageTerms",
}
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_lock = threading.RLock()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def normalize_fields(fields) -> dict[str, str]:
    """
    Validate a ``{field: value}`` dict. Unknown fields are an error; ``None``
    means "" (clear). Control characters are dropped; only the caption and the
    usage terms keep line breaks.
    """
    if not isinstance(fields, dict) or not fields:
        raise ValueError("No metadata fields given")
    out: dict[str, str] = {}
    for key, value in fields.items():
        if key not in FIELDS:
            raise ValueError(f"Unknown metadata field: {key!r}")
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ValueError(f"{key} must be text")
        value = _CONTROL.sub("", value.replace("\r\n", "\n").replace("\r", "\n"))
        if key in ("caption", "usage_terms"):
            value = value.strip()
        else:
            value = " ".join(value.split())
        if len(value) > MAX_LEN:
            raise ValueError(f"{key} is longer than {MAX_LEN} characters")
        if key == "copyright_status":
            value = value.lower().replace(" ", "_").replace("-", "_") or "unknown"
            if value not in STATUSES:
                raise ValueError("copyright_status must be unknown, copyrighted or public_domain")
        out[key] = value
    return out


def write_args(fields: dict[str, str]) -> list[str]:
    """exiftool assignments for validated fields (an empty value clears the tag)."""
    args = []
    for key, value in fields.items():
        if key == "copyright_status":
            args.append("-XMP-xmpRights:Marked=" + {"copyrighted": "True", "public_domain": "False"}.get(value, ""))
        else:
            args.append(f"-{_TAGS[key]}={value}")
    return args


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

_READ_TAGS = ["-Make", "-Model", "-LensModel", "-LensID", "-Lens", "-ISO", "-ExposureTime", "-FNumber",
              "-FocalLength", "-DateTimeOriginal", "-CreateDate", "-GPSLatitude", "-GPSLongitude",
              "-GPSAltitude", "-ImageWidth", "-ImageHeight", "-ExifImageWidth", "-ExifImageHeight",
              "-XMP-dc:Title", "-XMP-dc:Description", "-XMP-dc:Creator", "-XMP-dc:Rights",
              "-XMP-xmpRights:Marked", "-XMP-xmpRights:UsageTerms", "-EXIF:Artist", "-EXIF:Copyright"]


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value) -> str:
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value)
    return "" if value is None else str(value)


def _shutter(seconds) -> str:
    s = _num(seconds)
    if not s or s <= 0:
        return ""
    if s < 0.4:
        return f"1/{round(1 / s)} s"
    return f"{s:g} s"


def _date(value) -> str:
    text = str(value or "")
    m = re.match(r"(\d{4})[:\-](\d\d)[:\-](\d\d)[ T](\d\d:\d\d:\d\d)", text)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}" if m else text


def _gps(entry: dict):
    lat, lon = _num(entry.get("GPSLatitude")), _num(entry.get("GPSLongitude"))
    if lat is None or lon is None:
        return None
    alt = _num(entry.get("GPSAltitude"))
    text = f"{abs(lat):.5f}\u00b0 {'N' if lat >= 0 else 'S'}, {abs(lon):.5f}\u00b0 {'E' if lon >= 0 else 'W'}"
    if alt is not None:
        text += f", {alt:.0f} m"
    return {"lat": lat, "lon": lon, "alt": alt, "text": text}


def parse_info(entry: dict) -> dict:
    """The read-only readout from one ``exiftool -j -n`` record."""
    make, model = _text(entry.get("Make")).strip(), _text(entry.get("Model")).strip()
    camera = model if model.lower().startswith(make.lower()) else f"{make} {model}".strip()
    width = _num(entry.get("ImageWidth")) or _num(entry.get("ExifImageWidth"))
    height = _num(entry.get("ImageHeight")) or _num(entry.get("ExifImageHeight"))
    iso, fnum, focal = _num(entry.get("ISO")), _num(entry.get("FNumber")), _num(entry.get("FocalLength"))
    return {
        "camera": camera,
        "lens": _text(entry.get("LensModel") or entry.get("LensID") or entry.get("Lens")),
        "iso": int(iso) if iso else None,
        "shutter": _shutter(entry.get("ExposureTime")),
        "aperture": f"f/{fnum:g}" if fnum else "",
        "focal_length": f"{focal:g} mm" if focal else "",
        "captured": _date(entry.get("DateTimeOriginal") or entry.get("CreateDate")),
        "gps": _gps(entry),
        "width": int(width) if width else None,
        "height": int(height) if height else None,
        "dimensions": f"{int(width)} \u00d7 {int(height)}" if width and height else "",
    }


def _status(value) -> str:
    text = str(value).strip().lower()
    return "copyrighted" if text in ("true", "1") else "public_domain" if text in ("false", "0") else "unknown"


def parse_fields(entry: dict, fallback: bool = True) -> dict[str, str]:
    """The editable fields from one record; with ``fallback`` the camera's EXIF Artist / Copyright fill gaps."""
    creator = entry.get("Creator")
    out = {
        "title": _text(entry.get("Title")),
        "caption": _text(entry.get("Description")),
        "creator": _text(creator),
        "copyright": _text(entry.get("Rights")),
        "copyright_status": _status(entry.get("Marked", "")),
        "usage_terms": _text(entry.get("UsageTerms")),
    }
    if fallback:
        out["creator"] = out["creator"] or _text(entry.get("Artist"))
        out["copyright"] = out["copyright"] or _text(entry.get("Copyright"))
    return out


def _records(paths: list[Path], tags: list[str]) -> dict[str, dict]:
    result = exifpool.run(["-j", "-n", *tags], files=[str(p) for p in paths])
    try:
        entries = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        entries = []
    return {str(Path(e.get("SourceFile", "")).resolve()): e for e in entries if e.get("SourceFile")}


def read(path: Path) -> dict:
    """``{info, fields}`` for one photo; the sidecar's fields win over the file's own."""
    side = library.meta_target(path)
    files = [path] + ([side] if side != path and side.exists() else [])
    recs = _records(files, _READ_TAGS)
    photo = recs.get(str(path.resolve()), {})
    sidecar = recs.get(str(side.resolve()), {}) if side != path else {}
    merged = {**photo, **{k: v for k, v in sidecar.items() if k != "SourceFile"}}
    return {"info": parse_info(photo), "fields": parse_fields(merged)}


def read_sidecar_fields(path: Path) -> dict[str, str]:
    """Only what the metadata target itself holds (no camera fallback): the undo snapshot."""
    side = library.meta_target(path)
    if not side.exists():
        return parse_fields({}, fallback=False)
    rec = _records([side], _READ_TAGS).get(str(side.resolve()), {})
    return parse_fields(rec, fallback=False)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def write(path: Path, fields) -> dict[str, str]:
    """Write ``fields`` to the photo's sidecar; return the previous values of those fields."""
    fields = normalize_fields(fields)
    target = library.meta_target(path)
    with fsutil.lock_for(target):
        before = read_sidecar_fields(path)
        library.write_target_args(path, write_args(fields))
    return {k: before[k] for k in fields}


def write_many(items: dict[Path, dict]) -> dict[Path, dict[str, str]]:
    """
    Write per-photo fields. If one photo fails, the ones already written are put
    back and the error is raised, so a batch is all or nothing.
    """
    items = {p: normalize_fields(f) for p, f in items.items()}     # validate everything first
    previous: dict[Path, dict[str, str]] = {}
    try:
        for path, fields in items.items():
            previous[path] = write(path, fields)
    except Exception:
        for path, old in previous.items():
            try:
                write(path, old)
            except Exception:
                pass
        raise
    return previous


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

def _store(root: Path) -> Path:
    return Path(root) / STORE_NAME


def load_presets(root: Path) -> dict[str, dict[str, str]]:
    try:
        data = json.loads(_store(root).read_text())
        presets = data.get("presets", {})
        if not isinstance(presets, dict):
            return {}
    except (OSError, ValueError, AttributeError):
        return {}
    out = {}
    for name, fields in presets.items():
        try:
            out[str(name)] = normalize_fields(fields)
        except ValueError:
            continue
    return out


def _clean_name(name) -> str:
    name = " ".join(str(name or "").split())
    if not name or _CONTROL.search(name):
        raise ValueError("Give the preset a name")
    if len(name) > MAX_NAME:
        raise ValueError(f"Preset names are at most {MAX_NAME} characters")
    return name


def save_preset(root: Path, name, fields) -> dict[str, str]:
    """Create or replace a preset. Empty fields are not stored (they would clear data when applied)."""
    name = _clean_name(name)
    clean = {k: v for k, v in normalize_fields(fields).items()
             if v and not (k == "copyright_status" and v == "unknown")}
    if not clean:
        raise ValueError("Fill in at least one field to save a preset")
    with _lock:
        presets = load_presets(root)
        if name not in presets and len(presets) >= MAX_PRESETS:
            raise ValueError(f"At most {MAX_PRESETS} presets")
        presets[name] = clean
        fsutil.atomic_write_text(_store(root), json.dumps({"presets": presets}, indent=1, sort_keys=True))
    return clean


def delete_preset(root: Path, name) -> None:
    with _lock:
        presets = load_presets(root)
        if name not in presets:
            raise KeyError(f"No preset named {name!r}")
        del presets[name]
        fsutil.atomic_write_text(_store(root), json.dumps({"presets": presets}, indent=1, sort_keys=True))


def get_preset(root: Path, name) -> dict[str, str]:
    presets = load_presets(root)
    if name not in presets:
        raise KeyError(f"No preset named {name!r}")
    return presets[name]
