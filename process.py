#!/usr/bin/env python3
"""
CR2 → DNG converter with automatic tone + white balance adjustments.

Subcommands:
  convert   Convert CR2 files to DNG (default)
  watch     Watch a folder and auto-convert new CR2 files
"""

import argparse
import functools
import os

import colour
import editversion
import fsutil
import imgload
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import rawpy
from tqdm import tqdm


# Conversion is dominated by the DNG Converter subprocess and by raw decoding,
# both of which release the GIL, so threads parallelise it as well as processes
# (measured within a few percent of each other) without pickling work across a
# pool.
#
# The realistic speedup is about 2.4x: 30 frames read cold from an external
# drive took 54s serial and 23s with twelve workers on a 14-core machine. A
# warm-cache microbenchmark suggested 6x, which is what disk and DNG Converter
# contention cost in practice — worth knowing before raising this further.
DEFAULT_JOBS = min(12, (os.cpu_count() or 4))


DNG_CONVERTER = Path(
    "/Applications/Adobe DNG Converter.app/Contents/MacOS/Adobe DNG Converter"
)


def dng_converter_available() -> bool:
    """True when Adobe DNG Converter is installed (macOS/Windows only; optional)."""
    return DNG_CONVERTER.exists()


# A DNG may be the only original (Leica, Pentax, DJI, phones), and exiftool
# rewrites it non-atomically, so by default DNGs are treated like any other
# raw: edits, ratings and labels go to the .xmp sidecar. Embedding into the
# DNG is an explicit opt-in (PHOTO_ROOM_EMBED_DNG=1 or editor.py --embed-dng).
def embed_dng_enabled() -> bool:
    return os.environ.get("PHOTO_ROOM_EMBED_DNG", "") == "1"


def is_dng(path: Path) -> bool:
    return path.suffix.lower() == ".dng"


def edit_target(path: Path) -> Path:
    """
    The file that carries a photo's metadata edits (develop settings, rating).

    Every raw, DNG included, is left byte-for-byte alone and gets a
    Lightroom-style ``.xmp`` sidecar beside it, which is also what Lightroom
    and Camera Raw read. Only with the explicit embed opt-in is a DNG itself
    written to.
    """
    if is_dng(path) and embed_dng_enabled():
        return path
    return path.with_suffix(".xmp")


def read_target(path: Path) -> Path:
    """Where to READ metadata from: a DNG without a sidecar is read directly."""
    t = edit_target(path)
    if t != path and is_dng(path) and not t.exists():
        return path
    return t


# The DNG Converter is a GUI application driven from the command line, and it
# answers some invocations by opening a window instead of writing to stdout —
# `-h` does exactly that, producing no output and never exiting. Without a
# deadline one such file would stall an entire shoot with nothing to show for
# it. Generous enough for a large raw on a slow external drive.
CONVERTER_TIMEOUT = 300

# Metadata reads and writes are fast; this only bounds a hang.
EXIFTOOL_TIMEOUT = 120


def run_tool(cmd: list[str], *, timeout: int, what: str, **kwargs):
    """Run a helper program with a deadline, reporting a hang as a failure."""
    try:
        return subprocess.run(cmd, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"{what} did not finish within {timeout}s and was stopped. "
            "It may be waiting on a dialog — try running it once by hand."
        ) from None


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------

def _exiftool_get(path: Path, *tags: str) -> dict[str, str]:
    """Return a dict of {tag: value} from exiftool for the given file."""
    args = [f"-{t}" for t in tags] + ["-s3", "-f", str(path)]
    result = run_tool(["exiftool"] + args, timeout=EXIFTOOL_TIMEOUT,
                      what="exiftool", capture_output=True, text=True)
    values = result.stdout.strip().split("\n")
    return {tag: (values[i] if i < len(values) else "-") for i, tag in enumerate(tags)}


def get_shoot_date(cr2_path: Path) -> str | None:
    """Return shoot date as 'YYYY/MM/DD' from EXIF, or None."""
    meta = _exiftool_get(cr2_path, "DateTimeOriginal")
    raw_date = meta.get("DateTimeOriginal", "-")
    if raw_date and raw_date != "-" and ":" in raw_date:
        # Format: '2026:09:14 12:34:56'
        parts = raw_date.split(" ")[0].split(":")
        if len(parts) == 3:
            return f"{parts[0]}/{parts[1]}/{parts[2]}"
    return None


def get_iso(cr2_path: Path) -> int:
    """Return ISO value from EXIF metadata."""
    meta = _exiftool_get(cr2_path, "ISO")
    try:
        return int(meta.get("ISO", "0"))
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# Auto-adjustment analysis
# ---------------------------------------------------------------------------

# Analysis only needs the shape of the histogram, not every pixel, so tone
# statistics are taken from a strided sample. At stride 4 that is ~20x faster
# than the full frame and moves the resulting values by well under one unit.
ANALYSIS_STRIDE = 4
# Auto exposure never proposes more than this many stops either way.
AUTO_EXPOSURE_LIMIT = 4.0


class _NoMatrix:
    """Stands in for a raw with no colour matrix (header-only callers, test doubles)."""


def baseline_temp_from_wb(camera_whitebalance, raw=None) -> tuple[int, np.ndarray]:
    """
    (as-shot temp in K, normalised wb multipliers) from rawpy's camera WB.

    Pass the open ``raw`` so the camera's colour matrix is used: the white the
    camera balanced for is taken to XYZ and read off as a correlated colour
    temperature (analysis.kelvin_from_camera). The old ``5500 / (R/B)`` ran the
    wrong way round and read a daylight frame as ~3100 K.
    """
    import analysis

    cam_mul = np.array(camera_whitebalance[:4], dtype=float)
    cam_mul /= cam_mul[1]
    return analysis.baseline_kelvin(raw if raw is not None else _NoMatrix(), cam_mul), cam_mul


def camera_baseline_temp(cr2_path: Path) -> int:
    """The camera white balance as a temperature, from the header only."""
    if imgload.is_image(cr2_path):
        return imgload.BASELINE_TEMP
    with rawpy.imread(str(cr2_path)) as raw:
        return baseline_temp_from_wb(raw.camera_whitebalance, raw)[0]


def camera_crop(raw, scale: int = 1):
    """
    The camera's default crop of the decoded frame as ``(top, left, height, width)``
    in output pixels (``scale`` 2 for a half-size decode), or None when the file
    has none, it is the whole frame, or the decode is rotated (flip != 0).

    LibRaw reports the crop in raw-sensor coordinates; the decoded image starts at
    (left_margin, top_margin) inside them.
    """
    sz = getattr(raw, "sizes", None)
    try:
        cw, ch = int(sz.crop_width), int(sz.crop_height)
        left = int(sz.crop_left_margin) - int(sz.left_margin)
        top = int(sz.crop_top_margin) - int(sz.top_margin)
        full_w, full_h = int(sz.iwidth), int(sz.iheight)
        flip = int(sz.flip)
    except (AttributeError, TypeError, ValueError):
        return None
    if flip or cw <= 0 or ch <= 0 or left < 0 or top < 0 or left + cw > full_w or top + ch > full_h:
        return None
    if (cw, ch) == (full_w, full_h):
        return None
    if scale > 1:
        left, top, cw, ch = left // scale, top // scale, cw // scale, ch // scale
    return top, left, ch, cw


def decode_linear(raw, *, half_size: bool = False, space: str = "srgb"):
    """
    Demosaic ``raw`` to **linear light**: uint16, camera white balance applied, the
    camera-to-``space`` matrix applied, no tone curve and no auto brightening
    (``gamma=(1, 1)``). Every edit and every transfer function starts from this.
    """
    return raw.postprocess(
        use_camera_wb=True,
        half_size=half_size,
        no_auto_bright=True,
        output_bps=16,
        gamma=(1, 1),
        output_color=rawpy_color_space(space),
    )


def rawpy_color_space(space: str):
    return {"srgb": rawpy.ColorSpace.sRGB, "adobergb": rawpy.ColorSpace.Adobe,
            "prophoto": rawpy.ColorSpace.ProPhoto}[colour.normalise_space(space)]


def decode_preview_linear(cr2_path: Path) -> tuple[np.ndarray, int, np.ndarray]:
    """
    (half-size linear uint16 RGB in sRGB primaries, baseline temp in K, wb multipliers),
    cropped to the camera's default area. The develop preview and the 8-bit
    analysis image both come from this one demosaic.
    """
    if imgload.is_image(cr2_path):
        return imgload.decode_preview(cr2_path)
    with rawpy.imread(str(cr2_path)) as raw:
        baseline_temp, cam_mul = baseline_temp_from_wb(raw.camera_whitebalance, raw)
        lin = decode_linear(raw, half_size=True)
        crop = camera_crop(raw, scale=2)
        if crop:
            top, left, h, w = crop
            lin = np.ascontiguousarray(lin[top:top + h, left:left + w])
    return lin, baseline_temp, cam_mul


@functools.lru_cache(maxsize=1)
def _u8_lut() -> np.ndarray:
    return np.rint(colour.encode_lut16("srgb") * 255.0).astype(np.uint8)


def linear16_to_u8(lin16: np.ndarray) -> np.ndarray:
    """uint16 linear light -> 8-bit sRGB (exact sRGB curve) for analysis and thumbnails."""
    return np.take(_u8_lut(), lin16)


def decode_for_analysis(cr2_path: Path) -> tuple[np.ndarray, int, np.ndarray]:
    """
    Decode a raw file once, returning (8-bit sRGB RGB, baseline temp in K, wb multipliers).

    Unpacking the sensor data is the dominant cost of an import, so callers that
    need both a preview and its statistics should decode through here and pass
    the result around rather than opening the file twice.
    """
    lin, baseline_temp, cam_mul = decode_preview_linear(cr2_path)
    return linear16_to_u8(lin), baseline_temp, cam_mul


def _tone_statistics(rgb: np.ndarray) -> tuple[float, float, float]:
    """Return (median, 10th, 90th percentile) luminance from a strided sample."""
    sample = rgb[::ANALYSIS_STRIDE, ::ANALYSIS_STRIDE]
    lum = (
        0.2126 * sample[:, :, 0]
        + 0.7152 * sample[:, :, 1]
        + 0.0722 * sample[:, :, 2]
    ).astype(np.float32) * (1.0 / 255.0)
    # One sort for all three cut points rather than three separate passes.
    p10, p50, p90 = np.percentile(lum, (10, 50, 90))
    return float(p50), float(p10), float(p90)


def compute_auto_adjustments(
    cr2_path: Path,
    decoded: tuple[np.ndarray, int, np.ndarray] | None = None,
    iso: int | None = None,
    style_model=None,
) -> dict:
    """
    Analyze raw image data and compute Camera Raw-style XMP adjustment values.
    Returns: exposure, shadows, highlights, color_temp, tint, luminance_nr

    Pass ``decoded`` (from decode_for_analysis) and ``iso`` to reuse work a
    caller has already done instead of paying for a second decode.

    ``style_model`` applies a learned correction on top of these heuristics.
    It only ever adjusts the result — with no training data the output is
    identical to the plain heuristic.
    """
    if iso is None:
        iso = get_iso(cr2_path)

    rgb, color_temp, cam_mul = decoded if decoded is not None else decode_for_analysis(cr2_path)

    # Tint from G1/G2 channel imbalance
    tint = 0
    if len(cam_mul) >= 4 and cam_mul[3] > 0:
        tint = int(np.clip((cam_mul[1] - cam_mul[3]) * 20, -50, 50))

    median_lum, p10, p90 = _tone_statistics(rgb)

    # Exposure: shift the median toward 0.40 (display-coded). Exposure is a gain
    # in linear light, so the shift is measured there: edit version 2 maths.
    if median_lum > 0.001:
        target = float(colour.srgb_decode_exact(0.40))
        ev_shift = np.log2(target / float(colour.srgb_decode_exact(median_lum)))
        exposure = round(float(np.clip(ev_shift, -AUTO_EXPOSURE_LIMIT, AUTO_EXPOSURE_LIMIT)) * 4) / 4
    else:
        exposure = 0.0

    # Shadows: lift based on 10th-percentile darkness
    if p10 < 0.05:
        shadows = int(np.clip((0.10 - p10) / 0.10 * 60, 0, 80))
    elif p10 < 0.12:
        shadows = int(np.clip((0.12 - p10) / 0.12 * 30, 0, 40))
    else:
        shadows = 0

    # Highlights: pull back based on 90th-percentile brightness
    if p90 > 0.92:
        highlights = int(np.clip((p90 - 0.92) / 0.08 * -80, -100, 0))
    elif p90 > 0.80:
        highlights = int(np.clip((p90 - 0.80) / 0.12 * -30, -50, 0))
    else:
        highlights = 0

    # Noise reduction: scale with ISO (0 at ISO 100, 60 at ISO 6400+)
    luminance_nr = 0
    if iso >= 3200:
        luminance_nr = int(np.clip((iso - 3200) / 3200 * 40 + 20, 20, 60))
    elif iso >= 1600:
        luminance_nr = 10
    elif iso >= 800:
        luminance_nr = 5

    result = {
        "exposure": exposure,
        "shadows": shadows,
        "highlights": highlights,
        "color_temp": color_temp,
        "tint": tint,
        "luminance_nr": luminance_nr,
        "iso": iso,
    }

    if style_model is not None and getattr(style_model, "is_trained", False):
        import style

        features = style.extract_features(rgb, iso, cam_mul)
        result = style.apply_style(result, features, style_model)
        result["iso"] = iso
        result["styled"] = True

    return result


def photo_features(cr2_path: Path, decoded=None, iso: int | None = None) -> dict:
    """Feature vector describing a photo, for recording or predicting an edit."""
    import style

    rgb, _, cam_mul = decoded if decoded is not None else decode_for_analysis(cr2_path)
    if iso is None:
        iso = get_iso(cr2_path)
    return style.extract_features(rgb, iso, cam_mul)


# ---------------------------------------------------------------------------
# Library listing
# ---------------------------------------------------------------------------

# Every raw format the DNG Converter and LibRaw both handle. Defined once
# here because convert, watch, cull, the library listing and the card ingest
# must agree: a narrower list in one of them silently skips files. Before
# this was shared, pointing convert at a folder holding a second body's CR3
# and NEF files converted the CR2s and said nothing about the rest.
RAW_SUFFIXES = (".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2", ".dng")

RAW_PATTERNS = tuple(
    f"**/*{ext}" for suffix in RAW_SUFFIXES for ext in (suffix, suffix.upper())
)

# Directories that hold generated files, never photographs.
LIBRARY_EXCLUDED = {".photo-room-thumbs"}


def list_photos(root: Path) -> list[Path]:
    """
    Every photograph under ``root``, once each.

    After conversion a library holds both IMG_0001.CR2 and IMG_0001.dng.
    They are one photograph, so the DNG is listed and the source raw hidden:
    the DNG carries the edits and embeds the original anyway. Listing both
    showed every frame twice and made a 431-frame shoot look like 862.
    """
    found: dict[tuple[Path, str], Path] = {}

    for pattern in RAW_PATTERNS:
        for path in root.glob(pattern):
            if LIBRARY_EXCLUDED & set(path.parts):
                continue
            key = (path.parent, path.stem)
            existing = found.get(key)
            if existing is None or (
                existing.suffix.lower() != ".dng" and path.suffix.lower() == ".dng"
            ):
                found[key] = path

    return sorted(found.values())


# ---------------------------------------------------------------------------
# Conversion pipeline
# ---------------------------------------------------------------------------

def convert_cr2_to_dng(cr2_path: Path, output_dir: Path) -> Path:
    """
    Convert one raw file to DNG. Named for CR2 but format-agnostic: the
    converter handles every format in RAW_SUFFIXES.
    """
    cmd = [
        str(DNG_CONVERTER),
        "-d", str(output_dir),
        # Mosaic rather than linear (-l): the sensor data is identical, but a
        # linear DNG stores it demosaiced at roughly twice the size. On a
        # 431-frame shoot that is 56GB against 26GB, and it converts faster.
        "-e",   # embed original raw
        str(cr2_path),
    ]
    result = run_tool(
        cmd, timeout=CONVERTER_TIMEOUT,
        what=f"DNG Converter on {cr2_path.name}",
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"DNG Converter failed: {result.stderr.strip()}")
    dng_path = output_dir / cr2_path.with_suffix(".dng").name
    if not dng_path.exists():
        raise FileNotFoundError(f"Expected DNG not found: {dng_path}")
    return dng_path


def verify_xmp_written(dng_path: Path, expected: dict[str, str]) -> None:
    """
    Confirm tags actually landed in the file.

    exiftool has been observed returning success while silently dropping
    XMP-crs tags — white balance in particular — so an exit code of 0 is not
    sufficient evidence that an edit was saved.
    """
    tags = list(expected)
    args = [f"-XMP-crs:{t}" for t in tags] + ["-s3", "-f", str(dng_path)]
    result = run_tool(["exiftool"] + args, timeout=EXIFTOOL_TIMEOUT,
                      what="exiftool", capture_output=True, text=True)
    values = result.stdout.strip().split("\n")

    missing = [
        tag for i, tag in enumerate(tags)
        if i >= len(values) or values[i].strip() in ("-", "")
    ]
    if missing:
        raise RuntimeError(
            f"exiftool reported success but did not write {', '.join(missing)} "
            f"to {dng_path.name}"
        )


def _auto_xmp_values(adj: dict) -> dict[str, str]:
    values = {
        "ProcessVersion": "11.0",
        "WhiteBalance": "Custom",
        "ColorTemperature": str(adj["color_temp"]),
        "Tint": str(adj["tint"]),
        "Exposure2012": f"{adj['exposure']:.2f}",
        "Shadows2012": str(adj["shadows"]),
        "Highlights2012": str(adj["highlights"]),
        "LuminanceSmoothing": str(adj["luminance_nr"]),
    }

    # A learned style can add settings the heuristic never produces. Writing
    # only the eight above meant ingest reported "[styled]" while discarding
    # the contrast, vibrance and saturation it had just computed.
    for key, tag in (
        ("contrast", "Contrast2012"), ("whites", "Whites2012"),
        ("blacks", "Blacks2012"), ("vibrance", "Vibrance"),
        ("saturation", "Saturation"),
    ):
        if key in adj:
            values[tag] = str(int(round(float(adj[key]))))
    return values


def write_sidecar_adjustments(raw_path: Path, adj: dict) -> Path:
    """
    Store auto-computed develop settings in ``<raw>.xmp``, leaving the raw alone.

    This is the portable counterpart of ``embed_xmp_adjustments`` for when no
    DNG exists. Existing sidecar content (ratings, other Camera Raw settings)
    is kept; the file is created if absent.
    """
    target = raw_path.with_suffix(".xmp")
    values = _auto_xmp_values(adj)
    args = [f"-XMP-crs:{k}={v}" for k, v in values.items()]
    if not target.exists():
        # A brand-new sidecar holds only current-maths numbers; an existing one may hold older edits
        # whose version must not be rewritten here.
        args.append(f"-XMP-xmp:CreatorTool={editversion.tool_label()}")
    with fsutil.lock_for(target):
        if target.exists():
            cmd = ["exiftool"] + args + ["-overwrite_original", str(target)]
        else:
            cmd = ["exiftool"] + args + ["-o", str(target), os.devnull]
        result = run_tool(cmd, timeout=EXIFTOOL_TIMEOUT, what="exiftool",
                          capture_output=True, text=True)
    # A new sidecar is seeded from an empty source, which exiftool warns about
    # while still succeeding, so the file is the test.
    if not target.exists():
        raise RuntimeError(f"Could not write {target.name}: {result.stderr.strip()}")
    verify_xmp_written(target, {"ColorTemperature": values["ColorTemperature"]})
    return target


def embed_xmp_adjustments(dng_path: Path, adj: dict) -> None:
    values = _auto_xmp_values(adj)
    xmp_args = ([f"-XMP-crs:{k}={v}" for k, v in values.items()]
                + [f"-XMP-xmp:CreatorTool={editversion.tool_label()}", "-overwrite_original"])

    result = run_tool(["exiftool"] + xmp_args + [str(dng_path)],
                      timeout=EXIFTOOL_TIMEOUT, what="exiftool",
                      capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"exiftool failed: {result.stderr.strip()}")

    # Colour temperature is the tag seen to vanish, so prove the write stuck.
    verify_xmp_written(dng_path, {"ColorTemperature": values["ColorTemperature"]})


# ---------------------------------------------------------------------------
# Edits made elsewhere
# ---------------------------------------------------------------------------

def detect_foreign_edits(path: Path) -> str | None:
    """
    Return the name of the software that last edited this file, if it was
    not this tool.

    A DNG exported from Lightroom carries its develop settings internally.
    Saving over them, or stripping them with a revert, destroys editing work
    that exists nowhere else unless the photographer also has a catalogue.
    """
    target = path if is_dng(path) else edit_target(path)
    if target != path and not target.exists():
        return None
    path = target
    meta = _exiftool_get(path, "XMP-crs:Version", "CreatorTool", "Software")

    version = meta.get("XMP-crs:Version", "-")
    creator = meta.get("CreatorTool", "-")
    software = meta.get("Software", "-")

    for value in (creator, software):
        if value and value != "-" and any(
            name in value for name in ("Lightroom", "Camera Raw", "Photoshop", "Bridge")
        ):
            return value

    # crs:Version without a recognised creator still means foreign settings.
    if version and version != "-":
        return "another Camera Raw application"
    return None


def has_sidecar(path: Path) -> bool:
    """True when a Lightroom-style .xmp sidecar sits beside this file."""
    return path.with_suffix(".xmp").exists()


# ---------------------------------------------------------------------------
# Revert to original
# ---------------------------------------------------------------------------

def strip_xmp_adjustments(dng_path: Path) -> None:
    """Remove all Camera Raw develop settings, returning the photo to a neutral render."""
    target = edit_target(dng_path)
    if target != dng_path and not target.exists():
        return  # no sidecar means nothing to clear
    with fsutil.lock_for(target):
        result = run_tool(
            ["exiftool", "-XMP-crs:all=", "-overwrite_original", str(target)],
            timeout=EXIFTOOL_TIMEOUT, what="exiftool",
            capture_output=True, text=True,
        )
    if result.returncode != 0:
        raise RuntimeError(f"exiftool failed: {result.stderr.strip()}")


def get_embedded_original_name(dng_path: Path) -> str | None:
    """Return the filename of the raw embedded by DNG Converter's -e flag, or None."""
    name = _exiftool_get(dng_path, "OriginalRawFileName").get("OriginalRawFileName", "-")
    return name if name and name != "-" else None


def extract_original_raw(dng_path: Path, output_dir: Path) -> Path:
    """
    Recover the untouched original raw file embedded inside the DNG.

    Extraction goes through the DNG Converter's own -x flag rather than
    exiftool: OriginalRawFileData hands back the compressed, chunked blob
    as stored, which is not a loadable raw file. Adobe's extractor
    reverses its own packing and returns the original bytes exactly.
    """
    if dng_path.suffix.lower() != ".dng":
        raise RuntimeError(f"{dng_path.name} is already a raw file, not a DNG")
    if not dng_converter_available():
        raise RuntimeError("Recovering an embedded original needs Adobe DNG Converter")
    original_name = get_embedded_original_name(dng_path)
    if not original_name:
        raise RuntimeError(
            f"{dng_path.name} has no embedded original "
            "(was it converted without the -e flag?)"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    result = run_tool(
        [str(DNG_CONVERTER), "-x", "-d", str(output_dir), str(dng_path)],
        timeout=CONVERTER_TIMEOUT,
        what=f"DNG Converter extracting {dng_path.name}",
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"DNG Converter extraction failed: {result.stderr.strip()}")

    out_path = output_dir / original_name
    if not out_path.exists():
        raise RuntimeError(
            f"Could not extract embedded original from {dng_path.name}: "
            f"expected {original_name}"
        )
    return out_path


def resolve_output_dir(cr2_path: Path, base_output: Path | None, date_folders: bool) -> Path:
    if base_output:
        out = base_output
    else:
        out = cr2_path.parent

    if date_folders:
        date = get_shoot_date(cr2_path)
        if date:
            out = out / date

    return out


def process_file(
    cr2_path: Path,
    base_output: Path | None,
    date_folders: bool,
    skip_existing: bool,
    verbose: bool,
    style_model=None,
) -> Path | None:
    """
    Convert one raw file and write its adjustments.

    ``style_model`` applies the learned correction, as the card ingest does.
    Without it the two paths disagreed: a shoot copied off a card came out
    in the photographer's own style while the same files converted by hand
    came out on the plain heuristic.
    """
    if not dng_converter_available():
        return process_file_portable(cr2_path, skip_existing, verbose, style_model)

    out_dir = resolve_output_dir(cr2_path, base_output, date_folders)
    dng_expected = out_dir / cr2_path.with_suffix(".dng").name

    if skip_existing and dng_expected.exists():
        if verbose:
            tqdm.write(f"  Skipping   {cr2_path.name} (already converted)")
        return dng_expected

    out_dir.mkdir(parents=True, exist_ok=True)

    adj = compute_auto_adjustments(cr2_path, style_model=style_model)

    if verbose:
        tqdm.write(
            f"  {cr2_path.name}: "
            f"WB={adj['color_temp']}K tint={adj['tint']:+d} "
            f"EV={adj['exposure']:+.2f} shadows={adj['shadows']:+d} "
            f"highlights={adj['highlights']:+d} NR={adj['luminance_nr']} "
            f"(ISO {adj['iso']})"
            + ("  [styled]" if adj.get("styled") else "")
        )

    dng_path = convert_cr2_to_dng(cr2_path, out_dir)
    embed_xmp_adjustments(dng_path, adj)
    return dng_path


def process_file_portable(
    raw_path: Path,
    skip_existing: bool,
    verbose: bool,
    style_model=None,
) -> Path:
    """
    Develop one raw without Adobe DNG Converter: the raw stays where it is and
    the auto adjustments go into a ``.xmp`` sidecar beside it. Returns the raw.
    """
    sidecar = raw_path.with_suffix(".xmp")
    if skip_existing and sidecar.exists():
        if verbose:
            tqdm.write(f"  Skipping   {raw_path.name} (sidecar exists)")
        return raw_path
    adj = compute_auto_adjustments(raw_path, style_model=style_model)
    if verbose:
        tqdm.write(
            f"  {raw_path.name}: WB={adj['color_temp']}K EV={adj['exposure']:+.2f} "
            f"(ISO {adj['iso']})" + ("  [styled]" if adj.get("styled") else "")
        )
    write_sidecar_adjustments(raw_path, adj)
    return raw_path


# ---------------------------------------------------------------------------
# Subcommand: convert
# ---------------------------------------------------------------------------

def cmd_convert(args: argparse.Namespace) -> None:
    # Setup instructions and errors go to stderr, which is unbuffered, while
    # stdout is block-buffered when piped — so without this the upload
    # instructions appeared above the conversion summary that precedes them.
    _unbuffer_stdout()

    cr2_files: list[Path] = []
    for inp in args.inputs:
        inp = Path(inp)
        if inp.is_dir():
            # Already-converted DNGs are skipped: converting a DNG to a DNG
            # is not what anyone means by pointing this at a folder.
            cr2_files.extend(
                p for p in list_photos(inp) if p.suffix.lower() != ".dng"
            )
        elif inp.suffix.lower() in RAW_SUFFIXES:
            cr2_files.append(inp)
        else:
            print(f"Skipping {inp} (not a raw file or directory)", file=sys.stderr)

    cr2_files = sorted(set(cr2_files))
    if not cr2_files:
        print("No raw files found.", file=sys.stderr)
        sys.exit(1)

    output = Path(args.output) if args.output else None
    converted: list[Path] = []
    failed: list[str] = []

    # Fitted once for the whole run rather than per file.
    style_model = _load_style_model(verbose=args.verbose)

    jobs = max(1, int(getattr(args, "jobs", DEFAULT_JOBS)))
    jobs = min(jobs, len(cr2_files))

    if jobs == 1:
        with tqdm(cr2_files, unit="file", desc="Converting") as bar:
            for cr2 in bar:
                bar.set_postfix_str(cr2.name)
                try:
                    dng = process_file(cr2, output, args.date_folders,
                                       args.skip_existing, args.verbose,
                                       style_model=style_model)
                    if dng:
                        converted.append(dng)
                except Exception as exc:
                    tqdm.write(f"  ERROR {cr2.name}: {exc}", file=sys.stderr)
                    failed.append(cr2.name)
    else:
        # Results are collected as they finish, then sorted, so the summary
        # does not depend on which worker happened to get there first.
        lock = threading.Lock()
        with tqdm(total=len(cr2_files), unit="file", desc=f"Converting x{jobs}") as bar:
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                futures = {
                    pool.submit(
                        process_file, cr2, output,
                        args.date_folders, args.skip_existing, args.verbose,
                        style_model,
                    ): cr2
                    for cr2 in cr2_files
                }
                for future in as_completed(futures):
                    cr2 = futures[future]
                    try:
                        dng = future.result()
                        if dng:
                            with lock:
                                converted.append(dng)
                    except Exception as exc:
                        tqdm.write(f"  ERROR {cr2.name}: {exc}", file=sys.stderr)
                        with lock:
                            failed.append(cr2.name)
                    bar.update(1)
                    bar.set_postfix_str(cr2.name)
        converted.sort()
        failed.sort()

    print(f"\nConverted {len(converted)} file(s)", end="")
    if failed:
        print(f", {len(failed)} failed: {', '.join(failed)}")
    else:
        print(".")

    _do_uploads(converted, args)


def _load_style_model(verbose: bool = False):
    """
    The learned style, or None when nothing has been learned yet.

    A failure here must not stop a conversion: an unreadable store means
    the plain heuristic, not a lost shoot.
    """
    try:
        import style

        model = style.load_model()
    except Exception:
        return None

    if model.is_trained:
        if verbose:
            print(f"  Using learned style ({model.n_samples} edits, "
                  f"{model.confidence:.0%} confidence)")
        return model
    return None


def _do_uploads(dngs: list[Path], args: argparse.Namespace) -> None:
    if not dngs:
        return
    if getattr(args, "upload_google", False):
        _upload_google(dngs, args.verbose)
    if getattr(args, "upload_amazon", False):
        _upload_amazon(dngs, args.verbose)


def _upload_google(dngs: list[Path], verbose: bool) -> None:
    try:
        from uploader import upload_to_google_photos
    except ImportError:
        print("uploader.py not found.", file=sys.stderr)
        return
    print(f"\nUploading {len(dngs)} DNG(s) to Google Photos...")
    try:
        n = upload_to_google_photos(dngs, verbose=verbose)
        print(f"Uploaded {n} file(s) to Google Photos.")
    except FileNotFoundError:
        # The setup instructions have already gone to stderr, but saying
        # nothing here leaves "Uploading..." as the last word and looks like
        # the upload is still running.
        print("Google Photos upload skipped — credentials.json not found "
              "(setup steps above).")
    except Exception as exc:
        print(f"Google Photos upload failed: {exc}")
        print(f"Google Photos upload failed: {exc}", file=sys.stderr)


def _upload_amazon(dngs: list[Path], verbose: bool) -> None:
    try:
        import uploader
        from uploader import upload_to_amazon_photos
    except ImportError:
        print("uploader.py not found.", file=sys.stderr)
        return
    try:
        n = upload_to_amazon_photos(dngs, verbose=verbose)
        print(f"\nUploaded {n} file(s) to Amazon Photos.")
    except uploader.AmazonUnavailable as exc:
        # Announced before the attempt rather than after, so the message is
        # not preceded by an "Uploading..." line that never completes.
        print(f"\nAmazon Photos: {exc}")
        return
    except FileNotFoundError:
        print("\nAmazon Photos upload skipped — amazon_credentials.json not found "
              "(setup steps above).")
    except Exception as exc:
        print(f"Amazon Photos upload failed: {exc}")
        print(f"Amazon Photos upload failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Subcommand: revert
# ---------------------------------------------------------------------------

def cmd_revert(args: argparse.Namespace) -> None:
    _unbuffer_stdout()

    dng_files: list[Path] = []
    for inp in args.inputs:
        inp = Path(inp)
        if inp.is_dir():
            dng_files.extend(sorted(inp.glob("**/*.dng")) + sorted(inp.glob("**/*.DNG")))
        elif inp.suffix.lower() == ".dng":
            dng_files.append(inp)
        else:
            print(f"Skipping {inp} (not a DNG file or directory)", file=sys.stderr)

    if not dng_files:
        print("No DNG files found.", file=sys.stderr)
        sys.exit(1)

    # Refuse to strip settings this tool did not write, unless told to.
    if not args.extract_original and not args.force:
        foreign = []
        for dng in dng_files:
            who = detect_foreign_edits(dng)
            if who:
                foreign.append((dng, who))

        if foreign:
            print(f"\n{len(foreign)} of {len(dng_files)} file(s) carry edits made "
                  "in other software:", file=sys.stderr)
            for dng, who in foreign[:5]:
                print(f"  {dng.name}: {who}", file=sys.stderr)
            if len(foreign) > 5:
                print(f"  ...and {len(foreign) - 5} more", file=sys.stderr)
            print(
                "\nReverting would discard that work. Re-run with --force to do it "
                "anyway,\nor use --extract-original, which only writes the embedded "
                "raw back out\nand changes nothing.",
                file=sys.stderr,
            )
            sys.exit(1)

    reverted = 0
    failed: list[str] = []

    with tqdm(dng_files, unit="file", desc="Reverting") as bar:
        for dng in bar:
            bar.set_postfix_str(dng.name)
            try:
                if args.extract_original:
                    out_dir = Path(args.output) if args.output else dng.parent
                    recovered = extract_original_raw(dng, out_dir)
                    if args.verbose:
                        tqdm.write(f"  {dng.name} → {recovered}")
                else:
                    strip_xmp_adjustments(dng)
                    if args.verbose:
                        tqdm.write(f"  {dng.name}: adjustments cleared")
                reverted += 1
            except Exception as exc:
                tqdm.write(f"  ERROR {dng.name}: {exc}", file=sys.stderr)
                failed.append(dng.name)

    action = "Recovered original raw for" if args.extract_original else "Reverted"
    print(f"\n{action} {reverted} file(s)", end="")
    if failed:
        print(f", {len(failed)} failed: {', '.join(failed)}")
    else:
        print(".")


def cmd_export(args: argparse.Namespace) -> None:
    _unbuffer_stdout()
    import exporter

    sources: list[Path] = []
    roots: list[Path] = []      # library folders, whose virtual copies are exported too
    for inp in args.inputs:
        inp = Path(inp)
        if inp.is_dir():
            sources.extend(list_photos(inp))
            roots.append(inp.resolve())
        elif inp.is_file():
            sources.append(inp)
        else:
            print(f"Skipping {inp} (not found)", file=sys.stderr)
    sources = sorted(set(sources))
    if not sources:
        print("No photos found.", file=sys.stderr)
        sys.exit(1)

    try:
        opts = exporter.ExportOptions(
            format=args.format, quality=args.quality, long_edge=args.long_edge,
            colorspace=args.colorspace, template=args.template,
            keep_metadata=not args.no_metadata, metadata=args.metadata, bit_depth=args.bit_depth,
        ).validate()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)

    out_dir = Path(args.output) if args.output else Path.cwd() / "export"
    written, failed = [], []
    # (raw, virtual copy or None): each virtual copy exports as its own file.
    jobs: list[tuple[Path, dict | None]] = []
    for src in sources:
        jobs.append((src, None))
        for root in roots:
            if root in src.resolve().parents:
                import catalog
                rel = src.resolve().relative_to(root).as_posix()
                jobs.extend((src, v) for v in catalog.virtuals_of(root, rel))
                break
    for seq, (src, virt) in enumerate(tqdm(jobs, unit="file", desc="Exporting"), start=1):
        try:
            dest = exporter.export_photo(src, out_dir, opts, seq=seq, virtual=virt)
            written.append(dest)
            if args.verbose:
                tqdm.write(f"  {src.name} -> {dest}")
        except Exception as exc:
            tqdm.write(f"  ERROR {src.name}: {exc}", file=sys.stderr)
            failed.append(src.name)
    print(f"\nExported {len(written)} file(s) to {out_dir}", end="")
    print(f", {len(failed)} failed: {', '.join(failed)}" if failed else ".")
    if failed:
        sys.exit(1)


def _unbuffer_stdout() -> None:
    """Switch stdout to line buffering, for long-running commands."""
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):  # pragma: no cover - exotic streams
        pass


# ---------------------------------------------------------------------------
# Subcommand: cull
# ---------------------------------------------------------------------------

def cmd_cull(args: argparse.Namespace) -> None:
    import quality

    # Errors go to stderr, which is unbuffered, while stdout is block-buffered
    # when piped — so without this the failures appeared above the summary that
    # was supposed to introduce them.
    _unbuffer_stdout()

    paths: list[Path] = []
    for inp in args.inputs:
        p = Path(inp)
        if p.is_dir():
            paths.extend(list_photos(p))
        elif p.suffix.lower() in RAW_SUFFIXES:
            paths.append(p)
    paths = sorted(set(paths))

    if not paths:
        print("No raw files found.", file=sys.stderr)
        sys.exit(1)

    assessments: dict[str, dict] = {}
    failed: list[str] = []

    with tqdm(paths, unit="file", desc="Assessing") as bar:
        for p in bar:
            bar.set_postfix_str(p.name)
            try:
                rgb, _, _ = decode_for_analysis(p)
                # Keyed by full path: two shoots both containing IMG_0001
                # silently overwrote each other, under-reporting the count.
                assessments[str(p)] = quality.assess(rgb)
            except Exception as exc:
                tqdm.write(f"  ERROR {p.name}: {exc}", file=sys.stderr)
                failed.append(p.name)

    unusable = {n: a for n, a in assessments.items() if a["verdict"]}

    total = len(assessments) + len(failed)
    if failed:
        # Saying "1 frame assessed" of four hides three files rather than
        # reporting them.
        print(f"\n{len(assessments)} of {total} frame(s) assessed "
              f"— {len(failed)} could not be read")
    else:
        print(f"\n{len(assessments)} frame(s) assessed")

    if unusable:
        print(f"\n{len(unusable)} unusable — no adjustment recovers these:")
        for name, a in sorted(unusable.items()):
            print(f"  {Path(name).name:<20} {a['verdict']:<6} {a['reason']}")
    else:
        print("\nNo unusable frames.")

    if args.sharpness:
        ranked = quality.rank_by_sharpness(assessments)
        shown = ranked[:args.limit]
        print(f"\nLeast detailed {len(shown)} of {len(ranked)} usable frames:")
        print(f"  {'frame':<20} {'score':>8} {'percentile':>11}")
        for name, score, pct in shown:
            print(f"  {Path(name).name:<20} {score:8.1f} {pct:>10.0f}%")
        print("\nThis ranks how much fine detail each frame contains, which is not")
        print("the same as whether focus was missed — a sharp subject against smooth")
        print("grass scores low. Use it to decide what to look at, not what to delete.")

    if failed:
        shown = ", ".join(failed[:5])
        more = f" and {len(failed) - 5} more" if len(failed) > 5 else ""
        print(f"\nUnreadable: {shown}{more}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Subcommand: style
# ---------------------------------------------------------------------------

def cmd_style(args: argparse.Namespace) -> None:
    import style as style_mod

    if args.clear:
        removed = style_mod.clear_history()
        print(f"Forgot {removed} recorded edit(s).")
        return

    summary = style_mod.summarize()
    if summary.get("error"):
        print(f"Could not read the style store: {summary['error']}", file=sys.stderr)
        sys.exit(1)

    n = summary["n_samples"]
    if n == 0:
        print("No edits recorded yet.")
        print("Save some edits in the editor and your style will be learned from them.")
        return

    print(f"{n} edit(s) recorded")
    if not summary["trained"]:
        print(f"Not enough yet to predict — needs {style_mod.MIN_SAMPLES}.")
        return

    print(f"Confidence: {summary['confidence']:.0%} "
          f"(share of the learned correction currently applied)\n")
    print(f"  {'adjustment':<12} {'tendency':>10}  {'consistency':>11}  {'edits':>6}")
    for key, t in sorted(
        summary["tendencies"].items(),
        key=lambda kv: -abs(kv[1]["mean_correction"]),
    ):
        print(f"  {key:<12} {t['mean_correction']:>+10.2f}  "
              f"{t['consistency']:>11.2f}  {t['samples']:>6}")
    print("\nTendency is the average amount you move a slider past the automatic value.")
    print("Consistency near 1.0 means you do it the same way every time.")


# ---------------------------------------------------------------------------
# Subcommand: watch
# ---------------------------------------------------------------------------

def cmd_watch(args: argparse.Namespace) -> None:
    # A watcher runs for hours, usually with its output redirected to a log or
    # a pipe — where Python block-buffers stdout and nothing appears until the
    # process exits. Line buffering makes progress visible as it happens.
    _unbuffer_stdout()

    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler, FileCreatedEvent
    except ImportError:
        print("watchdog not installed. Run: uv add watchdog", file=sys.stderr)
        sys.exit(1)

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"Not a directory: {folder}", file=sys.stderr)
        sys.exit(1)

    output = Path(args.output) if args.output else None
    watch_style_model = _load_style_model(verbose=args.verbose)

    class CR2Handler(FileSystemEventHandler):
        def on_created(self, event: FileCreatedEvent):
            if event.is_directory:
                return
            path = Path(event.src_path)
            if path.suffix.lower() not in RAW_SUFFIXES or path.suffix.lower() == ".dng":
                return

            # Wait for the file to finish writing (size stabilizes)
            prev_size = -1
            for _ in range(30):
                time.sleep(0.5)
                try:
                    size = path.stat().st_size
                except FileNotFoundError:
                    return
                if size == prev_size:
                    break
                prev_size = size

            print(f"\nNew file detected: {path.name}")
            try:
                dng = process_file(path, output, args.date_folders,
                                   args.skip_existing, args.verbose,
                                   style_model=watch_style_model)
                if dng:
                    print(f"Done → {dng}")
                    _do_uploads([dng], args)
            except Exception as exc:
                print(f"ERROR: {exc}", file=sys.stderr)

    observer = Observer()
    observer.schedule(CR2Handler(), str(folder), recursive=True)
    observer.start()
    print(f"Watching {folder} for new CR2 files... (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    # Adobe DNG Converter is optional. Without it, convert/watch/ingest leave
    # the raw untouched and store edits in an .xmp sidecar (see edit_target).
    if sys.argv[1:2] in (["convert"], ["watch"]) and not dng_converter_available():
        print(
            "Note: Adobe DNG Converter not found; developing without DNG "
            "(edits go to .xmp sidecars beside each raw).",
            file=sys.stderr,
        )

    parser = argparse.ArgumentParser(
        description="CR2 → DNG converter with automatic tone and white balance adjustments.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python process.py convert ~/Pictures/shoot/ -o ~/Pictures/dng/ --date-folders\n"
            "  python process.py convert IMG_0001.CR2 --upload-google --upload-amazon -v\n"
            "  python process.py watch ~/Pictures/import/ -o ~/Pictures/dng/ --upload-google --upload-amazon\n"
            "  python process.py revert ~/Pictures/dng/IMG_0001.dng\n"
            "  python process.py revert ~/Pictures/dng/ --extract-original -o ~/Pictures/recovered/\n"
            "  python process.py export ~/Pictures/shoot/ -o ~/Pictures/out/ --format jpeg --quality 90\n"
            "  python process.py style\n"
        ),
    )
    subs = parser.add_subparsers(dest="cmd")

    def _add_upload_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--upload-google", action="store_true", help="Upload DNGs to Google Photos")
        p.add_argument("--upload-amazon", action="store_true",
                       help="Upload DNGs to Amazon Photos (NOT POSSIBLE — Amazon "
                            "retired the API; see README)")

    # -- convert --
    conv = subs.add_parser("convert", help="Convert CR2 file(s) or folder(s) to DNG")
    conv.add_argument("inputs", nargs="+", help="CR2 file(s) or folder(s)")
    conv.add_argument("-o", "--output", help="Output directory")
    conv.add_argument("--date-folders", action="store_true", help="Organize into YYYY/MM/DD subfolders")
    conv.add_argument("--skip-existing", action="store_true", default=True, help="Skip already-converted files (default: on)")
    conv.add_argument("--no-skip", dest="skip_existing", action="store_false", help="Re-convert even if DNG exists")
    conv.add_argument("-v", "--verbose", action="store_true", help="Print adjustment values per file")
    conv.add_argument(
        "-j", "--jobs", type=int, default=DEFAULT_JOBS,
        help=f"Files to convert at once (default: {DEFAULT_JOBS}; 1 disables parallelism)",
    )
    _add_upload_flags(conv)

    # -- watch --
    watch = subs.add_parser("watch", help="Watch a folder and auto-convert new CR2 files")
    watch.add_argument("folder", help="Folder to watch")
    watch.add_argument("-o", "--output", help="Output directory for DNGs")
    watch.add_argument("--date-folders", action="store_true", help="Organize into YYYY/MM/DD subfolders")
    watch.add_argument("--skip-existing", action="store_true", default=True)
    watch.add_argument("-v", "--verbose", action="store_true")
    _add_upload_flags(watch)

    # -- revert --
    rev = subs.add_parser("revert", help="Undo edits on DNG(s) or recover the original raw")
    rev.add_argument("inputs", nargs="+", help="DNG file(s) or folder(s)")
    rev.add_argument(
        "--extract-original",
        action="store_true",
        help="Write out the untouched original raw embedded at conversion time",
    )
    rev.add_argument("-o", "--output", help="Where to write recovered originals (default: alongside the DNG)")
    rev.add_argument("-v", "--verbose", action="store_true", help="Print each file as it is reverted")
    rev.add_argument("--force", action="store_true",
                     help="Revert even files carrying edits made in other software")

    # -- cull --
    cull = subs.add_parser("cull", help="Find unusable frames and rank the rest by detail")
    cull.add_argument("inputs", nargs="+", help="Raw file(s) or folder(s)")
    cull.add_argument("--sharpness", action="store_true", help="Also rank frames by detail")
    cull.add_argument("--limit", type=int, default=15, help="How many to list (default: 15)")

    # -- export --
    ex = subs.add_parser("export", help="Render edited photos to PNG/JPEG, keeping all metadata")
    ex.add_argument("inputs", nargs="+", help="Raw/DNG file(s) or folder(s)")
    ex.add_argument("-o", "--output", help="Output folder (default: ./export)")
    ex.add_argument("--format", choices=["png", "jpeg", "jpg", "tiff", "tif", "webp"], default="jpeg")
    ex.add_argument("--quality", type=int, default=92, help="JPEG quality 1-100 (default: 92)")
    ex.add_argument("--long-edge", type=int, help="Resize so the longer side is this many pixels (never upscales)")
    ex.add_argument("--colorspace", choices=["srgb", "adobergb", "prophoto"],
                    help="Output colour space (raw default: srgb; other images keep their profile)")
    ex.add_argument("--template", default="{name}",
                    help="Filename template: {name} {date} {seq} {width} {height} (default: {name})")
    ex.add_argument("--bit-depth", type=int, choices=[8, 16], default=8,
                    help="PNG bit depth per channel (16 is PNG only; default: 8)")
    ex.add_argument("--metadata", choices=["all", "copyright", "no_location", "no_develop", "none"],
                    default="all", help="Which metadata to copy (default all)")
    ex.add_argument("--no-metadata", action="store_true", help="Do not copy EXIF/GPS/IPTC/XMP from the source")
    ex.add_argument("-v", "--verbose", action="store_true")

    # -- style --
    st = subs.add_parser("style", help="Show what has been learned from your edits")
    st.add_argument("--clear", action="store_true", help="Forget every recorded edit")

    args = parser.parse_args()

    if args.cmd == "style":
        cmd_style(args)
        return
    if args.cmd == "cull":
        cmd_cull(args)
        return
    if args.cmd == "export":
        cmd_export(args)
        return

    if args.cmd == "convert":
        cmd_convert(args)
    elif args.cmd == "watch":
        cmd_watch(args)
    elif args.cmd == "revert":
        cmd_revert(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
