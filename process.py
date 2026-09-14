#!/usr/bin/env python3
"""
CR2 → DNG converter with automatic tone + white balance adjustments.

Subcommands:
  convert   Convert CR2 files to DNG (default)
  watch     Watch a folder and auto-convert new CR2 files
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import rawpy
from tqdm import tqdm


DNG_CONVERTER = Path(
    "/Applications/Adobe DNG Converter.app/Contents/MacOS/Adobe DNG Converter"
)


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------

def _exiftool_get(path: Path, *tags: str) -> dict[str, str]:
    """Return a dict of {tag: value} from exiftool for the given file."""
    args = [f"-{t}" for t in tags] + ["-s3", "-f", str(path)]
    result = subprocess.run(["exiftool"] + args, capture_output=True, text=True)
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

def compute_auto_adjustments(cr2_path: Path) -> dict:
    """
    Analyze raw image data and compute Camera Raw-style XMP adjustment values.
    Returns: exposure, shadows, highlights, color_temp, tint, luminance_nr
    """
    iso = get_iso(cr2_path)

    with rawpy.imread(str(cr2_path)) as raw:
        # --- White Balance ---
        cam_mul = np.array(raw.camera_whitebalance[:4], dtype=float)
        cam_mul /= cam_mul[1]

        # R/B ratio → color temperature (empirical mapping)
        rb_ratio = cam_mul[0] / cam_mul[2]
        color_temp = int(np.clip(5500 / rb_ratio, 2500, 10000))

        # Tint from G1/G2 channel imbalance
        tint = 0
        if len(cam_mul) >= 4 and cam_mul[3] > 0:
            tint = int(np.clip((cam_mul[1] - cam_mul[3]) * 20, -50, 50))

        # --- Render preview for tone analysis ---
        rgb = raw.postprocess(
            use_camera_wb=True,
            half_size=True,
            no_auto_bright=True,
            output_bps=16,
        )

    img = rgb.astype(np.float32) / 65535.0
    lum = 0.2126 * img[:, :, 0] + 0.7152 * img[:, :, 1] + 0.0722 * img[:, :, 2]

    # Exposure: shift median toward 0.40
    median_lum = float(np.median(lum))
    if median_lum > 0.001:
        ev_shift = np.log2(0.40 / median_lum)
        exposure = round(float(np.clip(ev_shift, -2.0, 2.0)) * 4) / 4
    else:
        exposure = 0.0

    # Shadows: lift based on 10th-percentile darkness
    p10 = float(np.percentile(lum, 10))
    if p10 < 0.05:
        shadows = int(np.clip((0.10 - p10) / 0.10 * 60, 0, 80))
    elif p10 < 0.12:
        shadows = int(np.clip((0.12 - p10) / 0.12 * 30, 0, 40))
    else:
        shadows = 0

    # Highlights: pull back based on 90th-percentile brightness
    p90 = float(np.percentile(lum, 90))
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

    return {
        "exposure": exposure,
        "shadows": shadows,
        "highlights": highlights,
        "color_temp": color_temp,
        "tint": tint,
        "luminance_nr": luminance_nr,
        "iso": iso,
    }


# ---------------------------------------------------------------------------
# Conversion pipeline
# ---------------------------------------------------------------------------

def convert_cr2_to_dng(cr2_path: Path, output_dir: Path) -> Path:
    cmd = [
        str(DNG_CONVERTER),
        "-d", str(output_dir),
        "-l",   # linear DNG (preserves raw data)
        "-e",   # embed original raw
        str(cr2_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"DNG Converter failed: {result.stderr.strip()}")
    dng_path = output_dir / cr2_path.with_suffix(".dng").name
    if not dng_path.exists():
        raise FileNotFoundError(f"Expected DNG not found: {dng_path}")
    return dng_path


def embed_xmp_adjustments(dng_path: Path, adj: dict) -> None:
    xmp_args = [
        "-XMP-crs:ProcessVersion=11.0",
        "-XMP-crs:WhiteBalance=Custom",
        f"-XMP-crs:ColorTemperature={adj['color_temp']}",
        f"-XMP-crs:Tint={adj['tint']}",
        f"-XMP-crs:Exposure2012={adj['exposure']:.2f}",
        f"-XMP-crs:Shadows2012={adj['shadows']}",
        f"-XMP-crs:Highlights2012={adj['highlights']}",
        f"-XMP-crs:LuminanceSmoothing={adj['luminance_nr']}",
        "-overwrite_original",
    ]
    result = subprocess.run(["exiftool"] + xmp_args + [str(dng_path)], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"exiftool failed: {result.stderr.strip()}")


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
) -> Path | None:
    out_dir = resolve_output_dir(cr2_path, base_output, date_folders)
    dng_expected = out_dir / cr2_path.with_suffix(".dng").name

    if skip_existing and dng_expected.exists():
        if verbose:
            tqdm.write(f"  Skipping   {cr2_path.name} (already converted)")
        return dng_expected

    out_dir.mkdir(parents=True, exist_ok=True)

    adj = compute_auto_adjustments(cr2_path)

    if verbose:
        tqdm.write(
            f"  {cr2_path.name}: "
            f"WB={adj['color_temp']}K tint={adj['tint']:+d} "
            f"EV={adj['exposure']:+.2f} shadows={adj['shadows']:+d} "
            f"highlights={adj['highlights']:+d} NR={adj['luminance_nr']} "
            f"(ISO {adj['iso']})"
        )

    dng_path = convert_cr2_to_dng(cr2_path, out_dir)
    embed_xmp_adjustments(dng_path, adj)
    return dng_path


# ---------------------------------------------------------------------------
# Subcommand: convert
# ---------------------------------------------------------------------------

def cmd_convert(args: argparse.Namespace) -> None:
    cr2_files: list[Path] = []
    for inp in args.inputs:
        inp = Path(inp)
        if inp.is_dir():
            cr2_files.extend(sorted(inp.glob("**/*.CR2")) + sorted(inp.glob("**/*.cr2")))
        elif inp.suffix.lower() == ".cr2":
            cr2_files.append(inp)
        else:
            print(f"Skipping {inp} (not a CR2 file or directory)", file=sys.stderr)

    if not cr2_files:
        print("No CR2 files found.", file=sys.stderr)
        sys.exit(1)

    output = Path(args.output) if args.output else None
    converted: list[Path] = []
    failed: list[str] = []

    with tqdm(cr2_files, unit="file", desc="Converting") as bar:
        for cr2 in bar:
            bar.set_postfix_str(cr2.name)
            try:
                dng = process_file(cr2, output, args.date_folders, args.skip_existing, args.verbose)
                if dng:
                    converted.append(dng)
            except Exception as exc:
                tqdm.write(f"  ERROR {cr2.name}: {exc}", file=sys.stderr)
                failed.append(cr2.name)

    print(f"\nConverted {len(converted)} file(s)", end="")
    if failed:
        print(f", {len(failed)} failed: {', '.join(failed)}")
    else:
        print(".")

    if args.upload and converted:
        _do_upload(converted, args.verbose)


def _do_upload(dngs: list[Path], verbose: bool) -> None:
    try:
        from uploader import upload_to_google_photos
    except ImportError:
        print("uploader.py not found — skipping Google Photos upload.", file=sys.stderr)
        return

    print(f"\nUploading {len(dngs)} DNG(s) to Google Photos...")
    try:
        n = upload_to_google_photos(dngs, verbose=verbose)
        print(f"Uploaded {n} file(s) to Google Photos.")
    except FileNotFoundError:
        pass  # uploader already printed setup instructions
    except Exception as exc:
        print(f"Upload failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Subcommand: watch
# ---------------------------------------------------------------------------

def cmd_watch(args: argparse.Namespace) -> None:
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler, FileCreatedEvent
    except ImportError:
        print("watchdog not installed. Run: poetry add watchdog", file=sys.stderr)
        sys.exit(1)

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"Not a directory: {folder}", file=sys.stderr)
        sys.exit(1)

    output = Path(args.output) if args.output else None

    class CR2Handler(FileSystemEventHandler):
        def on_created(self, event: FileCreatedEvent):
            if event.is_directory:
                return
            path = Path(event.src_path)
            if path.suffix.lower() != ".cr2":
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
                dng = process_file(path, output, args.date_folders, args.skip_existing, args.verbose)
                if dng:
                    print(f"Done → {dng}")
                    if args.upload:
                        _do_upload([dng], args.verbose)
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
    if not DNG_CONVERTER.exists():
        print(
            "ERROR: Adobe DNG Converter not found.\n"
            "Download free from: https://helpx.adobe.com/camera-raw/using/adobe-dng-converter.html",
            file=sys.stderr,
        )
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="CR2 → DNG converter with automatic tone and white balance adjustments.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python process.py convert ~/Pictures/shoot/ -o ~/Pictures/dng/ --date-folders\n"
            "  python process.py convert IMG_0001.CR2 --upload --verbose\n"
            "  python process.py watch ~/Pictures/import/ -o ~/Pictures/dng/ --upload\n"
        ),
    )
    subs = parser.add_subparsers(dest="cmd")

    # -- convert --
    conv = subs.add_parser("convert", help="Convert CR2 file(s) or folder(s) to DNG")
    conv.add_argument("inputs", nargs="+", help="CR2 file(s) or folder(s)")
    conv.add_argument("-o", "--output", help="Output directory")
    conv.add_argument("--date-folders", action="store_true", help="Organize into YYYY/MM/DD subfolders")
    conv.add_argument("--skip-existing", action="store_true", default=True, help="Skip already-converted files (default: on)")
    conv.add_argument("--no-skip", dest="skip_existing", action="store_false", help="Re-convert even if DNG exists")
    conv.add_argument("--upload", action="store_true", help="Upload DNGs to Google Photos after conversion")
    conv.add_argument("-v", "--verbose", action="store_true", help="Print adjustment values per file")

    # -- watch --
    watch = subs.add_parser("watch", help="Watch a folder and auto-convert new CR2 files")
    watch.add_argument("folder", help="Folder to watch")
    watch.add_argument("-o", "--output", help="Output directory for DNGs")
    watch.add_argument("--date-folders", action="store_true", help="Organize into YYYY/MM/DD subfolders")
    watch.add_argument("--skip-existing", action="store_true", default=True)
    watch.add_argument("--upload", action="store_true", help="Upload to Google Photos after each conversion")
    watch.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()

    if args.cmd == "convert":
        cmd_convert(args)
    elif args.cmd == "watch":
        cmd_watch(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
