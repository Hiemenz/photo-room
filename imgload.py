"""
Decode seam for ordinary image files (JPEG, TIFF, PNG and, with pillow-heif, HEIC).

Raws start Develop from LibRaw's linear-light demosaic. An image file starts from
the same place: this module decodes it with Pillow, applies the EXIF orientation,
converts from the embedded ICC profile (sRGB when there is none) to the working
space, and linearises it, returning ``uint16`` linear light exactly like
``process.decode_linear``. The render stages, the 16-bit export path and the tile
server then treat it as any other photo.

What is different from a raw: there is no camera white balance, so the baseline
temperature is a nominal 5500 K and Temp/Tint act as relative adjustments from
there; there is no sensor data, so lens/CA profile corrections do not apply.
Precision is that of the file (8 bit for most JPEGs), so heavy edits show banding
sooner than on a raw.
"""
from __future__ import annotations

import functools
import io
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, ImageOps

import colour

BASELINE_TEMP = 5500
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".tif", ".tiff", ".png")
HEIF_SUFFIXES = (".heic", ".heif")

_heif_state: bool | None = None


def heif_available() -> bool:
    """True when pillow-heif is installed (and is registered with Pillow)."""
    global _heif_state
    if _heif_state is None:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
            _heif_state = True
        except Exception:
            _heif_state = False
    return _heif_state


def editable_suffixes() -> tuple[str, ...]:
    return IMAGE_SUFFIXES + (HEIF_SUFFIXES if heif_available() else ())


def is_image(path) -> bool:
    """True for a file Develop decodes with Pillow instead of LibRaw."""
    return Path(str(path)).suffix.lower() in editable_suffixes()


def _cms_profile(data: bytes | None):
    if not data:
        return None
    try:
        return ImageCms.ImageCmsProfile(io.BytesIO(data))
    except Exception:
        return None


def _is_srgb(profile) -> bool:
    try:
        return "srgb" in ImageCms.getProfileDescription(profile).lower()
    except Exception:
        return False


@functools.lru_cache(maxsize=3)
def _lut16(space: str) -> np.ndarray:
    """uint16 linear value of every 8-bit code in ``space``."""
    lin = colour.to_linear(np.arange(256, dtype=np.float32) / 255.0, space)
    return np.rint(np.clip(lin, 0.0, 1.0) * 65535.0).astype(np.uint16)


def _target_profile(space: str):
    if space == "srgb":
        return ImageCms.createProfile("sRGB")
    import exporter
    return ImageCms.ImageCmsProfile(io.BytesIO(exporter.icc_profile_for(space)))


def _to_rgb8(img: Image.Image) -> Image.Image:
    """Any Pillow mode to 8-bit RGB (alpha dropped, 16-bit scaled, CMYK via its profile)."""
    mode = img.mode
    if mode == "RGB":
        return img
    if mode in ("I;16", "I;16L", "I;16B", "I"):
        arr = np.asarray(img, dtype=np.float32)
        top = 65535.0 if mode.startswith("I;16") else max(float(arr.max()), 1.0)
        return Image.fromarray(np.clip(arr / top * 255.0 + 0.5, 0, 255).astype(np.uint8)).convert("RGB")
    if mode == "F":
        arr = np.asarray(img, dtype=np.float32)
        return Image.fromarray(np.clip(arr * 255.0 + 0.5, 0, 255).astype(np.uint8)).convert("RGB")
    return img.convert("RGB")


def native_space(path: Path) -> str:
    """'adobergb' or 'prophoto' when the file embeds that profile, else 'srgb' (untagged or anything else)."""
    try:
        if Path(path).suffix.lower() in HEIF_SUFFIXES:
            heif_available()
        with Image.open(path) as im:
            profile = _cms_profile(im.info.get("icc_profile"))
        if profile is not None:
            desc = ImageCms.getProfileDescription(profile).lower()
            if "adobe" in desc:
                return "adobergb"
            if "prophoto" in desc or "romm" in desc:
                return "prophoto"
    except Exception:
        pass
    return "srgb"


def decode_linear16(path: Path, space: str = "srgb", half: bool = False) -> np.ndarray:
    """
    The file as ``uint16`` linear light in ``space`` (sRGB, Adobe RGB or ProPhoto
    primaries), orientation applied. ``half`` lets JPEG decode at reduced size
    (DCT scaling) when only a preview is needed.
    """
    space = colour.normalise_space(space)
    path = Path(path)
    if path.suffix.lower() in HEIF_SUFFIXES:
        heif_available()
    with Image.open(path) as src:
        if half:
            src.draft("RGB", (max(1, src.width // 2), max(1, src.height // 2)))
        src.load()
        icc = src.info.get("icc_profile")
        img = _to_rgb8(ImageOps.exif_transpose(src))
    source = _cms_profile(icc)
    if source is not None and not (space == "srgb" and _is_srgb(source)):
        try:
            img = ImageCms.profileToProfile(img, source, _target_profile(space), outputMode="RGB")
        except Exception:
            pass          # unreadable or unusable profile: treat the pixels as sRGB-like
    elif source is None and space != "srgb":
        # An untagged file is sRGB by convention; carry it into the wider space.
        img = ImageCms.profileToProfile(img, ImageCms.createProfile("sRGB"),
                                        _target_profile(space), outputMode="RGB")
    return np.ascontiguousarray(np.take(_lut16(space), np.asarray(img)))


def decode_preview(path: Path) -> tuple[np.ndarray, int, np.ndarray]:
    """Same shape as ``process.decode_preview_linear``: (linear uint16 sRGB, baseline K, wb multipliers)."""
    return decode_linear16(path, "srgb", half=True), BASELINE_TEMP, np.ones(4)


def oriented_size(path: Path) -> tuple[int, int] | None:
    """(width, height) after the EXIF rotation, from the header only."""
    try:
        if Path(path).suffix.lower() in HEIF_SUFFIXES:
            heif_available()
        with Image.open(path) as im:
            w, h = im.size
            if im.getexif().get(274, 1) in (5, 6, 7, 8):
                w, h = h, w
            return w, h
    except Exception:
        return None
