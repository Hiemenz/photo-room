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
import os
import shutil
import tempfile
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


_SAMPLE = 64 * 1024


def _sample_equal(a: Path, b: Path, size: int) -> bool:
    """Cheap pre-check: head, middle and tail blocks match."""
    offsets = {0, max(0, size // 2 - _SAMPLE // 2), max(0, size - _SAMPLE)}
    with open(a, "rb") as fa, open(b, "rb") as fb:
        for off in sorted(offsets):
            fa.seek(off)
            fb.seek(off)
            if fa.read(_SAMPLE) != fb.read(_SAMPLE):
                return False
    return True


def same_content(a: Path, b: Path, a_hash: list | None = None) -> bool:
    """
    True only when the two files hold identical bytes. Size first, then a
    head/middle/tail sample, and only then a full hash. A matching name and
    size is never enough: two cameras both write IMG_0001.CR2.
    """
    try:
        size = a.stat().st_size
        if size != b.stat().st_size:
            return False
        if not _sample_equal(a, b, size):
            return False
        if a_hash is None:
            a_hash = [None]
        if a_hash[0] is None:
            a_hash[0] = _sha256(a)
        return a_hash[0] == _sha256(b)
    except OSError:
        return False


def _candidate_names(name: str):
    """IMG_0001.CR2, IMG_0001-1.CR2, IMG_0001-2.CR2, ..."""
    yield name
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 1
    while True:
        yield f"{stem}-{n}{suffix}"
        n += 1


# Serialises the "pick a free name and claim it" step between worker threads.
_claim_lock = threading.Lock()


def _claim(tmp: Path, dest_dir: Path, name: str, source: Path, src_hash: list):
    """
    Move the finished temp file to the first name that is free, never
    replacing anything. Returns (final, status) with status "copied",
    "renamed" or "identical" (an identical file appeared meanwhile).
    """
    with _claim_lock:
        for cand in _candidate_names(name):
            target = dest_dir / cand
            if target.exists() or target.is_symlink():
                if same_content(source, target, src_hash):
                    tmp.unlink(missing_ok=True)
                    return target, "identical"
                continue
            try:
                os.link(tmp, target)  # fails if the name exists: no overwrite
            except FileExistsError:
                continue
            except OSError:
                # Filesystem without hard links (FAT/exFAT): the lock and the
                # exists() check above stand in for it.
                os.replace(tmp, target)
            else:
                tmp.unlink(missing_ok=True)
            return target, ("copied" if cand == name else "renamed")


def copy_from_card_ex(
    source: Path,
    library: Path,
    date_folders: bool = True,
    verify: bool = True,
    dest_name: str | None = None,
    dest_dir: Path | None = None,
) -> tuple[Path, str]:
    """
    Copy one raw into the library without ever losing or overwriting a file.

    ``dest_name`` gives the copy another file name (a rename template's
    result); ``dest_dir`` the exact folder, in place of ``library`` plus the
    shoot date. Both go through the same no-overwrite claim.

    Returns (destination, status): "copied", "identical" (same bytes already
    there, nothing written) or "renamed" (a different file already used the
    name, so this one was kept as IMG_0001-1.CR2 and so on).

    The copy goes to a unique temporary name, is flushed and verified, and
    is only then given its final name by a no-overwrite link/rename.
    """
    name = dest_name or source.name
    if dest_dir is None:
        dest_dir = library
        if date_folders:
            shot = get_shoot_date(source)
            if shot:
                dest_dir = library / shot
    dest_dir.mkdir(parents=True, exist_ok=True)
    src_hash: list = [None]

    # Already imported (under this name or a numbered one)? Then do nothing.
    for cand in _candidate_names(name):
        target = dest_dir / cand
        if not (target.exists() or target.is_symlink()):
            break
        if same_content(source, target, src_hash):
            return target, "identical"

    fd, tmp_name = tempfile.mkstemp(
        prefix="." + Path(name).stem + ".", suffix=".partial", dir=dest_dir)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(source, tmp)
        with open(tmp, "rb+") as f:
            os.fsync(f.fileno())
        if verify:
            if src_hash[0] is None:
                src_hash[0] = _sha256(source)
            if _sha256(tmp) != src_hash[0]:
                raise RuntimeError(f"Checksum mismatch copying {source.name}")
        return _claim(tmp, dest_dir, name, source, src_hash)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def find_sidecar(raw: Path) -> Path | None:
    """The .xmp that travels with a raw on the card: IMG_0001.xmp, or IMG_0001.CR2.xmp."""
    for cand in (raw.with_suffix(".xmp"), raw.with_suffix(".XMP"),
                 raw.with_name(raw.name + ".xmp"), raw.with_name(raw.name + ".XMP")):
        try:
            if cand.is_file() and not cand.is_symlink():
                return cand
        except OSError:
            continue
    return None


def copy_companion(src: Path, dest: Path, verify: bool = True) -> str:
    """
    Copy a small companion file (a sidecar) to exactly ``dest``, never replacing
    anything. Returns "copied", "exists" (a file is already there and is left
    alone) or "identical". Raises RuntimeError on a checksum mismatch.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        return "identical" if same_content(src, dest) else "exists"
    fd, tmp_name = tempfile.mkstemp(prefix="." + dest.stem + ".", suffix=".partial", dir=dest.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copyfile(src, tmp)
        with open(tmp, "rb+") as f:
            os.fsync(f.fileno())
        if verify and _sha256(tmp) != _sha256(src):
            raise RuntimeError(f"Checksum mismatch copying {src.name}")
        with _claim_lock:
            if dest.exists() or dest.is_symlink():
                return "exists"
            try:
                os.link(tmp, dest)
            except FileExistsError:
                return "exists"
            except OSError:
                os.replace(tmp, dest)
        return "copied"
    finally:
        tmp.unlink(missing_ok=True)


def copy_sidecar(raw_src: Path, raw_dest: Path, verify: bool = True) -> tuple[Path | None, str]:
    """Carry the source raw's .xmp next to its copy (named after the copy). (path, status)."""
    side = find_sidecar(raw_src)
    if side is None:
        return None, "none"
    target = raw_dest.with_suffix(".xmp")
    return target, copy_companion(side, target, verify)


def copy_from_card(
    source: Path,
    library: Path,
    date_folders: bool = True,
    verify: bool = True,
) -> tuple[Path, bool]:
    """Back-compatible wrapper: (destination, was_written)."""
    dest, status = copy_from_card_ex(source, library, date_folders, verify)
    return dest, status != "identical"


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
    spec=None,
    sidecars: bool = True,
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
            "skipped_identical": 0,
            "renamed": [],
            "converted": 0,
            "already_converted": 0,
            "failed": [],
            "styled": False,
        }

    library.mkdir(parents=True, exist_ok=True)
    if spec is not None and spec.backup is not None:
        import importkit
        spec.backup = importkit.check_backup_dir(spec.backup, library, card)
        spec.backup.mkdir(parents=True, exist_ok=True)
    model = style.load_model()
    if verbose and model.is_trained:
        print(f"  Using learned style ({model.n_samples} edits, "
              f"{model.confidence:.0%} confidence)")

    copied: list[Path] = []
    converted: list[Path] = []
    already_converted: list[Path] = []
    skipped = 0
    renamed: list[dict] = []
    failed: list[dict] = []
    lock = threading.Lock()
    warnings: list[str] = []
    last_dest: dict = {}
    seq_box = [max(1, int(getattr(spec, "seq_start", 1)))]

    def handle(raw: Path) -> None:
        nonlocal skipped
        if spec is not None:
            import importkit
            with lock:
                n = seq_box[0]
            import dataclasses
            # Metadata goes in after conversion: a sidecar made first would look "already developed".
            one = importkit.import_one(raw, library, dataclasses.replace(
                spec, rating=None, keywords=[], copyright=""), n)
            dest, status = one.dest, one.status
            with lock:
                if status != "identical":
                    last_dest[raw] = dest
                    seq_box[0] += 1
                warnings.extend(one.warnings)
        else:
            dest, status = copy_from_card_ex(raw, library, date_folders, verify)
            if sidecars:
                try:
                    copy_sidecar(raw, dest, verify)
                except (OSError, RuntimeError) as exc:
                    with lock:
                        warnings.append(f"sidecar for {raw.name} not carried over: {exc}")
        with lock:
            if status == "identical":
                skipped += 1
            elif status == "renamed":
                renamed.append({"file": str(raw), "saved_as": str(dest)})
            else:
                copied.append(dest)

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

    if spec is not None and getattr(spec, "writes_metadata", False):
        _inner = handle

        def handle(raw: Path) -> None:       # noqa: F811 - wrap: write metadata after convert
            _inner(raw)
            last = last_dest.pop(raw, None)
            if last is not None:
                import importkit
                try:
                    importkit.write_metadata(last, spec)
                except Exception as exc:
                    with lock:
                        warnings.append(f"rating/keywords/copyright not applied to {last.name}: {exc}")

    workers = max(1, min(int(jobs), len(raws)))
    if spec is not None and "{seq" in (getattr(spec, "rename", "") or ""):
        workers = 1                          # numbers follow the card's order
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
        "skipped_identical": skipped,
        "renamed": sorted(renamed, key=lambda r: r["saved_as"]),
        "converted": len(converted),
        "already_converted": len(already_converted),
        "failed": failed,
        # True only when the style reached a photograph, not merely when a
        # model exists. Re-running an ingest that converts nothing had been
        # reporting "[learned style applied]" while applying it to nothing.
        "styled": bool(model.is_trained and converted),
        "warnings": warnings,
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

def _spec_from(args: argparse.Namespace):
    """An ImportSpec when a layout, rename or backup was asked for, else None (the classic path)."""
    if not (args.layout or args.rename or args.backup):
        return None
    import importkit
    layout = args.layout or ("flat" if args.no_date_folders else "date")
    try:
        return importkit.ImportSpec(layout=layout, rename=args.rename or "", sidecars=not args.no_sidecars,
                                    backup=args.backup).validate()
    except importkit.TemplateError as exc:
        sys.exit(f"Error: {exc}")


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
        spec=_spec_from(args), sidecars=not args.no_sidecars,
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
                        spec=_spec_from(args), sidecars=not args.no_sidecars,
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
        f"{summary['copied']} copied, "
        f"{summary['already_present']} skipped as identical, "
        f"{len(summary.get('renamed') or [])} renamed (name clash), "
        f"{len(summary['failed'])} failed, "
        f"{summary['converted']} converted"
        + (f", {summary.get('already_converted', 0)} already converted"
           if summary.get("already_converted") else "")
        + ("  [learned style applied]" if summary.get("styled") else "")
    )
    for w in (summary.get("warnings") or [])[:10]:
        print(f"  note: {w}")
    for r in (summary.get("renamed") or [])[:20]:
        print(f"  renamed: {r['file']} -> {r['saved_as']}")
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
        p.add_argument("--layout", help="Folder layout: date, month, flat or a template such as '{yyyy}/{date}'")
        p.add_argument("--rename", help="Rename template, e.g. '{date}_{seq:4}' ({date} {time} {seq} {camera} {original})")
        p.add_argument("--backup", type=Path, help="Also write a verified second copy of each file here")
        p.add_argument("--no-sidecars", action="store_true", help="Do not carry the card's .xmp sidecars")
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
