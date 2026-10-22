#!/usr/bin/env python3
"""
Folders panel back end: folder tree, create folder, and safe move / rename of
photos and folders.

Rules this module never breaks:

* Nothing is ever deleted and nothing is ever overwritten. Every destination
  is checked first, the whole operation is planned before the first file is
  touched, and a file is moved with a hard link (which fails if the target
  exists) followed by removing the old name. A failure part-way rolls the
  files already moved back.
* A photo moves together with its companions: same-stem raws / JPEG / ``.xmp``
  and, for each of those, ``<name>.photoroom.json`` (edit history),
  ``<name>.masks.json``, ``<name>.develop.json``, ``<name>.extras.json`` and the virtual copies' ``<name>.vc-xxxxxxxx.*`` files.
* Everything that remembers a photo by its library path follows it: the
  catalog (virtual copies, collection membership), the pick flags in
  ``.photo-room-library.json`` and the cull database. (The index's pick set is
  carried by the API layer.)
* Every path from the client goes through ``security.safe_relpath`` /
  ``safe_name``.
* Each operation is appended to ``.photo-room-moves.json`` so the latest one
  can be undone.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import catalog
import fsutil
import library
import process
import security

LOG_NAME = ".photo-room-moves.json"
LOG_KEEP = 50
GROUP_EXT = {*process.RAW_SUFFIXES, ".xmp", ".jpg", ".jpeg"}
RAW_EXT = set(process.RAW_SUFFIXES)
_VC_FILE = r"\.vc-[0-9a-f]{8}\.(?:photoroom|masks)\.json"
_lock = threading.RLock()


class MoveError(ValueError):
    """A refused or failed operation; the message is shown to the user."""


# ---------------------------------------------------------------------------
# Tree
# ---------------------------------------------------------------------------

def _hidden(name: str) -> bool:
    return name.startswith(".") or name in process.LIBRARY_EXCLUDED


def tree(root, rels: list[str] | None = None) -> list[dict]:
    """
    Every folder under ``root`` (empty ones too): ``{path, name, depth, count,
    total}``; ``count`` is the photos directly inside, ``total`` includes
    subfolders. ``rels`` are the library's photo paths (default: scan).
    """
    root = Path(os.path.realpath(root))
    if rels is None:
        rels = [p.relative_to(root).as_posix() for p in process.list_photos(root)]
    direct: dict[str, int] = {}
    total: dict[str, int] = {}
    for rel in rels:
        rel = catalog.split_id(rel)[0]
        parent = rel.rpartition("/")[0]
        direct[parent] = direct.get(parent, 0) + 1
        while True:
            total[parent] = total.get(parent, 0) + 1
            if not parent:
                break
            parent = parent.rpartition("/")[0]
    out = [{"path": "", "name": root.name or "Library", "depth": 0,
            "count": direct.get("", 0), "total": total.get("", 0)}]
    for dirpath, dirs, _ in os.walk(root):
        dirs[:] = sorted(d for d in dirs
                         if not _hidden(d) and not os.path.islink(os.path.join(dirpath, d)))
        base = Path(dirpath).relative_to(root).as_posix()
        base = "" if base == "." else base
        for d in dirs:
            rel = f"{base}/{d}" if base else d
            out.append({"path": rel, "name": d, "depth": rel.count("/") + 1,
                        "count": direct.get(rel, 0), "total": total.get(rel, 0)})
    return out


def in_folder(photo_id: str, folder: str, subfolders: bool) -> bool:
    """Is the photo (or virtual copy) id inside ``folder`` ('' is the root)?"""
    parent = catalog.split_id(photo_id)[0].rpartition("/")[0]
    if parent == folder:
        return True
    return bool(subfolders) and (not folder or parent.startswith(folder + "/"))


# ---------------------------------------------------------------------------
# Low level moves
# ---------------------------------------------------------------------------

def _move_one(src: Path, dst: Path) -> None:
    """Move without ever replacing ``dst``."""
    if os.path.lexists(dst):
        raise FileExistsError(str(dst))
    if src.is_dir() and not src.is_symlink():
        os.rename(src, dst)
        return
    try:
        os.link(src, dst)             # atomic, and refuses to replace an existing file
    except FileExistsError:
        raise
    except OSError:                   # filesystem without hard links
        if os.path.lexists(dst):
            raise FileExistsError(str(dst)) from None
        os.rename(src, dst)
        return
    os.unlink(src)                    # the data now lives under the new name


def _apply(pairs: list[tuple[Path, Path]]) -> None:
    """Plan-checked, all-or-nothing batch of moves."""
    seen = set()
    for src, dst in pairs:
        if not os.path.lexists(src):
            raise MoveError(f"{src.name} no longer exists")
        if os.path.lexists(dst) or dst in seen:
            raise MoveError(f"{dst.name} already exists in {dst.parent.name or 'the library'}; "
                            "nothing was moved")
        if not dst.parent.is_dir():
            raise MoveError("The destination folder does not exist")
        seen.add(dst)
    done: list[tuple[Path, Path]] = []
    try:
        for src, dst in pairs:
            _move_one(src, dst)
            done.append((src, dst))
    except BaseException as exc:
        for src, dst in reversed(done):
            try:
                _move_one(dst, src)
            except OSError:
                pass
        if isinstance(exc, FileExistsError):
            raise MoveError(f"{Path(exc.args[0]).name} already exists; nothing was moved") from None
        if isinstance(exc, OSError):
            raise MoveError(f"Could not move: {exc.strerror or exc}") from None
        raise


def group_files(photo: Path) -> list[Path]:
    """The photo and every companion that must travel with it."""
    d, stem = photo.parent, photo.stem
    if photo.is_symlink():
        raise MoveError(f"{photo.name} is a symbolic link; it is not moved")
    names = set(os.listdir(d))
    members = [n for n in names
               if os.path.splitext(n)[0] == stem and os.path.splitext(n)[1].lower() in GROUP_EXT]
    if photo.name not in members:
        raise MoveError(f"No such photo: {photo.name}")
    found = set(members)
    for m in members:
        pat = re.compile(re.escape(m) + r"(?:\.photoroom\.json|\.masks\.json|\.develop\.json|\.extras\.json|" + _VC_FILE + ")$")
        found.update(n for n in names if pat.fullmatch(n))
    out = [d / n for n in sorted(found)]
    for p in out:
        if p.is_symlink() or not p.is_file():
            raise MoveError(f"{p.name} is not a regular file; nothing was moved")
    return out


# ---------------------------------------------------------------------------
# State that remembers photos by path
# ---------------------------------------------------------------------------

def _key_mapper(mapping: dict[str, str]):
    def f(key):
        if not isinstance(key, str):
            return key
        src, vid = catalog.split_id(key)
        new = mapping.get(src)
        if new is None:
            return key
        return new if vid is None else catalog.virtual_id(new, vid)
    return f


def remap_state(root: Path, mapping: dict[str, str]) -> list[str]:
    """Rewrite every stored reference old rel -> new rel. Returns warnings."""
    if not mapping:
        return []
    f = _key_mapper(mapping)
    warnings: list[str] = []
    with catalog._lock:
        store = catalog.load(root)
        changed = False
        virtual = {}
        for k, v in store["virtual"].items():
            nk = f(k)
            if nk != k:
                v = {**v, "id": nk, "source": mapping.get(v.get("source"), v.get("source"))}
                changed = True
            virtual[nk] = v
        store["virtual"] = virtual
        for col in store["collections"]:
            if isinstance(col.get("photos"), list):
                new = catalog._dedupe([f(p) for p in col["photos"]])
                if new != col["photos"]:
                    col["photos"] = new
                    changed = True
        if changed:
            catalog._save(root, store)
    with library._lock:
        store = library.load_store(root)
        changed = False
        for part in ("flags", "pre_reject"):
            moved = {f(k): v for k, v in store[part].items()}
            if moved != store[part]:
                store[part] = moved
                changed = True
        if changed:
            library._save_store(root, store)
    db = Path(root) / ".photo-room-cull.sqlite"
    if db.exists():
        try:
            con = sqlite3.connect(db, timeout=30)
            with con:
                for table in ("features", "decisions", "autoedit", "offsets"):
                    for old, new in mapping.items():
                        con.execute(f"UPDATE OR REPLACE {table} SET path=? WHERE path=?", (new, old))
            con.close()
        except sqlite3.Error as exc:
            warnings.append(f"Cull database not updated: {exc}")
    return warnings


def _state_pairs(file_pairs: list[tuple[Path, Path]], root: Path) -> dict[str, str]:
    out = {}
    for src, dst in file_pairs:
        if src.suffix.lower() in RAW_EXT and dst.suffix.lower() in RAW_EXT:
            out[src.relative_to(root).as_posix()] = dst.relative_to(root).as_posix()
    return out


# ---------------------------------------------------------------------------
# Undo log
# ---------------------------------------------------------------------------

def _log_path(root) -> Path:
    return Path(root) / LOG_NAME


def load_log(root) -> list[dict]:
    try:
        data = json.loads(_log_path(root).read_text())
        return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _record(root: Path, entry: dict) -> dict:
    entry = {"id": uuid.uuid4().hex[:10], "ts": time.time(), "undone": False, **entry}
    log = (load_log(root) + [entry])[-LOG_KEEP:]
    fsutil.atomic_write_text(_log_path(root), json.dumps(log, indent=1))
    return entry


def last_undoable(root) -> dict | None:
    for e in reversed(load_log(root)):
        if not e.get("undone"):
            return e
    return None


def _summary(entry: dict, n: int, warnings: list[str]) -> dict:
    return {"ok": True, "op": entry["id"], "label": entry["label"], "moved": n,
            "mapping": entry.get("state", {}), "warnings": warnings}


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def _root(root) -> Path:
    return Path(os.path.realpath(root))


def create_folder(root, parent: str, name: str) -> dict:
    root = _root(root)
    with _lock:
        try:
            base = security.safe_relpath(root, parent, allow_root=True)
            name = security.safe_name(name)
        except ValueError as exc:
            raise MoveError(str(exc)) from None
        if not base.is_dir():
            raise MoveError("The parent is not a folder")
        target = base / name
        if os.path.lexists(target):
            raise MoveError(f"{name} already exists")
        os.mkdir(target)                                   # fails rather than reuse
        rel = target.relative_to(root).as_posix()
        entry = _record(root, {"kind": "mkdir", "label": f"Create folder {rel}", "created": rel})
        return {"ok": True, "op": entry["id"], "label": entry["label"], "path": rel,
                "moved": 0, "mapping": {}, "warnings": []}


def move_photos(root, rels: list, dest: str) -> dict:
    root = _root(root)
    with _lock:
        try:
            dest_dir = security.safe_relpath(root, dest, allow_root=True)
            if not dest_dir.is_dir():
                raise MoveError("The destination is not a folder")
            if not isinstance(rels, list) or not rels:
                raise MoveError("No photos given")
            photos, seen = [], set()
            for r in rels:                              # a virtual copy moves with its raw
                r = catalog.split_id(r)[0] if isinstance(r, str) else r
                p = security.safe_relpath(root, r)
                if p.suffix.lower() not in RAW_EXT or not p.is_file():
                    raise MoveError(f"Not a photo: {r}")
                if p not in seen:
                    seen.add(p)
                    photos.append(p)
        except MoveError:
            raise
        except ValueError as exc:
            raise MoveError(str(exc)) from None
        pairs: list[tuple[Path, Path]] = []
        for p in photos:
            if p.parent == dest_dir:
                raise MoveError(f"{p.name} is already in that folder")
            pairs += [(f, dest_dir / f.name) for f in group_files(p)]
        _apply(pairs)
        state = _state_pairs(pairs, root)
        warnings = remap_state(root, state)
        label = f"Move {len(photos)} photo(s) to {dest_dir.relative_to(root).as_posix() or 'the library root'}"
        entry = _record(root, {
            "kind": "move_photos", "label": label, "state": state,
            "pairs": [[s.relative_to(root).as_posix(), d.relative_to(root).as_posix()] for s, d in pairs]})
        return _summary(entry, len(pairs), warnings)


def rename_photo(root, rel: str, new_name: str) -> dict:
    root = _root(root)
    with _lock:
        try:
            p = security.safe_relpath(root, catalog.split_id(rel)[0] if isinstance(rel, str) else rel)
            new = security.safe_name(new_name)
        except ValueError as exc:
            raise MoveError(str(exc)) from None
        if p.suffix.lower() not in RAW_EXT or not p.is_file():
            raise MoveError("Not a photo")
        if new.lower().endswith(p.suffix.lower()):
            new = new[: -len(p.suffix)]
            new = security.safe_name(new)
        if new == p.stem:
            raise MoveError("That is already its name")
        pairs = [(f, f.with_name(new + f.name[len(p.stem):])) for f in group_files(p)]
        _apply(pairs)
        state = _state_pairs(pairs, root)
        warnings = remap_state(root, state)
        entry = _record(root, {
            "kind": "rename_photo", "label": f"Rename {p.stem} to {new}", "state": state,
            "pairs": [[s.relative_to(root).as_posix(), d.relative_to(root).as_posix()] for s, d in pairs]})
        return _summary(entry, len(pairs), warnings)


def _dir_state(src: Path, dst: Path, root: Path) -> dict[str, str]:
    out = {}
    for dirpath, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(dirpath, d))]
        for fn in files:
            if os.path.splitext(fn)[1].lower() in RAW_EXT:
                a = Path(dirpath) / fn
                out[a.relative_to(root).as_posix()] = (dst / a.relative_to(src)).relative_to(root).as_posix()
    return out


def _move_dir(root: Path, src: Path, dst: Path, kind: str, label: str) -> dict:
    if src == root:
        raise MoveError("The library folder itself cannot be moved")
    if dst == src or src in dst.parents:
        raise MoveError("A folder cannot be moved into itself")
    state = _dir_state(src, dst, root)
    _apply([(src, dst)])
    warnings = remap_state(root, state)
    entry = _record(root, {
        "kind": kind, "label": label, "state": state,
        "pairs": [[src.relative_to(root).as_posix(), dst.relative_to(root).as_posix()]]})
    return _summary(entry, len(state), warnings)


def move_folder(root, rel: str, dest: str) -> dict:
    root = _root(root)
    with _lock:
        try:
            src = security.safe_relpath(root, rel)
            dest_dir = security.safe_relpath(root, dest, allow_root=True)
        except ValueError as exc:
            raise MoveError(str(exc)) from None
        if not src.is_dir() or not dest_dir.is_dir():
            raise MoveError("Not a folder")
        if src.parent == dest_dir:
            raise MoveError("It is already in that folder")
        return _move_dir(root, src, dest_dir / src.name, "move_folder",
                         f"Move folder {src.name} to {dest_dir.relative_to(root).as_posix() or 'the library root'}")


def rename_folder(root, rel: str, new_name: str) -> dict:
    root = _root(root)
    with _lock:
        try:
            src = security.safe_relpath(root, rel)
            new = security.safe_name(new_name)
        except ValueError as exc:
            raise MoveError(str(exc)) from None
        if not src.is_dir():
            raise MoveError("Not a folder")
        if new == src.name:
            raise MoveError("That is already its name")
        return _move_dir(root, src, src.with_name(new), "rename_folder",
                         f"Rename folder {src.name} to {new}")


def undo_last(root) -> dict:
    root = _root(root)
    with _lock:
        entry = last_undoable(root)
        if entry is None:
            raise MoveError("Nothing to undo")
        log = load_log(root)
        try:
            if entry["kind"] == "mkdir":
                d = security.safe_relpath(root, entry["created"])
                try:
                    os.rmdir(d)                       # only ever an empty folder we made
                except OSError:
                    raise MoveError(f"{entry['created']} is no longer empty; it was left in place") from None
                warnings, n = [], 0
            else:
                pairs = []
                for old, new in reversed(entry["pairs"]):
                    pairs.append((security.safe_relpath(root, new),
                                  security.safe_relpath(root, old, must_exist=False)))
                _apply(pairs)
                inverse = {v: k for k, v in entry.get("state", {}).items()}
                warnings, n = remap_state(root, inverse), len(pairs)
        except MoveError:
            raise
        except ValueError as exc:
            raise MoveError(str(exc)) from None
        for e in log:
            if e.get("id") == entry["id"]:
                e["undone"] = True
        fsutil.atomic_write_text(_log_path(root), json.dumps(log, indent=1))
        return {"ok": True, "op": entry["id"], "label": "Undid: " + entry["label"], "moved": n,
                "mapping": {v: k for k, v in entry.get("state", {}).items()}, "warnings": warnings}
