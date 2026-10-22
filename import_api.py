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


# ---- Import dialog: choose a source, pick frames, destination, options ------------------
# GET  /api/import/sources  cards found now, library path, presets
# GET  /api/import/browse   sub-folders of a folder, for the folder picker
# POST /api/import/scan     the frames in a source, flagged when already in the library
# GET  /api/import/thumb    one frame's embedded preview
# POST /api/import/run      start a background import job; GET /api/import/job polls it
# The copy itself is cardwatch.import_card (the code `watch-cards` runs): verified, never
# overwriting, content-deduped, then a second SHA-256 pass before any eject.
import hashlib
import os
import time

import cardwatch
import importkit
import presetlib

MAX_FRAMES = 20000
_ALL_SUFFIXES = cardwatch.PHOTO_SUFFIXES | cardwatch.VIDEO_SUFFIXES


class BadSource(ValueError):
    pass


def _library() -> Path:
    return _ed().LIBRARY_ROOT.resolve()


def source_dir(raw) -> Path:
    """A folder or card mount the user named. Absolute, existing, and not the library or inside it."""
    text = str(raw or "").strip()
    if not text or "\x00" in text:
        raise BadSource("Choose a card or a folder to import from.")
    p = Path(text).expanduser()
    if not p.is_absolute():
        raise BadSource("The source must be a full path, such as /media/card or /home/you/Pictures.")
    try:
        p = p.resolve()
    except (OSError, RuntimeError):
        raise BadSource(f"Cannot read {text}.")
    if not p.is_dir():
        raise BadSource(f"{p} is not a folder.")
    lib = _library()
    if p == lib or lib in p.parents:
        raise BadSource("That folder is already inside the library. Pick the card or folder to copy from.")
    return p


def frame_path(source: Path, rel) -> Path:
    """A file inside ``source`` named by a client-supplied relative path; refuses anything that escapes it."""
    text = str(rel or "")
    if not text or "\x00" in text or Path(text).is_absolute():
        raise BadSource("Bad file name.")
    cand = (source / text).resolve()
    if source not in cand.parents:
        raise BadSource("That file is outside the source folder.")
    if not cand.is_file() or cand.suffix.lower() not in _ALL_SUFFIXES:
        raise BadSource(f"No such importable file: {text}")
    return cand


def dest_subfolder(raw) -> str:
    """The folder under the library the photos go to ('' for the library root); never outside it."""
    text = str(raw or "").strip().replace("\\", "/").strip("/")
    if not text:
        return ""
    if "{" in text or "}" in text or "\x00" in text or ".." in text.split("/"):
        raise BadSource("The destination must be a folder inside the library (no .. and no {fields}).")
    return "/".join(importkit._clean(p) for p in text.split("/") if p and p != ".")


@bp.route("/api/import/sources")
def sources():
    cards = [{"path": str(c.mount), "label": c.label or c.mount.name} for c in cardwatch.find_cards()]
    try:
        presets = [{"id": p["id"], "name": p["name"], "category": p.get("category", "")}
                   for p in presetlib.list_all()]
    except Exception:
        presets = []
    return jsonify({"cards": cards, "library": str(_library()), "presets": presets,
                    "home": str(Path.home()), "layouts": list(importkit.LAYOUTS)})


@bp.route("/api/import/browse")
def browse():
    try:
        p = Path(request.args.get("path") or Path.home()).expanduser().resolve()
    except (OSError, RuntimeError):
        return _error("Cannot read that folder.")
    if not p.is_dir():
        return _error(f"{p} is not a folder.")
    dirs = []
    try:
        for e in sorted(os.scandir(p), key=lambda e: e.name.lower()):
            if e.name.startswith(".") or not e.is_dir(follow_symlinks=False):
                continue
            dirs.append(e.name)
    except OSError as exc:
        return _error(f"Cannot read {p}: {exc.strerror or exc}")
    return jsonify({"path": str(p), "parent": str(p.parent) if p.parent != p else None, "dirs": dirs[:2000]})


@bp.route("/api/import/scan", methods=["POST"])
def scan():
    try:
        source = source_dir(_ed()._json_body().get("source"))
    except ValueError as exc:
        return _error(str(exc))
    lib = _library()
    skip = [lib] if source in lib.parents else []
    files, errors = cardwatch.list_card_files(source, _ALL_SUFFIXES, skip)
    truncated = len(files) > MAX_FRAMES
    files = files[:MAX_FRAMES]
    index = cardwatch.LibraryIndex(lib, _ALL_SUFFIXES)
    frames = []
    for f in files:
        try:
            dup = index.find(f.path, f.size)
        except OSError:
            dup = None
        suffix = f.path.suffix.lower()
        frames.append({
            "rel": str(f.path.relative_to(source)), "name": f.path.name, "size": f.size,
            "mtime": int(f.mtime_ns // 1_000_000_000), "photo": f.photo,
            "raw": suffix in cardwatch.RAW_ONLY, "video": suffix in cardwatch.VIDEO_SUFFIXES,
            "sidecar": ingest.find_sidecar(f.path) is not None,
            "duplicate": str(dup.relative_to(lib)) if dup and lib in dup.parents else (dup and dup.name),
        })
    is_card = any(c.mount.resolve() == source for c in cardwatch.find_cards())
    return jsonify({"source": str(source), "card": is_card, "frames": frames, "errors": errors[:20],
                    "truncated": truncated})


_thumb_gate = threading.Semaphore(2)


@bp.route("/api/import/thumb")
def thumb():
    import io

    import thumbs
    try:
        source = source_dir(request.args.get("source"))
        path = frame_path(source, request.args.get("rel"))
    except ValueError as exc:
        return _error(str(exc), 404)
    if path.suffix.lower() in cardwatch.VIDEO_SUFFIXES:
        return Response(status=204)             # no preview: quiet for the browser, the page shows a placeholder
    with _thumb_gate:
        try:
            data = thumbs.build(path, None, 256)
        except Exception:
            try:
                from PIL import Image
                img = Image.open(path)
                img.thumbnail((256, 256))
                buf = io.BytesIO()
                img.convert("RGB").save(buf, "JPEG", quality=80)
                data = buf.getvalue()
            except Exception:
                return Response(status=204)
    return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


def _preset_hook(preset_id: str, amount: float):
    """post_copy callable: merge a preset into the new photo's own settings."""
    ed = _ed()
    import history
    preset = presetlib.get(str(preset_id))
    label = f"Preset on import: {preset['name']}" + (f" ({round(amount * 100)}%)" if amount < 1 else "")

    def apply(dest: Path) -> None:
        who = ed.detect_foreign_edits(dest)
        if who:
            raise RuntimeError(f"it already has edits from {who}, which are kept")
        baseline = ed._baseline_temp_of(dest)
        current = ed.complete_adjustments(ed.read_saved_adjustments(dest) or {}, baseline)
        if "_baseline_temp" not in current:
            current["_baseline_temp"] = baseline
        merged = presetlib.apply_preset(current, preset, amount, baseline)
        merged.pop("_baseline_temp", None)
        if amount > 0:
            merged["preset_name"] = preset["name"][:60]
            merged["preset_amount"] = round(amount * 100)
        if history.load(dest)["original"] is None:
            history.set_original(dest, current)
        frame_size, source_size = ed._save_frames(dest, merged)
        ed.save_adjustments(dest, merged, frame_size=frame_size, source_size=source_size)
        history.record(dest, label, merged)

    return apply


def build_spec(body: dict) -> importkit.ImportSpec:
    """Validate the dialog's choices into an ImportSpec. Raises ValueError with a user-readable message."""
    sub = dest_subfolder(body.get("dest"))
    base = importkit.LAYOUTS.get(str(body.get("layout") or "date"), str(body.get("layout") or "date"))
    rating = body.get("rating")
    try:
        rating = None if rating in (None, "") else int(rating)
        seq = max(1, int(body.get("seq_start") or 1))
    except (TypeError, ValueError):
        raise BadSource("The rating and the starting number must be whole numbers.")
    keywords = body.get("keywords") or []
    spec = importkit.ImportSpec(
        layout="/".join(p for p in (sub, base) if p), rename=str(body.get("rename") or ""), seq_start=seq,
        sidecars=body.get("sidecars", True) is not False,
        backup=Path(str(body["backup"])) if str(body.get("backup") or "").strip() else None,
        rating=rating, keywords=keywords, copyright=str(body.get("copyright") or ""),
        date_fn=lambda p: cardwatch._shoot_date(p))
    spec.validate()
    preset = body.get("preset")
    if isinstance(preset, dict) and preset.get("id"):
        amount = max(0.0, min(1.0, float(preset.get("amount", 100)) / 100.0))
        spec.post_copy = _preset_hook(preset["id"], amount)
    return spec


# One job at a time; the dialog polls it.
_job_lock = threading.Lock()
_job: dict = {"state": "idle"}
_job_stop = threading.Event()
_RUNNING = ("scanning", "importing", "verifying", "ejecting")


def _set_job(**kw):
    with _job_lock:
        _job.update(kw, updated=time.time())


@bp.route("/api/import/run", methods=["POST"])
def run():
    ed = _ed()
    try:
        body = ed._json_body()
        source = source_dir(body.get("source"))
        rels = body.get("files")
        if not isinstance(rels, list) or not rels:
            raise BadSource("Tick at least one frame to import.")
        only = {str(frame_path(source, r)) for r in rels[:MAX_FRAMES]}
        spec = build_spec(body)
        if spec.backup is not None:
            spec.backup = importkit.check_backup_dir(spec.backup, _library(), source)
    except Exception as exc:                          # bad path, template, rating, unknown preset
        return _error(str(exc))
    with _job_lock:
        if _job.get("state") in _RUNNING:
            return _error("An import is already running. Wait for it to finish.", 409)
        _job.clear()
        _job.update(state="scanning", id=hashlib.sha1(os.urandom(8)).hexdigest()[:12],
                    total=len(only), done=0, started=time.time(), updated=time.time())
        _job_stop.clear()
        job_id = _job["id"]
    threading.Thread(target=_run_job, args=(source, only, spec, bool(body.get("eject"))),
                     daemon=True, name="import-job").start()
    return jsonify({"started": True, "id": job_id, "total": len(only)})


def _run_job(source: Path, only: set[str], spec, eject: bool) -> None:
    ed = _ed()
    lib = ed.LIBRARY_ROOT
    try:
        def prog(s: dict) -> None:
            _set_job(**{k: v for k, v in s.items() if k in
                        ("state", "total", "done", "name", "copied", "duplicates", "failed")})
        res = cardwatch.import_card(source, lib, spec.layout, _ALL_SUFFIXES, prog, _job_stop.is_set,
                                    spec=spec, only=only)
        if not res.problem and not res.interrupted:
            cardwatch.verify_import(res, prog, _job_stop.is_set)
        card = next((c for c in cardwatch.find_cards() if c.mount.resolve() == source), None)
        ejected, blocker = False, ""
        if eject:
            if card is None:
                blocker = "the source is a folder, not a card"
            else:
                blocker = cardwatch.eject_blockers(res, True)
            if not blocker:
                _set_job(state="ejecting")
                res.eject = cardwatch.eject_card(card)
                ejected = bool(res.eject.get("ok"))
                if not ejected:
                    blocker = res.eject.get("message", "")
        idx = ed._idx(fresh=False)
        idx.scan()
        queue_smart_previews([p for p in res.new_paths if p.suffix.lower() in ingest.RAW_SUFFIXES])
        text = cardwatch.summary_text(res)
        if ejected:
            msg = f"{text}. Safe to remove the card."
        elif eject:
            msg = f"{text}. Card NOT ejected: {blocker.rstrip('.') or 'import incomplete'}."
        else:
            msg = text + ("" if res.clean else f". {cardwatch.eject_blockers(res, True)}")
        state = "done" if res.clean else ("error" if (res.problem or res.interrupted) else "done_with_errors")
        _set_job(state=state, message=msg, ejected=ejected, summary=res.as_dict(),
                 failed_files=(res.failed + res.verify_failed)[:20], seq=idx.seq,
                 new=[str(p.relative_to(lib)) for p in res.new_paths[:2000] if lib in p.parents])
    except Exception as exc:
        _set_job(state="error", message=f"The import stopped: {exc}")


@bp.route("/api/import/job")
def job():
    with _job_lock:
        return jsonify(dict(_job))


@bp.route("/api/import/cancel", methods=["POST"])
def cancel():
    _job_stop.set()
    return jsonify({"ok": True})
