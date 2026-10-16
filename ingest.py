#!/usr/bin/env python3
"""
Card ingest: copy, convert, auto-edit, stage for review.

Watches for a camera card to mount, copies the raws into the library,
converts them to DNG with learned adjustments applied, and stages the
result. Uploading is deliberately a separate, explicit step — a shoot
usually contains frames you would not want published.

The card is treated as read-only throughout. Nothing here deletes or
modifies anything on it; erase it in-camera when you are satisfied the
copy is good.

Run:
  python ingest.py watch -o ~/Pictures/library/
  python ingest.py run /Volumes/EOS_DIGITAL -o ~/Pictures/library/
"""

import argparse
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from tqdm import tqdm

import style
from process import (
    DEFAULT_JOBS,
    RAW_SUFFIXES as process_raw_suffixes,
    compute_auto_adjustments,
    convert_cr2_to_dng,
    decode_for_analysis,
    dng_converter_available,
    embed_xmp_adjustments,
    get_iso,
    get_shoot_date,
    photo_features,
    write_sidecar_adjustments,
)


# Imported rather than redefined, so ingest and convert cannot disagree
# about which files are photographs.
RAW_SUFFIXES = set(process_raw_suffixes)

# Volumes that are never a camera card.
IGNORED_VOLUMES = {"Macintosh HD", "com.apple.TimeMachine.localsnapshots"}

# A card is identified by this directory, which every DCF-compliant camera
# writes. Matching on it rather than on the volume name means a card
# formatted in any camera is recognised.
DCIM = "DCIM"


# ---------------------------------------------------------------------------
# Card detection
# ---------------------------------------------------------------------------

def list_volumes() -> list[Path]:
    root = Path("/Volumes")
    if not root.is_dir():
        return []
    return [v for v in root.iterdir() if v.name not in IGNORED_VOLUMES]


def is_camera_card(volume: Path) -> bool:
    try:
        return (volume / DCIM).is_dir()
    except OSError:
        return False


def find_cards() -> list[Path]:
    return [v for v in list_volumes() if is_camera_card(v)]


def find_raws(card: Path) -> list[Path]:
    """Every raw file on the card, in a stable order."""
    dcim = card / DCIM
    search_root = dcim if dcim.is_dir() else card
    found = [
        p for p in search_root.rglob("*")
        if p.is_file() and p.suffix.lower() in RAW_SUFFIXES
    ]
    return sorted(found)


# ---------------------------------------------------------------------------
# Copying
# ---------------------------------------------------------------------------

def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def copy_from_card(
    source: Path,
    library: Path,
    date_folders: bool = True,
    verify: bool = True,
) -> tuple[Path, bool]:
    """
    Copy one raw into the library. Returns (destination, was_copied).

    The copy is written to a temporary name and only moved into place once
    it is complete, so an interrupted ingest can never leave a truncated
    file that later looks like a successful import.
    """
    dest_dir = library
    if date_folders:
        shot = get_shoot_date(source)
        if shot:
            dest_dir = library / shot
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / source.name

    if dest.exists() and dest.stat().st_size == source.stat().st_size:
        return dest, False

    tmp = dest.with_suffix(dest.suffix + ".partial")
    shutil.copy2(source, tmp)

    if verify and _sha256(tmp) != _sha256(source):
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Checksum mismatch copying {source.name}")

    tmp.replace(dest)
    return dest, True


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def ingest_card(
    card: Path,
    library: Path,
    convert: bool = True,
    date_folders: bool = True,
    verify: bool = True,
    verbose: bool = False,
    jobs: int = DEFAULT_JOBS,
    reconvert: bool = False,
) -> dict:
    """
    Copy every raw off a card, convert it, and apply learned adjustments.

    Returns a summary describing what landed and what failed. Each file is
    handled independently so one unreadable frame does not end the import.
    """
    raws = find_raws(card)
    if not raws:
        # Must carry every key _report reads, or a JPEG-only card crashes it.
        return {
            "card": str(card),
            "library": str(library),
            "found": 0,
            "copied": 0,
            "already_present": 0,
            "converted": 0,
            "already_converted": 0,
            "failed": [],
            "styled": False,
        }

    library.mkdir(parents=True, exist_ok=True)
    model = style.load_model()
    if verbose and model.is_trained:
        print(f"  Using learned style ({model.n_samples} edits, "
              f"{model.confidence:.0%} confidence)")

    copied: list[Path] = []
    converted: list[Path] = []
    already_converted: list[Path] = []
    skipped = 0
    failed: list[dict] = []
    lock = threading.Lock()

    def handle(raw: Path) -> None:
        nonlocal skipped
        dest, was_copied = copy_from_card(raw, library, date_folders, verify)
        with lock:
            if was_copied:
                copied.append(dest)
            else:
                skipped += 1

        if convert:
            dng_path = dest.with_suffix(
                ".dng" if dng_converter_available() else ".xmp")
            existed = dng_path.exists()
            dng = _convert_with_style(dest, model, verbose, reconvert=reconvert)
            if dng:
                with lock:
                    # Re-running an ingest should not claim to have converted
                    # files it recognised and skipped.
                    if existed and not reconvert:
                        already_converted.append(dng)
                    else:
                        converted.append(dng)

    workers = max(1, min(int(jobs), len(raws)))
    with tqdm(total=len(raws), unit="file", desc=f"Ingesting x{workers}") as bar:
        if workers == 1:
            for raw in raws:
                bar.set_postfix_str(raw.name)
                try:
                    handle(raw)
                except Exception as exc:
                    tqdm.write(f"  ERROR {raw.name}: {exc}", file=sys.stderr)
                    failed.append({"file": raw.name, "error": str(exc)})
                bar.update(1)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(handle, raw): raw for raw in raws}
                for future in as_completed(futures):
                    raw = futures[future]
                    try:
                        future.result()
                    except Exception as exc:
                        tqdm.write(f"  ERROR {raw.name}: {exc}", file=sys.stderr)
                        with lock:
                            failed.append({"file": raw.name, "error": str(exc)})
                    bar.update(1)
                    bar.set_postfix_str(raw.name)
    copied.sort()
    converted.sort()
    failed.sort(key=lambda f: f["file"])

    summary = {
        "card": str(card),
        "library": str(library),
        "found": len(raws),
        "copied": len(copied),
        "already_present": skipped,
        "converted": len(converted),
        "already_converted": len(already_converted),
        "failed": failed,
        # True only when the style reached a photograph, not merely when a
        # model exists. Re-running an ingest that converts nothing had been
        # reporting "[learned style applied]" while applying it to nothing.
        "styled": bool(model.is_trained and converted),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_manifest(library, summary)
    return summary


def _convert_with_style(raw: Path, model, verbose: bool, reconvert: bool = False) -> Path | None:
    """
    Convert one copied raw, applying the learned style to its adjustments.

    ``reconvert`` develops a photograph again even when its DNG is already
    there — which is how a shoot gets re-developed after the style has
    improved. Without it, ingest had no route to that at all while convert
    has had --no-skip since the beginning.
    """
    portable = not dng_converter_available()
    dng_path = raw.with_suffix(".xmp" if portable else ".dng")
    if dng_path.exists() and not reconvert:
        return raw if portable else dng_path
    if dng_path.exists() and not portable:
        dng_path.unlink()

    decoded = decode_for_analysis(raw)
    iso = get_iso(raw)
    adj = compute_auto_adjustments(raw, decoded=decoded, iso=iso, style_model=model)

    if verbose:
        tqdm.write(
            f"  {raw.name}: EV={adj['exposure']:+.2f} "
            f"shadows={adj['shadows']:+d} highlights={adj['highlights']:+d} "
            f"WB={adj['color_temp']}K"
            + ("  [styled]" if adj.get("styled") else "")
        )

    if portable:
        write_sidecar_adjustments(raw, adj)
        return raw
    dng = convert_cr2_to_dng(raw, raw.parent)
    embed_xmp_adjustments(dng, adj)
    return dng


def _write_manifest(library: Path, summary: dict) -> None:
    """
    Record what this run did, so the review step knows what is new and an
    interrupted ingest leaves a trace.
    """
    manifest = library / ".photo-room-ingests.jsonl"
    try:
        with open(manifest, "a") as f:
            f.write(json.dumps(summary) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> None:
    card = Path(args.card)
    if not card.is_dir():
        print(f"Not a directory: {card}", file=sys.stderr)
        sys.exit(1)

    summary = ingest_card(
        card,
        Path(args.output).expanduser(),
        convert=not args.no_convert,
        date_folders=not args.no_date_folders,
        verify=not args.no_verify,
        verbose=args.verbose,
        jobs=args.jobs,
        reconvert=args.reconvert,
    )
    _report(summary)


def cmd_watch(args: argparse.Namespace) -> None:
    # Same reason as process.py's watcher: redirected output is block-buffered
    # and a card ingest hours later would otherwise appear to have produced
    # nothing at all.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):  # pragma: no cover
        pass

    library = Path(args.output).expanduser()
    seen: set[str] = set()

    print(f"Watching for camera cards. Library: {library}")
    print("The card is never modified — erase it in-camera when you're ready.")
    print("Ctrl+C to stop.\n")

    try:
        while True:
            for card in find_cards():
                key = str(card)
                if key in seen:
                    continue
                seen.add(key)

                print(f"\nCard detected: {card}")
                # Give the OS a moment to finish mounting before reading.
                time.sleep(2)
                try:
                    summary = ingest_card(
                        card, library,
                        convert=not args.no_convert,
                        date_folders=not args.no_date_folders,
                        verify=not args.no_verify,
                        verbose=args.verbose,
                        jobs=args.jobs,
                        reconvert=args.reconvert,
                    )
                    _report(summary)
                except Exception as exc:
                    print(f"Ingest failed: {exc}", file=sys.stderr)

            # Forget cards that have been ejected so reinserting re-triggers.
            present = {str(c) for c in find_cards()}
            seen &= present

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")


def cmd_cards(args: argparse.Namespace) -> None:
    volumes = list_volumes()
    if not volumes:
        print("No volumes mounted.")
        return

    for v in volumes:
        if is_camera_card(v):
            n = len(find_raws(v))
            print(f"  {v}  — camera card, {n} raw file(s)")
        else:
            print(f"  {v}  — not a camera card (no {DCIM}/)")


def _report(summary: dict) -> None:
    print(
        f"\n{summary['found']} raw file(s) on card: "
        f"{summary['copied']} copied, {summary['already_present']} already present, "
        f"{summary['converted']} converted"
        + (f", {summary.get('already_converted', 0)} already converted"
           if summary.get("already_converted") else "")
        + ("  [learned style applied]" if summary.get("styled") else "")
    )
    if summary["failed"]:
        print(f"{len(summary['failed'])} failed:", file=sys.stderr)
        for f in summary["failed"][:10]:
            print(f"  {f['file']}: {f['error']}", file=sys.stderr)
    print(f"\nReview them with:\n  python editor.py {summary['library']}")
    print("Upload happens from there, once you've looked them over.")


def main():
    parser = argparse.ArgumentParser(
        description="Copy photos off a camera card, convert them, and apply your learned style.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python ingest.py cards\n"
            "  python ingest.py watch -o ~/Pictures/library/ -v\n"
            "  python ingest.py run /Volumes/EOS_DIGITAL -o ~/Pictures/library/\n"
        ),
    )
    subs = parser.add_subparsers(dest="cmd")

    def _common(p):
        p.add_argument("-o", "--output", required=True, help="Library folder")
        p.add_argument("--no-convert", action="store_true", help="Copy only, do not convert")
        p.add_argument("--no-date-folders", action="store_true", help="Do not sort into YYYY/MM/DD")
        p.add_argument("--no-verify", action="store_true", help="Skip the copy checksum")
        p.add_argument("-v", "--verbose", action="store_true")
        p.add_argument("-j", "--jobs", type=int, default=DEFAULT_JOBS,
                       help=f"Files to handle at once (default: {DEFAULT_JOBS})")
        p.add_argument("--no-skip", dest="reconvert", action="store_true",
                       help="Develop photographs again even when already "
                            "converted — use after the learned style has changed")

    run = subs.add_parser("run", help="Ingest a card that is already mounted")
    run.add_argument("card", help="Path to the mounted card")
    _common(run)

    watch = subs.add_parser("watch", help="Wait for a card, then ingest it automatically")
    _common(watch)
    watch.add_argument("--interval", type=float, default=3.0, help="Poll seconds (default: 3)")

    subs.add_parser("cards", help="List mounted volumes and say which look like cards")

    args = parser.parse_args()
    if args.cmd == "run":
        cmd_run(args)
    elif args.cmd == "watch":
        cmd_watch(args)
    elif args.cmd == "cards":
        cmd_cards(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
