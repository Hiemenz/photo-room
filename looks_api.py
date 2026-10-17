"""
HTTP side of the preset panel, the LUT library and "match reference look".

The maths lives in presetlib.py, lut.py and matchlook.py; this file resolves the photo,
shapes the JSON and keeps the cheap paths cheap:

* Thumbnails (``/api/looks/*/thumb``) render a preset or LUT on the photo's cached
  160 px thumbnail through the real colour stages, so a panel of thirty tiles costs a few
  milliseconds each and every tile URL is immutable (the content revision is in it).
* Hover preview and the amount slider post to ``/api/looks/preset/preview``, which merges
  the preset into the current edit and renders once through the same serialised,
  supersedable path as slider drags (``editor._render_if_current``).
* Applying to the open photo returns the merged adjustments; the page puts them in its
  slider state and records ONE history step, exactly like any other edit.
* Applying to a selection writes each photo's own edit with the preset merged in and
  records one history step per photo.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np
from flask import Blueprint, Response, jsonify, request
from PIL import Image

import history
import lut as lutmod
import matchlook
import presetlib

bp = Blueprint("looks_api", __name__)

THUMB_EDGE = 160
MAX_BATCH = 2000


def _ed():
    import editor  # deferred: editor registers this blueprint

    return editor


class BadRequest(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@bp.errorhandler(BadRequest)
def _bad_request(exc):
    return jsonify({"error": str(exc)}), exc.status


@bp.errorhandler(presetlib.PresetError)
def _preset_error(exc):
    return jsonify({"error": str(exc)}), 400


@bp.errorhandler(lutmod.LutError)
def _lut_error(exc):
    return jsonify({"error": str(exc)}), 400


@bp.errorhandler(lutmod.LutNotFound)
def _lut_missing(exc):
    return jsonify({"error": str(exc)}), 404


@bp.errorhandler(matchlook.MatchError)
def _match_error(exc):
    return jsonify({"error": str(exc)}), 400


def _body() -> dict:
    if request.method == "GET":
        return request.args.to_dict()
    try:
        return _ed()._json_body()
    except ValueError as exc:
        raise BadRequest(str(exc), 415)


def _photo(raw_id):
    ed = _ed()
    if not raw_id or not isinstance(raw_id, str):
        raise BadRequest("path is required")
    try:
        vc = ed._virtual(raw_id)
        return (vc[0], vc[1]) if vc else (ed._safe_path(raw_id), None)
    except ValueError as exc:
        raise BadRequest(str(exc))


def _amount(body: dict, default: float = 100.0) -> float:
    try:
        a = float(body.get("amount", default))
    except (TypeError, ValueError):
        raise BadRequest("amount must be a number")
    if a != a or not 0 <= a <= 100:
        raise BadRequest("amount must be between 0 and 100")
    return a / 100.0


def _baseline(body: dict, default: float = 5500.0) -> float:
    try:
        bt = float(body.get("baseline_temp") or default)
    except (TypeError, ValueError):
        raise BadRequest("baseline_temp must be a number")
    if not 1000 <= bt <= 40000:
        raise BadRequest("baseline_temp is out of range")
    return bt


def _preset_from(body: dict) -> dict:
    """The preset a request names: a stored one by ``id`` or an inline one (match result, unsaved)."""
    if isinstance(body.get("preset"), dict):
        values, _ = presetlib.clean_values(body["preset"].get("values"))
        return {"id": "inline", "name": presetlib._name(body["preset"].get("name") or "Preset"),
                "values": values, "groups": presetlib.groups_of(values), "builtin": False,
                "category": "User"}
    return presetlib.get(str(body.get("id", "")))


def _revision(preset: dict) -> str:
    return hashlib.sha1(json.dumps(preset["values"], sort_keys=True, default=str).encode()).hexdigest()[:8]


def _public(p: dict) -> dict:
    return {"id": p["id"], "name": p["name"], "category": p.get("category", "User"), "builtin": p["builtin"],
            "groups": p["groups"], "rev": _revision(p)}


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

_legacy_lock = threading.Lock()
_legacy_done: set[str] = set()


def _migrate_legacy_once() -> None:
    """Presets saved by older versions (in the style database) appear in the new panel."""
    key = str(presetlib.preset_dir())
    with _legacy_lock:
        marker = presetlib.preset_dir() / ".legacy-imported"
        if key in _legacy_done or marker.exists():
            _legacy_done.add(key)
            return
        _legacy_done.add(key)
        try:
            import style

            if style.DB_PATH.exists():
                presetlib.import_legacy()
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("1")
        except Exception:
            pass                                    # a damaged old database must not break the panel


@bp.route("/api/looks/presets", methods=["GET"])
def api_presets():
    _migrate_legacy_once()
    return jsonify({
        "presets": [_public(p) for p in presetlib.list_all()],
        "groups": [{"id": g, "label": presetlib.GROUP_LABELS[g], "default": g in presetlib.DEFAULT_GROUPS}
                   for g in presetlib.GROUPS],
        "luts": lutmod.list_luts(),
    })


@bp.route("/api/looks/preset/save", methods=["POST"])
def api_preset_save():
    body = _body()
    if isinstance(body.get("preset"), dict):                       # e.g. a "match reference" result
        pre = _preset_from(body)
        record = {**pre, "name": presetlib._name(body.get("name") or pre["name"]), "category": "Matched"}
        record["id"] = "user:" + presetlib.slug(record["name"])
    else:
        adj = body.get("adjustments")
        if not isinstance(adj, dict):
            raise BadRequest("adjustments must be an object")
        groups = body.get("groups")
        if groups is not None and not isinstance(groups, list):
            raise BadRequest("groups must be a list")
        record = presetlib.from_adjustments(body.get("name"), adj, groups, _baseline(body))
    if not body.get("overwrite"):
        try:
            if presetlib.get(record["id"]):
                return jsonify({"error": f"A preset called '{record['name']}' already exists",
                                "exists": True}), 409
        except presetlib.PresetError:
            pass
    presetlib.save(record)
    return jsonify({"ok": True, "id": record["id"], "message": f"Saved preset '{record['name']}'",
                    "presets": [_public(p) for p in presetlib.list_all()]})


@bp.route("/api/looks/preset/delete", methods=["POST"])
def api_preset_delete():
    body = _body()
    removed = presetlib.delete(str(body.get("id", "")))
    return jsonify({"ok": removed, "message": "Deleted preset" if removed else "No such preset",
                    "presets": [_public(p) for p in presetlib.list_all()]})


@bp.route("/api/looks/preset/rename", methods=["POST"])
def api_preset_rename():
    body = _body()
    new = presetlib.rename(str(body.get("id", "")), body.get("name"))
    return jsonify({"ok": True, "id": new["id"], "presets": [_public(p) for p in presetlib.list_all()]})


def _check_version(adj: dict) -> None:
    ed = _ed()
    if int(adj.get("edit_version") or ed.EDIT_VERSION) < ed.EDIT_VERSION:
        raise BadRequest("This photo's edit uses the old colour maths. Click Upgrade edit first, "
                         "then apply the preset.", 409)


def _merged(body: dict) -> tuple[dict, dict, float]:
    preset = _preset_from(body)
    adj = body.get("adjustments")
    if not isinstance(adj, dict):
        raise BadRequest("adjustments must be an object")
    _check_version(adj)
    amount = _amount(body)
    merged = presetlib.apply_preset(adj, preset, amount, _baseline(body))
    if amount > 0:
        merged["preset_name"] = preset["name"][:60]
        merged["preset_amount"] = round(amount * 100)
    return merged, preset, amount


def _label(preset: dict, amount: float) -> str:
    return f"Preset: {preset['name']}" + (f" ({round(amount * 100)}%)" if amount < 1 else "")


@bp.route("/api/looks/preset/apply", methods=["POST"])
def api_preset_apply():
    body = _body()
    merged, preset, amount = _merged(body)
    return jsonify({"adjustments": merged, "label": _label(preset, amount)})


@bp.route("/api/looks/preset/preview", methods=["POST"])
def api_preset_preview():
    """Render the open photo with a preset merged in, without recording or saving anything."""
    ed = _ed()
    body = _body()
    path, _ = _photo(body.get("path"))
    merged, _, _ = _merged(body)
    seq = body.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool):
        return jsonify(ed.render_with_histogram(path, merged, fast=True))
    return jsonify(ed._render_if_current(path, merged, seq, True))


@bp.route("/api/looks/preset/apply_batch", methods=["POST"])
def api_preset_apply_batch():
    """
    Merge one preset into many photos' own edits. Each photo keeps everything the preset
    does not name, gets one history step, and failures do not stop the rest.
    """
    ed = _ed()
    body = _body()
    preset = _preset_from(body)
    amount = _amount(body)
    paths = body.get("paths") or []
    if not isinstance(paths, list) or not paths:
        raise BadRequest("No files selected")
    if len(paths) > MAX_BATCH:
        raise BadRequest(f"At most {MAX_BATCH} files at once")
    label = _label(preset, amount)
    allow_foreign = bool(body.get("overwrite_foreign"))
    applied, failed, skipped = [], [], []
    for raw in dict.fromkeys(str(p) for p in paths):
        try:
            vc = ed._virtual(raw)
            path = vc[0] if vc else ed._safe_path(raw)
            if not vc and not allow_foreign:
                who = ed.detect_foreign_edits(path)
                if who:
                    skipped.append({"path": raw, "editor": who})
                    continue
            baseline = ed._baseline_temp_of(path)
            saved = (vc[1].get("adjustments") if vc else ed.read_saved_adjustments(path)) or {}
            current = ed.complete_adjustments(saved, baseline)
            if int(current.get("edit_version") or ed.EDIT_VERSION) < ed.EDIT_VERSION:
                skipped.append({"path": raw, "reason": "old colour maths: upgrade the edit first"})
                continue
            if "_baseline_temp" not in current:
                current["_baseline_temp"] = baseline
            merged = presetlib.apply_preset(current, preset, amount, baseline)
            merged.pop("_baseline_temp", None)
            if amount > 0:
                merged["preset_name"] = preset["name"][:60]
                merged["preset_amount"] = round(amount * 100)
            hkey = ed._history_key(*vc) if vc else path
            hist = history.load(hkey)
            if hist["original"] is None:
                history.set_original(hkey, current)
            if not hist["entries"]:
                history.record(hkey, "Before preset", current)
            if vc:
                import catalog
                catalog.update_virtual(ed.LIBRARY_ROOT, vc[1]["id"], adjustments=merged)
            else:
                frame_size, source_size = ed._save_frames(path, merged)
                ed.save_adjustments(path, merged, frame_size=frame_size, source_size=source_size)
            history.record(hkey, label, merged)
            applied.append(raw)
        except Exception as exc:
            failed.append({"path": raw, "error": ed.describe_error(exc)})
    return jsonify({
        "ok": not failed and not skipped, "applied": applied, "failed": failed, "skipped": skipped,
        "message": (f"{label} applied to {len(applied)} file(s)"
                    + (f", {len(failed)} failed" if failed else "")
                    + (f", {len(skipped)} skipped" if skipped else "")),
    })


# ---------------------------------------------------------------------------
# Thumbnails
# ---------------------------------------------------------------------------

_thumb_lock = threading.Lock()
_thumb_src: OrderedDict[str, tuple[float, np.ndarray]] = OrderedDict()


def _thumb_base(path: Path) -> np.ndarray:
    """The photo's cached 160 px thumbnail as float RGB 0..1 (kept for the next tile)."""
    key = str(path.resolve())
    try:
        stamp = path.stat().st_mtime
    except OSError:
        stamp = 0.0
    with _thumb_lock:
        hit = _thumb_src.get(key)
        if hit and hit[0] == stamp:
            _thumb_src.move_to_end(key)
            return hit[1]
    data = _ed()._thumb_pool().get_now(path, size=THUMB_EDGE)
    arr = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"), dtype=np.float32) / 255.0
    with _thumb_lock:
        _thumb_src[key] = (stamp, arr)
        while len(_thumb_src) > 8:
            _thumb_src.popitem(last=False)
    return arr


def _jpeg(arr: np.ndarray) -> Response:
    buf = io.BytesIO()
    Image.fromarray((np.clip(arr, 0, 1) * 255 + 0.5).astype(np.uint8)).save(buf, format="JPEG", quality=80)
    return Response(buf.getvalue(), mimetype="image/jpeg",
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


def _render_thumb(path: Path, preset: dict, amount: float, baseline: float) -> np.ndarray:
    ed = _ed()
    base = _thumb_base(path)
    adj = presetlib.apply_preset(presetlib.neutral_adjustments(baseline), preset, amount, baseline)
    for key in ("crop", "straighten"):
        adj.pop(key, None)
    adj, _ = ed._without_missing_lut(adj)
    return ed.apply_adjustments(base, int(baseline), adj)


@bp.route("/api/looks/preset/thumb", methods=["GET"])
def api_preset_thumb():
    body = _body()
    path, _ = _photo(body.get("path"))
    preset = presetlib.get(str(body.get("id", "")))
    return _jpeg(_render_thumb(path, preset, _amount(body), _baseline(body)))


@bp.route("/api/looks/lut/thumb", methods=["GET"])
def api_lut_thumb():
    body = _body()
    path, _ = _photo(body.get("path"))
    lut_id = str(body.get("id", ""))
    lutmod.get(lut_id)
    preset = {"values": {"lut": lut_id, "lut_amount": 100}}
    return _jpeg(_render_thumb(path, preset, _amount(body), _baseline(body)))


# ---------------------------------------------------------------------------
# Preset files
# ---------------------------------------------------------------------------

@bp.route("/api/looks/preset/export", methods=["GET"])
def api_preset_export():
    ids = [i for i in str(request.args.get("ids", "")).split(",") if i]
    text = presetlib.export_json(ids)
    first = presetlib.get(ids[0])["name"]
    name = presetlib.slug(first if len(ids) == 1 else "photo-room-presets")
    return Response(text, mimetype="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}.json"'})


@bp.route("/api/looks/preset/import", methods=["POST"])
def api_preset_import():
    """Body: ``{"filename": "...", "data": "<base64 of the file>"}``; our .json or a Lightroom .xmp preset."""
    body = _body()
    try:
        data = base64.b64decode(str(body.get("data", "")), validate=True)
    except (binascii.Error, ValueError):
        raise BadRequest("data must be the base64 of the file")
    presets, skipped = presetlib.import_file(data, str(body.get("filename", "")))
    saved = []
    for p in presets:
        if not body.get("overwrite"):
            try:
                presetlib.get(p["id"])
                p = {**p, "name": f"{p['name']} (imported)", "id": "user:" + presetlib.slug(p["name"] + " imported")}
            except presetlib.PresetError:
                pass
        presetlib.save(p)
        saved.append(p["name"])
    return jsonify({"ok": True, "imported": saved, "skipped": skipped,
                    "message": f"Imported {len(saved)} preset(s)"
                               + (f"; {len(skipped)} setting(s) could not be mapped" if skipped else ""),
                    "presets": [_public(p) for p in presetlib.list_all()]})


# ---------------------------------------------------------------------------
# LUT library
# ---------------------------------------------------------------------------

@bp.route("/api/looks/luts", methods=["GET"])
def api_luts():
    return jsonify({"luts": lutmod.list_luts()})


@bp.route("/api/looks/lut/import", methods=["POST"])
def api_lut_import():
    """Body: ``{"name": "...", "text": "<.cube text>", "space": "srgb"|"linear"}``."""
    body = _body()
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise BadRequest("text must be the contents of a .cube file")
    space = body.get("space") or None
    entry = lutmod.import_text(text, name=body.get("name"), space=space)
    return jsonify({"ok": True, "lut": entry, "luts": lutmod.list_luts(),
                    "message": f"Imported LUT '{entry['name']}' ({entry['kind'].upper()} {entry['size']})"})


@bp.route("/api/looks/lut/rename", methods=["POST"])
def api_lut_rename():
    body = _body()
    entry = lutmod.rename(str(body.get("id", "")), str(body.get("name", "")))
    return jsonify({"ok": True, "lut": entry, "luts": lutmod.list_luts()})


@bp.route("/api/looks/lut/delete", methods=["POST"])
def api_lut_delete():
    body = _body()
    lut_id = str(body.get("id", ""))
    if lut_id in lutmod.builtin_ids():
        raise BadRequest("Built-in LUTs cannot be deleted")
    removed = lutmod.delete(lut_id)
    _ed()._stage_cache.clear()
    return jsonify({"ok": removed, "luts": lutmod.list_luts(),
                    "message": "Deleted LUT" if removed else "No such LUT"})


@bp.route("/api/looks/lut/export", methods=["GET"])
def api_lut_export():
    lut_id = str(request.args.get("id", ""))
    text = lutmod.export_text(lut_id)
    name = presetlib.slug(lutmod.describe(lut_id)["name"])
    return Response(text, mimetype="text/plain",
                    headers={"Content-Disposition": f'attachment; filename="{name}.cube"'})


# ---------------------------------------------------------------------------
# Match reference look
# ---------------------------------------------------------------------------

@bp.route("/api/looks/match", methods=["POST"])
def api_match():
    """
    Derive an editable preset that moves the open photo toward a reference picture.

    The reference is either another library photo (``reference_path``) or an uploaded
    image (``reference_data``, base64). Nothing is saved or applied: the reply carries
    the preset and a report; the page previews it, applies it at an amount or saves it.
    """
    ed = _ed()
    body = _body()
    path, _ = _photo(body.get("path"))
    if body.get("reference_data"):
        try:
            raw = base64.b64decode(str(body["reference_data"]), validate=True)
        except (binascii.Error, ValueError):
            raise BadRequest("reference_data must be base64")
        reference = matchlook.load_image(raw)
    elif body.get("reference_path"):
        ref_path, _ = _photo(body["reference_path"])
        try:
            reference = matchlook.load_image(io.BytesIO(ed._thumb_pool().get_now(ref_path, size=1024)))
        except matchlook.MatchError:
            raise
        except Exception as exc:
            raise BadRequest(f"Could not read the reference photo: {exc}")
    else:
        raise BadRequest("Choose a reference: reference_path or reference_data")

    base, _ = ed._load_base_render(path)
    result = matchlook.derive(base, reference)
    values, _ = presetlib.clean_values(result["values"])
    preset = {"name": "Matched look", "values": values, "groups": presetlib.groups_of(values)}
    return jsonify({"preset": preset, "report": result["report"]})
