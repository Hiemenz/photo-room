#!/usr/bin/env python3
"""
Keyword list: hierarchy, synonyms and the "include on export" flag.

A photo's keywords are the flat names in ``XMP-dc:Subject``; Lightroom also
reads ``XMP-lr:HierarchicalSubject`` ("France|Paris") to rebuild the tree. This
module owns the tree and the per-keyword settings, kept in
``.photo-room-keywords.json`` in the library root (the "library state"):

    {"keywords": {"paris": {"name": "Paris", "parent": "france",
                            "synonyms": ["Paname"], "export": true}}}

Keys are the lower-cased name (keywords are case-insensitive, like everywhere
else in the app); ``parent`` is the parent's key or "". A keyword name is
unique library-wide. Parents found in sidecars written by Lightroom are merged
in (the saved tree wins), so the panel shows the hierarchy even before
anything was edited here.
"""

from __future__ import annotations

import json
from pathlib import Path

import fsutil
import library

STATE_NAME = ".photo-room-keywords.json"
SEP = "|"            # lr:HierarchicalSubject separator


def state_path(root: Path) -> Path:
    return Path(root) / STATE_NAME


def key_of(name: str) -> str:
    return " ".join(str(name).split()).lower()


def _entry(name: str, parent: str = "") -> dict:
    return {"name": name, "parent": parent, "synonyms": [], "export": True}


def load_state(root: Path) -> dict:
    try:
        data = json.loads(state_path(root).read_text())
        raw = data.get("keywords", {})
    except (OSError, ValueError, AttributeError):
        raw = {}
    out: dict[str, dict] = {}
    for k, v in (raw.items() if isinstance(raw, dict) else []):
        if not isinstance(v, dict) or not v.get("name"):
            continue
        out[str(k)] = {"name": str(v["name"]), "parent": str(v.get("parent") or ""),
                       "synonyms": library.normalize_keywords(v.get("synonyms", [])),
                       "export": v.get("export", True) is not False}
    return out


def save_state(root: Path, state: dict) -> None:
    fsutil.atomic_write_text(state_path(root), json.dumps({"keywords": state}, indent=1, sort_keys=True))


def validate_name(name) -> str:
    text = " ".join(str(name or "").split())
    if not text:
        raise ValueError("Keyword name is empty")
    if any(ord(c) < 32 for c in text) or SEP in text or "," in text:
        raise ValueError("A keyword cannot contain commas, '|' or control characters")
    if len(text) > 200:
        raise ValueError("Keyword name is too long")
    return text


def parse_hier(entry: dict) -> list[str]:
    """The ``HierarchicalSubject`` paths of one exiftool record ("A|B" strings)."""
    raw = entry.get("HierarchicalSubject", [])
    if not isinstance(raw, list):
        raw = [raw]
    out, seen = [], set()
    for p in raw:
        parts = [" ".join(s.split()) for s in str(p).split(SEP)]
        path = SEP.join(s for s in parts if s)
        if path and path.lower() not in seen:
            seen.add(path.lower())
            out.append(path)
    return out


def merged_state(root: Path, items: list[dict]) -> dict:
    """Saved state plus parents found in the photos' own sidecars (the saved parent wins)."""
    state = load_state(root)
    for it in items:
        for path in it.get("hier") or []:
            parts = path.split(SEP)
            for i, part in enumerate(parts):
                state.setdefault(key_of(part), _entry(part))
                if i and not state[key_of(part)]["parent"] and key_of(part) != key_of(parts[i - 1]) \
                        and _acyclic(state, key_of(part), key_of(parts[i - 1])):
                    state[key_of(part)]["parent"] = key_of(parts[i - 1])
    return state


def _acyclic(state: dict, child: str, parent: str) -> bool:
    seen = set()
    while parent:
        if parent == child or parent in seen:
            return False
        seen.add(parent)
        parent = state.get(parent, {}).get("parent", "")
    return True


def ancestors(state: dict, key: str) -> list[str]:
    """Keys from the root down to (not including) ``key``."""
    chain, seen = [], {key}
    parent = state.get(key, {}).get("parent", "")
    while parent and parent not in seen and parent in state:
        chain.append(parent)
        seen.add(parent)
        parent = state[parent].get("parent", "")
    return chain[::-1]


def descendants(state: dict, key: str) -> set[str]:
    out, todo = set(), [key]
    while todo:
        cur = todo.pop()
        for k, v in state.items():
            if v["parent"] == cur and k not in out and k != key:
                out.add(k)
                todo.append(k)
    return out


def path_of(state: dict, name: str) -> str:
    """"France|Paris" for Paris, or just the name when it has no known parent."""
    k = key_of(name)
    names = [state[a]["name"] for a in ancestors(state, k)]
    return SEP.join(names + [state[k]["name"] if k in state else name])


def paths_for(state: dict, keywords: list[str], old_hier: list[str] | None = None) -> list[str]:
    """
    The ``HierarchicalSubject`` value for a photo's keywords. Parents known to
    the tree win; otherwise a path already in the photo's sidecar is kept.
    """
    old = {key_of(p.rsplit(SEP, 1)[-1]): p for p in (old_hier or [])}
    out = []
    for kw in keywords:
        k = key_of(kw)
        if k in state and state[k]["parent"]:
            out.append(path_of(state, kw))
        else:
            out.append(old.get(k, kw))
    return out


def tree(items: list[dict], state: dict) -> list[dict]:
    """
    Flat, depth-first list of every keyword: name, key, parent, path ("France >
    Paris"), depth, count (photos tagged with exactly this keyword), total
    (photos with it or any descendant), synonyms, export.
    """
    direct: dict[str, set[str]] = {}
    state = {k: dict(v) for k, v in state.items()}
    for it in items:
        for kw in it.get("keywords", []):
            direct.setdefault(key_of(kw), set()).add(it["path"])
            state.setdefault(key_of(kw), _entry(kw))
    kids: dict[str, list[str]] = {}
    for k, v in state.items():
        parent = v["parent"] if v["parent"] in state and v["parent"] != k else ""
        kids.setdefault(parent, []).append(k)
    out: list[dict] = []

    def walk(k: str, depth: int, trail: list[str], seen: frozenset) -> set[str]:
        v = state[k]
        row = {"keyword": v["name"], "key": k,
               "parent": state[v["parent"]]["name"] if v["parent"] in state else "",
               "path": " > ".join(trail + [v["name"]]), "depth": depth,
               "count": len(direct.get(k, ())), "synonyms": v["synonyms"], "export": v["export"]}
        out.append(row)
        photos = set(direct.get(k, ()))
        for c in sorted(kids.get(k, []), key=lambda x: state[x]["name"].lower()):
            if c not in seen:
                photos |= walk(c, depth + 1, trail + [v["name"]], seen | {k})
        row["total"] = len(photos)
        return photos

    for k in sorted(kids.get("", []), key=lambda x: state[x]["name"].lower()):
        walk(k, 0, [], frozenset())
    return out


def unexported(root: Path) -> frozenset[str]:
    """Names of keywords flagged "do not include on export"."""
    return frozenset(v["name"] for v in load_state(root).values() if not v["export"])


def strip_for_export(dest: Path, hidden, run) -> None:
    """Remove ``hidden`` keywords (and any hierarchy path through them) from an exported file."""
    hide = {key_of(h) for h in hidden}
    if not hide:
        return
    res = run(["-j", "-XMP-dc:Subject", "-XMP-lr:HierarchicalSubject", str(dest)])
    try:
        entry = json.loads(res.stdout)[0]
    except (ValueError, IndexError):
        return
    subj = entry.get("Subject", [])
    subj = subj if isinstance(subj, list) else [subj]
    keep = [str(s) for s in subj if key_of(s) not in hide]
    old_hier = parse_hier(entry)
    hier = [p for p in old_hier if not any(key_of(part) in hide for part in p.split(SEP))]
    if len(keep) == len(subj) and len(hier) == len(old_hier):
        return
    args = [*library.xmp_args(keywords=keep, hier=hier), "-overwrite_original", str(dest)]
    res = run(args)
    if res.returncode != 0:
        raise RuntimeError(f"exiftool failed removing keywords: {res.stderr.strip()}")


# ---------------------------------------------------------------------------
# Operations: pure planning, so the caller can preview before touching a photo
# ---------------------------------------------------------------------------

def plan_rename(state: dict, old: str, new: str, merge: bool = False) -> dict:
    """The state after renaming ``old`` to ``new``. Raises ValueError if it cannot be done safely."""
    ok, new = key_of(old), validate_name(new)
    nk = key_of(new)
    if ok not in state:
        raise ValueError(f"No such keyword: {old}")
    merging = nk != ok and nk in state
    if merging and not merge:
        raise ValueError(f"A keyword named '{state[nk]['name']}' already exists; merge to combine them")
    if merging and nk in descendants(state, ok):
        raise ValueError("Cannot merge a keyword into one of its own children")
    new_state = {k: dict(v) for k, v in state.items()}
    ent = new_state.pop(ok)
    if merging:
        tgt = new_state[nk]
        tgt["synonyms"] = library.normalize_keywords(tgt["synonyms"] + ent["synonyms"])
        tgt["export"] = tgt["export"] and ent["export"]
        if not tgt["parent"] and ent["parent"] != nk and _acyclic(new_state, nk, ent["parent"]):
            tgt["parent"] = ent["parent"]
    else:
        ent["name"] = new
        new_state[nk] = ent
    for v in new_state.values():
        if v["parent"] == ok:
            v["parent"] = nk
    return {"state": new_state, "moved": {ok: nk}, "merging": merging}


def plan_delete(state: dict, key: str) -> dict:
    """Deleting a keyword removes only it; its children move up to its parent."""
    if key not in state:
        raise ValueError("No such keyword")
    new_state = {k: dict(v) for k, v in state.items()}
    gone = new_state.pop(key)
    for v in new_state.values():
        if v["parent"] == key:
            v["parent"] = gone["parent"]
    return {"state": new_state, "moved": {key: None}}


def photo_updates(items: list[dict], moved: dict, new_state: dict, scope: set[str]) -> dict[str, dict]:
    """
    Per-photo updates after a state change. ``moved`` maps an old key to its
    replacement key (rename / merge) or None (delete); ``scope`` is every key
    whose hierarchy path may have changed. Photos carrying none of those are
    left alone.
    """
    touch = set(moved) | scope
    updates: dict[str, dict] = {}
    for it in items:
        kws = list(it.get("keywords", []))
        if not any(key_of(k) in touch for k in kws):
            continue
        out = []
        for kw in kws:
            k = key_of(kw)
            if k in moved:
                repl = moved[k]
                if repl is not None:
                    out.append(new_state[repl]["name"])
            else:
                out.append(kw)
        out = library.normalize_keywords(out)
        hier = paths_for(new_state, out)
        if out != kws or hier != list(it.get("hier") or []):
            updates[it["path"]] = {"keywords": out, "hier": hier}
    return updates
