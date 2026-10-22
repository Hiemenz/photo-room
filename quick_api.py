"""
Auto Sync (Develop) and Quick Develop (Library): change a few settings on
many photos at once, each photo keeping its own other settings.

``POST /api/sync_settings`` takes ``paths`` plus ``changes`` (absolute values,
what Auto Sync sends for the slider being dragged) and/or ``steps`` (relative
deltas, what the Quick Develop buttons send). Per photo it reads that photo's
own current settings, applies the change, clamps to the slider range, writes
it and records one History step. Framing, perspective, spots and masks are
never touched (``geometry.PHOTO_SPECIFIC_KEYS``), exactly like copy-paste.
White balance is camera-relative: an absolute temperature change is carried
as a ratio of the sender's own camera baseline (``baseline_temp``) and
re-anchored on each target.

``POST /api/sync_undo`` reverses one sync, per photo, only where that photo's
current History step is still the one the sync recorded.
"""
from __future__ import annotations

from flask import Blueprint, jsonify

import geometry
import history

bp = Blueprint("quick_api", __name__)


def _ed():
    import editor  # deferred: editor registers this blueprint

    return editor


def _controls() -> dict:
    ed = _ed()
    return {c["key"]: c for c in ed.CONTROLS + ed.GLOBAL_CONTROLS + ed.COLOR_CONTROLS}


def _num(v) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError("not a number")
    return float(v)


def _fit(c: dict, v: float):
    """Clamp to the slider's range and snap to its step."""
    v = min(c["max"], max(c["min"], v))
    step = c["step"]
    v = round(v / step) * step
    return int(round(v)) if float(step).is_integer() else round(v, 2)


def _resolve(ed, raw):
    """(source path, virtual entry or None, history key)."""
    vc = ed._virtual(raw)
    if vc:
        return vc[0], vc[1], ed._history_key(*vc)
    path = ed._safe_path(raw)
    return path, None, path


def _current(ed, path, entry) -> dict:
    baseline = ed._baseline_temp_of(path)
    saved = (entry.get("adjustments") or None) if entry else ed.read_saved_adjustments(path)
    if not saved:
        auto = ed.compute_auto_adjustments(path, style_model=ed.get_style_model())
        saved = {k: v for k, v in auto.items() if k not in ("iso", "styled")}
        saved["edit_version"] = ed.EDIT_VERSION
    return ed.complete_adjustments(saved, baseline)


def _current_id(h: dict):
    i = h["index"]
    return h["entries"][i]["id"] if 0 <= i < len(h["entries"]) else None


def _write(ed, path, entry, adj):
    if entry:
        ed.catalog.update_virtual(ed.LIBRARY_ROOT, entry["id"], adjustments=adj)
    else:
        frame_size, source_size = ed._save_frames(path, adj)
        ed.save_adjustments(path, adj, frame_size=frame_size,
                            sidecar=ed.WRITE_SIDECARS, source_size=source_size)


@bp.route("/api/sync_settings", methods=["POST"])
def api_sync_settings():
    ed = _ed()
    try:
        payload = ed._json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    paths = payload.get("paths")
    if not isinstance(paths, list) or not paths:
        return jsonify({"error": "No files selected"}), 400
    changes = payload.get("changes") or {}
    steps = payload.get("steps") or {}
    if not isinstance(changes, dict) or not isinstance(steps, dict) or not (changes or steps):
        return jsonify({"error": "Nothing to change"}), 400

    ctl = _controls()
    ignored = sorted(k for k in list(changes) + list(steps)
                     if k in geometry.PHOTO_SPECIFIC_KEYS or k.startswith("_"))
    changes = {k: v for k, v in changes.items() if k not in ignored}
    try:
        steps = {k: _num(v) for k, v in steps.items() if k not in ignored}
    except ValueError:
        return jsonify({"error": "steps must be numbers"}), 400
    unknown = [k for k in steps if k not in ctl]
    if unknown:
        return jsonify({"error": f"Cannot step: {', '.join(unknown)}"}), 400
    if not (changes or steps):
        return jsonify({"error": "Framing, perspective, spots and masks are not synced"}), 400

    temp_ratio = None
    try:
        if changes.get("color_temp") and payload.get("baseline_temp"):
            temp_ratio = float(changes["color_temp"]) / float(payload["baseline_temp"])
    except (TypeError, ValueError, ZeroDivisionError):
        temp_ratio = None
    label = str(payload.get("label") or "Sync")[:60]
    allow_foreign = bool(payload.get("overwrite_foreign", False))

    applied, failed, skipped, undo = [], [], [], []
    seen = set()
    for raw in paths:
        if raw in seen:
            continue
        seen.add(raw)
        try:
            path, entry, hkey = _resolve(ed, raw)
            if not entry and not allow_foreign:
                who = ed.detect_foreign_edits(path)
                if who:
                    skipped.append({"path": raw, "editor": who})
                    continue
            adj = _current(ed, path, entry)
            before = dict(adj)
            for k, v in changes.items():
                if k == "color_temp" and temp_ratio is not None:
                    v = ed._baseline_temp_of(path) * temp_ratio
                if k in ctl:
                    try:
                        v = _fit(ctl[k], _num(v))
                    except ValueError:
                        raise ValueError(f"{k} must be a number") from None
                adj[k] = v
            for k, d in steps.items():
                adj[k] = _fit(ctl[k], float(adj.get(k, ctl[k]["default"])) + d)
            if adj == before:
                applied.append(raw)     # already there (or at its limit): nothing to record
                continue
            hist = history.load(hkey)
            if not hist["entries"]:
                hist, _ = history.record(hkey, "Opened with saved edits", before)
            before_id = _current_id(hist)
            _write(ed, path, entry, adj)
            hist, _ = history.record(hkey, label, adj)
            applied.append(raw)
            undo.append({"path": raw, "before": before_id, "after": _current_id(hist)})
        except Exception as exc:
            failed.append({"path": raw, "error": str(exc.args[0] if isinstance(exc, KeyError) else exc)})

    return jsonify({
        "ok": not failed and not skipped,
        "applied": applied, "failed": failed, "skipped": skipped,
        "undo": undo, "ignored": ignored,
        "message": (f"{label}: {len(applied)} photo(s)"
                    + (f", {len(failed)} failed" if failed else "")
                    + (f", {len(skipped)} skipped (edited in other software)" if skipped else "")),
    })


@bp.route("/api/sync_undo", methods=["POST"])
def api_sync_undo():
    ed = _ed()
    try:
        payload = ed._json_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    items = payload.get("undo")
    if not isinstance(items, list) or not items:
        return jsonify({"error": "Nothing to undo"}), 400
    restored, skipped, failed = [], [], []
    for it in items:
        raw = it.get("path") if isinstance(it, dict) else None
        try:
            path, entry, hkey = _resolve(ed, raw)
            hist = history.load(hkey)
            if _current_id(hist) != it.get("after") or not it.get("before"):
                skipped.append(raw)     # edited again since; do not clobber
                continue
            _, adj = history.jump(hkey, it["before"])
            _write(ed, path, entry, adj)
            restored.append(raw)
        except Exception as exc:
            failed.append({"path": raw, "error": str(exc.args[0] if isinstance(exc, KeyError) else exc)})
    return jsonify({
        "ok": not failed, "restored": restored, "skipped": skipped, "failed": failed,
        "message": f"Undid {len(restored)} photo(s)"
                   + (f", {len(skipped)} edited since" if skipped else ""),
    })
