#!/usr/bin/env python3
"""
HTTP endpoints for the metadata panel.

``GET  /api/metadata?path=<p>[&path=<p2>...]``   ``{items: [{path, info, fields}]}``
    Read-only EXIF/IPTC readout (``info``) and the editable fields.
``POST /api/metadata``   ``{paths, fields}`` or ``{per_path: {path: fields}}``
    Write title / caption / creator / copyright / copyright_status / usage_terms
    to each photo's XMP sidecar. Returns ``previous`` (per path) for undo: post it
    back as ``per_path`` to restore. A virtual copy shares its source's sidecar.
``GET  /api/metadata/presets``                    ``{presets: {name: fields}}``
``POST /api/metadata/presets``                    ``{name, fields}`` save or replace
``POST /api/metadata/presets/delete``             ``{name}``
``POST /api/metadata/presets/apply``              ``{name, paths}`` writes the preset's filled fields
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

import metadata

bp = Blueprint("metadata_api", __name__)

MAX_PATHS = 2000


def _ed():
    import editor           # late: editor registers this blueprint
    return editor


def _photo(raw):
    if not isinstance(raw, str) or not raw:
        raise ValueError("No photos given")
    return _ed()._resolve_photo(raw)


def _photos(raws) -> dict:
    """Unique source files (a virtual copy resolves to its raw), keyed by resolved path."""
    if not isinstance(raws, list) or not raws:
        raise ValueError("No photos given")
    if len(raws) > MAX_PATHS:
        raise ValueError("Too many photos at once")
    out = {}
    for raw in raws:
        out.setdefault(_photo(raw), raw)
    return out


def _key(path):
    return _ed()._rel(path)


@bp.errorhandler(ValueError)
def _bad(exc):
    return jsonify({"error": str(exc)}), 400


@bp.errorhandler(KeyError)
def _missing(exc):
    return jsonify({"error": str(exc.args[0]) if exc.args else "Not found"}), 404


@bp.route("/api/metadata")
def read():
    items = []
    for path in _photos(request.args.getlist("path")):
        try:
            items.append({"path": _key(path), **metadata.read(path)})
        except OSError as exc:
            return jsonify({"error": _ed().describe_error(exc)}), 500
    return jsonify({"items": items})


def _write(items: dict, what: str):
    try:
        previous = metadata.write_many(items)
    except RuntimeError as exc:
        return jsonify({"error": _ed().describe_error(exc)}), 500
    n = len(items)
    return jsonify({"ok": True, "count": n, "message": f"{what} on {n} photo{'s' if n != 1 else ''}",
                    "previous": {_key(p): v for p, v in previous.items()}})


@bp.route("/api/metadata", methods=["POST"])
def write():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Expected a JSON object"}), 415
    if "per_path" in payload:
        per = payload["per_path"]
        if not isinstance(per, dict) or not per:
            raise ValueError("No photos given")
        items = {_photo(p): f for p, f in per.items()}
    else:
        items = {p: payload.get("fields") for p in _photos(payload.get("paths"))}
    return _write(items, "Metadata saved")


@bp.route("/api/metadata/presets")
def presets():
    return jsonify({"presets": metadata.load_presets(_ed().LIBRARY_ROOT)})


@bp.route("/api/metadata/presets", methods=["POST"])
def save_preset():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Expected a JSON object"}), 415
    saved = metadata.save_preset(_ed().LIBRARY_ROOT, payload.get("name"), payload.get("fields"))
    return jsonify({"ok": True, "fields": saved, "presets": metadata.load_presets(_ed().LIBRARY_ROOT)})


@bp.route("/api/metadata/presets/delete", methods=["POST"])
def delete_preset():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Expected a JSON object"}), 415
    metadata.delete_preset(_ed().LIBRARY_ROOT, payload.get("name"))
    return jsonify({"ok": True, "presets": metadata.load_presets(_ed().LIBRARY_ROOT)})


@bp.route("/api/metadata/presets/apply", methods=["POST"])
def apply_preset():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Expected a JSON object"}), 415
    fields = metadata.get_preset(_ed().LIBRARY_ROOT, payload.get("name"))
    paths = _photos(payload.get("paths"))
    return _write({p: fields for p in paths}, f"Preset {payload.get('name')!r} applied")
