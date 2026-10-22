"""
HTTP side of Faces / People (faces.py does the work). Registered by editor.py.

    GET  /api/faces/status            {detector, embedder, advanced, limits, indexed, library, faces, building, progress}
    POST /api/faces/build             start (or resume) the background scan; opt-in, never downloads anything
    POST /api/faces/cancel            stop it (scanned photos are kept)
    GET  /api/faces/people            {people: [{id, name, photos, faces, face_id}]}
    POST /api/faces/people/<id>/name  {name}
    POST /api/faces/people/<id>/merge {into}
    POST /api/faces/people/<id>/remove   hide a person (false faces, strangers)
    GET  /api/faces/face/<id>.jpg     stored face thumbnail

The Library grid filters through ``/api/library?person=<id>`` (see ``filter_library``),
keeping every other filter.
"""
from __future__ import annotations

from flask import Blueprint, Response, jsonify, request

import faces

bp = Blueprint("faces_api", __name__)


def _ed():
    import editor           # late: editor registers this blueprint

    return editor


def wants_person(args) -> bool:
    return bool((args.get("person") or "").strip())


def filter_library(matched: list[dict], args) -> list[dict]:
    return faces.filter_items(_ed().LIBRARY_ROOT, matched, args["person"].strip())


def _store():
    return faces.get_store(_ed().LIBRARY_ROOT)


@bp.route("/api/faces/status")
def status():
    ed = _ed()
    info = faces.describe_backend()
    _, emb = faces.get_backend()
    rels = [i["path"] for i in ed._idx().items()]
    b = faces._builders.get(str(ed.LIBRARY_ROOT.resolve()))
    st = _store()
    return jsonify({**info, "limits": faces.LIMITS, "model_dir": str(faces.model_dir()),
                    "indexed": st.scanned_count(emb.name, rels), "library": len(rels),
                    "faces": st.face_count(), "building": bool(b and b.running()),
                    "progress": b.progress if b else None})


@bp.route("/api/faces/build", methods=["POST"])
def build():
    ed = _ed()
    rels = [i["path"] for i in ed._idx().items()]
    started = faces.get_builder(ed.LIBRARY_ROOT).start(rels)
    return jsonify({"started": started, "total": len(rels)})


@bp.route("/api/faces/cancel", methods=["POST"])
def cancel():
    b = faces._builders.get(str(_ed().LIBRARY_ROOT.resolve()))
    if b:
        b.cancel()
    return jsonify({"ok": True})


@bp.route("/api/faces/people")
def people():
    return jsonify({"people": _store().people(faces.get_backend()[1].name)})


def _body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@bp.route("/api/faces/people/<int:pid>/name", methods=["POST"])
def name(pid):
    st = _store()
    if st.get_person(pid) is None:
        return jsonify({"error": "No such person"}), 404
    nm = str(_body().get("name", "")).strip()[:100]
    st.rename(pid, nm)
    return jsonify({"ok": True, "name": nm})


@bp.route("/api/faces/people/<int:pid>/merge", methods=["POST"])
def merge(pid):
    st = _store()
    try:
        dst = int(_body().get("into"))
    except (TypeError, ValueError):
        return jsonify({"error": "Give the person id to merge into"}), 400
    if dst == pid or st.get_person(pid) is None or st.get_person(dst) is None:
        return jsonify({"error": "Pick two different, existing people"}), 400
    src_name, dst_name = st.get_person(pid)["name"], st.get_person(dst)["name"]
    if not dst_name and src_name:
        st.rename(dst, src_name)             # keep the name the user gave
    st.merge(pid, dst)
    return jsonify({"ok": True})


@bp.route("/api/faces/people/<int:pid>/remove", methods=["POST"])
def remove(pid):
    st = _store()
    if st.get_person(pid) is None:
        return jsonify({"error": "No such person"}), 404
    st.remove(pid)
    return jsonify({"ok": True})


@bp.route("/api/faces/face/<int:fid>.jpg")
def face_image(fid):
    data = _store().thumb(fid)
    if not data:
        return jsonify({"error": "No such face"}), 404
    return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "max-age=3600"})
