"""
Card watcher: import a camera card when it is inserted, then eject it safely.

    photo-room watch-cards [library_folder]

The pieces, each testable without hardware:

* detection  - ``find_cards`` reads /proc/mounts (Linux) or /Volumes (macOS);
               tests point it at a fake mount root.
* import     - ``import_card`` copies through ``ingest.copy_from_card_ex``
               (fsync, SHA-256 against the source, never overwrite, numbered
               name on a clash) after a library-wide content-hash dedupe.
* verify     - ``verify_import`` re-reads every card file and its library copy
               and compares SHA-256 again. Only a clean pass allows eject.
* delete     - ``delete_verified`` (opt-in) removes only individually verified
               photo files, one at a time, and logs each.
* eject      - ``eject_card`` (udisksctl / umount / diskutil) with an injectable
               command runner. It is never called after a failure.

Nothing is ever written to the card except an explicitly enabled deletion.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import fsutil
import importkit
import ingest

try:  # POSIX only; Windows is not a supported platform for photo-room
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

DCIM = "DCIM"
SPACE_MARGIN = 64 * 1024**2
STATUS_NAME = ".photo-room-card-status.json"
LOCK_NAME = ".photo-room-card-watch.lock"
DELETE_LOG_NAME = ".photo-room-card-deletions.log"
DEFAULT_LIBRARY = "~/Pictures/photo-room"

# What counts as a photo (may be deleted after verification) and what else is
# imported but never deleted.
PHOTO_SUFFIXES = frozenset(ingest.RAW_SUFFIXES) | {".jpg", ".jpeg", ".heic", ".heif"}
VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mts", ".m2ts", ".avi", ".mxf"})
RAW_ONLY = frozenset(ingest.RAW_SUFFIXES)

LAYOUTS = importkit.LAYOUTS

LINUX_ROOTS = ("/run/media", "/media", "/mnt")
MAC_ROOTS = ("/Volumes",)
IGNORED_VOLUMES = {"Macintosh HD", "com.apple.TimeMachine.localsnapshots"}


class CardError(Exception):
    pass


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Card:
    mount: Path
    device: str | None = None
    label: str = ""

    @property
    def key(self) -> str:
        return f"{self.device or ''}|{self.mount}"


def _unescape_mount(s: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def read_mounts(mounts_file: str | os.PathLike = "/proc/mounts") -> list[tuple[str, Path, str]]:
    """(device, mount point, fstype) for every line; [] when unreadable."""
    try:
        text = Path(mounts_file).read_text(errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            out.append((_unescape_mount(parts[0]), Path(_unescape_mount(parts[1])), parts[2]))
    return out


def has_dcim(volume: Path) -> bool:
    try:
        return (volume / DCIM).is_dir()
    except OSError:
        return False


def _scan_roots(roots, depth: int) -> list[Path]:
    found = []
    for root in roots:
        root = Path(root)
        try:
            level = [p for p in root.iterdir() if p.is_dir() and not p.is_symlink()]
        except OSError:
            continue
        found.extend(level)
        if depth > 1:  # /run/media/<user>/<label>
            for p in level:
                try:
                    found.extend(c for c in p.iterdir() if c.is_dir() and not c.is_symlink())
                except OSError:
                    pass
    return found


def mac_volume_info(mount: Path, run=None) -> dict:
    """Optional `diskutil info -plist` enrichment; {} when unavailable."""
    run = run or subprocess.run
    try:
        out = run(["diskutil", "info", "-plist", str(mount)], capture_output=True, timeout=15)
        if out.returncode == 0:
            return plistlib.loads(out.stdout)
    except Exception:
        pass
    return {}


def find_cards(
    roots: tuple[str, ...] | None = None,
    mounts_file: str | os.PathLike | None | bool = True,
    platform: str | None = None,
    diskutil: Callable[[Path], dict] | None = None,
) -> list[Card]:
    """
    Mounted volumes that contain a DCIM folder.

    Linux: a mount from /proc/mounts whose mount point lies under /run/media,
    /media or /mnt. macOS: a directory in /Volumes (the boot volume and
    snapshots are ignored; ``diskutil info`` adds the device and rejects
    internal disks when it is available). With ``mounts_file=None`` the roots
    are simply scanned, which is how tests use a fake mount root.
    """
    platform = platform or sys.platform
    mac = platform == "darwin"
    roots = tuple(roots) if roots is not None else (MAC_ROOTS if mac else LINUX_ROOTS)
    cards: list[Card] = []
    seen: set[Path] = set()

    if mounts_file is True:
        mounts_file = None if mac else "/proc/mounts"
    mounts = read_mounts(mounts_file) if mounts_file else []

    def under_roots(p: Path) -> bool:
        return any(p == Path(r) or Path(r) in p.parents for r in roots)

    if mounts_file:
        for dev, mnt, _fs in mounts:
            if mnt in seen or mnt in map(Path, roots) or not under_roots(mnt):
                continue
            seen.add(mnt)
            if has_dcim(mnt):
                cards.append(Card(mnt, dev if dev.startswith("/dev/") else None, mnt.name))
    else:
        depth = 2 if any("/run/media" in r for r in roots) else 1
        for v in _scan_roots(roots, depth):
            if v.name in IGNORED_VOLUMES or v in seen or v.name.startswith("."):
                continue
            seen.add(v)
            if has_dcim(v):
                info = (diskutil or mac_volume_info)(v) if mac else {}
                if info.get("Internal") is True and not info.get("RemovableMedia", False):
                    continue
                dev = info.get("DeviceNode")
                cards.append(Card(v, dev, v.name))
    return sorted(cards, key=lambda c: str(c.mount))


# ---------------------------------------------------------------------------
# Layout, hashing, inventory
# ---------------------------------------------------------------------------

def resolve_layout(layout: str) -> str:
    """A named layout or a template ({yyyy} {mm} {dd} {date} {camera}); raises CardError if unsafe."""
    try:
        return importkit.resolve_layout(layout)
    except importkit.TemplateError as exc:
        raise CardError(str(exc)) from None


def _shoot_date(path: Path) -> tuple[str, str, str]:
    """(yyyy, mm, dd) from EXIF, falling back to the file's modification time."""
    try:
        import process
        d = process.get_shoot_date(path)
        if d:
            y, m, dd = d.split("/")
            return y, m, dd
    except Exception:
        pass
    t = datetime.fromtimestamp(path.stat().st_mtime)
    return f"{t.year:04d}", f"{t.month:02d}", f"{t.day:02d}"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(1 << 20):
            h.update(block)
    return h.hexdigest()


@dataclass
class CardFile:
    path: Path
    size: int
    mtime_ns: int
    photo: bool


def list_card_files(card: Path, suffixes: frozenset[str],
                    skip_dirs=()) -> tuple[list[CardFile], list[str]]:
    """Importable files under DCIM (stable order) and any directory read errors."""
    errors: list[str] = []
    files: list[CardFile] = []

    def onerror(exc):
        errors.append(f"{getattr(exc, 'filename', card)}: {exc.strerror or exc}")

    base = card / DCIM if (card / DCIM).is_dir() else card      # a plain folder works too
    skip = {p.resolve() for p in (skip_dirs or ())}
    for dirpath, dirnames, filenames in os.walk(base, onerror=onerror):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".")
                             and (Path(dirpath) / d).resolve() not in skip)
        for name in sorted(filenames):
            if name.startswith("."):           # macOS ._AppleDouble, .Trashes
                continue
            p = Path(dirpath) / name
            suf = p.suffix.lower()
            if suf not in suffixes:
                continue
            try:
                st = p.stat()
            except OSError as exc:
                errors.append(f"{p}: {exc.strerror or exc}")
                continue
            files.append(CardFile(p, st.st_size, st.st_mtime_ns, suf in PHOTO_SUFFIXES))
    return files, errors


class LibraryIndex:
    """size -> library files, so a card file is checked by content, not name."""

    def __init__(self, library: Path, suffixes: frozenset[str]):
        self.by_size: dict[int, list[Path]] = {}
        for dirpath, dirnames, filenames in os.walk(library):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if name.startswith(".") or Path(name).suffix.lower() not in suffixes:
                    continue
                p = Path(dirpath) / name
                try:
                    self.by_size.setdefault(p.stat().st_size, []).append(p)
                except OSError:
                    pass

    def find(self, src: Path, size: int) -> Path | None:
        cache: list = [None]
        for cand in self.by_size.get(size, ()):
            if ingest.same_content(src, cand, cache):
                return cand
        return None

    def add(self, path: Path) -> None:
        try:
            self.by_size.setdefault(path.stat().st_size, []).append(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Import + verify
# ---------------------------------------------------------------------------

@dataclass
class ImportResult:
    card: str
    library: str
    found: int = 0
    copied: int = 0
    renamed: int = 0
    duplicates: int = 0
    failed: list[dict] = field(default_factory=list)
    interrupted: bool = False
    scan_errors: list[str] = field(default_factory=list)
    bytes_total: int = 0
    verified: int = 0
    verify_failed: list[dict] = field(default_factory=list)
    destinations: dict[str, Path] = field(default_factory=dict)   # card file -> library file
    verified_set: set = field(default_factory=set)                # card files re-checksummed OK
    files: dict[str, CardFile] = field(default_factory=dict)
    backups: dict[str, Path] = field(default_factory=dict)        # card file -> backup copy
    sidecars: int = 0                                              # .xmp files carried over
    warnings: list[str] = field(default_factory=list)              # copied fine, something extra did not apply
    new_paths: list[Path] = field(default_factory=list)            # library files written by this run
    deleted: list[str] = field(default_factory=list)
    delete_failed: list[dict] = field(default_factory=list)
    would_delete: list[str] = field(default_factory=list)
    eject: dict | None = None
    problem: str = ""

    @property
    def clean(self) -> bool:
        """Every file copied or already present, and every one re-verified."""
        return (not self.failed and not self.verify_failed and not self.scan_errors
                and not self.interrupted and not self.problem
                and self.verified == self.found)

    def as_dict(self) -> dict:
        return {
            "card": self.card, "library": self.library, "found": self.found,
            "copied": self.copied, "renamed": self.renamed, "duplicates": self.duplicates,
            "failed": len(self.failed) + len(self.verify_failed) + len(self.scan_errors),
            "verified": self.verified, "interrupted": self.interrupted,
            "deleted": len(self.deleted), "would_delete": len(self.would_delete),
            "problem": self.problem, "eject": self.eject,
            "sidecars": self.sidecars, "backed_up": len(self.backups), "warnings": self.warnings[:20],
        }


def import_card(
    card: Path,
    library: Path,
    layout: str = "date",
    suffixes: frozenset[str] = PHOTO_SUFFIXES | VIDEO_SUFFIXES,
    progress: Callable[[dict], None] | None = None,
    should_stop: Callable[[], bool] = lambda: False,
    disk_usage=shutil.disk_usage,
    spec: importkit.ImportSpec | None = None,
    only: set[str] | None = None,
) -> ImportResult:
    """
    Copy what is importable off the card (or only the files in ``only``, as
    absolute path strings). Reads the card; writes only to ``library`` and, when
    the spec asks for it, the backup folder.
    """
    if spec is None:
        spec = importkit.ImportSpec(layout=layout)
    spec.date_fn = spec.date_fn or (lambda p: _shoot_date(p))
    try:
        spec.validate()
    except importkit.TemplateError as exc:
        raise CardError(str(exc)) from None
    res = ImportResult(str(card), str(library))
    emit = progress or (lambda _s: None)
    library.mkdir(parents=True, exist_ok=True)
    backup_need = 0
    if spec.backup is not None:
        try:
            spec.backup = importkit.check_backup_dir(spec.backup, library, card)
            spec.backup.mkdir(parents=True, exist_ok=True)
        except (importkit.TemplateError, OSError) as exc:
            raise CardError(f"backup folder: {exc}") from None

    skip = [library] if library.resolve() != Path(card).resolve() and Path(card).resolve() in library.resolve().parents else []
    files, errors = list_card_files(card, suffixes, skip)
    if only is not None:
        files = [f for f in files if str(f.path) in only]
    res.scan_errors = errors
    res.found = len(files)
    res.files = {str(f.path): f for f in files}
    emit({"state": "scanning", "total": len(files)})

    # Plan: which files are already in the library (by content)?
    index = LibraryIndex(library, suffixes)
    plan: list[tuple[CardFile, Path | None]] = []
    need = 0
    for f in files:
        if should_stop():
            res.interrupted = True
            return res
        try:
            dup = index.find(f.path, f.size)
        except OSError as exc:
            res.failed.append({"file": f.path.name, "error": f"cannot read: {exc}"})
            continue
        plan.append((f, dup))
        if dup is None:
            need += f.size
    res.bytes_total = need

    try:
        free = disk_usage(library).free
    except OSError:
        free = None
    if free is not None and need + SPACE_MARGIN > free:
        res.problem = (f"not enough free space in {library}: need {need // 2**20} MB "
                       f"(+{SPACE_MARGIN // 2**20} MB margin), {free // 2**20} MB free. "
                       "Nothing was copied.")
        return res
    if spec.backup is not None:
        backup_need = sum(f.size for f, _ in plan)
        try:
            bfree = disk_usage(spec.backup).free
        except OSError:
            bfree = None
        if bfree is not None and backup_need + SPACE_MARGIN > bfree:
            res.problem = (f"not enough free space in the backup folder {spec.backup}: need "
                           f"{backup_need // 2**20} MB (+{SPACE_MARGIN // 2**20} MB margin), "
                           f"{bfree // 2**20} MB free. Nothing was copied.")
            return res

    done_bytes = 0
    seq = max(1, int(spec.seq_start))
    for i, (f, dup) in enumerate(plan, 1):
        if should_stop():
            res.interrupted = True
            break
        key = str(f.path)
        try:
            one = importkit.import_one(f.path, library, spec, seq, dup)
            res.destinations[key] = one.dest
            res.warnings += one.warnings
            if one.backup is not None:
                res.backups[key] = one.backup
            if one.status == "identical":
                res.duplicates += 1
            else:
                seq += 1
                res.copied += 1
                res.renamed += one.status == "renamed"
                res.sidecars += one.sidecar == "copied"
                res.new_paths.append(one.dest)
                index.add(one.dest)
                done_bytes += f.size
        except Exception as exc:   # unreadable file, card pulled, disk full, checksum mismatch
            res.failed.append({"file": f.path.name, "error": str(exc)})
        emit({"state": "importing", "total": len(plan), "done": i, "name": f.path.name,
              "copied": res.copied, "duplicates": res.duplicates,
              "failed": len(res.failed), "bytes_total": need, "bytes_done": done_bytes})
    return res


def verify_import(res: ImportResult, progress: Callable[[dict], None] | None = None,
                  should_stop: Callable[[], bool] = lambda: False) -> ImportResult:
    """
    Second, independent pass: re-read each card file and its library copy and
    compare SHA-256. Records ``verified`` and ``verify_failed``; the per-file
    result is also what gates deletion.
    """
    emit = progress or (lambda _s: None)
    ok: set[str] = set()
    items = list(res.destinations.items())
    for i, (src, dest) in enumerate(items, 1):
        if should_stop():
            res.interrupted = True
            return res
        try:
            want = sha256(Path(src))
            if want != sha256(dest):
                res.verify_failed.append({"file": Path(src).name, "error": "checksum differs from library copy"})
            elif src in res.backups and want != sha256(res.backups[src]):
                res.verify_failed.append({"file": Path(src).name, "error": "checksum differs from backup copy"})
            else:
                ok.add(src)
        except OSError as exc:
            res.verify_failed.append({"file": Path(src).name, "error": f"cannot re-read: {exc}"})
        emit({"state": "verifying", "total": len(items), "done": i, "name": Path(src).name})
    res.verified = len(ok)
    res.verified_set = ok
    return res


# ---------------------------------------------------------------------------
# Deletion (opt-in)
# ---------------------------------------------------------------------------

def delete_verified(res: ImportResult, card: Path, dry_run: bool = False,
                    log_path: Path | None = None, log=print) -> ImportResult:
    """
    Remove from the card only photo files that were individually verified by
    checksum, and only if the file is unchanged since (size and mtime).
    Never removes a directory, a video or any other file. Every deletion is logged.
    """
    verified = res.verified_set
    dcim = (card / DCIM).resolve()
    for src in sorted(verified):
        f = res.files[src]
        p = f.path
        if not f.photo or p.suffix.lower() not in PHOTO_SUFFIXES:
            continue
        try:
            if dcim not in p.resolve().parents or p.is_symlink():
                continue
            st = p.stat()
            if (st.st_size, st.st_mtime_ns) != (f.size, f.mtime_ns):
                res.delete_failed.append({"file": p.name, "error": "changed since verification; kept"})
                continue
            dest = res.destinations[src]
            if dry_run:
                res.would_delete.append(src)
                _log_deletion(log_path, log, f"DRY-RUN would delete {p} (verified copy {dest})")
                continue
            p.unlink()
            res.deleted.append(src)
            _log_deletion(log_path, log, f"deleted {p} (verified copy {dest}, sha256 matched)")
        except OSError as exc:
            res.delete_failed.append({"file": p.name, "error": str(exc)})
    return res


def _log_deletion(log_path: Path | None, log, line: str) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log(f"  {line}")
    if log_path:
        try:
            with open(log_path, "a") as fh:
                fh.write(f"{stamp} {line}\n")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Eject
# ---------------------------------------------------------------------------

def parent_disk(dev: str, sys_block: str | os.PathLike = "/sys/class/block") -> str:
    """/dev/sdb1 -> /dev/sdb, /dev/mmcblk0p1 -> /dev/mmcblk0 (partition -> whole drive)."""
    name = Path(dev).name
    link = Path(sys_block) / name
    try:
        if link.exists():
            if (link / "partition").exists():
                return str(Path(dev).parent / link.resolve().parent.name)
            return dev
    except OSError:
        pass
    m = re.fullmatch(r"(mmcblk\d+|nvme\d+n\d+)p\d+", name) or re.fullmatch(r"([a-z]+)\d+", name)
    return str(Path(dev).parent / m.group(1)) if m else dev


def eject_card(card: Card, platform: str | None = None, run=None,
               which=None, sys_block: str | os.PathLike = "/sys/class/block") -> dict:
    """
    Unmount and power off. Returns {ok, state, message, steps}. State is
    "ejected", "unmounted" (data safe, power-off failed) or "failed".
    Never forces: a busy volume is reported, not killed.
    """
    platform = platform or sys.platform
    run = run or subprocess.run
    which = which or shutil.which
    steps: list[dict] = []

    def do(cmd: list[str]) -> bool:
        try:
            p = run(cmd, capture_output=True, text=True, timeout=120)
        except Exception as exc:
            steps.append({"cmd": cmd, "ok": False, "error": str(exc)})
            return False
        err = (getattr(p, "stderr", "") or getattr(p, "stdout", "") or "").strip()
        steps.append({"cmd": cmd, "ok": p.returncode == 0, "error": "" if p.returncode == 0 else err})
        return p.returncode == 0

    def last_error() -> str:
        return steps[-1].get("error") or "command failed"

    if platform == "darwin":
        if not which("diskutil"):
            return {"ok": False, "state": "failed", "message": "diskutil not found", "steps": steps}
        if do(["diskutil", "eject", str(card.mount)]):
            return {"ok": True, "state": "ejected", "message": "ejected", "steps": steps}
        return {"ok": False, "state": "failed", "steps": steps,
                "message": f"could not eject (is something using the card?): {last_error()}"}

    dev = card.device
    unmounted = busy = False
    have_udisks = bool(which("udisksctl") and dev)
    if have_udisks:
        unmounted = do(["udisksctl", "unmount", "-b", dev])
        err = last_error().lower()
        if not unmounted and "not mounted" in err:
            unmounted = True
        busy = not unmounted and "busy" in err
    if not unmounted and not busy and which("umount"):
        # Fallback: plain umount of the mount point (may need privileges). Never forced.
        unmounted = do(["umount", str(card.mount)])
    if not unmounted:
        return {"ok": False, "state": "failed", "steps": steps,
                "message": f"could not unmount (the card may be busy): {last_error()}"}
    if have_udisks:
        if do(["udisksctl", "power-off", "-b", parent_disk(dev, sys_block)]):
            return {"ok": True, "state": "ejected", "message": "unmounted and powered off", "steps": steps}
        return {"ok": False, "state": "unmounted", "steps": steps,
                "message": f"unmounted, but power-off failed: {last_error()}"}
    return {"ok": False, "state": "unmounted", "steps": steps,
            "message": "unmounted (no udisksctl, so the card was not powered off)"}


def eject_blockers(res: ImportResult, enabled: bool) -> str:
    """Why the card must NOT be ejected; '' when it is safe."""
    if not enabled:
        return "eject disabled"
    if res.interrupted:
        return "the import was interrupted"
    if res.problem:
        return res.problem
    if res.scan_errors:
        return f"the card could not be fully read ({res.scan_errors[0]})"
    if res.failed:
        return f"{len(res.failed)} file(s) failed to import"
    if res.verify_failed:
        return f"{len(res.verify_failed)} file(s) failed verification"
    if res.verified != res.found:
        return "not every file was verified"
    return ""


# ---------------------------------------------------------------------------
# Notifications and status
# ---------------------------------------------------------------------------

def notify(title: str, body: str, platform: str | None = None, run=None,
           which=None) -> bool:
    platform = platform or sys.platform
    run = run or subprocess.run
    which = which or shutil.which
    try:
        if platform == "darwin" and which("osascript"):
            esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
            run(["osascript", "-e", f'display notification "{esc(body)}" with title "{esc(title)}"'],
                capture_output=True, timeout=10)
            return True
        if which("notify-send"):
            run(["notify-send", "--app-name=photo-room", title, body], capture_output=True, timeout=10)
            return True
    except Exception:
        pass
    return False


class Status:
    """Progress for the editor: an atomic JSON file in the library, throttled."""

    def __init__(self, library: Path, card: str, label: str, clock=time.monotonic):
        self.path = library / STATUS_NAME
        self.data = {"id": uuid.uuid4().hex, "card": card, "label": label, "state": "detected",
                     "started": time.time()}
        self._last = 0.0
        self._clock = clock

    def update(self, force: bool = False, **fields) -> None:
        self.data.update(fields)
        self.data["updated"] = time.time()
        now = self._clock()
        if not force and now - self._last < 0.5:
            return
        self._last = now
        try:
            fsutil.atomic_write_text(self.path, json.dumps(self.data))
        except OSError:
            pass


def summary_text(res: ImportResult) -> str:
    bad = len(res.failed) + len(res.verify_failed) + len(res.scan_errors)
    return (f"{res.copied} imported, {res.duplicates} duplicates skipped, {bad} failed"
            + (f", {len(res.deleted)} deleted from card" if res.deleted else "")
            + (f", {len(res.would_delete)} would be deleted (dry run)" if res.would_delete else "")
            + (f", {res.sidecars} sidecar(s) carried" if res.sidecars else "")
            + (f", {len(res.backups)} backed up" if res.backups else "")
            + (f", {len(res.warnings)} warning(s): {res.warnings[0]}" if res.warnings else ""))


# ---------------------------------------------------------------------------
# One card, end to end
# ---------------------------------------------------------------------------

@dataclass
class Options:
    library: Path
    layout: str = "date"
    eject: bool = True
    delete_after_import: bool = False
    delete_dry_run: bool = False
    raw_only: bool = False
    notify: bool = True
    interval: float = 2.0
    debounce: int = 2
    rename: str = ""
    seq_start: int = 1
    sidecars: bool = True
    backup: Path | None = None
    rating: int | None = None
    keywords: tuple = ()
    copyright: str = ""

    def spec(self) -> importkit.ImportSpec:
        return importkit.ImportSpec(
            layout=self.layout, rename=self.rename, seq_start=self.seq_start, sidecars=self.sidecars,
            backup=self.backup, rating=self.rating, keywords=list(self.keywords), copyright=self.copyright)


def process_card(card: Card, opts: Options, *, notifier=notify, ejector=eject_card,
                 should_stop: Callable[[], bool] = lambda: False, log=print,
                 disk_usage=shutil.disk_usage) -> ImportResult:
    suffixes = RAW_ONLY if opts.raw_only else PHOTO_SUFFIXES | VIDEO_SUFFIXES
    status = Status(opts.library, str(card.mount), card.label or card.mount.name)
    say = notifier if opts.notify else (lambda *_a, **_k: None)

    def prog(s: dict) -> None:
        status.update(**s)

    log(f"Card detected: {card.mount} ({card.device or 'unknown device'})")
    status.update(force=True, state="detected")
    say("Photo Room", f"Card {status.data['label']} found, importing...")
    try:
        res = import_card(card.mount, opts.library, opts.layout, suffixes, prog, should_stop,
                          disk_usage=disk_usage, spec=opts.spec())
        if not res.problem and not res.interrupted:
            verify_import(res, prog, should_stop)
        blocker = eject_blockers(res, True) if not res.clean else ""
        if res.clean and opts.delete_after_import:
            status.update(force=True, state="deleting")
            delete_verified(res, card.mount, opts.delete_dry_run,
                            opts.library / DELETE_LOG_NAME, log)
        if res.clean:
            blocker = eject_blockers(res, opts.eject)
            if not blocker:
                status.update(force=True, state="ejecting")
                res.eject = ejector(card)
                if not res.eject["ok"]:
                    blocker = res.eject["message"]
        elif res.failed or res.verify_failed or res.scan_errors:
            # Say which files, once, in the log.
            for item in (res.failed + res.verify_failed)[:10]:
                log(f"  FAILED {item['file']}: {item['error']}")
    except CardError as exc:
        res = ImportResult(str(card.mount), str(opts.library), problem=str(exc))
        blocker = str(exc)
    except Exception as exc:
        res = ImportResult(str(card.mount), str(opts.library), problem=f"unexpected error: {exc}")
        blocker = res.problem

    text = summary_text(res)
    ejected = bool(res.eject and res.eject.get("ok"))
    if ejected:
        msg = f"{text}. Safe to remove the card."
    elif res.clean and not opts.eject:
        msg = f"{text}. The card was not ejected (eject is off)."
    elif res.clean and res.eject:
        msg = f"{text}. Imported and verified, but NOT ejected: {blocker.rstrip('.')}."
    else:
        msg = f"{text}. Card NOT ejected: {blocker or 'import incomplete'}."
    state = "done" if res.clean else ("error" if (res.problem or res.interrupted) else "done_with_errors")
    status.update(force=True, state=state, message=msg, ejected=ejected, summary=res.as_dict(),
                  eject_blocked=blocker if not ejected else "")
    log(msg)
    say("Photo Room: card " + ("ready to remove" if ejected else "needs attention" if not res.clean
                               else "imported"), msg)
    return res


# ---------------------------------------------------------------------------
# Watching
# ---------------------------------------------------------------------------

class LockHeld(Exception):
    pass


class RunLock:
    """One watcher/import per library at a time (flock; released if the process dies)."""

    def __init__(self, library: Path):
        self.path = library / LOCK_NAME
        self.fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        if fcntl is not None:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                self.fh.close()
                if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise LockHeld(f"another watch-cards/import is already running for {self.path.parent}")
                raise
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(str(os.getpid()))
        self.fh.flush()
        return self

    def __exit__(self, *exc):
        if self.fh:
            self.fh.close()


class Watcher:
    """Polls for cards; imports each insertion once (re-insertion = new insertion)."""

    def __init__(self, opts: Options, finder: Callable[[], list[Card]] | None = None,
                 processor=None, sleep=time.sleep, should_stop=lambda: False):
        self.opts = opts
        self.finder = finder or (lambda: find_cards())
        self.processor = processor or (lambda *a, **k: process_card(*a, **k))
        self.sleep, self.should_stop = sleep, should_stop
        self.sightings: dict[str, int] = {}
        self.handled: set[str] = set()

    def poll_once(self) -> list[ImportResult]:
        present = {c.key: c for c in self.finder()}
        # Removed volumes are forgotten, so putting the card back imports again.
        self.handled &= set(present)
        self.sightings = {k: n + 1 for k, n in self.sightings.items() if k in present} | \
                         {k: 1 for k in present if k not in self.sightings}
        results = []
        for key, card in present.items():
            if key in self.handled or self.sightings[key] < self.opts.debounce:
                continue
            self.handled.add(key)
            if self.should_stop():
                break
            results.append(self.processor(card, self.opts, should_stop=self.should_stop))
        return results

    def serve(self) -> None:
        while not self.should_stop():
            self.poll_once()
            self.sleep(self.opts.interval)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "photo-room" / "card-watch.json"


def load_config(path: Path | None = None) -> dict:
    """Settings file (JSON). `delete_after_import` is deliberately never read from it."""
    path = path or config_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="photo-room watch-cards",
        description="Import a camera card when it is inserted, verify it, then eject it safely.")
    p.add_argument("library", nargs="?", help=f"Library folder (default: config or {DEFAULT_LIBRARY})")
    p.add_argument("--layout", help="date (YYYY/MM/DD, default), month, flat, or a template "
                                    "such as '{yyyy}/{yyyy}-{mm}-{dd}'")
    p.add_argument("--rename", help="Rename on import, e.g. '{date}_{camera}_{seq:4}' "
                                    "(fields: {date} {yyyy} {mm} {dd} {time} {seq} {camera} {original}); default keeps the name")
    p.add_argument("--seq-start", type=int, default=1, help="First {seq} number (default 1)")
    p.add_argument("--no-sidecars", action="store_true", help="Do not carry the card's .xmp sidecars along with the raws")
    p.add_argument("--backup", type=Path, help="Also copy every file (verified) to this second folder")
    p.add_argument("--rating", type=int, help="Star rating (0-5, -1 rejected) written to each new photo's sidecar")
    p.add_argument("--keywords", help="Comma separated keywords added to each new photo's sidecar")
    p.add_argument("--copyright", help="Copyright notice written to each new photo's sidecar")
    p.add_argument("--no-eject", action="store_true", help="Import and verify but leave the card mounted")
    p.add_argument("--delete-after-import", action="store_true",
                   help="After a fully verified import, delete the verified PHOTO files from the card "
                        "(never videos, other files or folders). Off by default.")
    p.add_argument("--delete-dry-run", action="store_true",
                   help="With --delete-after-import: only log what would be deleted")
    p.add_argument("--raw-only", action="store_true", help="Import raw files only (default: raw, JPEG/HEIC and video)")
    p.add_argument("--no-notify", action="store_true", help="No desktop notifications")
    p.add_argument("--once", action="store_true", help="Import the cards mounted now, then exit")
    p.add_argument("--interval", type=float, help="Poll seconds (default 2)")
    p.add_argument("--config", type=Path, help=f"Settings file (default {config_path()})")
    return p


def options_from(args: argparse.Namespace, cfg: dict) -> Options:
    lib = args.library or cfg.get("library") or DEFAULT_LIBRARY
    return Options(
        library=Path(lib).expanduser(),
        layout=args.layout or cfg.get("layout", "date"),
        eject=not args.no_eject and bool(cfg.get("eject", True)),
        delete_after_import=bool(args.delete_after_import),
        delete_dry_run=bool(args.delete_dry_run),
        raw_only=args.raw_only or bool(cfg.get("raw_only", False)),
        notify=not args.no_notify and bool(cfg.get("notify", True)),
        interval=float(args.interval or cfg.get("interval", 2.0)),
        rename=args.rename if args.rename is not None else cfg.get("rename", ""),
        seq_start=args.seq_start,
        sidecars=not args.no_sidecars and bool(cfg.get("sidecars", True)),
        backup=(args.backup or (Path(cfg["backup"]) if cfg.get("backup") else None)),
        rating=args.rating,
        keywords=tuple(k.strip() for k in (args.keywords or "").split(",") if k.strip()),
        copyright=args.copyright or "",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    opts = options_from(args, load_config(args.config))
    if args.delete_dry_run and not args.delete_after_import:
        print("--delete-dry-run needs --delete-after-import", file=sys.stderr)
        return 2
    try:
        opts.spec().validate()
    except importkit.TemplateError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    try:
        resolve_layout(opts.layout)
    except CardError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):  # pragma: no cover
        pass

    stop = {"flag": False}

    def on_signal(_sig, _frm):
        stop["flag"] = True        # finish the current file, then stop: never eject after this

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    try:
        opts.library.mkdir(parents=True, exist_ok=True)
        with RunLock(opts.library):
            print(f"Watching for camera cards. Library: {opts.library}  "
                  f"layout={opts.layout}  eject={'on' if opts.eject else 'off'}"
                  + ("  DELETE-AFTER-IMPORT"
                     + (" (dry run)" if opts.delete_dry_run else "") if opts.delete_after_import else ""))
            w = Watcher(opts, should_stop=lambda: stop["flag"])
            if args.once:
                w.opts = Options(**{**opts.__dict__, "debounce": 1})
                failed = any(not r.clean for r in w.poll_once())
                return 1 if failed else 0
            w.serve()
            print("Stopped.")
    except LockHeld as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"Error: cannot use library folder {opts.library}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
