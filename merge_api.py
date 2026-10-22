"""
HTTP side of the Library's "Merge to HDR" and "Merge to Panorama".

Thin over merge.py (the same code `photo-room merge` runs). A merge is minutes
of CPU and a lot of memory, so jobs run one at a time on a background thread
and are polled (the export dialog's pattern); the new TIFF appears in the Library
through the change feed once the index rescans. Sources are only read.
"""
from __future__ import annotations

import threading
import uuid
from pathlib import Path

from flask import Blueprint, jsonify

import merge

bp = Blueprint("merge_api", __name__)

MAX_JOBS_KEPT = 20
MAX_QUEUED = 5
_jobs: dict[str, dict] = {}
_lock = threading.Lock()
_run_one = threading.Semaphore(1)       # one heavy merge at a time


def _ed():
    import editor  # deferred: editor registers this blueprint

    return editor


def _public(job: dict) -> dict:
    return {k: job[k] for k in ("id", "kind", "state", "progress", "message", "sources",
                                "result", "error")}


def _run(job: dict) -> None:
    with _run_one:
        with _lock:
            if job["_cancel"]:
                job.update(state="cancelled", message="Cancelled")
                return
            job["state"] = "running"

        def progress(frac, msg):
            with _lock:
                job["progress"], job["message"] = round(frac, 3), msg

        try:
            result = merge.execute(job["kind"], job["paths"], None, progress=progress,
                               cancelled=lambda: job["_cancel"], **job["options"])
            _rescan()
            with _lock:
                job.update(state="done", progress=1.0, message="Done", result=_rel_result(result))
        except merge.MergeCancelled:
            with _lock:
                job.update(state="cancelled", message="Cancelled")
        except Exception as exc:
            with _lock:
                job.update(state="error", error=str(exc) or exc.__class__.__name__, message="Failed")


def _rel_result(result: dict) -> dict:
    out = dict(result)
    try:
        root = _ed().LIBRARY_ROOT.resolve()
        out["path"] = Path(result["output"]).resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        out["path"] = None
    return out


def _rescan() -> None:
    try:
        _ed()._idx(fresh=False).scan()      # the change feed then tells open Library grids
    except Exception:
        pass


@bp.route("/api/merge", methods=["POST"])
def api_merge():
    ed = _ed()
    try:
        payload = ed._json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    kind = payload.get("kind")
    if kind not in ("hdr", "pano"):
        return jsonify({"error": "kind must be hdr or pano"}), 400
    raw = payload.get("paths")
    if not isinstance(raw, list) or len(raw) < 2:
        return jsonify({"error": "Select at least 2 photos to merge"}), 400
    if len(raw) > 40:
        return jsonify({"error": "Too many photos for one merge (40 at most)"}), 400
    try:
        paths = [ed._safe_path(p) for p in raw]
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if len(set(paths)) != len(paths):
        return jsonify({"error": "The same photo is selected twice"}), 400
    opts = payload.get("options") or {}
    allowed = ("align", "half", "float_output") if kind == "hdr" else ("scans", "half")
    options = {k: bool(opts[k]) for k in allowed if k in opts}
    with _lock:
        if sum(j["state"] in ("queued", "running") for j in _jobs.values()) >= MAX_QUEUED:
            return jsonify({"error": "Too many merges waiting; let one finish first"}), 409
        job = {"id": uuid.uuid4().hex, "kind": kind, "state": "queued", "progress": 0.0,
               "message": "Waiting", "sources": [p.name for p in paths], "result": None,
               "error": None, "paths": paths, "options": options, "_cancel": False}
        _jobs[job["id"]] = job
        while len(_jobs) > MAX_JOBS_KEPT:
            old = next((k for k, j in _jobs.items() if j["state"] not in ("queued", "running")), None)
            if old is None:
                break
            _jobs.pop(old)
        body = _public(job)
    threading.Thread(target=_run, args=(job,), daemon=True, name="merge").start()
    return jsonify(body), 202


@bp.route("/api/merge/status/<job_id>")
def api_merge_status(job_id: str):
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Unknown merge"}), 404
        return jsonify(_public(job))


@bp.route("/api/merge/cancel/<job_id>", methods=["POST"])
def api_merge_cancel(job_id: str):
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Unknown merge"}), 404
        if job["state"] not in ("queued", "running"):
            return jsonify({"error": f"This merge is already {job['state']}"}), 409
        job["_cancel"] = True
        return jsonify(_public(job))
