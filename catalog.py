#!/usr/bin/env python3
"""
Collections, smart collections and virtual copies.

Everything lives in one JSON store in the library root
(``.photo-room-catalog.json``), next to the flag store. Nothing here touches a
raw file.

* Photo ids. A real photo is identified by its path relative to the library
  root; a virtual copy by ``<source path>#vc-<hex>``. Collections hold ids, so
  a collection can mix originals and copies.
* Manual collections hold an explicit list of ids. Smart collections hold
  rules (rating, flag, colour label, keyword, camera, date) combined with
  "all" or "any" and are evaluated against the library each time they are
  shown, so they never go stale.
* Stacks group photos (a burst, near-duplicates) so the grid can show one cell
  per stack. A stack is a list of ids with a "top" (the cell shown when the
  stack is collapsed) and a collapsed flag. They live under ``"stacks"`` in the
  same store and never touch a raw or a sidecar (Lightroom keeps its stacks in
  its own catalog, not in XMP, so there is no tag to write).
* A virtual copy is a second, independent edit of one raw. It stores its own
  adjustments, rating, colour label, keywords and pick flag, and no pixels:
  rendering and export read the source raw and apply the copy's settings.
  Deleting a copy removes only its store entry and history sidecar.
"""

from __future__ import annotations

import fsutil
import json
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

import library

STORE_NAME = ".photo-room-catalog.json"
MAX_NAME = 80
MAX_RULES = 20
VC_MARK = "#vc-"

RULE_OPS = {
    "rating": ("ge", "le", "eq"),
    "flag": ("is",),
    "label": ("is",),
    "keyword": ("has", "not_has"),
    "camera": ("is", "contains"),
    "date": ("on", "after", "before"),
    "lens": ("is", "contains"),
    "iso": ("ge", "le", "eq"),
    "focal": ("ge", "le", "eq"),
    "edited": ("is",),
    "filetype": ("is",),
}
MATCH_MODES = ("all", "any")

_lock = threading.RLock()
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def _store_path(root: Path) -> Path:
    return Path(root) / STORE_NAME


def _empty() -> dict:
    return {"collections": [], "virtual": {}, "stacks": {}}


def load(root: Path) -> dict:
    try:
        data = json.loads(_store_path(root).read_text())
    except (OSError, ValueError):
        return _empty()
    out = _empty()
    if isinstance(data, dict):
        if isinstance(data.get("collections"), list):
            out["collections"] = [c for c in data["collections"] if isinstance(c, dict) and c.get("id")]
        if isinstance(data.get("virtual"), dict):
            out["virtual"] = {k: v for k, v in data["virtual"].items() if isinstance(v, dict)}
        if isinstance(data.get("stacks"), dict):
            out["stacks"] = {k: v for k, v in data["stacks"].items()
                             if isinstance(v, dict) and isinstance(v.get("photos"), list)}
    return out


def _save(root: Path, store: dict) -> None:
    fsutil.atomic_write_text(_store_path(root), json.dumps(store, indent=1, sort_keys=True))


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# Smart-collection rules
# ---------------------------------------------------------------------------

def normalize_rule(rule) -> dict:
    """Validate one rule and return it in canonical form; raises ValueError."""
    if not isinstance(rule, dict):
        raise ValueError("A rule must be an object")
    field = str(rule.get("field", "")).lower()
    if field not in RULE_OPS:
        raise ValueError(f"Unknown rule field {field!r}; use one of {', '.join(RULE_OPS)}")
    op = str(rule.get("op") or RULE_OPS[field][0]).lower()
    if op not in RULE_OPS[field]:
        raise ValueError(f"Rule on {field} does not support {op!r}")
    value = rule.get("value")
    if field == "rating":
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ValueError("Rating rules need a whole number") from None
        if not -1 <= value <= 5:
            raise ValueError("Rating must be between -1 and 5")
    elif field == "flag":
        value = str(value or "").lower()
        if value not in ("pick", "reject", "none", "unflagged"):
            raise ValueError("Flag must be pick, reject or unflagged")
        value = "none" if value == "unflagged" else value
    elif field == "label":
        value = "none" if str(value or "").lower() in ("", "none") else library.normalize_label(value)
    elif field in ("iso", "focal"):
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{field} rules need a number") from None
        if not 0 <= value <= 1_000_000:
            raise ValueError(f"{field} is out of range")
        value = int(value) if value == int(value) else value
    elif field == "edited":
        value = str(value or "").lower()
        if value not in ("edited", "unedited"):
            raise ValueError("Edit state must be edited or unedited")
    elif field == "filetype":
        value = library.normalize_filetype(value)
    elif field in ("keyword", "camera", "lens"):
        value = " ".join(str(value or "").split())
        if not value:
            raise ValueError(f"A {field} rule needs a value")
    else:  # date
        value = str(value or "")[:10]
        if not _DATE.match(value):
            raise ValueError("Date rules need YYYY-MM-DD")
    return {"field": field, "op": op, "value": value}


def normalize_rules(rules) -> list[dict]:
    if not isinstance(rules, list) or not rules:
        raise ValueError("A smart collection needs at least one rule")
    if len(rules) > MAX_RULES:
        raise ValueError(f"At most {MAX_RULES} rules")
    return [normalize_rule(r) for r in rules]


def rule_matches(item: dict, rule: dict) -> bool:
    """Whether one library item (see library.build_items) satisfies one rule."""
    field, op, value = rule["field"], rule["op"], rule["value"]
    if field == "rating":
        r = item["rating"]
        return r >= value if op == "ge" else r <= value if op == "le" else r == value
    if field == "flag":
        return item["flag"] == value
    if field == "label":
        return (not item["label"]) if value == "none" else item["label"] == value
    if field == "keyword":
        has = value.lower() in (k.lower() for k in item["keywords"])
        return has if op == "has" else not has
    if field in ("camera", "lens"):
        cam = (item.get(field) or "").lower()
        return cam == value.lower() if op == "is" else value.lower() in cam
    if field in ("iso", "focal"):
        n = item.get(field) or 0
        if not n:
            return False
        return n >= value if op == "ge" else n <= value if op == "le" else n == value
    if field == "edited":
        return bool(item.get("edited")) == (value == "edited")
    if field == "filetype":
        return library.item_filetype(item) == value
    day = (item.get("date") or "")[:10]
    if not day:
        return False
    return day == value if op == "on" else day > value if op == "after" else day < value


def smart_matches(items: list[dict], rules: list[dict], match: str = "all") -> list[dict]:
    combine = any if match == "any" else all
    return [it for it in items if combine(rule_matches(it, r) for r in rules)]


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------

def _clean_name(name) -> str:
    name = " ".join(str(name or "").split())
    if not name:
        raise ValueError("A collection needs a name")
    return name[:MAX_NAME]


def _check_unique(store: dict, name: str, ignore: str | None = None) -> None:
    for c in store["collections"]:
        if c["id"] != ignore and c["name"].lower() == name.lower():
            raise ValueError(f"A collection named {name!r} already exists")


def list_collections(root: Path) -> list[dict]:
    return [dict(c) for c in load(root)["collections"]]


def get_collection(root: Path, cid: str) -> dict:
    for c in load(root)["collections"]:
        if c["id"] == cid:
            return c
    raise KeyError(f"No such collection: {cid}")


def create_collection(root: Path, name, smart: bool = False, rules=None,
                      match: str = "all", photos=None) -> dict:
    name = _clean_name(name)
    col: dict = {"id": _new_id(), "name": name, "smart": bool(smart), "created": time.time()}
    if smart:
        if match not in MATCH_MODES:
            raise ValueError("match must be 'all' or 'any'")
        col["rules"] = normalize_rules(rules)
        col["match"] = match
    else:
        col["photos"] = _dedupe(photos or [])
    with _lock:
        store = load(root)
        _check_unique(store, name)
        store["collections"].append(col)
        _save(root, store)
    return col


def _find(store: dict, cid: str) -> dict:
    for c in store["collections"]:
        if c["id"] == cid:
            return c
    raise KeyError(f"No such collection: {cid}")


def rename_collection(root: Path, cid: str, name) -> dict:
    name = _clean_name(name)
    with _lock:
        store = load(root)
        col = _find(store, cid)
        _check_unique(store, name, ignore=cid)
        col["name"] = name
        _save(root, store)
        return col


def update_rules(root: Path, cid: str, rules, match: str | None = None) -> dict:
    with _lock:
        store = load(root)
        col = _find(store, cid)
        if not col.get("smart"):
            raise ValueError("Only smart collections have rules")
        col["rules"] = normalize_rules(rules)
        if match is not None:
            if match not in MATCH_MODES:
                raise ValueError("match must be 'all' or 'any'")
            col["match"] = match
        _save(root, store)
        return col


def delete_collection(root: Path, cid: str) -> None:
    """Remove the collection only; the photos in it are untouched."""
    with _lock:
        store = load(root)
        _find(store, cid)
        store["collections"] = [c for c in store["collections"] if c["id"] != cid]
        _save(root, store)


def _dedupe(ids) -> list[str]:
    seen, out = set(), []
    for i in ids:
        if isinstance(i, str) and i and i not in seen:
            seen.add(i)
            out.append(i)
    return out


def add_photos(root: Path, cid: str, ids) -> dict:
    with _lock:
        store = load(root)
        col = _find(store, cid)
        if col.get("smart"):
            raise ValueError("Smart collections fill themselves from their rules")
        col["photos"] = _dedupe(list(col.get("photos", [])) + list(ids))
        _save(root, store)
        return col


def remove_photos(root: Path, cid: str, ids) -> dict:
    drop = set(ids)
    with _lock:
        store = load(root)
        col = _find(store, cid)
        if col.get("smart"):
            raise ValueError("Smart collections fill themselves from their rules")
        col["photos"] = [p for p in col.get("photos", []) if p not in drop]
        _save(root, store)
        return col


def collection_items(col: dict, items: list[dict]) -> list[dict]:
    """The members of ``col`` among ``items`` (all library items, copies included)."""
    if col.get("smart"):
        return smart_matches(items, col["rules"], col.get("match", "all"))
    members = set(col.get("photos", []))
    return [it for it in items if it["path"] in members]


def collection_counts(root: Path, items: list[dict]) -> dict[str, int]:
    return {c["id"]: len(collection_items(c, items)) for c in load(root)["collections"]}


# ---------------------------------------------------------------------------
# Virtual copies
# ---------------------------------------------------------------------------

def is_virtual_id(photo_id: str) -> bool:
    return isinstance(photo_id, str) and VC_MARK in photo_id


def split_id(photo_id: str) -> tuple[str, str | None]:
    """(source path, copy id or None)."""
    if is_virtual_id(photo_id):
        src, _, vid = photo_id.rpartition(VC_MARK)
        return src, "vc-" + vid
    return photo_id, None


def virtual_id(source_rel: str, vid: str) -> str:
    return f"{source_rel}#{vid}"


def get_virtual(root: Path, photo_id: str) -> dict:
    entry = load(root)["virtual"].get(photo_id)
    if entry is None:
        raise KeyError(f"No such virtual copy: {photo_id}")
    return entry


def create_virtual(root: Path, source_rel: str, adjustments: dict | None = None,
                   rating: int = 0, label: str = "", keywords=None, flag: str = "none") -> dict:
    """
    Make a virtual copy of ``source_rel`` seeded with the given settings.

    The copy starts with the source's adjustments and metadata (the caller
    passes them) and is independent from then on.
    """
    source_rel = split_id(source_rel)[0]    # a copy of a copy is a copy of the raw
    with _lock:
        store = load(root)
        used = [v["copy_no"] for v in store["virtual"].values() if v["source"] == source_rel]
        vid = "vc-" + _new_id()
        entry = {
            "id": virtual_id(source_rel, vid), "vid": vid, "source": source_rel,
            "copy_no": (max(used) if used else 0) + 1,
            "adjustments": dict(adjustments or {}),
            "rating": max(-1, min(5, int(rating))),
            "label": library.normalize_label(label),
            "keywords": library.normalize_keywords(keywords or []),
            "flag": flag if flag in ("pick", "none") else "none",
            "created": time.time(),
        }
        store["virtual"][entry["id"]] = entry
        _save(root, store)
        return entry


def update_virtual(root: Path, photo_id: str, **fields) -> dict:
    allowed = {"adjustments", "rating", "label", "keywords", "flag"}
    with _lock:
        store = load(root)
        entry = store["virtual"].get(photo_id)
        if entry is None:
            raise KeyError(f"No such virtual copy: {photo_id}")
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"Cannot set {key} on a virtual copy")
            if key == "label":
                value = library.normalize_label(value)
            elif key == "keywords":
                value = library.normalize_keywords(value)
            elif key == "rating":
                value = max(-1, min(5, int(value)))
            elif key == "flag":
                value = value if value in ("pick", "none") else "none"
            elif key == "adjustments":
                value = dict(value)
            entry[key] = value
        _save(root, store)
        return entry


def delete_virtual(root: Path, photo_id: str) -> dict:
    """Remove a copy from the store (and from every collection). Never touches the raw."""
    with _lock:
        store = load(root)
        entry = store["virtual"].pop(photo_id, None)
        if entry is None:
            raise KeyError(f"No such virtual copy: {photo_id}")
        for col in store["collections"]:
            if "photos" in col:
                col["photos"] = [p for p in col["photos"] if p != photo_id]
        _save(root, store)
        return entry


def virtuals_of(root: Path, source_rel: str) -> list[dict]:
    return sorted((v for v in load(root)["virtual"].values() if v["source"] == source_rel),
                  key=lambda v: v["copy_no"])


def copy_name(source_name: str, entry: dict) -> str:
    p = Path(source_name)
    return f"{p.stem} (copy {entry['copy_no']}){p.suffix}"


def has_virtuals(root: Path) -> bool:
    """Whether the library has any virtual copy (lets the grid skip building them)."""
    return bool(load(root)["virtual"])


def virtual_items(root: Path, source_items: list[dict]) -> list[dict]:
    """
    Library items for every virtual copy whose raw is present in
    ``source_items``. A copy inherits the source's camera and capture date and
    carries its own rating, label, keywords and flag.
    """
    by_path = {it["path"]: it for it in source_items}
    out = []
    for entry in load(root)["virtual"].values():
        src = by_path.get(entry["source"])
        if src is None:
            continue    # raw missing: hide the orphan, keep its settings
        rating = entry.get("rating", 0)
        flag = "reject" if rating == -1 else ("pick" if entry.get("flag") == "pick" else "none")
        out.append({
            "path": entry["id"], "name": copy_name(src["name"], entry),
            "rating": rating, "label": entry.get("label", ""),
            "keywords": list(entry.get("keywords", [])),
            "camera": src["camera"], "date": src["date"], "flag": flag,
            **{k: src[k] for k in ("lens", "iso", "focal", "edited", "gps") if k in src},
            "virtual": True, "src_path": src["path"], "source": entry["source"],
        })
    return out


# ---------------------------------------------------------------------------
# Stacks
# ---------------------------------------------------------------------------

def _drop_from_stacks(stacks: dict, ids) -> None:
    """Remove ``ids`` from every stack; a stack left with fewer than two photos dissolves."""
    gone = set(ids)
    for sid in list(stacks):
        st = stacks[sid]
        st["photos"] = [p for p in st["photos"] if p not in gone]
        if len(st["photos"]) < 2:
            del stacks[sid]
        elif st.get("top") not in st["photos"]:
            st["top"] = st["photos"][0]


def _make_stack(stacks: dict, ids: list[str], collapsed: bool = True) -> dict:
    _drop_from_stacks(stacks, ids)
    st = {"id": _new_id(), "photos": list(ids), "top": ids[0], "collapsed": bool(collapsed)}
    stacks[st["id"]] = st
    return st


def has_stacks(root: Path) -> bool:
    return bool(load(root)["stacks"])


def list_stacks(root: Path) -> list[dict]:
    return list(load(root)["stacks"].values())


def stack_photos(root: Path, ids, collapsed: bool = True) -> dict:
    """Stack ``ids`` (first one on top). Members leave any stack they were in."""
    ids = _dedupe(ids)
    if len(ids) < 2:
        raise ValueError("Select at least two photos to stack")
    with _lock:
        store = load(root)
        st = _make_stack(store["stacks"], ids, collapsed)
        _save(root, store)
        return st


def unstack(root: Path, ids) -> int:
    """Take ``ids`` out of their stacks; returns how many stacks changed or dissolved."""
    ids = set(_dedupe(ids))
    with _lock:
        store = load(root)
        before = json.dumps(store["stacks"], sort_keys=True)
        hit = sum(1 for st in store["stacks"].values() if ids & set(st["photos"]))
        _drop_from_stacks(store["stacks"], ids)
        if json.dumps(store["stacks"], sort_keys=True) != before:
            _save(root, store)
        return hit


def set_stack_state(root: Path, stack_ids=None, collapsed: bool = True, top: str | None = None) -> int:
    """Collapse/expand the given stacks (all when ``stack_ids`` is None); optionally set the top photo."""
    with _lock:
        store = load(root)
        wanted = store["stacks"].keys() if stack_ids is None else stack_ids
        n = 0
        for sid in wanted:
            st = store["stacks"].get(sid)
            if st is None:
                if stack_ids is not None:
                    raise KeyError(f"No stack {sid!r}")
                continue
            if top is not None:
                if top not in st["photos"]:
                    raise ValueError("That photo is not in the stack")
                st["top"] = top
            st["collapsed"] = bool(collapsed)
            n += 1
        _save(root, store)
        return n


def auto_stack(root: Path, groups: list[list[str]], collapsed: bool = True) -> list[dict]:
    """
    Stack each group of two or more ids (for example Smart cull burst groups).
    Photos already in a stack are left alone, so manual stacks survive.
    """
    with _lock:
        store = load(root)
        taken = {p for st in store["stacks"].values() for p in st["photos"]}
        made = []
        for g in groups:
            free = [p for p in _dedupe(g) if p not in taken]
            if len(free) < 2:
                continue
            made.append(_make_stack(store["stacks"], free, collapsed))
            taken.update(free)
        if made:
            _save(root, store)
        return made


def apply_stacks(root: Path, matched: list[dict], everything: list[dict], collapse: bool = True) -> list[dict]:
    """
    ``matched`` (already filtered and sorted) with stack info added and, when
    ``collapse``, the hidden members of collapsed stacks removed. A collapsed
    stack shows its top, or its first matching member when the top is filtered out.
    Items are copied; the index's own dicts are never modified.
    """
    stacks = load(root)["stacks"]
    if not stacks:
        return matched
    present = {it["path"] for it in everything}
    of: dict[str, dict] = {}
    for st in stacks.values():
        live = [p for p in st["photos"] if p in present]
        if len(live) >= 2:
            for p in live:
                of[p] = {"st": st, "live": live}
    shown: dict[str, str] = {}      # stack id -> path shown while collapsed
    for it in matched:
        info = of.get(it["path"])
        if info:
            sid = info["st"]["id"]
            if sid not in shown or it["path"] == info["st"].get("top"):
                shown[sid] = it["path"]
    out = []
    for it in matched:
        info = of.get(it["path"])
        if not info:
            out.append(it)
            continue
        st = info["st"]
        hidden = collapse and st["collapsed"] and shown.get(st["id"]) != it["path"]
        if hidden:
            continue
        out.append({**it, "stack": {
            "id": st["id"], "count": len(info["live"]), "pos": info["live"].index(it["path"]) + 1,
            "top": it["path"] == shown.get(st["id"]), "collapsed": bool(st["collapsed"])}})
    return out
