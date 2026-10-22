#!/usr/bin/env python3
"""
HTTP endpoints for the Keyword List panel (see keywordlib.py for the model).

``GET  /api/keywords/list``      ``{keywords: [{keyword, key, parent, path, depth, count, total,
                                 synonyms, export}], library_total}`` depth-first, parents first.
``POST /api/keywords/create``    ``{name, parent?}``
``POST /api/keywords/update``    ``{keyword, parent?, synonyms?, export?}`` (``parent: ""`` makes it top level)
``POST /api/keywords/rename``    ``{keyword, new_name, merge?, dry_run?}``
``POST /api/keywords/delete``    ``{keyword, dry_run?}``  children move up; photos just lose it

Rename, merge, delete and re-parenting rewrite every affected photo through the
library index (write-ahead log first, then the XMP sidecar with the usual
per-file lock), virtual copies included. Nothing is written when the request
is invalid, and ``dry_run`` reports how many photos would change.
"""

from __future__ import annotations

import threading

from flask import Blueprint, jsonify

import catalog
import keywordlib as kl
import library

bp = Blueprint("kwapi", __name__)
_op_lock = threading.Lock()      # one keyword edit at a time: each reads, then rewrites, the shared state


def _ed():
    import editor            # late: editor owns LIBRARY_ROOT (and registers this blueprint)
    return editor


def _fail(msg: str, code: int = 400):
    return jsonify({"error": msg}), code


def _context():
    ed = _ed()
    idx = ed._idx()
    root = ed.LIBRARY_ROOT
    return ed, idx, root, idx.items()


def _virtual_updates(root, moved: dict, new_state: dict, scope: set) -> dict[str, list[str]]:
    out = {}
    touch = set(moved) | scope
    for vid, entry in catalog.load(root)["virtual"].items():
        have = list(entry.get("keywords", []))
        if not any(kl.key_of(k) in touch for k in have):
            continue
        new = []
        for k in have:
            key = kl.key_of(k)
            if key in moved:
                if moved[key] is not None:
                    new.append(new_state[moved[key]]["name"])
            else:
                new.append(k)
        new = library.normalize_keywords(new)
        if new != have:
            out[vid] = new
    return out


def _commit(idx, root, items, new_state, moved, scope, dry: bool) -> dict:
    """Rewrite the photos, then save the state. Returns the counts."""
    updates = kl.photo_updates(items, moved, new_state, scope)
    virtuals = _virtual_updates(root, moved, new_state, scope)
    if not dry:
        if updates:
            idx.set_fields(updates)
        for vid, kws in virtuals.items():
            catalog.update_virtual(root, vid, keywords=kws)
        kl.save_state(root, new_state)
    return {"photos": len(updates), "virtual_copies": len(virtuals), "pending": idx.pending_count()}


def _resolve(state: dict, name) -> str:
    key = kl.key_of(name)
    if key not in state:
        raise ValueError(f"No such keyword: {name}")
    return key


@bp.route("/api/keywords/list")
def keyword_list():
    ed, idx, root, items = _context()
    return jsonify({"keywords": kl.tree(items, kl.merged_state(root, items)),
                    "library_total": len(items)})


@bp.route("/api/keywords/create", methods=["POST"])
def keyword_create():
    ed, idx, root, items = _context()
    try:
        payload = ed._json_body()
        name = kl.validate_name(payload.get("name"))
        with _op_lock:
            state = kl.merged_state(root, items)
            key = kl.key_of(name)
            if key in state:
                raise ValueError(f"'{state[key]['name']}' already exists")
            parent = ""
            if payload.get("parent"):
                parent = _resolve(state, payload["parent"])
            state[key] = {"name": name, "parent": parent, "synonyms": [], "export": True}
            kl.save_state(root, state)
    except ValueError as exc:
        return _fail(str(exc))
    return jsonify({"ok": True, "message": f"Created keyword '{name}'"})


@bp.route("/api/keywords/update", methods=["POST"])
def keyword_update():
    ed, idx, root, items = _context()
    try:
        payload = ed._json_body()
        with _op_lock:
            state = kl.merged_state(root, items)
            key = _resolve(state, payload.get("keyword"))
            if "synonyms" in payload:
                state[key]["synonyms"] = [s for s in library.normalize_keywords(payload["synonyms"])
                                          if kl.key_of(kl.validate_name(s)) != key]
            if "export" in payload:
                state[key]["export"] = bool(payload["export"])
            scope: set[str] = set()
            if "parent" in payload:
                parent = ""
                if payload["parent"]:
                    parent = _resolve(state, payload["parent"])
                    if parent == key or parent in kl.descendants(state, key):
                        raise ValueError("A keyword cannot be moved under itself")
                state[key]["parent"] = parent
                scope = {key} | kl.descendants(state, key)
            result = _commit(idx, root, items, state, {}, scope, dry=False)
    except ValueError as exc:
        return _fail(str(exc))
    return jsonify({"ok": True, **result, "message": f"Updated keyword '{state[key]['name']}'"})


@bp.route("/api/keywords/rename", methods=["POST"])
def keyword_rename():
    ed, idx, root, items = _context()
    try:
        payload = ed._json_body()
        with _op_lock:
            state = kl.merged_state(root, items)
            key = _resolve(state, payload.get("keyword"))
            plan = kl.plan_rename(state, payload.get("keyword"), payload.get("new_name"),
                                  merge=bool(payload.get("merge")))
            new_key = plan["moved"][key]
            scope = {new_key} | kl.descendants(plan["state"], new_key)
            result = _commit(idx, root, items, plan["state"], plan["moved"], scope,
                             dry=bool(payload.get("dry_run")))
    except ValueError as exc:
        return _fail(str(exc), 409 if "already exists" in str(exc) else 400)
    name = plan["state"][new_key]["name"]
    verb = "Would rename" if payload.get("dry_run") else "Renamed"
    return jsonify({"ok": True, **result, "merged": plan["merging"], "name": name,
                    "message": f"{verb} '{state[key]['name']}' to '{name}' on {result['photos']} photo(s)"})


@bp.route("/api/keywords/delete", methods=["POST"])
def keyword_delete():
    ed, idx, root, items = _context()
    try:
        payload = ed._json_body()
        with _op_lock:
            state = kl.merged_state(root, items)
            key = _resolve(state, payload.get("keyword"))
            plan = kl.plan_delete(state, key)
            scope = kl.descendants(state, key)
            result = _commit(idx, root, items, plan["state"], plan["moved"], scope,
                             dry=bool(payload.get("dry_run")))
    except ValueError as exc:
        return _fail(str(exc))
    verb = "Would delete" if payload.get("dry_run") else "Deleted"
    return jsonify({"ok": True, **result,
                    "message": f"{verb} '{state[key]['name']}' from {result['photos']} photo(s)"})
