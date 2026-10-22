"""
HTTP side of the Timelapse dialog. Thin: frames are chosen here and everything
else is timelapse.build(), the code `photo-room timelapse` runs. One timelapse
renders at a time on a background thread and is polled for progress.
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

from flask import Blueprint, jsonify

import export_api
import timelapse

bp = Blueprint("timelapse_api", __name__)

_jobs: dict[str, dict] = {}
_lock = threading.Lock()
MAX_JOBS_KEPT = 10


def _ed():
    import editor

    return editor


def _frames(payload: dict) -> list[Path]:
    ed = _ed()
    scope = payload.get("scope", "selected")
    if scope == "selected":
        raw = list(payload.get("paths") or [])
        if not raw:
            raise ValueError("No photos are selected")
        return [ed._safe_path(p) for p in raw]
    if scope == "collection":
        return timelapse.collection_paths(ed.LIBRARY_ROOT, str(payload.get("collection") or ""))
    if scope == "folder":
        return timelapse.folder_paths(ed.LIBRARY_ROOT)
    raise ValueError("scope must be selected, collection or folder")


def _output(payload: dict) -> Path:
    name = Path(str(payload.get("name") or "timelapse.mp4")).name
    if not Path(name).suffix:
        name += ".mp4"
    return export_api._destination(payload) / name


def _options(payload: dict) -> timelapse.TimelapseOptions:
    ed = _ed()
    ref = payload.get("reference")
    return timelapse.TimelapseOptions(
        size=timelapse.parse_size(payload.get("size") or "1920x1080"),
        fps=float(payload.get("fps", 24)),
        reference=ed._safe_path(ref) if ref else None,
        per_frame=bool(payload.get("per_frame")),
        deflicker=bool(payload.get("deflicker")),
        stabilise=bool(payload.get("stabilise")),
        frames_only=bool(payload.get("frames_only")),
        overwrite=bool(payload.get("overwrite")),
    ).validate()


def _public(job: dict) -> dict:
    return {k: job[k] for k in ("id", "state", "done", "total", "label", "report", "error")}


def _run(job: dict, paths, out, opts) -> None:
    def progress(done, total, label):
        with _lock:
            job.update(done=done, total=total, label=label)

    try:
        report = timelapse.build(paths, out, opts, progress=progress,
                                 cancel=lambda: bool(job.get("_cancel")))
        with _lock:
            job.update(state="done", report=report, error=report.get("error"), label="")
    except timelapse.Cancelled:
        with _lock:
            job.update(state="cancelled", label="")
    except Exception as exc:
        with _lock:
            job.update(state="error", error=_ed().describe_error(exc), label="")


@bp.route("/api/timelapse/defaults")
def api_defaults():
    return jsonify({"ffmpeg": timelapse.ffmpeg_path() is not None,
                    "hint": timelapse.install_hint(), "dest": export_api.default_destination()})


@bp.route("/api/timelapse", methods=["POST"])
def api_start():
    ed = _ed()
    try:
        payload = ed._json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    try:
        paths = _frames(payload)
        if len(paths) < 2:
            raise ValueError("A timelapse needs at least two photos")
        out = _output(payload)
        opts = _options(payload)
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc.args[0] if isinstance(exc, KeyError) else exc)}), 400
    with _lock:
        if any(j["state"] == "running" for j in _jobs.values()):
            return jsonify({"error": "A timelapse is already rendering"}), 409
        job = {"id": uuid.uuid4().hex, "state": "running", "done": 0, "total": len(paths),
               "label": "Starting", "report": None, "error": None}
        _jobs[job["id"]] = job
        while len(_jobs) > MAX_JOBS_KEPT:
            _jobs.pop(next(iter(_jobs)))
        body = _public(job)
    threading.Thread(target=_run, args=(job, paths, out, opts), daemon=True).start()
    return jsonify(body), 202


@bp.route("/api/timelapse/status/<job_id>")
def api_status(job_id: str):
    with _lock:
        job = _jobs.get(job_id)
        return (jsonify(_public(job)) if job else (jsonify({"error": "Unknown timelapse"}), 404))


@bp.route("/api/timelapse/cancel/<job_id>", methods=["POST"])
def api_cancel(job_id: str):
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Unknown timelapse"}), 404
        job["_cancel"] = True
        return jsonify(_public(job))
