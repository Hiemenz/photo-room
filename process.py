#!/usr/bin/env python3
"""
CR2 → DNG converter with automatic exposure, shadows, highlights, and white balance adjustments.
Outputs DNG files with embedded XMP Camera Raw adjustments (compatible with Lightroom/ACR,
Google Photos, and Amazon Photos).
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import rawpy


DNG_CONVERTER = Path(
    "/Applications/Adobe DNG Converter.app/Contents/MacOS/Adobe DNG Converter"
)


def convert_cr2_to_dng(cr2_path: Path, output_dir: Path) -> Path:
    """Run Adobe DNG Converter on a single CR2 file, return the output DNG path."""
    cmd = [
        str(DNG_CONVERTER),
        "-d", str(output_dir),
        "-l",          # linear DNG (preserves raw data)
        "-e",          # embed original raw
        str(cr2_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"DNG Converter failed for {cr2_path.name}:\n{result.stderr}")
    dng_path = output_dir / cr2_path.with_suffix(".dng").name
    if not dng_path.exists():
        raise FileNotFoundError(f"Expected DNG not found: {dng_path}")
    return dng_path


def compute_auto_adjustments(cr2_path: Path) -> dict:
    """
    Analyze the raw image data and compute Camera Raw-style adjustment values.

    Returns a dict with keys matching Adobe Camera Raw XMP tags:
      Exposure2012, Shadows2012, Highlights2012, WhiteBalance, ColorTemperature, Tint
    """
    with rawpy.imread(str(cr2_path)) as raw:
        # --- White Balance ---
        # Camera-measured WB multipliers (RGGB order)
        cam_mul = np.array(raw.camera_whitebalance[:4], dtype=float)
        cam_mul /= cam_mul[1]  # normalize to green channel

        # Estimate color temperature from R/B ratio
        # Empirical mapping: R/B ~0.6 → ~3000K, ~1.0 → ~5500K, ~1.5 → ~7500K
        rb_ratio = cam_mul[0] / cam_mul[2]
        color_temp = int(np.clip(5500 / rb_ratio, 2500, 10000))

        # Tint: deviation of G1 vs G2 channels (positive = magenta, negative = green)
        if len(cam_mul) >= 4 and cam_mul[3] > 0:
            tint = int(np.clip((cam_mul[1] - cam_mul[3]) * 20, -50, 50))
        else:
            tint = 0

        # --- Exposure / Shadows / Highlights ---
        # Render a fast preview at reduced size to analyze the tone distribution
        rgb = raw.postprocess(
            use_camera_wb=True,
            half_size=True,
            no_auto_bright=True,
            output_bps=16,
        )

        # Normalize to [0, 1]
        img = rgb.astype(np.float32) / 65535.0
        lum = 0.2126 * img[:, :, 0] + 0.7152 * img[:, :, 1] + 0.0722 * img[:, :, 2]

        # Exposure: aim for median luminance around 0.40 (slightly below mid-tone)
        median_lum = float(np.median(lum))
        if median_lum > 0.001:
            target = 0.40
            ev_shift = np.log2(target / median_lum)
            # Clamp to a conservative range; heavy adjustments belong in editing
            exposure = float(np.clip(ev_shift, -2.0, 2.0))
            exposure = round(exposure * 4) / 4  # round to nearest 0.25 EV
        else:
            exposure = 0.0

        # Shadows: measure 10th percentile; lift if image is blocking up in the shadows
        p10 = float(np.percentile(lum, 10))
        if p10 < 0.05:
            # Significant shadow blocking — lift shadows noticeably
            shadows = int(np.clip((0.10 - p10) / 0.10 * 60, 0, 80))
        elif p10 < 0.12:
            shadows = int(np.clip((0.12 - p10) / 0.12 * 30, 0, 40))
        else:
            shadows = 0

        # Highlights: measure 90th percentile; pull back if highlights are clipping
        p90 = float(np.percentile(lum, 90))
        if p90 > 0.92:
            highlights = int(np.clip((p90 - 0.92) / 0.08 * -80, -100, 0))
        elif p90 > 0.80:
            highlights = int(np.clip((p90 - 0.80) / 0.12 * -30, -50, 0))
        else:
            highlights = 0

    return {
        "exposure": exposure,
        "shadows": shadows,
        "highlights": highlights,
        "color_temp": color_temp,
        "tint": tint,
    }


def embed_xmp_adjustments(dng_path: Path, adj: dict) -> None:
    """Write Camera Raw XMP adjustment tags into the DNG using exiftool."""
    # Map our values to Adobe Camera Raw 2012 process XMP tags
    xmp_args = [
        "-XMP-crs:ProcessVersion=11.0",
        "-XMP-crs:WhiteBalance=Custom",
        f"-XMP-crs:ColorTemperature={adj['color_temp']}",
        f"-XMP-crs:Tint={adj['tint']}",
        f"-XMP-crs:Exposure2012={adj['exposure']:.2f}",
        f"-XMP-crs:Shadows2012={adj['shadows']}",
        f"-XMP-crs:Highlights2012={adj['highlights']}",
        "-overwrite_original",
    ]
    cmd = ["exiftool"] + xmp_args + [str(dng_path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"exiftool failed for {dng_path.name}:\n{result.stderr}")


def process_file(cr2_path: Path, output_dir: Path, verbose: bool = False) -> Path:
    print(f"  Analyzing  {cr2_path.name}...")
    adj = compute_auto_adjustments(cr2_path)

    if verbose:
        print(
            f"    WB: {adj['color_temp']}K  tint:{adj['tint']:+d}  "
            f"EV:{adj['exposure']:+.2f}  shadows:{adj['shadows']:+d}  "
            f"highlights:{adj['highlights']:+d}"
        )

    print(f"  Converting {cr2_path.name} → DNG...")
    dng_path = convert_cr2_to_dng(cr2_path, output_dir)

    print(f"  Embedding  adjustments into {dng_path.name}...")
    embed_xmp_adjustments(dng_path, adj)

    return dng_path


def main():
    parser = argparse.ArgumentParser(
        description="Convert CR2 files to DNG with automatic tone and white balance adjustments."
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="CR2 file(s) or folder(s)")
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Output directory (default: same folder as each input file)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Print adjustment values")
    args = parser.parse_args()

    if not DNG_CONVERTER.exists():
        print(
            "ERROR: Adobe DNG Converter not found.\n"
            "Download it free from: https://helpx.adobe.com/camera-raw/using/adobe-dng-converter.html",
            file=sys.stderr,
        )
        sys.exit(1)

    # Gather all CR2 files
    cr2_files: list[Path] = []
    for inp in args.inputs:
        if inp.is_dir():
            cr2_files.extend(sorted(inp.glob("**/*.CR2")) + sorted(inp.glob("**/*.cr2")))
        elif inp.suffix.lower() == ".cr2":
            cr2_files.append(inp)
        else:
            print(f"Skipping {inp} (not a CR2 file or directory)", file=sys.stderr)

    if not cr2_files:
        print("No CR2 files found.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(cr2_files)} CR2 file(s).\n")
    success, failed = 0, []

    for cr2 in cr2_files:
        out_dir = args.output if args.output else cr2.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            dng = process_file(cr2, out_dir, verbose=args.verbose)
            print(f"  Done → {dng}\n")
            success += 1
        except Exception as exc:
            print(f"  ERROR: {exc}\n", file=sys.stderr)
            failed.append(cr2.name)

    print(f"Finished: {success} converted", end="")
    if failed:
        print(f", {len(failed)} failed: {', '.join(failed)}")
    else:
        print(".")


if __name__ == "__main__":
    main()
