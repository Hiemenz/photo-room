"""
HTTP side of smart culling and Auto edit (cull.py does the analysis, cull_store.py
the learning). Registered by editor.py.

Analysis runs in background jobs (``/api/cull/job`` is polled), never in a request.
Suggestions never change anything: only ``/api/cull/apply`` and
``/api/cull/autoedit/apply`` write, each as one batch that ``/undo`` reverses.

    POST /api/cull/analyze        {scope, paths?, collection?}      -> {job}
    GET  /api/cull/job?id=        progress / result of a job
    POST /api/cull/suggest        {scope, ..., window?, hash?}       -> groups + suggestions
    POST /api/cull/apply          {choices: [{path, action, suggested, ...}]}
    POST /api/cull/undo           {batch}
    GET  /api/cull/stats          accuracy over time, model summaries
    GET  /api/cull/export         everything learned, as a JSON download
    POST /api/cull/reset          forget learned decisions and offsets
    POST /api/cull/clear          also drop the feature cache and undo records
    POST /api/cull/autoedit/preview  {paths}  before / after images + the changes
    POST /api/cull/autoedit/apply    {paths}  -> {job}   one history step per photo
    POST /api/cull/autoedit/undo     {batch}
"""
from __future__ import annotations

import base64
import io
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Blueprint, Response, jsonify, request
from PIL import Image

import cull
import cull_store
import history
import thumbs

bp = Blueprint("cull_api", __name__)

ANALYSIS_THREADS = 3
AUTO_TONE_KEYS = ("exposure", "contrast", "highlights", "shadows", "whites", "blacks", "vibrance")
PREVIEW_EDGE = 560
MAX_PREVIEW_PHOTOS = 6


def _ed():
    import editor           # late: editor registers this blueprint

    return editor


class BadRequest(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@bp.errorhandler(BadRequest)
def _bad(exc):
    return jsonify({"error": str(exc)}), exc.status


def _payload() -> dict:
    try:
        return _ed()._json_body()
    except ValueError as exc:
        raise BadRequest(str(exc), 415)


# ---------------------------------------------------------------------------
# Store and scope
# ---------------------------------------------------------------------------

_stores: dict[str, cull_store.CullStore] = {}
_stores_lock = threading.Lock()


def store() -> cull_store.CullStore:
    root = _ed().LIBRARY_ROOT
    with _stores_lock:
        key = str(Path(root).resolve())
        if key not in _stores:
            _stores[key] = cull_store.CullStore(root)
        return _stores[key]


def _scope(payload: dict) -> list[dict]:
    """The index records the request is about: the whole library, a collection or a path list."""
    import catalog

    ed = _ed()
    idx = ed._idx()
    scope = payload.get("scope", "all")
    items = idx.items()
    if scope == "selection":
        wanted = payload.get("paths")
        if not isinstance(wanted, list) or not wanted:
            raise BadRequest("paths is required for the selection scope")
        keep = set(str(p) for p in wanted)
        items = [i for i in items if i["path"] in keep]
    elif scope == "collection":
        try:
            col = catalog.get_collection(ed.LIBRARY_ROOT, str(payload.get("collection", "")))
        except KeyError as exc:
            raise BadRequest(exc.args[0], 404)
        items = catalog.collection_items(col, items)
    elif scope != "all":
        raise BadRequest("scope must be all, collection or selection")
    return [i for i in items if not i.get("virtual")]


def _fkey(path: str) -> str:
    return thumbs._cache_key(_ed().LIBRARY_ROOT / path)


def _item_time(item: dict) -> float | None:
    t = cull.parse_time(item.get("date"))
    if t is not None:
        return t
    try:
        return (_ed().LIBRARY_ROOT / item["path"]).stat().st_mtime
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
MAX_JOBS = 20


def _start_job(kind: str, total: int, fn) -> str:
    """Run ``fn(job)`` on a thread; ``job`` is the shared progress dict."""
    job = {"id": uuid.uuid4().hex[:10], "kind": kind, "state": "running", "total": total,
           "done": 0, "errors": [], "result": None, "started": time.time()}
    with _jobs_lock:
        _jobs[job["id"]] = job
        for old in [j for j in _jobs.values() if j["state"] != "running"][:-MAX_JOBS]:
            _jobs.pop(old["id"], None)

    def run():
        try:
            job["result"] = fn(job)
            job["state"] = "done"
        except Exception as exc:                       # reported, never raised into a request
            job["state"] = "error"
            job["errors"].append(str(exc))

    threading.Thread(target=run, daemon=True, name=f"cull-{kind}").start()
    return job["id"]


@bp.route("/api/cull/job")
def api_job():
    with _jobs_lock:
        job = _jobs.get(request.args.get("id", ""))
    if job is None:
        return jsonify({"error": "Unknown job"}), 404
    return jsonify({k: job[k] for k in ("id", "kind", "state", "total", "done", "result")}
                   | {"errors": job["errors"][-5:]})


# ---------------------------------------------------------------------------
# Analysis and suggestions
# ---------------------------------------------------------------------------

def _analyse_one(path: str) -> dict:
    ed = _ed()
    data = ed._thumb_pool().get_now(ed.LIBRARY_ROOT / path, size=1024)
    return cull.extract(data)


def _features_for(items: list[dict]) -> tuple[dict[str, dict], list[str]]:
    """(cached features, paths still needing analysis) for these records."""
    keyed = {i["path"]: _fkey(i["path"]) for i in items}
    have = store().get_features(keyed)
    return have, [p for p in keyed if p not in have]


@bp.route("/api/cull/analyze", methods=["POST"])
def api_analyze():
    payload = _payload()
    items = _scope(payload)
    _, missing = _features_for(items)
    st = store()
    ed = _ed()

    def work(job):
        def one(path):
            try:
                st.put_features(path, _fkey(path), _analyse_one(path))
            except Exception as exc:
                job["errors"].append(f"{path}: {exc}")
            job["done"] += 1
        with ThreadPoolExecutor(ANALYSIS_THREADS) as pool:
            list(pool.map(one, missing))
        return {"analysed": len(missing), "photos": len(items)}

    if not missing:
        return jsonify({"job": _start_job("analyze", 0, lambda job: {"analysed": 0, "photos": len(items)}),
                        "cached": len(items), "missing": 0})
    return jsonify({"job": _start_job("analyze", len(missing), work),
                    "cached": len(items) - len(missing), "missing": len(missing)})


def _num(payload: dict, key: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(payload.get(key, default))
    except (TypeError, ValueError):
        raise BadRequest(f"{key} must be a number")
    if not lo <= v <= hi:
        raise BadRequest(f"{key} must be between {lo:g} and {hi:g}")
    return v


@bp.route("/api/cull/suggest", methods=["POST"])
def api_suggest():
    payload = _payload()
    window = _num(payload, "window", cull.BURST_WINDOW_S, 0, 3600)
    loose = int(_num(payload, "hash", cull.LOOSE_HASH, 0, 40))
    items = _scope(payload)
    feats, missing = _features_for(items)
    st = store()
    st.learn_offsets(*_history_probes())
    model = st.pick_model()
    rows = [{"path": i["path"], "name": i["name"], "t": _item_time(i)} for i in items]
    res = cull.suggest(rows, feats, model if model.confidence > 0 or model.reject_w is not None else None,
                       window=window, loose=loose)
    by_path = {i["path"]: i for i in items}
    for g in res["groups"]:
        g["photos"] = []
        for p in g["paths"]:
            ph = res["photos"][p]
            it = by_path[p]
            g["photos"].append({
                "path": p, "name": it["name"], "flag": it.get("flag", "none"), "rating": it.get("rating", 0),
                "date": it.get("date", ""), "score": ph["score"], "action": ph["action"],
                "reasons": ph["reasons"], "vec": ph["vec"], "best": p == g["best"],
                "faces": feats[p].get("faces", 0), "focus": feats[p].get("focus"),
                "size": [feats[p]["w"], feats[p]["h"]],
            })
    show_all = bool(payload.get("singles", True))
    groups = res["groups"] if show_all else [g for g in res["groups"] if len(g["paths"]) > 1]
    return jsonify({
        "groups": [{"id": g["id"], "best": g["best"], "photos": g["photos"]} for g in groups],
        "missing": len(missing), "photos": len(items),
        "model": model.info(), "settings": {"window": window, "hash": loose},
    })


# ---------------------------------------------------------------------------
# Applying suggestions (an explicit, undoable batch)
# ---------------------------------------------------------------------------

def _flag_state(rec: dict) -> dict:
    return {"rating": rec.get("rating", 0), "pick": rec.get("flag") == "pick"}


@bp.route("/api/cull/apply", methods=["POST"])
def api_apply():
    """
    Write the user's final choices. ``choices``: ``{path, action, suggested, group,
    gsize, vec}``; action pick / reject / none. Only photos whose flag would change are
    touched; every reviewed photo is logged as a decision for learning.
    """
    payload = _payload()
    ed = _ed()
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise BadRequest("choices is required")
    if len(choices) > 50000:
        raise BadRequest("too many choices in one batch")
    idx = ed._idx()
    prior, changes, log = {}, {"pick": [], "reject": [], "none": []}, []
    for c in choices:
        if not isinstance(c, dict) or c.get("action") not in ("pick", "reject", "none") \
                or c.get("suggested") not in ("pick", "reject", "none"):
            raise BadRequest("each choice needs path, action and suggested (pick | reject | none)")
        try:
            path = ed._safe_path(str(c.get("path")))
        except ValueError as exc:
            raise BadRequest(str(exc))
        rel = ed._rel(path)
        rec = idx.get(rel)
        if rec is None:
            continue
        vec = c.get("vec")
        if isinstance(vec, list) and len(vec) == len(cull.FEATURE_NAMES):
            log.append({"path": rel, "group": c.get("group"), "gsize": c.get("gsize", 1),
                        "suggested": c["suggested"], "final": c["action"],
                        "vec": [float(x) for x in vec]})
        if rec.get("flag", "none") == c["action"]:
            continue
        prior[rel] = _flag_state(rec)
        changes[c["action"]].append(path)
    for flag, paths in changes.items():
        if paths:
            ed._flag_paths(paths, flag)
    st = store()
    batch = st.new_batch("flags", {"prior": prior})
    logged = st.log_decisions(log, batch)
    changed = sum(len(v) for v in changes.values())
    return jsonify({"ok": True, "batch": batch, "changed": changed, "logged": logged,
                    "counts": {k: len(v) for k, v in changes.items()},
                    "message": f"Applied {changed} flag change(s); undo is available"})


@bp.route("/api/cull/undo", methods=["POST"])
def api_undo():
    payload = _payload()
    st = store()
    b = st.get_batch(str(payload.get("batch", "")))
    if b is None or b["kind"] != "flags":
        raise BadRequest("Unknown batch", 404)
    if b["undone"]:
        raise BadRequest("That batch was already undone", 409)
    idx = _ed()._idx()
    idx.set_fields({rel: dict(state) for rel, state in b["data"]["prior"].items()})
    st.mark_undone(b["id"])
    st.drop_batch_decisions(b["id"])
    return jsonify({"ok": True, "restored": len(b["data"]["prior"]),
                    "message": f"Restored {len(b['data']['prior'])} photo(s)"})


# ---------------------------------------------------------------------------
# Stats and data controls
# ---------------------------------------------------------------------------

def _history_probes():
    """(current_of, mtime_of) for CullStore.learn_offsets, reading each photo's history sidecar."""
    root = _ed().LIBRARY_ROOT

    def current_of(rel):
        data = history.load(root / rel)
        if not 0 <= data["index"] < len(data["entries"]):
            return None
        e = data["entries"][data["index"]]
        return e["id"], e["adjustments"]

    def mtime_of(rel):
        try:
            return history.history_path(root / rel).stat().st_mtime
        except OSError:
            return None
    return current_of, mtime_of


@bp.route("/api/cull/stats")
def api_stats():
    st = store()
    st.learn_offsets(*_history_probes())
    return jsonify({"accuracy": st.accuracy(), "pick_model": st.pick_model().info(),
                    "offset_model": st.offset_model().info(), "db": cull_store.DB_NAME})


@bp.route("/api/cull/export")
def api_export():
    st = store()
    st.learn_offsets(*_history_probes())
    return Response(json.dumps(st.export(), indent=1), mimetype="application/json", headers={
        "Content-Disposition": 'attachment; filename="photo-room-cull-export.json"'})


@bp.route("/api/cull/reset", methods=["POST"])
def api_reset():
    _payload()
    store().reset_learning()
    return jsonify({"ok": True, "message": "Learned decisions and style offsets were deleted"})


@bp.route("/api/cull/clear", methods=["POST"])
def api_clear():
    _payload()
    store().clear_all()
    return jsonify({"ok": True, "message": "All cull data for this library was deleted"})


# ---------------------------------------------------------------------------
# Auto edit: auto tone + auto white balance + learned offsets
# ---------------------------------------------------------------------------

def _auto_for(rel: str) -> dict:
    """
    The Auto edit proposal for one photo: ``{before, after, applied, changes, feats}``.

    Starts from what the photo currently has (saved edits, else the editor's own
    proposal), replaces the tone sliders with auto tone and Temp/Tint with auto white
    balance, then shifts them by what this user has habitually changed afterwards.
    """
    import analysis
    import develop_api as dv
    import cull_store as cs

    ed = _ed()
    path = ed._safe_path(rel)
    base, baseline = ed._load_base_render(path)
    adj = ed._current_adjustments(path)
    adj = ed.resolve_edit_version(adj, baseline)
    if int(adj.get("edit_version", 2) or 2) < 2:
        raise BadRequest(f"{path.name} uses the old colour maths; use Upgrade edit first", 409)
    adj.setdefault("color_temp", baseline)
    wb_in = {k: adj[k] for k in ("color_temp", "tint") if k in adj}
    tone = analysis.auto_tone(ed._stage_tone(dv._pre_wb(base, baseline, adj), baseline, wb_in))["settings"]
    wb = dv._auto_wb(path, adj, base, baseline)
    auto = {**adj, **tone, "color_temp": wb["color_temp"], "tint": wb["tint"]}

    st = store()
    feats = None
    try:
        feats = st.get_features({rel: _fkey(rel)}).get(rel)
    except Exception:
        pass
    offsets = st.offset_model().predict(feats)
    final = cs.apply_offsets(auto, offsets) if offsets else auto
    keys = list(AUTO_TONE_KEYS) + ["color_temp", "tint"] + [k for k in offsets if k != "temp_ratio"]
    changes = {}
    for k in dict.fromkeys(keys):
        if k in final and final.get(k) != adj.get(k):
            changes[k] = {"from": adj.get(k), "to": final[k]}
    return {"path": path, "base": base, "baseline": baseline, "before": adj, "after": final,
            "changes": changes, "offsets": offsets,
            "feats": {k: feats[k] for k in ("median", "range") if feats and k in feats} or None}


def _jpeg_uri(arr) -> str:
    img = Image.fromarray(arr)
    img.thumbnail((PREVIEW_EDGE, PREVIEW_EDGE))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _paths_arg(payload: dict, limit: int) -> list[str]:
    paths = payload.get("paths")
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
        raise BadRequest("paths must be a non-empty list")
    paths = list(dict.fromkeys(paths))
    if len(paths) > limit:
        raise BadRequest(f"at most {limit} photos")
    return paths


@bp.route("/api/cull/autoedit/preview", methods=["POST"])
def api_autoedit_preview():
    """Before and after renders (small) and the slider changes. Writes nothing."""
    import develop_api as dv

    payload = _payload()
    paths = _paths_arg(payload, MAX_PREVIEW_PHOTOS)
    store().learn_offsets(*_history_probes())
    out = []
    for rel in paths:
        try:
            r = _auto_for(rel)
        except ValueError as exc:
            raise BadRequest(str(exc))
        out.append({
            "path": rel, "changes": r["changes"], "learned": r["offsets"],
            "before": _jpeg_uri(_u8(dv._render(r["path"], r["before"], r["base"], r["baseline"]))),
            "after": _jpeg_uri(_u8(dv._render(r["path"], r["after"], r["base"], r["baseline"]))),
        })
    return jsonify({"photos": out, "model": store().offset_model().info()})


def _u8(arr):
    import numpy as np

    return arr if arr.dtype == np.uint8 else (np.clip(arr, 0, 1) * 255.0 + 0.5).astype(np.uint8)


def _apply_auto(rel: str) -> dict:
    """Write Auto edit to one photo as one history step; returns what undo needs."""
    ed = _ed()
    r = _auto_for(rel)
    path, after = r["path"], r["after"]
    full = ed.complete_adjustments(after, r["baseline"])
    full["_baseline_temp"] = r["baseline"]
    prior_hist = history.load(path)
    prior_saved = ed.read_saved_adjustments(path)
    history.set_original(path, ed.complete_adjustments(r["before"], r["baseline"]))
    hist, recorded = history.record(path, "Auto edit", full)
    frame_size, source_size = ed._save_frames(path, full)
    ed.save_adjustments(path, full, frame_size=frame_size, source_size=source_size,
                        sidecar=ed.WRITE_SIDECARS)
    entry_id = hist["entries"][hist["index"]]["id"]
    store().record_autoedit(rel, entry_id, {k: full.get(k) for k in
                                            (*AUTO_TONE_KEYS, "color_temp", "tint", "saturation", "clarity")
                                            if k in full}, r["feats"])
    return {"path": rel, "recorded": recorded, "entry_id": entry_id,
            "prior_index": prior_hist["index"], "prior_entry": (prior_hist["entries"][prior_hist["index"]]["id"]
                                                              if prior_hist["entries"] and prior_hist["index"] >= 0 else None),
            "prior_saved": prior_saved is not None}


@bp.route("/api/cull/autoedit/apply", methods=["POST"])
def api_autoedit_apply():
    payload = _payload()
    paths = _paths_arg(payload, 5000)
    ed = _ed()
    for rel in paths:
        try:
            ed._safe_path(rel)
        except ValueError as exc:
            raise BadRequest(str(exc))
    st = store()
    st.learn_offsets(*_history_probes())

    def work(job):
        done, failed = [], []
        for rel in paths:
            try:
                done.append(_apply_auto(rel))
            except Exception as exc:
                failed.append({"path": rel, "error": str(exc)})
            job["done"] += 1
        batch = st.new_batch("autoedit", {"done": done}) if done else None
        return {"batch": batch, "applied": len(done), "failed": failed}

    return jsonify({"job": _start_job("autoedit", len(paths), work)})


@bp.route("/api/cull/autoedit/undo", methods=["POST"])
def api_autoedit_undo():
    """Step each photo back to the history step it was on before Auto edit."""
    payload = _payload()
    st = store()
    b = st.get_batch(str(payload.get("batch", "")))
    if b is None or b["kind"] != "autoedit":
        raise BadRequest("Unknown batch", 404)
    if b["undone"]:
        raise BadRequest("That batch was already undone", 409)
    ed = _ed()
    restored, failed = 0, []
    for item in b["data"]["done"]:
        rel = item["path"]
        try:
            path = ed._safe_path(rel)
            if item["prior_entry"]:
                _, adj = history.jump(path, item["prior_entry"])
            else:
                data = history.load(path)
                if data["original"] is None:
                    raise ValueError("no earlier state recorded")
                adj = data["original"]
                history.record(path, "Undo Auto edit", adj)
            _, baseline = ed._load_base_render(path)
            full = ed.complete_adjustments(adj, baseline)
            fs, ss = ed._save_frames(path, full)
            ed.save_adjustments(path, full, frame_size=fs, source_size=ss, sidecar=ed.WRITE_SIDECARS)
            st.restore_autoedit(rel)
            restored += 1
        except Exception as exc:
            failed.append({"path": rel, "error": str(exc)})
    st.mark_undone(b["id"])
    return jsonify({"ok": not failed, "restored": restored, "failed": failed,
                    "message": f"Restored {restored} photo(s) to their state before Auto edit"})
