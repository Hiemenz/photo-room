"""
Soft proofing: show the picture the way a printer or another display would.

Uses ``PIL.ImageCms`` (littleCMS). The rendered picture is treated as sRGB (the
editor's working output), converted through the chosen destination profile and
back to the display, so what you see is what that paper and ink can reproduce.
The out-of-gamut warning marks pixels the destination cannot hold.

Profiles come from a short list of colour-management folders (and
``PHOTO_ROOM_ICC_DIR``); a request may only name a profile from that list.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import numpy as np
from PIL import ImageCms

SRGB_ID = "srgb"
INTENTS = {
    "perceptual": ImageCms.Intent.PERCEPTUAL,
    "relative": ImageCms.Intent.RELATIVE_COLORIMETRIC,
    "saturation": ImageCms.Intent.SATURATION,
    "absolute": ImageCms.Intent.ABSOLUTE_COLORIMETRIC,
}
ALARM = (255, 0, 80)           # out-of-gamut warning colour
MAX_PROFILE_BYTES = 8 * 1024 * 1024
_SUFFIXES = {".icc", ".icm"}
DEFAULTS = {"enabled": False, "profile": SRGB_ID, "intent": "relative", "bpc": True,
            "simulate_paper": False, "gamut_warning": False}


def search_dirs() -> list[Path]:
    dirs = [Path(d) for d in os.environ.get("PHOTO_ROOM_ICC_DIR", "").split(os.pathsep) if d]
    home = Path.home()
    dirs += [home / ".color" / "icc", home / ".local" / "share" / "color" / "icc",
             Path("/usr/share/color/icc"), Path("/usr/local/share/color/icc")]
    return dirs


def _profile_id(path: Path) -> str:
    return "icc:" + hashlib.sha1(str(path).encode()).hexdigest()[:12]


def _describe(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_PROFILE_BYTES:
            return None
        return ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(str(path))).strip() or path.stem
    except (OSError, ImageCms.PyCMSError, ValueError):
        return None           # not a usable profile: leave it out


def list_profiles(limit: int = 400) -> list[dict]:
    """``[{id, name, file}]``: the built-in sRGB first, then every readable .icc / .icm."""
    out = [{"id": SRGB_ID, "name": "sRGB (screen)", "file": None}]
    seen: set[Path] = set()
    for root in search_dirs():
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix.lower() not in _SUFFIXES or path in seen:
                continue
            seen.add(path)
            name = _describe(path)
            if name:
                out.append({"id": _profile_id(path), "name": name, "file": str(path)})
            if len(out) > limit:
                return out
    return out


def resolve_profile(profile_id: str):
    """The ImageCmsProfile for an id from ``list_profiles``; ValueError for anything else."""
    if profile_id == SRGB_ID:
        return ImageCms.createProfile("sRGB")
    for p in list_profiles():
        if p["id"] == profile_id and p["file"]:
            return ImageCms.ImageCmsProfile(p["file"])
    raise ValueError("Unknown profile; choose one from the list")


def normalise_settings(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    if isinstance(raw.get("profile"), str) and 0 < len(raw["profile"]) < 64:
        out["profile"] = raw["profile"]
    if raw.get("intent") in INTENTS:
        out["intent"] = raw["intent"]
    for key in ("enabled", "bpc", "simulate_paper", "gamut_warning"):
        if isinstance(raw.get(key), bool):
            out[key] = raw[key]
    return out


def _intent_for(settings: dict) -> int:
    # "Simulate paper and ink" is absolute colorimetric: the paper white and
    # the ink's darkest black are shown, not scaled to the display's.
    return INTENTS["absolute"] if settings["simulate_paper"] else INTENTS[settings["intent"]]


def proof(rgb: np.ndarray, settings: dict, profile=None) -> tuple[np.ndarray, float]:
    """
    Soft-proof an H x W x 3 uint8 picture. Returns ``(proofed uint8, out_of_gamut_percent)``.

    With ``gamut_warning`` the pixels the profile cannot reproduce are painted ``ALARM``.
    """
    from PIL import Image

    settings = normalise_settings(settings)
    if profile is None:
        profile = resolve_profile(settings["profile"])
    srgb = ImageCms.createProfile("sRGB")
    intent = _intent_for(settings)
    flags = ImageCms.Flags.SOFTPROOFING
    if settings["bpc"] and not settings["simulate_paper"]:
        flags |= ImageCms.Flags.BLACKPOINTCOMPENSATION
    img = Image.fromarray(np.ascontiguousarray(rgb[:, :, :3]), "RGB")

    def run(extra=0):
        t = ImageCms.buildProofTransform(srgb, srgb, profile, "RGB", "RGB",
                                         renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
                                         proofRenderingIntent=intent, flags=flags | extra)
        return np.asarray(ImageCms.applyTransform(img, t)).astype(np.int16)

    out = run()
    percent = 0.0
    if settings["gamut_warning"]:
        checked = run(ImageCms.Flags.GAMUTCHECK)
        outside = (np.abs(checked - out) > 6).any(axis=2)
        percent = round(float(outside.mean()) * 100.0, 3)
        out[outside] = ALARM
    return np.clip(out, 0, 255).astype(np.uint8), percent
