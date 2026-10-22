#!/usr/bin/env python3
"""
Library module: pick flags, colour labels, keywords, filtering and sorting.

Where each piece of metadata lives, so Lightroom sees what this tool writes:

* rating        XMP-xmp:Rating   (-1 = rejected; handled by editor.write_rating)
* colour label  XMP-xmp:Label    ("Red", "Yellow", "Green", "Blue", "Purple")
* keywords      XMP-dc:Subject   (a list, which Lightroom reads as keywords)
* pick flag     a small JSON store in the library root. Camera Raw has no
                standard field for it (Lightroom keeps flags in its catalogue),
                so it cannot be written into the file.

"Rejected" is the existing rating of -1, so the reject flag and the X key are
the same state everywhere.

Grid performance: nothing here decodes a raw. Metadata comes from one
batched ``exiftool -fast2 -j`` pass over files whose (size, mtime) changed
since the last query, and is cached in memory, so repeated queries on a large
library cost no subprocess at all. The API paginates the result and thumbnails
are fetched lazily by the browser.
"""

from __future__ import annotations

import fsutil
import json
import os
import subprocess
import tempfile
import threading
from pathlib import Path

EXIFTOOL_TIMEOUT = 120
CHUNK = 400

LABELS = ["Red", "Yellow", "Green", "Blue", "Purple"]
FLAGS = ("pick", "reject", "none")
SORT_KEYS = ("name", "date", "rating", "label", "flag", "camera", "lens", "iso")
STORE_NAME = ".photo-room-library.json"

_lock = threading.RLock()

# What the Library lists besides raws. Raws are editable; JPEG/HEIC are listed,
# thumbnailed and rated but view-only (the Develop pipeline starts from a raw
# decode); video is a placeholder cell with no thumbnail.
RAW_SUFFIXES = (".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2", ".dng")
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".heic", ".heif")
VIDEO_SUFFIXES = (".mp4", ".mov", ".mts", ".m2ts", ".avi", ".mxf")


def kind_of(name: str) -> str:
    """'raw', 'image', 'video', or '' for a file the Library ignores."""
    ext = os.path.splitext(name)[1].lower()
    if ext in RAW_SUFFIXES:
        return "raw"
    if ext in IMAGE_SUFFIXES:
        return "image"
    if ext in VIDEO_SUFFIXES:
        return "video"
    return ""


# ---------------------------------------------------------------------------
# Normalisation and XMP field mapping
# ---------------------------------------------------------------------------

def normalize_label(label) -> str:
    """Canonical label name, or '' for none. Raises ValueError if unknown."""
    if label is None or str(label).strip().lower() in ("", "none"):
        return ""
    wanted = str(label).strip().lower()
    for name in LABELS:
        if name.lower() == wanted:
            return name
    raise ValueError(f"Unknown colour label: {label!r}")


def normalize_keywords(keywords) -> list[str]:
    """Trim, drop empties and case-insensitive duplicates, keep first spelling."""
    if isinstance(keywords, str):
        keywords = keywords.split(",")
    seen: set[str] = set()
    out: list[str] = []
    for kw in keywords or []:
        text = " ".join(str(kw).split())
        if not text or any(ord(c) < 32 for c in text):
            continue
        if text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append(text)
    return out


def xmp_args(label=None, keywords=None) -> list[str]:
    """
    exiftool arguments writing the Camera Raw / Dublin Core fields.

    ``label=None`` / ``keywords=None`` leave that field alone; an empty label
    or list clears it. The list is cleared before being refilled so removed
    keywords really disappear.
    """
    args: list[str] = []
    if label is not None:
        args.append(f"-XMP-xmp:Label={normalize_label(label)}")
    if keywords is not None:
        args.append("-XMP-dc:Subject=")
        args.extend(f"-XMP-dc:Subject+={kw}" for kw in normalize_keywords(keywords))
    return args


def parse_entry(entry: dict) -> dict:
    """Map one exiftool -j record onto the fields the library uses."""
    subject = entry.get("Subject", [])
    if not isinstance(subject, list):
        subject = [subject]
    try:
        rating = max(-1, min(5, int(float(entry.get("Rating", 0)))))
    except (TypeError, ValueError):
        rating = 0
    try:
        label = normalize_label(entry.get("Label"))
    except ValueError:
        label = ""    # a custom label set from another app: shown as none
    date = str(entry.get("DateTimeOriginal") or entry.get("CreateDate") or "")
    return {
        "rating": rating,
        "label": label,
        "keywords": normalize_keywords([str(s) for s in subject]),
        "camera": " ".join(str(entry.get("Model") or "").split()),
        "date": date.replace(":", "-", 2) if date[:4].isdigit() and date[4:5] == ":" else date,
    }


def _run_exiftool(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["exiftool"] + args, capture_output=True, text=True, timeout=EXIFTOOL_TIMEOUT
    )


def meta_target(path: Path) -> Path:
    """The file holding a photo's label / keywords / rating (its .xmp sidecar; a DNG only on opt-in)."""
    import process
    return process.edit_target(path)


def write_xmp(path: Path, label=None, keywords=None) -> None:
    """
    Write label / keywords where Lightroom reads them. A raw is never opened for
    writing (exiftool would rewrite the CR2 in place); its sidecar is created
    or updated instead, like ratings and develop settings.
    """
    args = xmp_args(label, keywords)
    if not args:
        return
    write_target_args(path, args)


def write_target_args(path: Path, args: list[str]) -> None:
    """Apply exiftool tag assignments to a photo's metadata target (sidecar, created if missing)."""
    target = meta_target(path)
    with fsutil.lock_for(target):
        if target != path and not target.exists():
            result = _run_exiftool(args + ["-o", str(target), os.devnull])
            # Seeding from an empty source warns "no writable tags set" yet succeeds.
            if not target.exists():
                raise RuntimeError(f"exiftool failed: {result.stderr.strip()}")
            return
        result = _run_exiftool(args + ["-overwrite_original", str(target)])
        if result.returncode != 0:
            raise RuntimeError(f"exiftool failed: {result.stderr.strip()}")


# ---------------------------------------------------------------------------
# Flag store (persistent, one JSON file in the library root)
# ---------------------------------------------------------------------------

def _store_path(root: Path) -> Path:
    return Path(root) / STORE_NAME


def load_store(root: Path) -> dict:
    try:
        data = json.loads(_store_path(root).read_text())
        flags = data.get("flags", {})
        pre = data.get("pre_reject", {})
        return {"flags": flags if isinstance(flags, dict) else {},
                "pre_reject": pre if isinstance(pre, dict) else {}}
    except (OSError, ValueError, AttributeError):
        return {"flags": {}, "pre_reject": {}}


def _save_store(root: Path, store: dict) -> None:
    fsutil.atomic_write_text(_store_path(root), json.dumps(store, indent=1, sort_keys=True))


def apply_picks(root: Path, picks: dict[str, bool]) -> None:
    """Set the pick flag of many files in one read-modify-write of the store."""
    with _lock:
        store = load_store(root)
        for rel, picked in picks.items():
            if picked:
                store["flags"][rel] = "pick"
            else:
                store["flags"].pop(rel, None)
        _save_store(root, store)


def get_pick(root: Path, rel: str) -> bool:
    return load_store(root)["flags"].get(rel) == "pick"


def remember_pre_reject(root: Path, ratings: dict[str, int]) -> None:
    """Keep each photo's star rating from before it was rejected (-1 erases it
    in XMP), so un-rejecting can put it back."""
    if not ratings:
        return
    with _lock:
        store = load_store(root)
        for rel, stars in ratings.items():
            if stars and int(stars) > 0:
                store["pre_reject"][rel] = int(stars)
            else:
                store["pre_reject"].pop(rel, None)
        _save_store(root, store)


def take_pre_reject(root: Path, rels: list[str]) -> dict[str, int]:
    """The stars remembered by ``remember_pre_reject`` (0 if none), forgotten."""
    with _lock:
        store = load_store(root)
        out = {}
        for rel in rels:
            try:
                out[rel] = max(0, min(5, int(store["pre_reject"].pop(rel, 0))))
            except (TypeError, ValueError):
                out[rel] = 0
        _save_store(root, store)
        return out


def set_picks(root: Path, rels: list[str], picked: bool) -> None:
    """Set or clear the pick flag on many files with a single store write."""
    with _lock:
        store = load_store(root)
        for rel in rels:
            if picked:
                store["flags"][rel] = "pick"
            else:
                store["flags"].pop(rel, None)
        _save_store(root, store)


# ---------------------------------------------------------------------------
# Metadata index (cached; invalidated by file size/mtime)
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[tuple[int, ...], dict]] = {}


def _sig(path: Path) -> tuple[int, ...]:
    """(size, mtime) of the photo and of its sidecar (zeros when it has none)."""
    st = path.stat()
    side = meta_target(path)
    try:
        ss = side.stat() if side != path else None
    except OSError:
        ss = None
    return (st.st_size, st.st_mtime_ns, ss.st_size if ss else 0, ss.st_mtime_ns if ss else 0)


def clear_cache() -> None:
    _cache.clear()


def read_metadata(paths: list[Path]) -> dict[str, dict]:
    """Parsed metadata for many files, in one exiftool call per CHUNK stale ones."""
    stale: list[Path] = []
    with _lock:
        for p in paths:
            try:
                sig = _sig(p)
            except OSError:
                continue
            hit = _cache.get(str(p))
            if hit is None or hit[0] != sig:
                stale.append(p)

    for i in range(0, len(stale), CHUNK):
        batch = stale[i:i + CHUNK]
        result = subprocess.run(
            ["exiftool", "-fast2", "-j", "-d", "%Y-%m-%d %H:%M:%S",
             "-XMP-xmp:Rating", "-XMP-xmp:Label", "-XMP-dc:Subject",
             "-Model", "-DateTimeOriginal", "-CreateDate", "-@", "-"],
            input="\n".join(str(p) for p in batch),
            capture_output=True, text=True,
            timeout=max(EXIFTOOL_TIMEOUT, len(batch) // 10),
        )
        try:
            entries = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            entries = []
        by_resolved = {str(p.resolve()): p for p in batch}
        seen: set[str] = set()
        for entry in entries:
            source = entry.get("SourceFile")
            if not source:
                continue
            p = by_resolved.get(str(Path(source).resolve()))
            if p is None:
                continue
            seen.add(str(p))
            with _lock:
                _cache[str(p)] = (_sig(p), parse_entry(entry))
        for p in batch:       # exiftool failed on it: show it, with no metadata
            if str(p) not in seen:
                with _lock:
                    _cache[str(p)] = (_sig(p), parse_entry({}))
        _overlay_sidecars(batch)

    with _lock:
        return {str(p): dict(_cache[str(p)][1]) for p in paths if str(p) in _cache}


def _overlay_sidecars(batch: list[Path]) -> None:
    """Rating, label and keywords live in the sidecar of a raw; they win over the raw's own."""
    sides = {str(meta_target(p).resolve()): p for p in batch
             if meta_target(p) != p and meta_target(p).exists()}
    if not sides:
        return
    result = subprocess.run(
        ["exiftool", "-fast2", "-j", "-XMP-xmp:Rating", "-XMP-xmp:Label", "-XMP-dc:Subject",
         "-@", "-"],
        input="\n".join(sides), capture_output=True, text=True,
        timeout=max(EXIFTOOL_TIMEOUT, len(sides) // 10))
    try:
        entries = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return
    for entry in entries:
        p = sides.get(str(Path(entry.get("SourceFile", "")).resolve()))
        if p is None:
            continue
        side = parse_entry(entry)
        with _lock:
            sig, meta = _cache[str(p)]
            meta = dict(meta)
            if p.suffix.lower() == ".dng":
                # A DNG keeps its own metadata; the sidecar only adds what it has.
                for key, tag in (("rating", "Rating"), ("label", "Label"),
                                 ("keywords", "Subject")):
                    if tag in entry:
                        meta[key] = side[key]
            else:
                meta.update(rating=side["rating"], label=side["label"], keywords=side["keywords"])
            _cache[str(p)] = (sig, meta)


def update_cached(path: Path, **fields) -> None:
    """After our own write, patch the cache so the next query needs no re-read."""
    with _lock:
        hit = _cache.get(str(path))
        if hit is None:
            return
        meta = dict(hit[1])
        meta.update(fields)
        try:
            _cache[str(path)] = (_sig(path), meta)
        except OSError:
            _cache.pop(str(path), None)


def build_items(root: Path, paths: list[Path]) -> list[dict]:
    """One dict per photo: path (relative), name and every filterable field."""
    root = Path(root).resolve()
    meta = read_metadata(paths)
    flags = load_store(root)["flags"]
    items = []
    for p in paths:
        m = meta.get(str(p))
        if m is None:
            continue
        rel = p.relative_to(root).as_posix() if root in p.resolve().parents else str(p)
        flag = "reject" if m["rating"] == -1 else ("pick" if flags.get(rel) == "pick" else "none")
        kind = kind_of(p.name) or "raw"
        items.append({"path": rel, "name": p.name, **m, "flag": flag,
                      "kind": kind, "editable": kind == "raw"})
    return items


# ---------------------------------------------------------------------------
# Filtering and sorting (pure)
# ---------------------------------------------------------------------------

FILTER_KEYS = ("rating_min", "rating_op", "flag", "label", "keyword", "camera", "lens", "iso_min",
               "iso_max", "focal_min", "focal_max", "filetype", "edit", "gps", "date_from", "date_to", "q")


def normalize_filetype(value) -> str:
    """'.CR2' / 'cr2' -> 'cr2'."""
    value = str(value or "").strip().lower().lstrip(".")
    if not value or not value.isalnum():
        raise ValueError("A file type is letters and digits, like cr2 or dng")
    return value


def item_filetype(it: dict) -> str:
    """Lower-case extension of the photo (a virtual copy has its source's)."""
    return os.path.splitext(it.get("src_path") or it["path"])[1].lstrip(".").lower()


def filter_items(items: list[dict], f: dict) -> list[dict]:
    """
    Keep items matching every given criterion.

    rating_min + rating_op ("ge" default, "eq", "le"); flag (pick / reject /
    unflagged); label (a colour or "none"); keyword (exact, case-insensitive);
    camera / lens (exact, case-insensitive); iso_min / iso_max; date_from / date_to (YYYY-MM-DD,
    inclusive); focal_min / focal_max (mm); filetype (extension, e.g. cr2);
    edit ("edited" / "unedited"); gps ("yes" / "no"); q (every word must appear in the name, a keyword or the camera).
    """
    rating = f.get("rating_min")
    rating = None if rating in (None, "") else int(rating)
    op = f.get("rating_op") or "ge"
    flag = f.get("flag") or ""
    label = f.get("label") or ""
    label = "none" if label.lower() == "none" else (normalize_label(label) if label else "")
    keyword = (f.get("keyword") or "").strip().lower()
    camera = (f.get("camera") or "").strip().lower()
    lens = (f.get("lens") or "").strip().lower()
    iso_min = None if f.get("iso_min") in (None, "") else int(f["iso_min"])
    iso_max = None if f.get("iso_max") in (None, "") else int(f["iso_max"])
    focal_min = None if f.get("focal_min") in (None, "") else float(f["focal_min"])
    focal_max = None if f.get("focal_max") in (None, "") else float(f["focal_max"])
    filetype = normalize_filetype(f["filetype"]) if f.get("filetype") else ""
    edit = (f.get("edit") or "").lower()
    if edit not in ("", "edited", "unedited"):
        raise ValueError("edit must be edited or unedited")
    gps = (f.get("gps") or "").lower()
    if gps not in ("", "yes", "no"):
        raise ValueError("gps must be yes or no")
    d_from = (f.get("date_from") or "")[:10]
    d_to = (f.get("date_to") or "")[:10]
    words = (f.get("q") or "").lower().split()

    out = []
    for it in items:
        if rating is not None:
            r = it["rating"]
            if op == "eq" and r != rating:
                continue
            if op == "le" and r > rating:
                continue
            if op == "ge" and r < rating:
                continue
        if flag == "unflagged":
            if it["flag"] != "none":
                continue
        elif flag and it["flag"] != flag:
            continue
        if label == "none":
            if it["label"]:
                continue
        elif label and it["label"] != label:
            continue
        if keyword and keyword not in (k.lower() for k in it["keywords"]):
            continue
        if camera and it["camera"].lower() != camera:
            continue
        if lens and (it.get("lens") or "").lower() != lens:
            continue
        if iso_min is not None or iso_max is not None:
            iso = it.get("iso") or 0
            if not iso or (iso_min is not None and iso < iso_min) or (iso_max is not None and iso > iso_max):
                continue
        if focal_min is not None or focal_max is not None:
            fl = it.get("focal") or 0
            if not fl or (focal_min is not None and fl < focal_min) or (focal_max is not None and fl > focal_max):
                continue
        if filetype and item_filetype(it) != filetype:
            continue
        if edit and bool(it.get("edited")) != (edit == "edited"):
            continue
        if gps and bool(it.get("gps")) != (gps == "yes"):
            continue
        if d_from or d_to:
            day = it["date"][:10]
            if not day or (d_from and day < d_from) or (d_to and day > d_to):
                continue
        if words:
            hay = " ".join([it["name"], it["camera"], it.get("lens") or "", *it["keywords"]]).lower()
            if not all(w in hay for w in words):
                continue
        out.append(it)
    return out


def sort_items(items: list[dict], key: str = "name", descending: bool = False) -> list[dict]:
    """
    Sort by name, date, rating, label, flag or camera; ties break on path.

    Items missing the sort field (no date, no label, no camera) always come
    last, whichever direction is chosen.
    """
    if key not in SORT_KEYS:
        raise ValueError(f"Unknown sort key: {key!r}")
    flag_rank = {"pick": 2, "none": 1, "reject": 0}
    label_rank = {name: i + 1 for i, name in enumerate(LABELS)}

    def value(it):
        if key == "name":
            return it["name"].lower()
        if key == "date":
            return it["date"] or None
        if key == "rating":
            return it["rating"]
        if key == "label":
            return label_rank.get(it["label"])
        if key == "flag":
            return flag_rank[it["flag"]]
        if key == "lens":
            return (it.get("lens") or "").lower() or None
        if key == "iso":
            return it.get("iso") or None
        return it["camera"].lower() or None

    present = [it for it in items if value(it) is not None]
    missing = [it for it in items if value(it) is None]
    present.sort(key=lambda it: it["path"])
    present.sort(key=value, reverse=descending)
    missing.sort(key=lambda it: it["path"])
    return present + missing


def facets(items: list[dict]) -> dict:
    """Values for the filter bar's dropdowns, from the whole library."""
    cams: dict[str, int] = {}
    lenses: set[str] = set()
    types: set[str] = set()
    kws: dict[str, list] = {}
    labels = {name: 0 for name in LABELS}
    for it in items:
        if it["camera"]:
            cams[it["camera"]] = cams.get(it["camera"], 0) + 1
        if it.get("lens"):
            lenses.add(it["lens"])
        types.add(item_filetype(it))
        if it["label"]:
            labels[it["label"]] += 1
        for k in it["keywords"]:
            entry = kws.setdefault(k.lower(), [k, 0])
            entry[1] += 1
    return {
        "cameras": sorted(cams),
        "lenses": sorted(lenses),
        "filetypes": sorted(t for t in types if t),
        "keywords": [{"keyword": v[0], "count": v[1]} for _, v in sorted(kws.items())],
        "labels": labels,
        "labels_order": LABELS,
    }
