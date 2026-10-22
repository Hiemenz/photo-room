#!/usr/bin/env python3
"""
HTTP endpoints of the Folders panel (see folders.py for the rules).

``GET  /api/folders``              tree with photo counts, plus the latest undoable operation
``POST /api/folders/create``       {parent, name}
``POST /api/folders/move``         {paths: [...], dest}   photos (with sidecars) into a folder
``POST /api/folders/move_folder``  {path, dest}
``POST /api/folders/rename``       {path, name}           a photo (its stem) or a folder
``POST /api/folders/undo``         undo the latest operation

Nothing here deletes or overwrites a file.
"""

from __future__ import annotations

from flask import Blueprint, jsonify

import folders
import library
import security

bp = Blueprint("folders_api", __name__)


def _editor():
    import editor            # late: editor owns LIBRARY_ROOT (and imports this module)
    return editor


def _run(fn, *args):
    ed = _editor()
    try:
        body = ed._json_body()
        if not isinstance(body, dict):
            raise ValueError("Expected a JSON object")
        idx = ed._idx()
        idx.flush(timeout=60)                 # queued sidecar writes must land before files move
        result = fn(ed.LIBRARY_ROOT, body, *args)
    except folders.MoveError as exc:
        return jsonify({"error": str(exc)}), 400
    except (ValueError, TypeError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": ed.describe_error(exc)}), 500
    if result.get("mapping"):
        idx.carry_picks(result["mapping"])
        library.clear_cache()
    idx.scan()                                # new paths appear, old ones disappear
    result["undo"] = _undo_info()
    return jsonify(result)


def _undo_info():
    last = folders.last_undoable(_editor().LIBRARY_ROOT)
    return {"op": last["id"], "label": last["label"]} if last else None


@bp.route("/api/folders")
def api_folders():
    ed = _editor()
    idx = ed._idx()
    rels = [it["path"] for it in idx.items()]
    return jsonify({"root": str(ed.LIBRARY_ROOT), "folders": folders.tree(ed.LIBRARY_ROOT, rels),
                    "undo": _undo_info()})


@bp.route("/api/folders/create", methods=["POST"])
def api_folder_create():
    return _run(lambda root, b: folders.create_folder(root, b.get("parent", ""), b.get("name")))


@bp.route("/api/folders/move", methods=["POST"])
def api_folder_move_photos():
    return _run(lambda root, b: folders.move_photos(root, b.get("paths"), b.get("dest", "")))


@bp.route("/api/folders/move_folder", methods=["POST"])
def api_folder_move_folder():
    return _run(lambda root, b: folders.move_folder(root, b.get("path"), b.get("dest", "")))


@bp.route("/api/folders/rename", methods=["POST"])
def api_folder_rename():
    def go(root, b):
        p = b.get("path")
        try:
            is_dir = security.safe_relpath(root, p).is_dir()
        except ValueError as exc:
            raise folders.MoveError(str(exc)) from None
        if is_dir:
            return folders.rename_folder(root, p, b.get("name"))
        return folders.rename_photo(root, p, b.get("name"))
    return _run(go)


@bp.route("/api/folders/undo", methods=["POST"])
def api_folder_undo():
    return _run(lambda root, b: folders.undo_last(root))
