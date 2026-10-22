"""
HTTP side of the on-image Develop tools: targeted adjustment sampling, soft
proofing, the reference view and the per-photo extras sidecar (Guided Upright
lines, proof setup, reference photo).

``POST /api/develop/targeted``      ``{path, adjustments, x, y}`` -> level / hue / band under the point
``GET  /api/develop/softproof/profiles``  -> ``{profiles: [{id, name}], intents: [...]}``
``POST /api/develop/softproof``     ``{path, adjustments, settings}`` -> proofed JPEG data URI + out-of-gamut %
``POST /api/develop/reference``     ``{path}`` -> the photo as saved, for the side-by-side view
``GET|POST /api/develop/extras``    read / replace the sidecar (``upright_guides``, ``softproof``, ``reference``)
"""
from __future__ import annotations

import base64
import io

import numpy as np
from flask import Blueprint, jsonify, request
from PIL import Image

import develop_extras
import softproof
import targeted
from develop_api import BadRequest, _context, _number, _payload, _photo, _render

bp = Blueprint("proof_api", __name__)


@bp.errorhandler(BadRequest)
def _bad_request(exc):
    return jsonify({"error": str(exc)}), exc.status


def _u8(arr: np.ndarray) -> np.ndarray:
    return (np.clip(arr[:, :, :3], 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


def _jpeg_uri(arr: np.ndarray, quality: int = 85) -> str:
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


@bp.route("/api/develop/targeted", methods=["POST"])
def api_targeted():
    payload = _payload()
    x = _number(payload, "x", 0.5, 0, 1)
    y = _number(payload, "y", 0.5, 0, 1)
    path, adj, base, baseline = _context(payload)
    return jsonify(targeted.sample(_render(path, adj, base, baseline), x, y))


@bp.route("/api/develop/softproof/profiles")
def api_softproof_profiles():
    return jsonify({"profiles": [{"id": p["id"], "name": p["name"]} for p in softproof.list_profiles()],
                    "intents": list(softproof.INTENTS), "defaults": softproof.DEFAULTS})


@bp.route("/api/develop/softproof", methods=["POST"])
def api_softproof():
    payload = _payload()
    settings = softproof.normalise_settings(payload.get("settings"))
    try:
        profile = softproof.resolve_profile(settings["profile"])
    except ValueError as exc:
        raise BadRequest(str(exc))
    path, adj, base, baseline = _context(payload)
    try:
        out, percent = softproof.proof(_u8(_render(path, adj, base, baseline)), settings, profile)
    except Exception as exc:                      # littleCMS rejects some profile/intent pairs
        raise BadRequest(f"This profile cannot be used for proofing: {exc}")
    return jsonify({"image": _jpeg_uri(out), "out_of_gamut": percent, "settings": settings,
                    "width": int(out.shape[1]), "height": int(out.shape[0])})


@bp.route("/api/develop/reference", methods=["POST"])
def api_reference():
    """The reference photo with its own saved edits (it is shown, never changed)."""
    payload = dict(_payload())
    payload.pop("adjustments", None)
    path, adj, base, baseline = _context(payload)
    out = _u8(_render(path, adj, base, baseline))
    return jsonify({"preview": _jpeg_uri(out), "name": path.name})


@bp.route("/api/develop/extras", methods=["GET", "POST"])
def api_extras():
    payload = _payload()
    path, _ = _photo(payload.get("path"))
    if request.method == "GET":
        return jsonify(develop_extras.load(path))
    extras = payload.get("extras")
    if not isinstance(extras, dict):
        raise BadRequest("extras must be an object")
    merged = {**develop_extras.load(path), **extras}      # a key that is absent is left alone
    try:
        return jsonify(develop_extras.save(path, merged))
    except OSError as exc:
        raise BadRequest(f"Could not write the Develop sidecar: {exc}", 500)

