"""
HTTP side of the editor's Export dialog.

Thin on purpose: every pixel and tag goes through exporter.py, the same code
`process.py export` runs, so the dialog and the command line cannot drift.
Batches run on a background thread and are polled for progress, because a
full-resolution raw render takes seconds per photo.
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, request

import exporter
from exporter import ExportOptions

bp = Blueprint("export_api", __name__)

SCOPES = ("current", "selected", "folder")
MAX_JOBS_KEPT = 20

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def _ed():
    import editor  # deferred: editor imports this module

    return editor


def default_destination() -> str:
    # Outside the library: exports inside it would show up as photos.
    return str(Path.home() / "Pictures" / "photo-room-export")


def _options(payload: dict) -> ExportOptions:
    long_edge = payload.get("long_edge")
    try:
        long_edge = int(long_edge) if long_edge not in (None, "", 0, "0") else None
        return ExportOptions(
            format=str(payload.get("format", "jpeg")),
            quality=int(payload.get("quality", 92)),
            long_edge=long_edge,
            colorspace=payload.get("colorspace") or None,
            template=str(payload.get("template") or "{name}"),
            keep_metadata=bool(payload.get("keep_metadata", True)),
            bit_depth=int(payload.get("bit_depth", 8)),
        ).validate()
    except (TypeError, ValueError) as exc:
        raise ValueError(str(exc)) from None


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


def _destination(payload: dict) -> Path:
    raw = str(payload.get("dest") or "").strip()
    if not raw:
        raise ValueError("Choose a destination folder")
    dest = Path(raw).expanduser()
    if not dest.is_absolute():
        dest = _ed().LIBRARY_ROOT / dest
    if dest.exists() and not dest.is_dir():
        raise ValueError(f"{dest} is a file, not a folder")
    return dest


def _require_exiftool() -> None:
    if not exporter.exiftool_available():
        raise RuntimeError(
            "exiftool was not found. Exporting reads each photo's saved edits and "
            "copies its metadata with exiftool. Install it (Linux: apt install "
            "libimage-exiftool-perl; macOS: brew install exiftool) and try again."
        )


def _source_size(path: Path) -> tuple[int, int] | None:
    try:
        if path.suffix.lower() in exporter.RAW_SUFFIXES:
            import rawpy

            with rawpy.imread(str(path)) as raw:
                return int(raw.sizes.width), int(raw.sizes.height)
        from PIL import Image

        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def preview_names(sources: list[tuple[Path, dict | None]], opts: ExportOptions, dest: Path | None) -> list[dict]:
    """What each file would be called, including -1/-2 suffixes for existing files."""
    ext = exporter.FORMATS[opts.format]
    used: set[str] = set()
    out = []
    for seq, (src, virt) in enumerate(sources, start=1):
        size = _source_size(src) or (0, 0)
        if opts.long_edge and size[0] and max(size) > opts.long_edge:
            k = opts.long_edge / max(size)
            size = (max(1, round(size[0] * k)), max(1, round(size[1] * k)))
        name = exporter.output_name(src, opts.template, ext, seq, size,
                                    stem=exporter.virtual_stem(src, virt) if virt else None)
        stem, n, final = Path(name).stem, 0, name
        while final in used or (dest is not None and (dest / final).exists()):
            n += 1
            final = f"{stem}-{n}{ext}"
        used.add(final)
        out.append({"source": src.name, "name": final})
    return out


@bp.route("/api/export/defaults")
def api_export_defaults():
    return jsonify({
        "dest": default_destination(),
        "exiftool": exporter.exiftool_available(),
        "colorspaces": list(exporter.COLOR_SPACES),
        "bit_depths": [8, 16],
        "tokens": ["{name}", "{date}", "{seq}", "{seq:04d}", "{width}", "{height}"],
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
        try:
            dest = _destination(payload)
        except ValueError:
            dest = None
        names = preview_names(sources[:50], opts, dest)  # a preview needs only a few
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({
        "names": names,
        "count": len(sources),
        "approximate": any(t in opts.template for t in ("{width", "{height")),
    })


def _run(job: dict, sources: list[tuple[Path, dict | None]], dest: Path, opts: ExportOptions) -> None:
    for seq, (src, virt) in enumerate(sources, start=1):
        with _lock:
            job["current"] = src.name
        try:
            written = exporter.export_photo(src, dest, opts, seq=seq, virtual=virt)
            with _lock:
                job["written"].append(str(written))
        except Exception as exc:
            with _lock:
                job["failed"].append({"source": src.name, "error": ed_error(exc)})
        with _lock:
            job["done"] += 1
    with _lock:
        job["state"] = "done"
        job["current"] = None


def ed_error(exc: Exception) -> str:
    if isinstance(exc, FileNotFoundError) and "exiftool" in str(exc).lower():
        return "exiftool was not found; install it and try again"
    return _ed().describe_error(exc)


def _public(job: dict) -> dict:
    return {k: job[k] for k in ("id", "state", "total", "done", "current",
                                "dest", "written", "failed")}


@bp.route("/api/export", methods=["POST"])
def api_export():
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
        dest = _destination(payload)
        preview_names(sources[:1], opts, None)  # reject a bad template before starting
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        _require_exiftool()
    except RuntimeError as exc:
        return jsonify({"error": str(exc), "code": "exiftool_missing"}), 424

    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return jsonify({"error": f"Cannot create {dest}: {exc.strerror or exc}"}), 400

    with _lock:
        if any(j["state"] == "running" for j in _jobs.values()):
            return jsonify({"error": "An export is already running"}), 409
        job = {"id": uuid.uuid4().hex, "state": "running", "total": len(sources),
               "done": 0, "current": None, "dest": str(dest), "written": [], "failed": []}
        _jobs[job["id"]] = job
        while len(_jobs) > MAX_JOBS_KEPT:
            _jobs.pop(next(iter(_jobs)))
    threading.Thread(target=_run, args=(job, sources, dest, opts), daemon=True).start()
    return jsonify(_public(job)), 202


@bp.route("/api/export/status/<job_id>")
def api_export_status(job_id: str):
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Unknown export"}), 404
        return jsonify(_public(job) | {"written": list(job["written"]),
                                       "failed": list(job["failed"])})
