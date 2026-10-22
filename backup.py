"""
Library backup and restore.

The "catalogue" is a set of small files beside the photos. ``create_backup``
snapshots them into ``<dest>/photo-room-backup-YYYYMMDD-HHMMSS`` (a folder, or
a ``.zip`` with ``zip_it=True``) together with ``manifest.json`` holding the
SHA-256 of every file. The cull database is copied with SQLite's online backup
API, so a busy editor cannot hand you a torn file.

Optionally the raws are mirrored to ``<dest>/raws/`` (``include_raws=True``): an
incremental copy that skips files already there with the same hash and verifies
every copy it makes. The mirror is shared by all snapshots and never pruned.
Only the newest ``keep`` snapshots are kept.

``restore_backup`` verifies every checksum before writing anything and never
replaces a file that differs from the backup unless ``force`` is set or the
``confirm`` callback agrees. Writes are atomic.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

import fsutil

PREFIX = "photo-room-backup-"
MANIFEST = "manifest.json"
STATE_DIR = "state"
RAW_MIRROR = "raws"
SQLITE_NAMES = {".photo-room-cull.sqlite"}
STATE_NAMES = {".photo-room-library.json", ".photo-room-catalog.json"} | SQLITE_NAMES
STATE_GLOBS = ("*.photoroom.json", "*.xmp")
RAW_SUFFIXES = (".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2", ".dng")
SKIP_DIRS = {".photo-room-thumbs"}
DEFAULT_KEEP = 10
_run_lock = threading.Lock()


class BackupError(Exception):
    pass


def sha256_file(path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def _walk(root: Path, skip: Path | None):
    for dirpath, dirs, files in os.walk(root):
        dp = Path(dirpath)
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".backup-tmp-")
                   and (skip is None or (dp / d).resolve() != skip)]
        for f in files:
            yield dp / f


def find_state_files(root, skip: Path | None = None) -> list[Path]:
    out = []
    for p in _walk(Path(root), skip):
        n = p.name
        if n in STATE_NAMES or any(fnmatch.fnmatch(n.lower(), g) for g in STATE_GLOBS):
            out.append(p)
    return sorted(out)


def find_raws(root, skip: Path | None = None) -> list[Path]:
    return sorted(p for p in _walk(Path(root), skip) if p.suffix.lower() in RAW_SUFFIXES)


def _copy_sqlite(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    a = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    b = sqlite3.connect(dst)
    try:
        a.backup(b)
    finally:
        b.close()
        a.close()


def _copy_verified(src: Path, dst: Path) -> str:
    """Copy via temp file + rename; returns the hash, raising if the copy differs."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dst.parent, prefix=f".{dst.name}.", suffix=".tmp")
    os.close(fd)
    try:
        shutil.copyfile(src, tmp)
        want, got = sha256_file(src), sha256_file(tmp)
        if want != got:
            raise BackupError(f"copy of {src} did not verify")
        os.replace(tmp, dst)
        return got
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _mirror_raws(root: Path, dest: Path, skip: Path | None) -> tuple[dict, int, int]:
    files, copied, skipped = {}, 0, 0
    for src in find_raws(root, skip):
        rel = src.relative_to(root).as_posix()
        dst = dest / RAW_MIRROR / rel
        if dst.is_file() and dst.stat().st_size == src.stat().st_size:
            h = sha256_file(src)
            if sha256_file(dst) == h:
                skipped += 1
                files[rel] = h
                continue
        files[rel] = _copy_verified(src, dst)
        copied += 1
    return files, copied, skipped


def list_backups(dest) -> list[Path]:
    dest = Path(dest).expanduser()
    if not dest.is_dir():
        return []
    return sorted(p for p in dest.iterdir()
                  if p.name.startswith(PREFIX) and (p.is_dir() or p.suffix == ".zip"))


def prune(dest, keep: int) -> list[str]:
    old = list_backups(dest)[:-keep] if keep > 0 else []
    for p in old:
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    return [p.name for p in old]


def create_backup(root, dest, *, include_raws: bool = False, zip_it: bool = False,
                  keep: int = DEFAULT_KEEP, now: datetime | None = None) -> dict:
    root = Path(root).expanduser().resolve()
    dest = Path(dest).expanduser().resolve()
    if not root.is_dir():
        raise BackupError(f"not a folder: {root}")
    dest.mkdir(parents=True, exist_ok=True)
    skip = dest if root in dest.parents else None   # backup folder inside the library
    name = PREFIX + (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    ext = ".zip" if zip_it else ""
    final, n = dest / (name + ext), 1
    while final.exists():
        n += 1
        final = dest / f"{name}-{n}{ext}"
    work = Path(tempfile.mkdtemp(dir=dest, prefix=".backup-tmp-"))
    try:
        files: dict[str, str] = {}
        for src in find_state_files(root, skip):
            rel = src.relative_to(root).as_posix()
            tgt = work / STATE_DIR / rel
            if src.name in SQLITE_NAMES:
                _copy_sqlite(src, tgt)
            else:
                tgt.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, tgt)
            files[rel] = sha256_file(tgt)
        raws, copied, skipped = {}, 0, 0
        if include_raws:
            raws, copied, skipped = _mirror_raws(root, dest, skip)
        manifest = {"version": 1, "created": time.time(), "library": str(root),
                    "files": files, "raws": raws,
                    "raws_mirror": RAW_MIRROR if include_raws else None}
        (work / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True))
        if zip_it:
            tmpzip = work.parent / (work.name + ".zip")
            with zipfile.ZipFile(tmpzip, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in sorted(work.rglob("*")):
                    if p.is_file():
                        zf.write(p, p.relative_to(work).as_posix())
            os.replace(tmpzip, final)
        else:
            os.replace(work, final)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    removed = prune(dest, keep)
    return {"path": str(final), "files": len(files), "raws": len(raws),
            "raws_copied": copied, "raws_skipped": skipped, "pruned": removed,
            "created": manifest["created"]}


# --- restore -----------------------------------------------------------------

class _Source:
    """Read access to a backup folder or zip."""

    def __init__(self, path: Path):
        self.path = path
        self.zf = zipfile.ZipFile(path) if path.is_file() else None

    def read(self, rel: str) -> bytes:
        if self.zf:
            return self.zf.read(rel)
        return (self.path / rel).read_bytes()

    def hash(self, rel: str) -> str:
        if self.zf:
            h = hashlib.sha256()
            with self.zf.open(rel) as fh:
                while b := fh.read(1 << 20):
                    h.update(b)
            return h.hexdigest()
        return sha256_file(self.path / rel)

    def close(self):
        if self.zf:
            self.zf.close()


def _open(backup) -> tuple[_Source, dict]:
    p = Path(backup).expanduser()
    if not p.exists():
        raise BackupError(f"no such backup: {p}")
    if p.is_file() and p.suffix != ".zip":
        raise BackupError(f"not a backup folder or .zip: {p}")
    try:
        src = _Source(p)
        manifest = json.loads(src.read(MANIFEST))
    except (KeyError, OSError, ValueError, zipfile.BadZipFile) as exc:
        raise BackupError(f"{p} has no readable {MANIFEST}: {exc}")
    return src, manifest


def _safe_rel(rel: str) -> str:
    parts = Path(rel).parts
    if Path(rel).is_absolute() or ".." in parts or not parts:
        raise BackupError(f"unsafe path in backup: {rel}")
    return rel


def verify_backup(backup) -> dict:
    """Check every state file against the manifest. Raises BackupError on any mismatch."""
    src, m = _open(backup)
    try:
        bad = []
        for rel, want in m["files"].items():
            _safe_rel(rel)
            try:
                if src.hash(f"{STATE_DIR}/{rel}") != want:
                    bad.append(rel)
            except (KeyError, OSError):
                bad.append(rel)
        if bad:
            raise BackupError("checksum mismatch or missing: " + ", ".join(bad[:5])
                              + (f" (+{len(bad) - 5} more)" if len(bad) > 5 else ""))
        return m
    finally:
        src.close()


def restore_backup(backup, library, *, force: bool = False, confirm=None,
                   include_raws: bool = False) -> dict:
    """Restore state files into ``library``.

    A file whose content already equals the backup is left alone. A file that
    differs is replaced only if ``force`` or ``confirm(rel)`` is true; otherwise
    it is skipped and listed in ``conflicts``.
    """
    library = Path(library).expanduser().resolve()
    m = verify_backup(backup)
    src, _ = _open(backup)
    restored, same, conflicts, raws_done = [], [], [], []
    try:
        for rel, want in sorted(m["files"].items()):
            tgt = library / _safe_rel(rel)
            if tgt.exists():
                if sha256_file(tgt) == want:
                    same.append(rel)
                    continue
                if not (force or (confirm and confirm(rel))):
                    conflicts.append(rel)
                    continue
            data = src.read(f"{STATE_DIR}/{rel}")
            if hashlib.sha256(data).hexdigest() != want:
                raise BackupError(f"checksum failed for {rel}")
            tgt.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=tgt.parent, prefix=f".{tgt.name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, tgt)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            restored.append(rel)
    finally:
        src.close()
    if include_raws and m.get("raws"):
        mirror = Path(backup).expanduser().resolve().parent / (m.get("raws_mirror") or RAW_MIRROR)
        for rel, want in sorted(m["raws"].items()):
            s, tgt = mirror / _safe_rel(rel), library / rel
            if not s.is_file() or sha256_file(s) != want:
                raise BackupError(f"raw mirror file missing or corrupt: {rel}")
            if tgt.exists():
                if sha256_file(tgt) == want:
                    same.append(rel)
                    continue
                if not (force or (confirm and confirm(rel))):
                    conflicts.append(rel)
                    continue
            _copy_verified(s, tgt)
            raws_done.append(rel)
    return {"restored": restored, "unchanged": same, "conflicts": conflicts, "raws": raws_done}


# --- settings (reminder interval, destination) ---------------------------------

DEFAULT_SETTINGS = {"dest": "", "interval_days": 7, "keep": DEFAULT_KEEP,
                    "include_raws": False, "zip": False, "auto": False, "last_backup": 0.0}


def settings_file() -> Path:
    import export_presets
    return export_presets.config_dir() / "backup.json"


def _write_settings(s: dict) -> None:
    with fsutil.lock_for(settings_file()):
        fsutil.atomic_write_text(settings_file(), json.dumps(s, indent=2, sort_keys=True))


def load_settings() -> dict:
    out = dict(DEFAULT_SETTINGS)
    try:
        data = json.loads(settings_file().read_text())
        if isinstance(data, dict):
            out.update({k: data[k] for k in DEFAULT_SETTINGS if k in data})
    except (OSError, ValueError):
        pass
    return out


def save_settings(changes: dict) -> dict:
    s = load_settings()
    for k, v in changes.items():
        if k not in DEFAULT_SETTINGS or k == "last_backup":
            continue
        if k in ("include_raws", "zip", "auto"):
            v = bool(v)
        elif k in ("interval_days", "keep"):
            v = int(v)
            if not 0 <= v <= 3650 or (k == "keep" and v < 1):
                raise ValueError(f"{k} out of range")
        else:
            v = str(v).strip()
        s[k] = v
    _write_settings(s)
    return s


def is_due(settings: dict | None = None, now: float | None = None) -> bool:
    s = settings or load_settings()
    if not s["dest"] or s["interval_days"] <= 0:
        return False
    return (now or time.time()) - float(s["last_backup"] or 0) >= s["interval_days"] * 86400


def run_configured(root) -> dict:
    """Back up using the saved settings and stamp ``last_backup``."""
    s = load_settings()
    if not s["dest"]:
        raise BackupError("no backup folder set")
    if not _run_lock.acquire(blocking=False):
        raise BackupError("a backup is already running")
    try:
        res = create_backup(root, s["dest"], include_raws=s["include_raws"],
                            zip_it=s["zip"], keep=s["keep"])
        s2 = load_settings()
        s2["last_backup"] = res["created"]
        _write_settings(s2)
        return res
    finally:
        _run_lock.release()


def maybe_auto_backup(root) -> bool:
    """Editor start: run a background backup when auto is on and one is due."""
    s = load_settings()
    if not (s["auto"] and is_due(s)):
        return False

    def go():
        try:
            run_configured(root)
        except Exception as exc:  # never break the editor over a backup
            print(f"Scheduled backup failed: {exc}")
    threading.Thread(target=go, daemon=True, name="auto-backup").start()
    return True


# --- command line ---------------------------------------------------------------

def main(argv=None) -> int:
    import argparse
    import sys
    p = argparse.ArgumentParser(prog="photo-room backup", description="Snapshot library state files.")
    p.add_argument("library")
    p.add_argument("-o", "--output", required=True, help="Folder to put the backup in")
    p.add_argument("--zip", action="store_true", help="Write a .zip instead of a folder")
    p.add_argument("--include-raws", action="store_true",
                   help="Also mirror raws to <output>/raws (incremental, hash-verified)")
    p.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="Snapshots to keep (default 10)")
    a = p.parse_args(argv)
    try:
        r = create_backup(a.library, a.output, include_raws=a.include_raws, zip_it=a.zip, keep=a.keep)
    except (BackupError, OSError) as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Backed up {r['files']} state files to {r['path']}")
    if a.include_raws:
        print(f"Raws: {r['raws_copied']} copied, {r['raws_skipped']} already up to date")
    if r["pruned"]:
        print(f"Removed old backups: {', '.join(r['pruned'])}")
    return 0


def restore_main(argv=None) -> int:
    import argparse
    import sys
    p = argparse.ArgumentParser(prog="photo-room restore", description="Restore a backup into a library.")
    p.add_argument("backup", help="Backup folder or .zip")
    p.add_argument("library")
    p.add_argument("--force", action="store_true", help="Overwrite differing files without asking")
    p.add_argument("--include-raws", action="store_true", help="Also restore mirrored raws")
    a = p.parse_args(argv)

    def ask(rel):
        if not sys.stdin.isatty():
            return False
        return input(f"Overwrite {rel}? [y/N] ").strip().lower() in ("y", "yes")

    try:
        r = restore_backup(a.backup, a.library, force=a.force, confirm=ask, include_raws=a.include_raws)
    except (BackupError, OSError) as exc:
        print(f"restore failed: {exc}", file=sys.stderr)
        return 1
    print(f"Restored {len(r['restored'])} files, {len(r['unchanged'])} already identical.")
    if r["conflicts"]:
        print(f"Skipped {len(r['conflicts'])} that differ (use --force to overwrite): "
              + ", ".join(r["conflicts"][:5]), file=sys.stderr)
        return 2
    return 0
