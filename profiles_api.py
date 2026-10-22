"""
HTTP side of the Profile browser (Develop): list, import, delete and thumbnails.

The maths is in profiles.py. Choosing a profile is an ordinary edit (``adjustments.camera_profile``),
so it goes through preview / save / history like any slider. Thumbnails render the photo's cached
160 px thumbnail through the real pipeline with only that profile set; every tile URL carries the
profile's revision so it can be cached for good.
"""
from __future__ import annotations

import base64
import binascii

from flask import Blueprint, jsonify

import looks_api
import presetlib
import profiles

bp = Blueprint("profiles_api", __name__)
BadRequest = looks_api.BadRequest
MAX_UPLOAD = 6 * 1024 * 1024


@bp.errorhandler(profiles.ProfileError)
def _profile_error(exc):
    return jsonify({"error": str(exc)}), 400


@bp.errorhandler(profiles.ProfileNotFound)
def _profile_missing(exc):
    return jsonify({"error": str(exc)}), 404


@bp.errorhandler(BadRequest)
def _bad_request(exc):
    return jsonify({"error": str(exc)}), exc.status


def _listing() -> list[dict]:
    return [dict(p, rev=profiles.revision(p["id"])) for p in profiles.list_profiles()]


@bp.route("/api/profiles", methods=["GET"])
def api_profiles():
    return jsonify({"profiles": _listing()})


@bp.route("/api/profiles/import", methods=["POST"])
def api_profile_import():
    """Body: ``{"name": "file name", "data": "<base64 of a .dcp or ICC file>"}``."""
    body = looks_api._body()
    data = body.get("data")
    if not isinstance(data, str) or not data:
        raise BadRequest("data must be the base64 contents of a .dcp or .icc file")
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise BadRequest("data is not valid base64")
    if len(raw) > MAX_UPLOAD:
        raise BadRequest("That profile is too large", 413)
    entry = profiles.import_bytes(raw, filename=str(body.get("name") or ""))
    return jsonify({"ok": True, "profile": entry, "profiles": _listing(),
                    "message": f"Imported profile '{entry['name']}'"})


@bp.route("/api/profiles/delete", methods=["POST"])
def api_profile_delete():
    pid = str(looks_api._body().get("id", ""))
    if pid in profiles.builtin_ids():
        raise BadRequest("Built-in profiles cannot be deleted")
    removed = profiles.delete(pid)
    looks_api._ed()._stage_cache.clear()
    return jsonify({"ok": removed, "profiles": _listing(),
                    "message": "Deleted profile" if removed else "No such profile"})


@bp.route("/api/profiles/thumb", methods=["GET"])
def api_profile_thumb():
    body = looks_api._body()
    path, _ = looks_api._photo(body.get("path"))
    pid = str(body.get("id", ""))
    profiles.get(pid)
    baseline = looks_api._baseline(body)
    adj = presetlib.neutral_adjustments(baseline)
    adj["camera_profile"] = pid
    ed = looks_api._ed()
    return looks_api._jpeg(ed.apply_adjustments(looks_api._thumb_base(path), int(baseline), adj))
