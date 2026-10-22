#!/usr/bin/env python3
"""
HTTP endpoints for Library stacks (state lives in catalog.py, beside the library).

    GET  /api/stacks                       every stack
    POST /api/stacks/create  {paths}       stack the photos, the first on top
    POST /api/stacks/remove  {paths}       take photos out of their stacks
    POST /api/stacks/state   {ids|paths, collapsed, top?}   collapse / expand (all when neither is given)
    POST /api/stacks/auto    {scope, paths?, collection?, window?, hash?, collapsed?}
        stack Smart cull burst groups (same grouping as /api/cull/suggest, best frame on top).
        Photos not analysed yet are skipped and counted in ``missing``; photos already
        stacked are left alone.

The grid reads stacks through ``/api/library`` (items carry ``stack``; ``stacks=0``
shows every member). Nothing here writes a raw or a sidecar.
"""

from __future__ import annotations

from flask import Blueprint, jsonify

import catalog

bp = Blueprint("stacks_api", __name__)


def _ed():
    import editor           # late: editor registers this blueprint
    return editor


def _body():
    return {"ok": True, "stacks": catalog.list_stacks(_ed().LIBRARY_ROOT)}


def _run(fn):
    ed = _ed()
    try:
        payload = ed._json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    return ed._catalog_call(lambda: fn(ed, payload))


@bp.route("/api/stacks")
def api_stacks():
    return jsonify(_body())


@bp.route("/api/stacks/create", methods=["POST"])
def api_create():
    def go(ed, p):
        st = catalog.stack_photos(ed.LIBRARY_ROOT, ed._known_ids(p.get("paths")), p.get("collapsed", True))
        return jsonify({**_body(), "stack": st, "message": f"Stacked {len(st['photos'])} photos"})
    return _run(go)


@bp.route("/api/stacks/remove", methods=["POST"])
def api_remove():
    def go(ed, p):
        n = catalog.unstack(ed.LIBRARY_ROOT, ed._known_ids(p.get("paths")))
        msg = f"Unstacked {n} stack{'' if n == 1 else 's'}" if n else "Nothing was stacked"
        return jsonify({**_body(), "message": msg})
    return _run(go)


@bp.route("/api/stacks/state", methods=["POST"])
def api_state():
    def go(ed, p):
        ids = p.get("ids")
        if ids is None and p.get("paths") is not None:
            wanted = set(ed._known_ids(p["paths"]))
            ids = [s["id"] for s in catalog.list_stacks(ed.LIBRARY_ROOT) if wanted & set(s["photos"])]
            if not ids:
                return jsonify({**_body(), "message": "Not in a stack", "changed": 0})
        if ids is not None and not isinstance(ids, list):
            raise ValueError("ids must be a list")
        n = catalog.set_stack_state(ed.LIBRARY_ROOT, ids, bool(p.get("collapsed", True)), p.get("top"))
        return jsonify({**_body(), "changed": n})
    return _run(go)


@bp.route("/api/stacks/auto", methods=["POST"])
def api_auto():
    import cull
    import cull_api

    def go(ed, p):
        try:
            window = cull_api._num(p, "window", cull.BURST_WINDOW_S, 0, 3600)
            loose = int(cull_api._num(p, "hash", cull.LOOSE_HASH, 0, 40))
            items = cull_api._scope(p)
        except cull_api.BadRequest as exc:
            return jsonify({"error": str(exc)}), exc.status
        feats, missing = cull_api._features_for(items)
        rows = [{"path": i["path"], "name": i["name"], "t": cull_api._item_time(i)} for i in items]
        res = cull.suggest(rows, feats, None, window=window, loose=loose)
        groups = []
        for g in res["groups"]:
            if len(g["paths"]) > 1:
                best = g["best"]
                groups.append([best] + [x for x in g["paths"] if x != best])
        made = catalog.auto_stack(ed.LIBRARY_ROOT, groups, bool(p.get("collapsed", True)))
        msg = f"Created {len(made)} stack{'' if len(made) == 1 else 's'}"
        if missing:
            msg += f"; {len(missing)} photos not analysed yet (run Smart cull analysis)"
        return jsonify({**_body(), "created": len(made), "photos": sum(len(s["photos"]) for s in made),
                        "missing": len(missing), "message": msg})
    return _run(go)
