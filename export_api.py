"""
HTTP side of the editor's Export dialog.

Thin on purpose: every pixel and tag goes through exporter.py, the same code
`process.py export` runs, so the dialog and the command line cannot drift.
Batches run on one background worker and are polled for progress, because a
full-resolution raw render takes seconds per photo. Exports are a queue: a
second batch started while one runs waits its turn, any batch can be
cancelled, every photo gets a result row (written file or error), and the
queue is saved to disk after each photo so a batch interrupted by an editor
restart can be resumed.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, request

import export_finish
import contactsheet_pdf
import export_presets
import exporter
import postexport
from exporter import ExportOptions

bp = Blueprint("export_api", __name__)

SCOPES = ("current", "selected", "folder")
MAX_JOBS_KEPT = 20
MAX_QUEUED = 10

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def _ed():
    import editor  # deferred: editor imports this module

    return editor


def default_destination() -> str:
    # Outside the library: exports inside it would show up as photos.
    return str(Path.home() / "Pictures" / "photo-room-export")


def _options(payload: dict) -> ExportOptions:
    return export_presets.options_from_payload(payload)


def _sources(payload: dict) -> list[tuple[Path, dict | None]]:
    """
    (photo, virtual copy or None) in the chosen scope, in a stable order.

    Like `process.py export <folder>`, the whole-folder scope also exports each
    photo's virtual copies as their own files.
    """
    ed = _ed()
    scope = payload.get("scope", "current")
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
    if scope == "current":
        if not payload.get("path"):
            raise ValueError("No photo is open")
        raw = [payload["path"]]
    elif scope == "selected":
        raw = list(payload.get("paths") or [])
        if not raw:
            raise ValueError("No photos are selected")
    else:
        import catalog

        root, jobs = ed.LIBRARY_ROOT.resolve(), []
        for p in ed.list_photos(ed.LIBRARY_ROOT):
            jobs.append((p, None))
            rel = p.resolve().relative_to(root).as_posix()
            jobs.extend((p, v) for v in catalog.virtuals_of(root, rel))
        return jobs
    seen, out = set(), []
    for item in raw:
        p = ed._safe_path(item)
        if p not in seen:
            seen.add(p)
            out.append((p, None))
    return out


SAME_AS_ORIGINAL = "(next to the originals)"


def _destination(payload: dict) -> Path:
    """The destination folder; with a sub-folder chosen, the sub-folder inside it."""
    base = _base_destination(payload)
    dest_cfg = export_presets.destination_settings(payload)
    if dest_cfg["dest_mode"] == "subfolder":
        return base / dest_cfg["subfolder"]
    return base


def _base_destination(payload: dict) -> Path:
    raw = str(payload.get("dest") or "").strip()
    if not raw:
        raise ValueError("Choose a destination folder")
    if "\x00" in raw:
        raise ValueError("The destination is not a valid folder name")
    dest = Path(raw).expanduser()
    if not dest.is_absolute():
        # A relative name lives under the default export folder, never in
        # the library.
        dest = Path(default_destination()) / dest
    # Normalise ("..", symlinks) before judging where it really points.
    dest = Path(os.path.realpath(dest))
    if dest.exists() and not dest.is_dir():
        raise ValueError(f"{dest} is a file, not a folder")
    root = Path(os.path.realpath(_ed().LIBRARY_ROOT))
    if not payload.get("allow_library") and (dest == root or root in dest.parents):
        raise ValueError(
            f"{dest} is inside the photo library ({root}). Exports there would "
            "mix with your raw files; choose a folder outside it."
        )
    return dest


def _require_exiftool() -> None:
    if not exporter.exiftool_available():
        raise RuntimeError(
            "exiftool was not found. Exporting reads each photo's saved edits and "
            "copies its metadata with exiftool. Install it (Linux: apt install "
            "libimage-exiftool-perl; macOS: brew install exiftool) and try again."
        )


def _out_dir(job: dict, source: Path) -> Path:
    return source.parent if job.get("dest_mode") == "original" else Path(job["dest"])


def _source_size(path: Path) -> tuple[int, int] | None:
    try:
        if path.suffix.lower() in exporter.RAW_SUFFIXES:
            import rawpy

            with rawpy.imread(str(path)) as raw:
                return int(raw.sizes.width), int(raw.sizes.height)
        import imgload

        return imgload.oriented_size(path)
    except Exception:
        return None


def preview_names(sources: list[tuple[Path, dict | None]], opts: ExportOptions, dest: Path | None,
                  same_dir: bool = False) -> list[dict]:
    """What each file would be called, including -1/-2 suffixes for existing files.
    With ``same_dir`` each file lands beside its original instead of in ``dest``."""
    ext = exporter.FORMATS[opts.format]
    used: set[str] = set()
    out = []
    for seq, (src, virt) in enumerate(sources, start=1):
        size = _source_size(src) or (0, 0)
        if opts.long_edge and size[0] and max(size) > opts.long_edge:
            k = opts.long_edge / max(size)
            size = (max(1, round(size[0] * k)), max(1, round(size[1] * k)))
        name = exporter.output_name(src, opts.template, ext, seq, size,
                                    stem=exporter.virtual_stem(src, virt) if virt else None,
                                    text=opts.custom_text)
        stem, n, final = Path(name).stem, 0, name
        folder = src.parent if same_dir else dest
        key = lambda f: f"{folder}/{f}" if same_dir else f
        while key(final) in used or (folder is not None and (folder / final).exists()):
            n += 1
            final = f"{stem}-{n}{ext}"
        used.add(key(final))
        out.append({"source": src.name, "name": final})
    return out


@bp.route("/api/export/defaults")
def api_export_defaults():
    return jsonify({
        "dest": default_destination(),
        "exiftool": exporter.exiftool_available(),
        "colorspaces": list(exporter.COLOR_SPACES),
        "bit_depths": [8, 16],
        "formats": exporter.available_formats(),
        "dest_modes": list(exporter.DEST_MODES),
        "has_previous": export_presets.load_last() is not None,
        "contact_sheet": {"pages": list(contactsheet_pdf.PAGES), "max_columns": contactsheet_pdf.MAX_COLUMNS,
                          "max_photos": contactsheet_pdf.MAX_PHOTOS},
        "sixteen_bit_formats": list(exporter.SIXTEEN_BIT_FORMATS),
        "metadata_modes": list(exporter.METADATA_MODES),
        "sharpen": {"for": list(export_finish.MEDIA), "amount": list(export_finish.AMOUNTS)},
        "watermark_positions": list(export_finish.POSITIONS),
        "fonts": export_finish.list_fonts(),
        "tokens": ["{name}", "{date}", "{seq}", "{seq:04d}", "{width}", "{height}",
                   "{rating}", "{camera}", "{keyword}", "{text}"],
    })


@bp.route("/api/export/preview", methods=["POST"])
def api_export_preview():
    ed = _ed()
    try:
        payload = ed._json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    try:
        opts = _options(payload)
        sources = _sources(payload)
        if not sources:
            raise ValueError("No photos to export")
        same_dir = payload.get("dest_mode") == "original"
        try:
            dest = None if same_dir else _destination(payload)
        except ValueError:
            dest = None
        names = preview_names(sources[:50], opts, dest, same_dir)  # a preview needs only a few
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({
        "names": names,
        "count": len(sources),
        "approximate": any(t in opts.template for t in ("{width", "{height")),
    })


def queue_file() -> Path:
    return export_presets.config_dir() / "export_queue.json"


_PRIVATE = ("_cancel",)
_loaded_for: Path | None = None
_worker: threading.Thread | None = None


def _persist() -> None:
    """Save the queue (call with ``_lock`` held). Never lets a disk error break an export."""
    try:
        jobs = [{k: v for k, v in j.items() if k not in _PRIVATE}
                for j in _jobs.values() if "items" in j]
        export_presets.atomic_write_json(queue_file(), {"jobs": jobs})
    except OSError:
        pass


def _ensure_loaded() -> None:
    """Load jobs saved by an earlier run. Ones that were queued or running are
    marked ``interrupted`` (their thread died with the editor) and can be resumed."""
    global _loaded_for
    path = queue_file()
    with _lock:
        if _loaded_for == path:
            return
        _loaded_for = path
        try:
            data = json.loads(path.read_text())
            saved = data.get("jobs", []) if isinstance(data, dict) else []
        except (OSError, ValueError):
            return
        for job in saved:
            if not isinstance(job, dict) or "id" not in job or "items" not in job or job["id"] in _jobs:
                continue
            if job.get("state") in ("queued", "running"):
                job["state"] = "interrupted"
                job["current"] = None
            _jobs[job["id"]] = job
        if saved:
            _persist()


def _public(job: dict) -> dict:
    out = {k: job[k] for k in ("id", "state", "total", "done", "current", "dest",
                               "written", "failed")}
    out["post"] = job.get("post")
    out["items"] = [dict(i) for i in job.get("items", [])]
    out["queue_position"] = _queue_position(job)
    return out


def _queue_position(job: dict) -> int | None:
    if job.get("state") != "queued":
        return None
    waiting = [j["id"] for j in _jobs.values() if j.get("state") == "queued"]
    return waiting.index(job["id"]) + 1 if job["id"] in waiting else None


def _run_job(job: dict) -> None:
    opts = export_presets.options_from_payload(job["options"])
    overwrite = bool(job.get("overwrite"))
    try:
        import keywordlib
        drop = keywordlib.unexported(_ed().LIBRARY_ROOT)     # keywords flagged "do not include on export"
    except Exception:
        drop = frozenset()
    for item in job["items"]:
        with _lock:
            if job.get("_cancel"):
                break
            if item["status"] != "pending":
                continue
            job["current"] = Path(item["source"]).name
        try:
            written = exporter.export_photo(Path(item["source"]), _out_dir(job, Path(item["source"])), opts,
                                            seq=item["seq"], virtual=item["virtual"],
                                            overwrite=overwrite, drop_keywords=drop)
            with _lock:
                item.update(status="ok", output=str(written), error=None)
                job["written"].append(str(written))
        except Exception as exc:
            msg = ed_error(exc)
            with _lock:
                item.update(status="error", error=msg)
                job["failed"].append({"source": item["name"], "error": msg})
        with _lock:
            job["done"] += 1
            _persist()
    action = job.get("post_action")
    with _lock:
        cancelled = bool(job.get("_cancel"))
        written = list(job["written"])
    if action and written and not cancelled:
        job["post"] = postexport.run_action(action, written)    # outside the lock: it may take a while
    with _lock:
        if job.get("_cancel"):
            for item in job["items"]:
                if item["status"] == "pending":
                    item["status"] = "cancelled"
            job["state"] = "cancelled"
        else:
            job["state"] = "done"
        job["current"] = None
        _persist()


def _work() -> None:
    global _worker
    while True:
        with _lock:
            job = next((j for j in _jobs.values() if j.get("state") == "queued" and "items" in j), None)
            if job is None:
                _worker = None
                return
            job["state"] = "running"
            _persist()
        try:
            _run_job(job)
        except Exception as exc:  # e.g. options no longer valid: fail the batch, keep the queue moving
            with _lock:
                job["state"] = "done"
                job["current"] = None
                for item in job["items"]:
                    if item["status"] == "pending":
                        item.update(status="error", error=ed_error(exc))
                        job["failed"].append({"source": item["name"], "error": item["error"]})
                        job["done"] += 1
                _persist()


def _ensure_worker() -> None:
    global _worker
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_work, daemon=True)
        _worker.start()


def ed_error(exc: Exception) -> str:
    if isinstance(exc, FileNotFoundError) and "exiftool" in str(exc).lower():
        return "exiftool was not found; install it and try again"
    return _ed().describe_error(exc)


@bp.route("/api/export", methods=["POST"])
def api_export():
    try:
        payload = _ed()._json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    return _start_export(payload)


def _start_export(payload: dict):
    _ensure_loaded()
    try:
        opts = _options(payload)
        cfg = export_presets.destination_settings(payload)
        sources = _sources(payload)
        if not sources:
            raise ValueError("No photos to export")
        same_dir = cfg["dest_mode"] == "original"
        dest = None if same_dir else _destination(payload)
        preview_names(sources[:1], opts, None, same_dir)  # reject a bad template before starting
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        _require_exiftool()
    except RuntimeError as exc:
        return jsonify({"error": str(exc), "code": "exiftool_missing"}), 424

    if dest is not None:
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return jsonify({"error": f"Cannot create {dest}: {exc.strerror or exc}"}), 400

    settings = export_presets.clean_settings(payload)
    if same_dir:
        settings.pop("dest", None)
    else:
        settings["dest"] = str(_base_destination(payload))   # the chosen folder, before any sub-folder
    items = [{"source": str(src), "name": src.name,
              "virtual": virt, "seq": seq, "status": "pending", "output": None, "error": None}
             for seq, (src, virt) in enumerate(sources, start=1)]
    with _lock:
        if sum(1 for j in _jobs.values() if j.get("state") == "queued") >= MAX_QUEUED:
            return jsonify({"error": "The export queue is full; wait for a batch to finish"}), 409
        busy = any(j.get("state") == "running" for j in _jobs.values())
        job = {"id": uuid.uuid4().hex, "state": "queued", "total": len(items),
               "done": 0, "current": None, "dest": str(dest) if dest else SAME_AS_ORIGINAL,
               "dest_mode": cfg["dest_mode"], "post_action": cfg["post_action"], "post": None,
               "written": [], "failed": [],
               "items": items, "options": settings, "queued_behind_running": busy,
               "overwrite": bool(payload.get("overwrite"))}
        _jobs[job["id"]] = job
        while len(_jobs) > MAX_JOBS_KEPT:
            old = next((k for k, j in _jobs.items() if j.get("state") not in ("queued", "running")), None)
            if old is None:
                break
            _jobs.pop(old)
        _persist()
        body = _public(job)
    export_presets.save_last(settings)
    _ensure_worker()
    return jsonify(body), 202


@bp.route("/api/export/last")
def api_export_last():
    """The settings of the previous export (what "Export with Previous" repeats)."""
    return jsonify({"settings": export_presets.load_last()})


@bp.route("/api/export/previous", methods=["POST"])
def api_export_previous():
    """Export the chosen photos again with the previous export's settings."""
    try:
        payload = _ed()._json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    last = export_presets.load_last()
    if last is None:
        return jsonify({"error": "There is no previous export yet"}), 404
    merged = {**last, **{k: payload[k] for k in ("scope", "path", "paths") if k in payload}}
    return _start_export(merged)


@bp.route("/api/export/contact-sheet", methods=["POST"])
def api_export_contact_sheet():
    """A PDF contact sheet of the chosen photos, written to the destination folder."""
    ed = _ed()
    try:
        payload = ed._json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    try:
        payload.setdefault("scope", "selected")
        photos = [src for src, virt in _sources(payload) if virt is None]
        if not photos:
            raise ValueError("No photos for the contact sheet")
        dest = _base_destination(payload)
        columns = int(payload.get("columns") or 4)
        page = str(payload.get("page") or "a4")
        title = str(payload.get("title") or "")[:120]
        name = Path(str(payload.get("name") or "contact-sheet")).stem or "contact-sheet"
        if "\x00" in name:
            raise ValueError("bad file name")
        dest.mkdir(parents=True, exist_ok=True)
        out = exporter.claim_output(dest, name + ".pdf")
        try:
            contactsheet_pdf.write_pdf(
                out, photos, lambda p: ed._thumb_pool().get_now(p, size=320), columns=columns,
                page=page, landscape=bool(payload.get("landscape")), title=title,
                labels=payload.get("labels", True) is not False)
        except BaseException:
            out.unlink(missing_ok=True)
            raise
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400
    except OSError as exc:
        return jsonify({"error": f"Cannot write the contact sheet: {exc.strerror or exc}"}), 500
    return jsonify({"ok": True, "path": str(out), "count": len(photos)})


@bp.route("/api/export/status/<job_id>")
def api_export_status(job_id: str):
    _ensure_loaded()
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Unknown export"}), 404
        return jsonify(_public(job))


@bp.route("/api/export/jobs")
def api_export_jobs():
    """Every batch in the queue and recent history, oldest first."""
    _ensure_loaded()
    with _lock:
        return jsonify({"jobs": [_public(j) for j in _jobs.values() if "items" in j]})


@bp.route("/api/export/cancel/<job_id>", methods=["POST"])
def api_export_cancel(job_id: str):
    _ensure_loaded()
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Unknown export"}), 404
        state = job.get("state")
        if state in ("queued", "interrupted"):
            for item in job["items"]:
                if item["status"] == "pending":
                    item["status"] = "cancelled"
            job["state"] = "cancelled"
            _persist()
        elif state == "running":
            job["_cancel"] = True  # stops after the photo being rendered
        else:
            return jsonify({"error": f"This export is already {state}"}), 409
        return jsonify(_public(job))


@bp.route("/api/export/resume/<job_id>", methods=["POST"])
def api_export_resume(job_id: str):
    """Re-queue the photos an interrupted export had not finished."""
    _ensure_loaded()
    try:
        _require_exiftool()
    except RuntimeError as exc:
        return jsonify({"error": str(exc), "code": "exiftool_missing"}), 424
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Unknown export"}), 404
        if job.get("state") != "interrupted":
            return jsonify({"error": "Only an interrupted export can be resumed"}), 409
        job["state"] = "queued"
        job.pop("_cancel", None)
        _persist()
        body = _public(job)
    _ensure_worker()
    return jsonify(body), 202


# -- presets ---------------------------------------------------------------

@bp.route("/api/export/presets")
def api_export_presets():
    return jsonify({"presets": export_presets.list_presets()})


@bp.route("/api/export/presets", methods=["POST"])
def api_export_preset_save():
    try:
        payload = _ed()._json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    try:
        name = export_presets.save_preset(payload.get("name"), payload.get("settings") or {})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except OSError as exc:
        return jsonify({"error": f"Could not save the preset: {exc.strerror or exc}"}), 500
    return jsonify({"ok": True, "name": name, "presets": export_presets.list_presets()})


@bp.route("/api/export/presets/delete", methods=["POST"])
def api_export_preset_delete():
    try:
        payload = _ed()._json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    try:
        found = export_presets.delete_preset(str(payload.get("name") or ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not found:
        return jsonify({"error": "No such preset"}), 404
    return jsonify({"ok": True, "presets": export_presets.list_presets()})
