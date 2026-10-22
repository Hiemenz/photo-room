"""
Named export presets: every setting of the Export dialog under a name.

Built-in presets are read-only and live in code. User presets are kept in one
JSON file in the user config directory (``~/.photo-room``, or
``$PHOTO_ROOM_CONFIG_DIR``), written atomically so a crash cannot truncate it.
A preset holds the *settings* (destination, file name, format, quality, bit
depth, colour space, resize, metadata, sharpening, watermark), never which
photos to export.
"""

from __future__ import annotations

import fsutil
import json
import os
import re
import threading
import uuid
from pathlib import Path

import exporter
import postexport
from exporter import ExportOptions

MAX_NAME = 60
_lock = threading.Lock()


def config_dir() -> Path:
    override = os.environ.get("PHOTO_ROOM_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".photo-room"


def presets_file() -> Path:
    return config_dir() / "export_presets.json"


def atomic_write_json(path: Path, data) -> None:
    with fsutil.lock_for(path):
        fsutil.atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True))


BUILTINS: dict[str, dict] = {
    "Web 2048px JPEG q80 sRGB": {
        "format": "jpeg", "quality": 80, "long_edge": 2048, "colorspace": "srgb",
        "bit_depth": 8, "keep_metadata": True,
        "sharpen": {"for": "screen", "amount": "standard"},
    },
    "Full-res JPEG q92": {
        "format": "jpeg", "quality": 92, "long_edge": None, "colorspace": "srgb",
        "bit_depth": 8, "keep_metadata": True,
    },
    "Full-res 16-bit PNG": {
        "format": "png", "quality": 92, "long_edge": None, "colorspace": None,
        "bit_depth": 16, "keep_metadata": True,
    },
    "Print AdobeRGB": {
        "format": "jpeg", "quality": 95, "long_edge": None, "colorspace": "adobergb",
        "bit_depth": 8, "keep_metadata": True,
        "sharpen": {"for": "matte", "amount": "standard"},
    },
}

def options_from_payload(payload: dict) -> ExportOptions:
    """Build validated ExportOptions from dialog/preset JSON. Raises ValueError."""
    long_edge = payload.get("long_edge")
    try:
        long_edge = int(long_edge) if long_edge not in (None, "", 0, "0") else None
        return ExportOptions(
            format=str(payload.get("format", "jpeg")),
            quality=int(payload.get("quality", 92)),
            long_edge=long_edge,
            colorspace=payload.get("colorspace") or None,
            template=str(payload.get("template") or "{name}"),
            keep_metadata=bool(payload.get("keep_metadata", True)),
            metadata=str(payload.get("metadata") or "all"),
            bit_depth=int(payload.get("bit_depth", 8)),
            sharpen=payload.get("sharpen") or None,
            watermark=payload.get("watermark") or None,
            full_sensor=bool(payload.get("full_sensor", False)),
            png_compression=int(payload.get("png_compression", 1)),
            optimize_jpeg=bool(payload.get("optimize_jpeg", False)),
            custom_text=str(payload.get("custom_text") or ""),
        ).validate()
    except (TypeError, ValueError) as exc:
        raise ValueError(str(exc)) from None


def clean_subfolder(value) -> str:
    """A single sub-folder name: no separators, no "..", no hidden names."""
    name = str(value or "").strip()
    if not name:
        return ""
    if re.search(r"[/\\\x00-\x1f]", name) or name in (".", "..") or name.startswith("."):
        raise ValueError("the sub-folder must be a plain folder name")
    if len(name) > 100:
        raise ValueError("the sub-folder name is too long")
    return name


def destination_settings(payload: dict) -> dict:
    """Validated dest_mode / subfolder / post_action of a payload (raises ValueError)."""
    mode = str(payload.get("dest_mode") or "folder")
    if mode not in exporter.DEST_MODES:
        raise ValueError(f"destination mode must be one of {', '.join(exporter.DEST_MODES)}")
    sub = clean_subfolder(payload.get("subfolder"))
    if mode == "subfolder" and not sub:
        raise ValueError("Name the sub-folder to export into")
    return {"dest_mode": mode, "subfolder": sub,
            "post_action": postexport.normalize(payload.get("post_action"))}


def clean_settings(payload: dict) -> dict:
    """The storable, validated form of dialog settings (raises ValueError)."""
    opts = options_from_payload(payload)
    out = {
        "format": opts.format, "quality": opts.quality, "long_edge": opts.long_edge,
        "colorspace": opts.colorspace, "template": opts.template,
        "keep_metadata": opts.keep_metadata, "metadata": opts.metadata,
        "bit_depth": opts.bit_depth,
        "sharpen": opts.sharpen, "watermark": opts.watermark,
        "custom_text": opts.custom_text, **destination_settings(payload),
    }
    dest = str(payload.get("dest") or "").strip()
    if dest:
        out["dest"] = dest
    return out


def _clean_name(name) -> str:
    name = " ".join(str(name or "").split())
    if not name:
        raise ValueError("A preset needs a name")
    if len(name) > MAX_NAME:
        raise ValueError(f"Preset names are limited to {MAX_NAME} characters")
    return name


def _load_user() -> dict[str, dict]:
    try:
        data = json.loads(presets_file().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def list_presets() -> list[dict]:
    user = _load_user()
    out = [{"name": n, "builtin": True, "settings": s} for n, s in BUILTINS.items()]
    out += [{"name": n, "builtin": False, "settings": s} for n, s in sorted(user.items(), key=lambda kv: kv[0].lower())]
    return out


def get_preset(name: str) -> dict | None:
    if name in BUILTINS:
        return dict(BUILTINS[name])
    s = _load_user().get(name)
    return dict(s) if s is not None else None


def save_preset(name: str, settings: dict) -> str:
    """Create or overwrite a user preset. Built-in names are reserved."""
    name = _clean_name(name)
    if name.lower() in {b.lower() for b in BUILTINS}:
        raise ValueError(f"{name!r} is a built-in preset; save under another name")
    cleaned = clean_settings(settings)
    with _lock:
        user = _load_user()
        for existing in list(user):  # names are case-insensitive: replace, don't duplicate
            if existing.lower() == name.lower():
                del user[existing]
        user[name] = cleaned
        atomic_write_json(presets_file(), user)
    return name


def delete_preset(name: str) -> bool:
    if name in BUILTINS:
        raise ValueError("Built-in presets cannot be deleted")
    with _lock:
        user = _load_user()
        if name not in user:
            return False
        del user[name]
        atomic_write_json(presets_file(), user)
    return True


# -- Export with Previous ----------------------------------------------------

def last_file() -> Path:
    return config_dir() / "export_last.json"


def save_last(settings: dict) -> None:
    """Remember the settings of the latest export. Never lets a disk error break it."""
    try:
        atomic_write_json(last_file(), clean_settings(settings))
    except (OSError, ValueError):
        pass


def load_last() -> dict | None:
    """The settings of the latest export, or None when there is none (or it no longer validates)."""
    try:
        data = json.loads(last_file().read_text())
        return clean_settings(data) if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None
