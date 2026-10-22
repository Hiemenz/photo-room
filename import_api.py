"""
In-app import: drag files or a folder onto the Library, or pick them with the
Import button.

The browser cannot hand the server a path, so each raw is streamed up one at a
time (``POST /api/import/upload``) into a private temporary folder outside the
library, then goes through ``ingest.copy_from_card_ex``: the same copy the card
ingest uses (fsync, SHA-256 verify against the source, never overwrite, a
numbered name if a different file already has that name).

On top of that the library is checked by content, not name: if any photo
already in the library has the same bytes, even under another name or folder,
the upload is reported as a duplicate and nothing is written.

``POST /api/import/finish`` rescans the library (the change feed then tells
every open Library grid about the new photos) and builds smart previews for
what was added in the background.

``GET /api/smartpreview/status?path=`` says whether one is built; ``GET /api/smartpreview?path=`` serves a cached 2048 px smart preview (404 when
none is built); ``POST /api/smartpreview/build`` builds them for some photos.
"""
from __future__ import annotations

import queue
import shutil
import tempfile
import threading
from pathlib import Path

from flask import Blueprint, Response, jsonify, request, send_file

import ingest
import smartpreview

bp = Blueprint("import_api", __name__)

MAX_UPLOAD = 4 * 1024**3
SPACE_MARGIN = 64 * 1024**2
_CHUNK = 1 << 20


def _ed():
    import editor  # deferred: editor registers this blueprint

    return editor


@bp.before_request
def _allow_big_bodies():
    # The app caps bodies at 8 MB (JSON); a raw file is far bigger.
    if request.endpoint == "import_api.upload":
        request.max_content_length = MAX_UPLOAD


def _error(message: str, status: int = 400):
    return jsonify({"error": message}), status


def clean_name(raw: str | None) -> str | None:
    """A bare file name from a client-supplied one, or None if it is unusable."""
    name = Path(str(raw or "").replace("\\", "/")).name
    if not name or name.startswith(".") or "\x00" in name:
        return None
    return name


def find_duplicate(src: Path, library: Path) -> Path | None:
    """A photo already in the library with exactly these bytes, if any."""
    from process import list_photos

    size = src.stat().st_size
    cache: list = [None]
    for existing in list_photos(library):
        try:
            if existing.stat().st_size != size:
                continue
        except OSError:
            continue
        if ingest.same_content(src, existing, cache):
            return existing
    return None


def import_file(src: Path, library: Path) -> dict:
    """Import one file already on disk. Returns {status, path, name, ...}."""
    if src.suffix.lower() not in ingest.RAW_SUFFIXES:
        return {"status": "skipped", "name": src.name,
                "reason": f"{src.suffix or 'this file type'} is not a supported raw format"}
    dup = find_duplicate(src, library)
    if dup is not None:
        return {"status": "duplicate", "name": src.name, "path": str(dup.relative_to(library)),
                "reason": "already in the library"}
    try:
        dest, status = ingest.copy_from_card_ex(src, library, date_folders=_has_date(src), verify=True)
    except RuntimeError as exc:                       # checksum mismatch
        return {"status": "failed", "name": src.name,
                "reason": f"{exc}. The copy was discarded; try again, and check the card or disk."}
    return {"status": "duplicate" if status == "identical" else status, "name": src.name,
            "path": str(dest.relative_to(library))}


def _has_date(src: Path) -> bool:
    import process

    try:
        return bool(process.get_shoot_date(src))
    except Exception:
        return False


@bp.route("/api/import/upload", methods=["POST"])
def upload():
    ed = _ed()
    library = ed.LIBRARY_ROOT
    name = clean_name(request.args.get("name"))
    if name is None:
        return _error("The file has no usable name, so it was not imported.")
    if Path(name).suffix.lower() not in ingest.RAW_SUFFIXES:
        return jsonify({"status": "skipped", "name": name,
                        "reason": f"{Path(name).suffix or 'this file type'} is not a supported raw format"})
    declared = request.content_length
    if declared is not None:
        try:
            free = shutil.disk_usage(library).free
        except OSError:
            free = None
        if free is not None and declared + SPACE_MARGIN > free:
            return _error(f"Not enough free space in the library folder for {name} "
                          f"({declared // 2**20} MB needed, {free // 2**20} MB free). "
                          "Free some space or point photo-room at another disk.", 507)
    with tempfile.TemporaryDirectory(prefix="photo-room-import-") as tmp:
        staged = Path(tmp) / name
        try:
            with open(staged, "wb") as fh:
                while chunk := request.stream.read(_CHUNK):
                    fh.write(chunk)
        except Exception as exc:                      # client went away, body too big, disk full
            return _error(f"The upload of {name} did not finish ({exc}). Nothing was imported; try again.", 400)
        if staged.stat().st_size == 0:
            return _error(f"{name} is empty, so it was not imported.", 422)
        try:
            out = import_file(staged, library)
        except OSError as exc:
            return _error(f"Could not write {name} into the library ({exc}). "
                          "Check that the folder is writable and the disk is not full.", 507)
    if out.get("path") and out["status"] in ("copied", "renamed"):
        _pending_new.append(out["path"])
    return jsonify(out)


_pending_new: list[str] = []
_build_q: "queue.Queue[Path]" = queue.Queue()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()


def _build_loop():
    while True:
        path = _build_q.get()
        try:
            smartpreview.ensure(path, _ed().LIBRARY_ROOT)
        except Exception:
            pass                                      # a smart preview is an optimisation
        finally:
            _build_q.task_done()


def queue_smart_previews(paths: list[Path]) -> None:
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_build_loop, daemon=True, name="smartpreview")
            _worker.start()
    for p in paths:
        _build_q.put(p)


@bp.route("/api/import/finish", methods=["POST"])
def finish():
    ed = _ed()
    new = list(dict.fromkeys(_pending_new))
    _pending_new.clear()
    idx = ed._idx(fresh=False)
    counts = idx.scan()
    queue_smart_previews([ed.LIBRARY_ROOT / r for r in new])
    return jsonify({"scan": counts, "new": new, "seq": idx.seq})


@bp.route("/api/import/lightroom", methods=["POST"])
def import_lightroom():
    """Lightroom .lrcat import. ``apply`` false (default) only returns the dry-run report."""
    import lrimport

    ed = _ed()
    body = request.get_json(silent=True) or {}
    path = str(body.get("path") or "").strip()
    if not path:
        return _error("Give the path of a Lightroom catalog (.lrcat) or the folder that holds it.")
    remaps = [str(r) for r in body.get("remap") or [] if str(r).strip()]
    try:
        cat = lrimport.find_catalog(Path(path))
        plan = lrimport.build_plan(cat, ed.LIBRARY_ROOT, remaps, bool(body.get("force")))
    except (OSError, ValueError) as exc:
        return _error(str(exc))
    out = {"catalog": str(cat), "report": plan["report"], "text": lrimport.format_report(plan["report"]),
           "applied": None}
    if body.get("apply"):
        out["applied"] = lrimport.apply_plan(plan)
        ed._idx(fresh=False).scan()
    return jsonify(out)


def _photo(raw):
    ed = _ed()
    return ed._safe_path(raw)


@bp.route("/api/smartpreview")
def get_smart_preview():
    try:
        path = _photo(request.args.get("path", ""))
    except ValueError as exc:
        return _error(str(exc))
    jpg, meta = smartpreview.entry_paths(path, _ed().LIBRARY_ROOT)
    if not jpg.is_file() or not meta.is_file():
        return _error("No smart preview built for this photo yet", 404)
    resp = send_file(jpg, mimetype="image/jpeg", max_age=3600)
    return resp


@bp.route("/api/smartpreview/status")
def smart_preview_status():
    """{ready: bool}: a cheap probe the UI polls instead of requesting an image that may not exist."""
    try:
        path = _photo(request.args.get("path", ""))
    except ValueError as exc:
        return _error(str(exc))
    jpg, meta = smartpreview.entry_paths(path, _ed().LIBRARY_ROOT)
    return jsonify({"ready": jpg.is_file() and meta.is_file()})


@bp.route("/api/smartpreview/build", methods=["POST"])
def build_smart_previews():
    try:
        body = _ed()._json_body()
        paths = [_photo(p) for p in (body.get("paths") or [])][:500]
    except ValueError as exc:
        return _error(str(exc))
    queue_smart_previews(paths)
    return jsonify({"queued": len(paths)})


# ---- card watcher status ---------------------------------------------------
# `photo-room watch-cards` runs as its own process and writes progress to a JSON
# file in the library; this endpoint relays it to the open editor. When an
# import finishes, the library is rescanned once so the change feed delivers
# the new photos to every open Library grid.
_scanned_card_ids: set[str] = set()
CARD_STALE_SECONDS = 30
_CARD_ACTIVE = ("detected", "scanning", "importing", "verifying", "deleting", "ejecting")


@bp.route("/api/cards/status")
def card_status():
    import json
    import time

    import cardwatch

    ed = _ed()
    try:
        data = json.loads((ed.LIBRARY_ROOT / cardwatch.STATUS_NAME).read_text())
    except (OSError, ValueError):
        return jsonify({"state": "idle"})
    if not isinstance(data, dict) or "id" not in data:
        return jsonify({"state": "idle"})
    if data.get("state") in _CARD_ACTIVE and time.time() - data.get("updated", 0) > CARD_STALE_SECONDS:
        data["state"] = "stale"
        data["message"] = "The card watcher stopped responding; check `photo-room watch-cards`."
    if data.get("state") in ("done", "done_with_errors") and data["id"] not in _scanned_card_ids:
        _scanned_card_ids.add(data["id"])
        ed._idx(fresh=False).scan()
    return jsonify(data)
