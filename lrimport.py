#!/usr/bin/env python3
"""
Lightroom Classic catalog (.lrcat) import.

    photo-room import-lightroom <catalog.lrcat | folder> --library DIR
        [--remap OLD=NEW]... [--force] [--dry-run] [--yes]

An .lrcat is a SQLite database. It is never opened in place: it is copied to a
temporary file first and that copy is opened read-only, so the catalog (and any
Lightroom lock/journal next to it) is never touched.

What maps cleanly, per photo: star rating, pick/reject flag, colour label,
keywords (leaf names in ``dc:subject``, full paths in ``lr:hierarchicalSubject``),
develop settings (the Lua table in ``Adobe_imageDevelopSettings.text`` ->
Camera Raw ``crs:`` tags in an ``.xmp`` sidecar), manual collections (collection
sets flattened into ``Set / Child`` names) and virtual copies (as photo-room
virtual copies). Smart collections, stacks, tone curves, masks, local
adjustments, lens corrections etc. are counted and listed in the report but not
converted; stacks are recorded in ``.photo-room-lrimport.json``.

Files are matched by original path (after ``--remap OLD=NEW`` prefix rewrites),
then by the catalog's folder path relative to the library. Nothing is matched by
bare file name. An existing sidecar is never overwritten unless ``force`` is set
(then it is updated in place, keeping tags we do not manage). Raws are never
written. Re-running is safe: virtual copies and collection members are not
duplicated.

Written against the commonly seen schema (Lightroom Classic 6 - 13); every
table and column is looked up first, so a missing one degrades to "not
imported" rather than an error. It has not been run against real catalogs.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import zlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import catalog
import colormix
import exifpool
import fsutil
import library

IMPORT_RECORD = ".photo-room-lrimport.json"
MAX_LISTED = 50

# Lightroom develop key -> (photo-room adjustment key, kind). crs tag == Lightroom key.
SCALARS = {
    "Temperature": ("color_temp", int), "Tint": ("tint", int),
    "Exposure2012": ("exposure", float), "Contrast2012": ("contrast", int),
    "Highlights2012": ("highlights", int), "Shadows2012": ("shadows", int),
    "Whites2012": ("whites", int), "Blacks2012": ("blacks", int),
    "Vibrance": ("vibrance", int), "Saturation": ("saturation", int),
    "Clarity2012": ("clarity", int), "Texture": ("texture", int),
    "Dehaze": ("dehaze", int), "Sharpness": ("sharpness", int),
    "LuminanceSmoothing": ("luminance_nr", int), "CropAngle": ("straighten", float),
}
CROP_KEYS = ("CropLeft", "CropTop", "CropRight", "CropBottom")
# crs tag name -> photo-room key for HSL / grayscale mix / colour grading
COLOR_KEYS = {tag: key for key, tag in colormix.XMP_TAGS.items()}
# Bookkeeping keys that carry no edit; not reported as unsupported.
IGNORED = {"Version", "ProcessVersion", "WhiteBalance", "HasSettings", "CompatibleVersion",
           "CameraProfile", "CameraProfileDigest", "AlreadyApplied", "ToneCurveName2012",
           "ToneCurveName", "CropConstrainToWarp", "CropConstrainAspectRatio", "HasCrop",
           "ConvertToGrayscale", "UprightVersion", "UprightCenterMode", "LensProfileEnable"}
DEFAULT_PV = "11.0"


# ---------------------------------------------------------------------------
# Lua table parsing (the format of Adobe_imageDevelopSettings.text)
# ---------------------------------------------------------------------------

_NUM = re.compile(r"-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_NAME = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_KEYEQ = re.compile(r"([A-Za-z_][A-Za-z_0-9]*)\s*=(?!=)")


class _Lua:
    def __init__(self, text: str):
        self.s, self.i = text, 0

    def ws(self):
        s, n = self.s, len(self.s)
        while self.i < n:
            if s[self.i].isspace():
                self.i += 1
            elif s.startswith("--", self.i):
                j = s.find("\n", self.i)
                self.i = n if j < 0 else j + 1
            else:
                break

    def value(self):
        self.ws()
        s, c = self.s, self.s[self.i]
        if c == "{":
            return self.table()
        if c in "\"'":
            return self.string(c)
        m = _NUM.match(s, self.i)
        if m:
            self.i = m.end()
            t = m.group()
            return int(t) if re.fullmatch(r"-?\d+", t) else float(t)
        m = _NAME.match(s, self.i)
        if not m:
            raise ValueError(f"unexpected {c!r} at {self.i}")
        self.i = m.end()
        return {"true": True, "false": False, "nil": None}.get(m.group(), m.group())

    def string(self, q):
        self.i += 1
        out = []
        s = self.s
        while self.i < len(s) and s[self.i] != q:
            if s[self.i] == "\\" and self.i + 1 < len(s):
                self.i += 1
                out.append({"n": "\n", "t": "\t", "r": "\r"}.get(s[self.i], s[self.i]))
            else:
                out.append(s[self.i])
            self.i += 1
        if self.i >= len(s):
            raise ValueError("unterminated string")
        self.i += 1
        return "".join(out)

    def table(self):
        self.i += 1
        d: dict = {}
        arr: list = []
        s = self.s
        while True:
            self.ws()
            if self.i >= len(s):
                raise ValueError("unterminated table")
            if s[self.i] == "}":
                self.i += 1
                break
            if s[self.i] in ",;":
                self.i += 1
                continue
            key = None
            if s[self.i] == "[":
                self.i += 1
                key = self.value()
                self.ws()
                if s[self.i] != "]":
                    raise ValueError("expected ]")
                self.i += 1
                self.ws()
                if s[self.i] != "=":
                    raise ValueError("expected =")
                self.i += 1
            else:
                m = _KEYEQ.match(s, self.i)
                if m:
                    key = m.group(1)
                    self.i = m.end()
            val = self.value()
            if key is None:
                arr.append(val)
            else:
                d[key] = val
        if arr and not d:
            return arr
        if arr:
            d["_array"] = arr
        return d


def parse_develop_text(text) -> dict:
    """The settings table of one photo ({} if it cannot be read)."""
    if isinstance(text, (bytes, bytearray, memoryview)):
        raw = bytes(text)
        for attempt in (raw, raw[4:]):          # some versions prefix a 4-byte length
            try:
                raw = zlib.decompress(attempt)
                break
            except zlib.error:
                continue
        text = raw.decode("utf-8", "replace")
    text = (text or "").strip()
    if not text:
        return {}
    text = re.sub(r"^\s*[A-Za-z_]\w*\s*=\s*(?=\{)", "", text, count=1)    # "s = {...}"
    try:
        parsed = _Lua(text).value()
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, IndexError, RecursionError):
        pass
    # Damaged table: salvage plain "Key = scalar" pairs.
    out = {}
    for m in re.finditer(r'([A-Za-z_]\w*)\s*=\s*(-?\d+\.?\d*(?:[eE][-+]?\d+)?|true|false|"[^"]*")', text):
        v = m.group(2)
        out[m.group(1)] = (v == "true") if v in ("true", "false") else (
            v.strip('"') if v.startswith('"') else float(v))
    return out


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def map_settings(settings: dict) -> tuple[dict, dict, list[str]]:
    """(crs tags to write, photo-room adjustments, unsupported setting names)."""
    tags: dict[str, str] = {}
    adj: dict = {}
    pv = settings.get("ProcessVersion")
    tags["ProcessVersion"] = pv if isinstance(pv, str) and re.fullmatch(r"\d+\.\d+", pv) else DEFAULT_PV
    handled = set()
    for lr, (key, kind) in SCALARS.items():
        v = settings.get(lr)
        if _is_number(v):
            adj[key] = int(round(v)) if kind is int else float(v)
            tags[lr] = f"{adj[key]:.2f}" if kind is float else str(adj[key])
            handled.add(lr)
    if "color_temp" in adj:
        tags["ColorTemperature"] = tags["Temperature"]     # the tag photo-room reads back
        tags["WhiteBalance"] = "Custom"
    elif isinstance(settings.get("WhiteBalance"), str):
        tags["WhiteBalance"] = settings["WhiteBalance"]
    for tag, key in COLOR_KEYS.items():
        v = settings.get(tag)
        if _is_number(v):
            adj[key] = int(round(v))
            tags[tag] = str(adj[key])
            handled.add(tag)
    if all(_is_number(settings.get(k)) for k in CROP_KEYS):
        tags.update({k: f"{float(settings[k]):.5f}" for k in CROP_KEYS})
        tags["HasCrop"] = "True" if settings.get("HasCrop") else "False"
        if settings.get("HasCrop"):
            adj["crop"] = {k[4:].lower(): float(settings[k]) for k in CROP_KEYS}
        handled.update(CROP_KEYS)
    if settings.get("ConvertToGrayscale") is True:
        adj["monochrome"] = True
        tags["ConvertToGrayscale"] = "True"
    unsupported = []
    for k, v in settings.items():
        if k in handled or k in IGNORED or k.startswith("_"):
            continue
        if v is None or v is False or v == 0 or v == "" or v == [] or v == {}:
            continue
        unsupported.append(k)
    return tags, adj, sorted(unsupported)


# ---------------------------------------------------------------------------
# Reading the catalog
# ---------------------------------------------------------------------------

def find_catalog(target: Path) -> Path:
    target = Path(target).expanduser()
    if target.is_file():
        return target
    if not target.is_dir():
        raise FileNotFoundError(f"No such catalog or folder: {target}")
    found = [p for p in target.rglob("*.lrcat")
             if not any(part.lower().startswith("backup") for part in p.relative_to(target).parts[:-1])]
    if not found:
        raise FileNotFoundError(f"No .lrcat file found under {target}")
    return max(found, key=lambda p: p.stat().st_mtime)


class Catalog:
    """Read-only view of a copy of an .lrcat."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._tmp = tempfile.mkdtemp(prefix="photo-room-lrcat-")
        copy = Path(self._tmp) / "catalog.sqlite"
        shutil.copyfile(self.path, copy)            # the source is only ever read
        self.db = sqlite3.connect(f"file:{copy}?mode=ro", uri=True, timeout=5)
        self.db.row_factory = sqlite3.Row
        self._cols: dict[str, set[str]] = {}
        try:
            self.tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.DatabaseError as exc:
            self.close()
            raise ValueError(f"{self.path.name} is not a readable Lightroom catalog: {exc}") from exc
        if "Adobe_images" not in self.tables:
            self.close()
            raise ValueError(f"{self.path.name} has no Adobe_images table; not a Lightroom Classic catalog")

    def close(self):
        try:
            self.db.close()
        finally:
            shutil.rmtree(self._tmp, ignore_errors=True)

    def cols(self, table: str) -> set[str]:
        if table not in self._cols:
            self._cols[table] = ({r[1] for r in self.db.execute(f'PRAGMA table_info("{table}")')}
                                 if table in self.tables else set())
        return self._cols[table]

    def select(self, table: str, wanted: list[str]) -> list[dict]:
        """Rows with the wanted columns that exist (missing ones read as None)."""
        have = self.cols(table)
        if not have:
            return []
        expr = ", ".join(f'"{c}"' if c in have else f'NULL AS "{c}"' for c in wanted)
        try:
            return [dict(r) for r in self.db.execute(f'SELECT {expr} FROM "{table}"')]
        except sqlite3.DatabaseError:
            return []


@dataclass
class Photo:
    lr_id: int
    path: str                    # absolute path as Lightroom recorded it (forward slashes)
    folder_rel: str              # pathFromRoot + file name
    root_name: str
    rating: int = 0
    pick: int = 0
    label: str = ""
    master: int | None = None
    copy_name: str = ""
    keywords: list[list[str]] = field(default_factory=list)
    settings: dict = field(default_factory=dict)
    rel: str | None = None       # library-relative path of the raw (the master's, for a copy)
    how: str = ""


def _norm(path: str) -> str:
    return (path or "").replace("\\", "/")


def read_catalog(cat: Catalog) -> dict:
    images = cat.select("Adobe_images", ["id_local", "rootFile", "rating", "pick", "colorLabels",
                                         "masterImage", "copyName"])
    files = {r["id_local"]: r for r in cat.select(
        "AgLibraryFile", ["id_local", "baseName", "extension", "folder", "originalFilename"])}
    folders = {r["id_local"]: r for r in cat.select("AgLibraryFolder", ["id_local", "pathFromRoot", "rootFolder"])}
    roots = {r["id_local"]: r for r in cat.select("AgLibraryRootFolder", ["id_local", "absolutePath", "name"])}

    kw = {r["id_local"]: r for r in cat.select("AgLibraryKeyword", ["id_local", "name", "parent"])}

    def kw_path(kid):
        out, seen = [], set()
        while kid in kw and kid not in seen:
            seen.add(kid)
            name = (kw[kid]["name"] or "").strip()
            if name:
                out.append(name)
            kid = kw[kid]["parent"]
        return out[::-1]

    by_image: dict[int, list[list[str]]] = {}
    for r in cat.select("AgLibraryKeywordImage", ["image", "tag"]):
        p = kw_path(r["tag"])
        if p:
            by_image.setdefault(r["image"], []).append(p)

    dev = {r["image"]: r["text"] for r in cat.select("Adobe_imageDevelopSettings", ["image", "text"])
           if r["image"] is not None and r["text"]}

    photos: dict[int, Photo] = {}
    for im in images:
        f = files.get(im["rootFile"])
        if not f:
            continue
        fo = folders.get(f["folder"], {})
        ro = roots.get(fo.get("rootFolder"), {})
        name = f["baseName"] or ""
        if f["extension"]:
            name += "." + f["extension"]
        elif f["originalFilename"]:
            name = f["originalFilename"]
        root_abs = _norm(ro.get("absolutePath") or "")
        if root_abs and not root_abs.endswith("/"):
            root_abs += "/"
        rel = _norm(fo.get("pathFromRoot") or "") + name
        try:
            label = library.normalize_label(im["colorLabels"])
        except ValueError:
            label = ""      # a custom or localised label name
        try:
            rating = max(0, min(5, int(float(im["rating"] or 0))))
        except (TypeError, ValueError):
            rating = 0
        try:
            pick = int(float(im["pick"] or 0))
        except (TypeError, ValueError):
            pick = 0
        master = im["masterImage"]
        photos[im["id_local"]] = Photo(
            lr_id=im["id_local"], path=root_abs + rel, folder_rel=rel, root_name=ro.get("name") or "",
            rating=rating, pick=pick, label=label, master=master if master else None,
            copy_name=im["copyName"] or "", keywords=by_image.get(im["id_local"], []),
            settings=parse_develop_text(dev[im["id_local"]]) if im["id_local"] in dev else {})

    cols = {r["id_local"]: r for r in cat.select("AgLibraryCollection", ["id_local", "name", "parent", "creationId"])}
    members: dict[int, list[int]] = {}
    for r in cat.select("AgLibraryCollectionImage", ["collection", "image"]):
        members.setdefault(r["collection"], []).append(r["image"])
    parents = {c["parent"] for c in cols.values() if c["parent"] is not None}

    def full_name(cid):
        parts, seen = [], set()
        while cid in cols and cid not in seen:
            seen.add(cid)
            parts.append((cols[cid]["name"] or "").strip())
            cid = cols[cid]["parent"]
        return " / ".join(p for p in parts[::-1] if p)

    collections, smart, groups = [], [], 0
    for cid, c in cols.items():
        kind = (c["creationId"] or "").lower()
        if "group" in kind or (cid in parents and cid not in members):
            groups += 1
        elif "smart" in kind:
            smart.append(full_name(cid))
        elif full_name(cid):
            collections.append({"name": full_name(cid), "images": members.get(cid, [])})

    stack_imgs: dict[int, list[tuple[int, int]]] = {}
    for r in cat.select("AgLibraryFolderStackImage", ["stack", "image", "position"]):
        stack_imgs.setdefault(r["stack"], []).append((r["position"] or 0, r["image"]))
    stacks = [[i for _, i in sorted(v)] for v in stack_imgs.values() if len(v) > 1]

    return {"photos": photos, "collections": collections, "smart_collections": smart,
            "collection_sets": groups, "stacks": stacks}


# ---------------------------------------------------------------------------
# Matching and planning
# ---------------------------------------------------------------------------

def parse_remaps(items) -> list[tuple[str, str]]:
    out = []
    for item in items or []:
        if isinstance(item, (tuple, list)):
            out.append((_norm(item[0]).rstrip("/"), _norm(item[1]).rstrip("/")))
            continue
        old, sep, new = str(item).partition("=")
        if not sep or not old:
            raise ValueError(f"--remap needs OLD=NEW, got {item!r}")
        out.append((_norm(old).rstrip("/"), _norm(new).rstrip("/")))
    return out


def resolve(photo: Photo, library_root: Path, remaps) -> tuple[str | None, str]:
    """(library-relative path, how it matched) for one catalog entry."""
    root = Path(library_root).resolve()
    cands: list[tuple[str, str]] = []
    for old, new in remaps:
        if photo.path.lower().startswith(old.lower() + "/"):
            cands.append((new + photo.path[len(old):], "remap"))
    cands.append((photo.path, "path"))
    cands.append((str(root / photo.folder_rel), "library-relative"))
    if photo.root_name:
        cands.append((str(root / photo.root_name / photo.folder_rel), "library-relative"))
    for cand, how in cands:
        p = Path(cand)
        try:
            if p.is_file() and p.resolve().is_relative_to(root):
                return p.resolve().relative_to(root).as_posix(), how
        except OSError:
            continue
    return None, ""


def sidecar_for(path: Path) -> Path:
    return path.with_suffix(".xmp")


def build_plan(cat_path: Path, library_root: Path, remaps=(), force: bool = False) -> dict:
    remaps = parse_remaps(remaps)
    library_root = Path(library_root)
    cat = Catalog(cat_path)
    try:
        data = read_catalog(cat)
    finally:
        cat.close()
    photos: dict[int, Photo] = data["photos"]
    originals = [p for p in photos.values() if p.master not in photos]
    copies = [p for p in photos.values() if p.master in photos]
    unmatched = []
    for p in originals:
        p.rel, p.how = resolve(p, library_root, remaps)
        if p.rel is None:
            unmatched.append(p.path)
    for p in copies:
        p.rel = photos[p.master].rel
        if p.rel is None:
            unmatched.append(f"{photos[p.master].path} (virtual copy {p.copy_name or p.lr_id})")

    matched = [p for p in originals if p.rel]
    kept = sum(1 for p in matched if sidecar_for(library_root / p.rel).exists() and not force)
    unsupported: Counter = Counter()
    for p in matched + [c for c in copies if c.rel]:
        if p.settings:
            unsupported.update(map_settings(p.settings)[2])
    report = {
        "photos_in_catalog": len(originals), "matched": len(matched),
        "unmatched": len(unmatched), "unmatched_files": unmatched[:MAX_LISTED],
        "match_methods": dict(Counter(p.how for p in matched)),
        "virtual_copies": sum(1 for p in copies if p.rel),
        "virtual_copies_unmatched": sum(1 for p in copies if not p.rel),
        "with_develop_settings": sum(1 for p in matched if p.settings),
        "rated": sum(1 for p in matched if p.rating), "picked": sum(1 for p in matched if p.pick == 1),
        "rejected": sum(1 for p in matched if p.pick == -1),
        "labelled": sum(1 for p in matched if p.label),
        "with_keywords": sum(1 for p in matched if p.keywords),
        "collections": len(data["collections"]), "smart_collections_skipped": data["smart_collections"],
        "collection_sets_flattened": data["collection_sets"],
        "stacks": len(data["stacks"]),
        "sidecars_to_write": len(matched) - kept, "sidecars_kept": kept,
        "unsupported_settings": dict(unsupported.most_common()),
    }
    return {"catalog": str(cat_path), "library": str(library_root), "force": force, "remaps": remaps,
            "data": data, "originals": originals, "copies": copies, "report": report}


def format_report(report: dict) -> str:
    r = report
    lines = [
        f"Photos in catalog:        {r['photos_in_catalog']}",
        f"  matched in library:     {r['matched']}  {r['match_methods'] or ''}",
        f"  not found:              {r['unmatched']}",
        f"Virtual copies:           {r['virtual_copies']} ({r['virtual_copies_unmatched']} not found)",
        f"Ratings / picks / rejects / labels: {r['rated']} / {r['picked']} / {r['rejected']} / {r['labelled']}",
        f"Photos with keywords:     {r['with_keywords']}",
        f"Photos with develop settings: {r['with_develop_settings']}",
        f"Collections:              {r['collections']} ({r['collection_sets_flattened']} sets flattened into names)",
        f"Stacks:                   {r['stacks']} (recorded, not applied; photo-room has no stacks)",
        f"Sidecars to write:        {r['sidecars_to_write']}; existing kept: {r['sidecars_kept']}"
        + ("" if r["sidecars_kept"] == 0 else " (use --force to update them)"),
    ]
    if r["smart_collections_skipped"]:
        lines.append("Smart collections not imported: " + ", ".join(r["smart_collections_skipped"][:MAX_LISTED]))
    if r["unsupported_settings"]:
        lines.append("Unsupported develop settings (not converted): "
                     + ", ".join(f"{k} x{v}" for k, v in list(r["unsupported_settings"].items())[:MAX_LISTED]))
    if r["unmatched_files"]:
        lines.append("Files not found (use --remap OLD=NEW if the library moved):")
        lines += [f"  {f}" for f in r["unmatched_files"]]
        if r["unmatched"] > len(r["unmatched_files"]):
            lines.append(f"  ... and {r['unmatched'] - len(r['unmatched_files'])} more")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------

def _meta_args(p: Photo) -> list[str]:
    args = [f"-XMP-xmp:Rating={-1 if p.pick == -1 else p.rating}"]
    if p.label:
        args.append(f"-XMP-xmp:Label={p.label}")
    leaves = library.normalize_keywords([k[-1] for k in p.keywords])
    if leaves:
        args.append("-XMP-dc:Subject=")
        args += [f"-XMP-dc:Subject+={k}" for k in leaves]
        paths = dict.fromkeys("|".join(s.replace("|", "/") for s in k) for k in p.keywords)
        args.append("-XMP-lr:HierarchicalSubject=")
        args += [f"-XMP-lr:HierarchicalSubject+={h}" for h in paths]
    return args


def _write_sidecar(path: Path, p: Photo, force: bool) -> str:
    target = sidecar_for(path)
    tags = map_settings(p.settings)[0] if p.settings else {}
    args = _meta_args(p) + [f"-XMP-crs:{k}={v}" for k, v in tags.items()]
    with fsutil.lock_for(target):
        if target.exists():
            if not force:
                return "kept"
            res = exifpool.run([*args, "-overwrite_original"], timeout=exifpool.DEFAULT_TIMEOUT,
                               files=[str(target)])
        else:
            res = exifpool.run([*args, "-o", str(target)], timeout=exifpool.DEFAULT_TIMEOUT,
                               files=[os.devnull])
    if not target.exists():
        raise RuntimeError(f"could not write {target.name}: {res.stderr.strip()}")
    return "written"


def apply_plan(plan: dict) -> dict:
    root = Path(plan["library"])
    data = plan["data"]
    stats: Counter = Counter()
    errors: list[str] = []
    rel_of: dict[int, str] = {}

    picks: dict[str, bool] = {}
    pre_reject: dict[str, int] = {}
    for p in plan["originals"]:
        if not p.rel:
            continue
        rel_of[p.lr_id] = p.rel
        try:
            state = _write_sidecar(root / p.rel, p, plan["force"])
            stats["sidecars_" + state] += 1
        except Exception as exc:            # one bad file must not stop the import
            stats["errors"] += 1
            errors.append(f"{p.rel}: {exc}")
        if p.pick == 1:
            picks[p.rel] = True
        elif p.pick == -1 and p.rating:
            pre_reject[p.rel] = p.rating
    if picks:
        library.apply_picks(root, picks)
    if pre_reject:
        library.remember_pre_reject(root, pre_reject)
    stats["picks"] = len(picks)

    existing = {v["lr_key"]: v["id"] for v in catalog.load(root)["virtual"].values() if v.get("lr_key")}
    stem = Path(plan["catalog"]).stem
    for p in plan["copies"]:
        if not p.rel:
            continue
        key = f"{stem}:{p.lr_id}"
        if key in existing:
            rel_of[p.lr_id] = existing[key]
            stats["virtual_copies_existing"] += 1
            continue
        try:
            adj = map_settings(p.settings)[1] if p.settings else {}
            entry = catalog.create_virtual(
                root, p.rel, adjustments=adj, rating=0 if p.pick == -1 else p.rating, label=p.label,
                keywords=[k[-1] for k in p.keywords], flag="pick" if p.pick == 1 else "none")
            catalog.tag_virtual(root, entry["id"], lr_key=key, lr_copy_name=p.copy_name)
            rel_of[p.lr_id] = entry["id"]
            stats["virtual_copies"] += 1
        except Exception as exc:
            stats["errors"] += 1
            errors.append(f"virtual copy {p.copy_name or p.lr_id} of {p.rel}: {exc}")

    # A re-run adds to the collection of the same name instead of failing on it.
    for c in data["collections"]:
        ids = list(dict.fromkeys(rel_of[i] for i in c["images"] if i in rel_of))
        if not ids:
            stats["collections_empty_skipped"] += 1
            continue
        try:
            name = " ".join(c["name"].split())[:catalog.MAX_NAME]
            current = {x["name"]: x for x in catalog.list_collections(root)}
            if name in current and not current[name].get("smart"):
                catalog.add_photos(root, current[name]["id"], ids)
            else:
                catalog.create_collection(root, name, photos=ids)
            stats["collections"] += 1
        except Exception as exc:
            stats["errors"] += 1
            errors.append(f"collection {c['name']}: {exc}")

    stacks = [[rel_of[i] for i in s if i in rel_of] for s in data["stacks"]]
    stacks = [s for s in stacks if len(s) > 1]
    fsutil.atomic_write_text(root / IMPORT_RECORD, json.dumps({
        "catalog": Path(plan["catalog"]).name, "stacks": stacks,
        "smart_collections_skipped": data["smart_collections"],
        "unsupported_settings": plan["report"]["unsupported_settings"]}, indent=1))
    stats["stacks_recorded"] = len(stacks)
    out = dict(stats)
    out["errors_list"] = errors[:MAX_LISTED]
    return out


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(prog="photo-room import-lightroom",
                                 description="Import ratings, flags, labels, keywords, collections, virtual "
                                             "copies and develop settings from a Lightroom Classic catalog.")
    ap.add_argument("catalog", help="a .lrcat file, or a folder containing one")
    ap.add_argument("--library", required=True, help="the photo-room library folder")
    ap.add_argument("--remap", action="append", default=[], metavar="OLD=NEW",
                    help="rewrite a path prefix recorded in the catalog (repeatable)")
    ap.add_argument("--force", action="store_true", help="update existing .xmp sidecars instead of keeping them")
    ap.add_argument("--dry-run", action="store_true", help="print the report and write nothing")
    ap.add_argument("--yes", "-y", action="store_true", help="write without asking after the report")
    args = ap.parse_args(argv)
    try:
        cat_path = find_catalog(Path(args.catalog))
        lib_root = Path(args.library).expanduser()
        if not lib_root.is_dir():
            raise FileNotFoundError(f"Library folder not found: {lib_root}")
        plan = build_plan(cat_path, lib_root, args.remap, args.force)
    except (OSError, ValueError) as exc:
        print(f"import-lightroom: {exc}", file=sys.stderr)
        return 2
    print(f"Catalog: {cat_path}\nLibrary: {lib_root}\n")
    print(format_report(plan["report"]))
    if args.dry_run:
        print("\nDry run: nothing was written.")
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            print("\nNothing written. Re-run with --yes to apply (or --dry-run to just preview).")
            return 1
        if input("\nWrite these changes? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing written.")
            return 1
    done = apply_plan(plan)
    print("\nDone: " + ", ".join(f"{k} {v}" for k, v in done.items() if k != "errors_list"))
    for e in done["errors_list"]:
        print("  " + e)
    return 1 if done.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
