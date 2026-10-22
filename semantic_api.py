"""
HTTP side of semantic search (semantic.py does the work). Registered by editor.py.

    GET  /api/semantic/status   {available, message, indexed, library, building, progress, model_dir}
    POST /api/semantic/build    start (or resume) the background index; never downloads a model
    POST /api/semantic/cancel   stop the builder (finished files are kept)
    GET  /api/semantic/search?q=...|similar=<path>&limit=   ranked [{path, score, ...}] + info

The Library grid uses the same ranking through ``/api/library?sem=<text>`` or
``/api/library?similar=<path>`` (see ``rank_library``), keeping every other filter.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

import semantic

bp = Blueprint("semantic_api", __name__)


def _ed():
    import editor           # late: editor registers this blueprint

    return editor


def _top() -> int:
    try:
        return max(1, min(2000, int(request.args.get("limit", semantic.DEFAULT_TOP))))
    except ValueError:
        return semantic.DEFAULT_TOP


def wants_ranking(args) -> bool:
    return bool((args.get("sem") or "").strip() or (args.get("similar") or "").strip())


def rank_library(matched: list[dict], args) -> tuple[list[dict], dict]:
    """Re-rank already-filtered library items for ``sem`` / ``similar``."""
    return semantic.search(_ed().LIBRARY_ROOT, matched, query=(args.get("sem") or "").strip(),
                           similar=(args.get("similar") or "").strip())


@bp.route("/api/semantic/status")
def status():
    ed = _ed()
    emb = semantic.get_embedder()
    rels = [i["path"] for i in ed._idx().items()]
    b = semantic._builders.get(str(ed.LIBRARY_ROOT.resolve()))
    return jsonify({
        "available": emb is not None,
        "message": "" if emb else semantic.unavailable_message(),
        "model": emb.name if emb else None,
        "model_dir": str(semantic.model_dir()),
        "indexed": semantic.get_store(ed.LIBRARY_ROOT).count(emb.name, rels) if emb else 0,
        "library": len(rels),
        "building": bool(b and b.running()),
        "progress": b.progress if b else None,
    })


@bp.route("/api/semantic/build", methods=["POST"])
def build():
    ed = _ed()
    emb = semantic.get_embedder()
    if emb is None:
        return jsonify({"error": semantic.unavailable_message()}), 409
    rels = [i["path"] for i in ed._idx().items()]
    started = semantic.get_builder(ed.LIBRARY_ROOT, emb).start(rels)
    return jsonify({"started": started, "total": len(rels)})


@bp.route("/api/semantic/cancel", methods=["POST"])
def cancel():
    b = semantic._builders.get(str(_ed().LIBRARY_ROOT.resolve()))
    if b:
        b.cancel()
    return jsonify({"ok": True})


@bp.route("/api/semantic/search")
def search():
    q, sim = request.args.get("q", ""), request.args.get("similar", "")
    if not q.strip() and not sim:
        return jsonify({"error": "Give q (text) or similar (a photo path)"}), 400
    ranked, info = semantic.search(_ed().LIBRARY_ROOT, _ed()._idx().items(), query=q, similar=sim, top=_top())
    return jsonify({"items": ranked, "info": info})
