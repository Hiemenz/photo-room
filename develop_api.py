"""
HTTP side of the Develop helpers (auto tone, white balance, clipping,
histogram, tiles). The maths is in analysis.py and tile_server.py; this file
only resolves the photo and its settings and shapes the JSON.

Every endpoint takes the photo the way /api/preview does (``path``, a library
path or a virtual-copy id) and the settings currently on screen
(``adjustments``); with no settings it uses the photo's saved ones.
"""
from __future__ import annotations

import base64
import io
import json
import time
from pathlib import Path

import numpy as np
from flask import Blueprint, Response, jsonify, request
from PIL import Image

import analysis
import tile_server

bp = Blueprint("develop_api", __name__)


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


def _payload() -> dict:
    if request.method == "GET":
        return request.args.to_dict()
    try:
        return _ed()._json_body()
    except ValueError as exc:
        raise BadRequest(str(exc), 415)


def _photo(raw_id) -> tuple[Path, dict | None]:
    """(file, virtual-copy entry or None) for a client-supplied id."""
    ed = _ed()
    if not raw_id or not isinstance(raw_id, str):
        raise BadRequest("path is required")
    try:
        vc = ed._virtual(raw_id)
        return (vc[0], vc[1]) if vc else (ed._safe_path(raw_id), None)
    except ValueError as exc:
        raise BadRequest(str(exc))


def _settings(raw, path: Path, entry: dict | None) -> dict:
    if raw is None:
        saved = (entry.get("adjustments") if entry else None) or _ed().read_saved_adjustments(path)
        return dict(saved or {})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise BadRequest("settings is not valid JSON")
    if not isinstance(raw, dict):
        raise BadRequest("adjustments must be an object")
    return dict(raw)


def _number(payload: dict, key: str, default, lo: float, hi: float) -> float:
    try:
        v = float(payload.get(key, default))
    except (TypeError, ValueError):
        raise BadRequest(f"{key} must be a number")
    if not np.isfinite(v) or not lo <= v <= hi:
        raise BadRequest(f"{key} must be between {lo:g} and {hi:g}")
    return v


def _context(payload: dict, key: str = "adjustments"):
    """(path, settings with a Temp, base render, baseline Kelvin)."""
    ed = _ed()
    path, entry = _photo(payload.get("path"))
    adj = _settings(payload.get(key), path, entry)
    base, baseline = ed._load_base_render(path)
    if payload.get(key) is None:
        adj = ed.resolve_edit_version(adj, baseline)      # saved edits may predate edit versions
    adj.setdefault("color_temp", baseline)
    return path, adj, base, baseline


def _require_current(adj: dict) -> None:
    """The solvers assume the linear-light maths; old-maths edits must be upgraded first."""
    if int(adj.get("edit_version", 2) or 2) < 2:
        raise BadRequest("This edit uses the old colour maths; use Upgrade edit first", 409)


def _render(path: Path, adj: dict, base, baseline) -> np.ndarray:
    ed = _ed()
    return ed.apply_adjustments(base, baseline, ed.lens.attach_meta(path, adj),
                                cache_id=str(path.resolve()))


def _data_uri(arr: np.ndarray) -> str:
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG", compress_level=1)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _pre_wb(base, baseline, adj) -> np.ndarray:
    """The frame as shown (crop, straighten, ...) but before white balance."""
    return _ed()._stage_geometry(base, adj)


# ---------------------------------------------------------------------------
# Auto tone and white balance
# ---------------------------------------------------------------------------

@bp.route("/api/develop/auto_tone", methods=["POST"])
def api_auto_tone():
    """
    Lightroom's Auto: Exposure, Contrast, Highlights, Shadows, Whites, Blacks
    and Vibrance. ``adjustments`` in the reply is the current set with those
    seven replaced -- post it to /api/history/record with the label for a
    single undoable step.
    """
    t0 = time.perf_counter()
    path, adj, base, baseline = _context(_payload())
    _require_current(adj)
    wb = {k: adj[k] for k in ("color_temp", "tint") if k in adj}
    img = _ed()._stage_tone(_pre_wb(base, baseline, adj), baseline, wb)
    result = analysis.auto_tone(img)
    return jsonify({
        "settings": result["settings"],
        "adjustments": {**adj, **result["settings"]},
        "label": "Auto Tone",
        "before": result["before"], "after": result["after"],
        "ms": round((time.perf_counter() - t0) * 1000, 1),
    })


def _auto_wb(path, adj, base, baseline) -> dict:
    return analysis.auto_white_balance(_pre_wb(base, baseline, adj), baseline)


@bp.route("/api/develop/auto_wb", methods=["POST"])
def api_auto_wb():
    path, adj, base, baseline = _context(_payload())
    _require_current(adj)
    res = _auto_wb(path, adj, base, baseline)
    return jsonify({**res, "label": "Auto White Balance", "baseline_temp": baseline,
                    "adjustments": {**adj, "color_temp": res["color_temp"], "tint": res["tint"]}})


@bp.route("/api/develop/wb_presets", methods=["GET", "POST"])
def api_wb_presets():
    """As Shot, Auto, Daylight, Cloudy, Shade, Tungsten, Fluorescent, Flash."""
    path, adj, base, baseline = _context(_payload())
    auto = _auto_wb(path, adj, base, baseline)
    return jsonify({"baseline_temp": baseline,
                    "presets": analysis.wb_presets(baseline, (auto["color_temp"], auto["tint"]))})


@bp.route("/api/develop/wb_pick", methods=["POST"])
def api_wb_pick():
    """
    White balance eyedropper. ``x``/``y`` are 0..1 fractions of the displayed
    preview; ``radius`` is the sample radius in preview pixels (default 3).
    Returns the Temp/Tint that make that neighbourhood neutral, plus the
    sampled RGB (0..255) as displayed now and before white balance.
    """
    payload = _payload()
    x = _number(payload, "x", None, 0.0, 1.0) if "x" in payload else _missing("x")
    y = _number(payload, "y", None, 0.0, 1.0) if "y" in payload else _missing("y")
    radius = int(_number(payload, "radius", 3, 0, 50))
    path, adj, base, baseline = _context(payload)
    _require_current(adj)

    geo = _pre_wb(base, baseline, adj)
    gh, gw = geo.shape[:2]
    cx, cy = min(gw - 1, int(x * gw)), min(gh - 1, int(y * gh))
    patch = _disc(geo, cx, cy, radius)
    import colour

    pre_lin = colour.to_linear(patch.reshape(-1, 1, 3)).reshape(-1, 3).mean(axis=0)
    pre = colour.to_encoded(pre_lin.reshape(1, 1, 3)).reshape(3)
    shown = _disc(_render(path, adj, base, baseline), cx, cy, radius).mean(axis=0)

    warnings = []
    if pre.max() >= 0.98:
        warnings.append("clipped: the sampled area has blown channels")
    if pre.min() < 0.02:
        raise BadRequest("The sampled area is too dark to judge a neutral", 422)
    temp, tint = analysis.wb_solve_linear(pre_lin, baseline)
    gains = analysis.wb_gains(temp, tint, baseline)
    return jsonify({
        "color_temp": temp, "tint": tint,
        "rgb": [int(round(v * 255)) for v in shown],
        "rgb_before_wb": [int(round(v * 255)) for v in pre],
        "rgb_after": [int(round(float(v) * 255)) for v in
                      colour.to_encoded(np.clip(pre_lin * gains, 0, 1).reshape(1, 1, 3)).reshape(3)],
        "pixels": int(len(patch)), "x": x, "y": y, "radius": radius,
        "warnings": warnings, "label": "White Balance Picker",
    })


def _missing(name: str):
    raise BadRequest(f"{name} is required")


def _disc(img: np.ndarray, cx: int, cy: int, r: int) -> np.ndarray:
    """RGB pixels within radius ``r`` of (cx, cy), shape (n, 3)."""
    h, w = img.shape[:2]
    x0, x1, y0, y1 = max(0, cx - r), min(w, cx + r + 1), max(0, cy - r), min(h, cy + r + 1)
    yy, xx = np.mgrid[y0:y1, x0:x1]
    inside = (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r
    return img[y0:y1, x0:x1][inside][:, :3]


# ---------------------------------------------------------------------------
# Clipping and histogram
# ---------------------------------------------------------------------------

@bp.route("/api/develop/clipping", methods=["POST"])
def api_clipping():
    """
    Clipping overlay of the rendered preview: shadows blue, highlights red.

    ``format``: ``json`` (default; overlay as a PNG data URI), ``png`` (the raw
    RGBA overlay) or ``bitmask`` (adds both masks as base64 np.packbits rows).
    ``shadow`` / ``highlight`` are thresholds as 0..1 fractions or 0..255 levels.
    """
    payload = _payload()
    fmt = str(payload.get("format", "json"))
    if fmt not in ("json", "png", "bitmask"):
        raise BadRequest("format must be json, png or bitmask")
    lo = _number(payload, "shadow", 2, 0, 255)
    hi = _number(payload, "highlight", 253, 0, 255)
    path, adj, base, baseline = _context(payload)
    try:
        res = analysis.clipping(_render(path, adj, base, baseline), lo, hi)
    except ValueError as exc:
        raise BadRequest(str(exc))
    rgba = analysis.clipping_rgba(res["masks"])
    if fmt == "png":
        buf = io.BytesIO()
        Image.fromarray(rgba).save(buf, format="PNG", compress_level=1)
        resp = Response(buf.getvalue(), mimetype="image/png")
        resp.headers["X-Clipping-Shadows"] = str(res["percent"]["shadows"])
        resp.headers["X-Clipping-Highlights"] = str(res["percent"]["highlights"])
        return resp
    out = {"overlay": _data_uri(rgba), "width": rgba.shape[1], "height": rgba.shape[0],
           "percent": res["percent"], "thresholds": res["thresholds"],
           "colors": {"shadows": list(analysis.SHADOW_COLOR),
                      "highlights": list(analysis.HIGHLIGHT_COLOR)}}
    if fmt == "bitmask":
        out["masks"] = {k: base64.b64encode(np.packbits(m, axis=1).tobytes()).decode("ascii")
                        for k, m in res["masks"].items()}
        out["mask_format"] = "packbits rows, MSB first, height x ceil(width/8) bytes"
    return jsonify(out)


@bp.route("/api/develop/histogram", methods=["POST"])
def api_histogram():
    """256-bin (or 128/64/32...) histogram of the rendered preview, with clipping percentages."""
    payload = _payload()
    bins = int(_number(payload, "bins", 256, 1, 256))
    if 256 % bins:
        raise BadRequest("bins must divide 256")
    path, adj, base, baseline = _context(payload)
    t0 = time.perf_counter()
    out = _render(path, adj, base, baseline)
    t1 = time.perf_counter()
    hist = analysis.histogram(out, bins=bins, stride=int(_number(payload, "stride", 2, 1, 8)))
    clip = analysis.clipping(out)["percent"]
    return jsonify({**hist, "clipping": clip,
                    "ms": round((time.perf_counter() - t1) * 1000, 2),
                    "render_ms": round((t1 - t0) * 1000, 1)})


# ---------------------------------------------------------------------------
# Tiles
# ---------------------------------------------------------------------------

def _tile_args() -> tuple[Path, dict, dict]:
    q = request.args
    path, entry = _photo(q.get("photo") or q.get("path"))
    if q.get("sid"):
        adj = tile_server.recall_settings(q["sid"])
        if adj is None:
            raise BadRequest("Unknown settings id; call /api/tile/meta again", 410)
    else:
        adj = _settings(q.get("settings"), path, entry)
    return path, adj, q


def _int(q, key: str, default=None) -> int:
    try:
        return int(q.get(key, default))
    except (TypeError, ValueError):
        raise BadRequest(f"{key} must be an integer")


@bp.route("/api/tile")
def api_tile():
    """
    One 512 px tile of the fully rendered photo.

    ``photo`` (library path), ``z`` (fit | 50 | 100 | 200), ``x``/``y`` (tile
    column and row, 0-based), and the settings as ``settings`` (JSON) or
    ``sid`` (id from /api/tile/meta). Edge tiles are cropped to the image.
    ``format`` is jpeg (default) or png.
    """
    path, adj, q = _tile_args()
    try:
        data, mime, hit = tile_server.get_tile(
            path, adj, q.get("z", "fit"), _int(q, "x"), _int(q, "y"),
            q.get("format", "jpeg"), _int(q, "fit_edge", 0) or None)
    except LookupError as exc:
        raise BadRequest(str(exc), 404)
    except ValueError as exc:
        raise BadRequest(str(exc))
    except MemoryError as exc:
        raise BadRequest(str(exc), 413)
    resp = Response(data, mimetype=mime)
    resp.headers["X-Tile-Cache"] = "hit" if hit else "miss"
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@bp.route("/api/tile/meta", methods=["GET", "POST"])
def api_tile_meta():
    """Image size, tile grid per zoom level, and a ``settings_id`` for /api/tile."""
    if request.method == "POST":
        payload = _payload()
        path, entry = _photo(payload.get("path") or payload.get("photo"))
        adj = _settings(payload.get("adjustments", payload.get("settings")), path, entry)
        fit = int(_number(payload, "fit_edge", 0, 0, 20000)) or None
    else:
        path, adj, q = _tile_args()
        fit = _int(q, "fit_edge", 0) or None
    return jsonify(tile_server.describe(path, adj, fit))
