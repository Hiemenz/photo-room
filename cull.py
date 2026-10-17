"""
Smart culling: burst grouping, per-photo scoring and pick/reject suggestions.

Everything here is a pure function of pixels and numbers, so it is unit-testable
without Flask and runs on-device with numpy, Pillow and OpenCV only.

Pipeline
  1. ``extract`` reads one cached 1024 px thumbnail (thumbs.py; the camera's
     embedded preview, so no raw decode) and returns a small, JSON-able feature
     dict: a 64-bit difference hash, a focus-region sharpness, exposure and
     clipping from the luminance histogram, and, where the OpenCV Haar cascades
     are available, face count, eyes-open and face sharpness.
  2. ``group_photos`` chains frames into bursts by capture time and hash
     distance.
  3. ``suggest`` scores each frame (a transparent heuristic, optionally blended
     with the personal model in cull_store.py) and proposes one pick per group
     and rejects only for clear failures.

Suggestions are advice. Nothing here touches a rating, a flag or a file.
"""
from __future__ import annotations

import datetime as _dt
import io
import math
import threading
from pathlib import Path

import numpy as np
from PIL import Image

try:
    import cv2
except ImportError:  # pragma: no cover - opencv is a dependency, but stay importable
    cv2 = None

FEATURE_VERSION = 2

# --- grouping ---------------------------------------------------------------
BURST_WINDOW_S = 8.0       # frames this close in time may join a burst...
LOOSE_HASH = 16            # ...if their 64-bit hashes differ by at most this many bits
STRICT_HASH = 7            # near-identical frames join even across a longer gap
STRICT_WINDOW_S = 90.0

# --- scoring ----------------------------------------------------------------
SHARP_REF = 1500.0         # Laplacian variance (1024 px) that counts as fully sharp
TILES = 8
FOCUS_TILE_FRACTION = 0.12  # the sharpest 12 % of tiles define the focus region
REJECT_REL_SHARP = 0.40     # a frame this much softer than its group's best...
REJECT_BLOWN = 0.45         # ...or this much of it at pure white / black
FLAT_CONTRAST = 0.02
MIN_ABS_SHARP = 4.0         # ...or almost no detail anywhere (even a lone frame)

FEATURE_NAMES = ("sharp_rel", "sharp_abs", "exposure_ok", "clip_high", "clip_low",
                 "contrast", "has_face", "eyes_open")

_TLS = threading.local()    # a cv2.CascadeClassifier is not safe to share between threads


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------

def dhash(gray: np.ndarray) -> int:
    """64-bit difference hash: 8 rows of 8 'is this pixel brighter than its right neighbour' bits."""
    small = cv2.resize(cv2.GaussianBlur(gray, (0, 0), 1.2), (9, 8), interpolation=cv2.INTER_AREA)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    out = 0
    for b in bits:
        out = (out << 1) | int(b)
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _cascade(name: str):
    if cv2 is None:
        return None
    cache = _TLS.__dict__.setdefault("cascades", {})
    if name not in cache:
        try:
            c = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / name))
            cache[name] = None if c.empty() else c
        except Exception:
            cache[name] = None
    return cache[name]


def _lap_var(gray: np.ndarray) -> float:
    return float(cv2.Laplacian(gray, cv2.CV_32F, ksize=3).var())


def focus_sharpness(gray: np.ndarray) -> tuple[float, float, float]:
    """
    (sharpness, focus_x, focus_y): Laplacian variance of the focus region.

    The frame is cut into a TILES x TILES grid; the focus region is the sharpest
    few tiles, so a crisp subject on smooth grass or sky is not scored down for
    the background, and the region's centre (0..1) is returned for the UI.
    """
    lap = cv2.Laplacian(gray, cv2.CV_32F, ksize=3)
    h, w = lap.shape
    th, tw = h // TILES, w // TILES
    if th < 4 or tw < 4:
        return float(lap.var()), 0.5, 0.5
    cropped = lap[: th * TILES, : tw * TILES].reshape(TILES, th, TILES, tw)
    var = cropped.var(axis=(1, 3))                                  # (TILES, TILES)
    k = max(2, int(round(TILES * TILES * FOCUS_TILE_FRACTION)))
    flat = var.ravel()
    top = np.argsort(flat)[-k:]
    ys, xs = np.divmod(top, TILES)
    weights = flat[top] + 1e-9
    fx = float(((xs + 0.5) / TILES * weights).sum() / weights.sum())
    fy = float(((ys + 0.5) / TILES * weights).sum() / weights.sum())
    return float(flat[top].mean()), fx, fy


def detect_faces(gray: np.ndarray) -> dict:
    """Face and eye cascades; a failure in OpenCV just means "no face information"."""
    try:
        return _detect_faces(gray)
    except Exception:
        return {"faces": 0, "eyes_open": -1.0, "face_sharp": -1.0}


def _detect_faces(gray: np.ndarray) -> dict:
    """
    Faces and eyes via Haar cascades on a ~640 px copy. Cheap and imperfect, so it
    is only a tie-breaker: ``eyes_open`` is the share of faces showing two eyes.
    """
    out = {"faces": 0, "eyes_open": -1.0, "face_sharp": -1.0}
    face_c, eye_c = _cascade("haarcascade_frontalface_default.xml"), _cascade("haarcascade_eye.xml")
    if face_c is None:
        return out
    h, w = gray.shape
    scale = 640.0 / max(h, w)
    small = cv2.resize(gray, (max(1, int(w * scale)), max(1, int(h * scale))),
                       interpolation=cv2.INTER_AREA) if scale < 1 else gray
    s = small.shape[1] / w
    faces = face_c.detectMultiScale(small, scaleFactor=1.15, minNeighbors=8,
                                    minSize=(max(24, small.shape[1] // 14),) * 2)
    if len(faces) == 0:
        return out
    faces = sorted(faces, key=lambda f: -f[2] * f[3])[:5]
    open_share = []
    biggest = None
    for (x, y, fw, fh) in faces:
        if eye_c is not None:
            roi = small[y: y + int(fh * 0.6), x: x + fw]               # eyes live in the top 60 %
            eyes = eye_c.detectMultiScale(roi, scaleFactor=1.1, minNeighbors=5,
                                          minSize=(max(8, fw // 10),) * 2)
            open_share.append(1.0 if len(eyes) >= 2 else (0.5 if len(eyes) == 1 else 0.0))
        if biggest is None:
            biggest = (x, y, fw, fh)
    out["faces"] = len(faces)
    if open_share:
        out["eyes_open"] = float(np.mean(open_share))
    x, y, fw, fh = biggest
    x0, y0 = int(x / s), int(y / s)
    crop = gray[y0: y0 + int(fh / s), x0: x0 + int(fw / s)]
    if crop.size >= 32 * 32:
        # A fixed 160 px face so the number does not depend on how big the face is
        # in this frame, only on how crisp it is.
        crop = cv2.resize(crop, (160, 160), interpolation=cv2.INTER_AREA)
        out["face_sharp"] = _lap_var(crop)
    return out


def extract(data: bytes | np.ndarray) -> dict:
    """Features of one frame from its thumbnail bytes (or an RGB uint8 array)."""
    if isinstance(data, (bytes, bytearray)):
        rgb = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))
    else:
        rgb = np.asarray(data)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    sharp, fx, fy = focus_sharpness(gray)
    lum = gray.astype(np.float32) / 255.0
    p5, p50, p95 = (float(v) for v in np.percentile(lum[::2, ::2], (5, 50, 95)))
    feats = {
        "v": FEATURE_VERSION, "w": int(w), "h": int(h),
        "hash": f"{dhash(gray):016x}",
        "sharp": round(sharp, 3), "sharp_global": round(_lap_var(gray), 3),
        "focus": [round(fx, 3), round(fy, 3)],
        "clip_high": round(float((lum >= 0.985).mean()), 5),
        "clip_low": round(float((lum <= 0.015).mean()), 5),
        "mean": round(float(lum.mean()), 4), "median": round(p50, 4),
        "contrast": round(float(lum.std()), 4), "range": round(p95 - p5, 4),
    }
    feats.update(detect_faces(gray))
    return feats


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def parse_time(text) -> float | None:
    """EXIF-style 'YYYY-MM-DD HH:MM:SS' (or with colons / a suffix) to epoch seconds, else None."""
    if not text:
        return None
    s = str(text).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return _dt.datetime.strptime(s[:19], fmt).replace(tzinfo=_dt.timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def group_photos(items: list[dict], window: float = BURST_WINDOW_S,
                 loose: int = LOOSE_HASH, strict: int = STRICT_HASH) -> list[list[str]]:
    """
    Chain frames into bursts. ``items``: dicts with ``path``, ``t`` (epoch seconds or
    None) and ``hash`` (hex). Frames are ordered by time then name; a frame joins the
    running group when it is close in time to the previous frame and looks alike
    (hash distance <= ``loose``), or looks almost identical (<= ``strict``) within a
    longer gap. With no capture time, only the strict test (on file order) applies.
    """
    ordered = sorted(items, key=lambda i: ((i["t"] if i.get("t") is not None else float("inf")), i["path"]))
    groups: list[list[dict]] = []
    for it in ordered:
        h = int(it["hash"], 16)
        joined = False
        if groups:
            prev = groups[-1][-1]
            d = hamming(h, int(prev["hash"], 16))
            if it.get("t") is not None and prev.get("t") is not None:
                gap = it["t"] - prev["t"]
                joined = (gap <= window and d <= loose) or (gap <= STRICT_WINDOW_S and d <= strict)
            else:
                joined = d <= strict
        if joined:
            groups[-1].append(it)
        else:
            groups.append([it])
    return [[i["path"] for i in g] for g in groups]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def sharp_effective(f: dict) -> float:
    """Focus-region sharpness, blended with the face's when a face was found."""
    s = float(f.get("sharp", 0.0))
    fs = float(f.get("face_sharp", -1.0))
    return 0.5 * s + 0.5 * fs if fs >= 0 else s


def exposure_ok(f: dict) -> float:
    """1 = well exposed, 0 = hopeless. Blown/blocked pixels dominate; a far-off mean adds a little."""
    pen = min(1.0, float(f["clip_high"]) * 6.0 + float(f["clip_low"]) * 4.0)
    pen = min(1.0, pen + max(0.0, abs(float(f["mean"]) - 0.45) - 0.28) * 2.0)
    return 1.0 - pen


def feature_vector(f: dict, sharp_rel: float) -> list[float]:
    """The numbers the personal model sees (all roughly 0..1), in FEATURE_NAMES order."""
    abs_norm = min(1.0, math.log1p(sharp_effective(f)) / math.log1p(SHARP_REF))
    face = 1.0 if f.get("faces", 0) > 0 else 0.0
    eyes = float(f.get("eyes_open", -1.0))
    return [round(sharp_rel, 4), round(abs_norm, 4), round(exposure_ok(f), 4),
            min(1.0, float(f["clip_high"]) * 5), min(1.0, float(f["clip_low"]) * 5),
            min(1.0, float(f["contrast"]) * 2), face, max(0.0, eyes) if face else 0.0]


def heuristic_score(vec: list[float]) -> float:
    """0..100 from the feature vector: sharpness 50 %, exposure 25 %, faces and eyes 25 %."""
    sharp_rel, sharp_abs, expo, _, _, _, face, eyes = vec
    sharp = 0.6 * sharp_rel + 0.4 * sharp_abs
    people = eyes if face else 1.0
    return 100.0 * (0.50 * sharp + 0.25 * expo + 0.25 * people)


def _explain(f: dict, vec: list[float], best: dict | None, group_size: int) -> list[str]:
    why = []
    sharp_rel, _, expo, _, _, _, face, eyes = vec
    if group_size > 1:
        if sharp_rel >= 0.97:
            why.append("sharpest focus region in the burst")
        elif sharp_rel < REJECT_REL_SHARP + 0.2:
            why.append(f"focus region is {100 - sharp_rel * 100:.0f}% softer than the burst's best")
    if f["clip_high"] >= 0.02:
        why.append(f"{f['clip_high'] * 100:.1f}% of the frame is blown to white")
    if f["clip_low"] >= 0.03:
        why.append(f"{f['clip_low'] * 100:.1f}% of the frame is crushed to black")
    if f["contrast"] < FLAT_CONTRAST:
        why.append("almost no tonal range")
    if face:
        if eyes >= 0.99:
            why.append("eyes open")
        elif eyes <= 0.01:
            why.append("eyes look closed")
        else:
            why.append("eyes partly visible")
    if expo >= 0.9 and not why:
        why.append("well exposed")
    if float(f.get("sharp", 0)) < MIN_ABS_SHARP:
        why.append("almost no fine detail (out of focus or motion blur)")
    return why


def _clearly_bad(f: dict, vec: list[float], group_size: int, is_best: bool,
                 best_eyes: float) -> str | None:
    if f["clip_high"] >= REJECT_BLOWN:
        return f"{f['clip_high'] * 100:.0f}% blown to white; no edit recovers it"
    if f["clip_low"] >= REJECT_BLOWN:
        return f"{f['clip_low'] * 100:.0f}% crushed to black; no edit recovers it"
    if f["contrast"] < FLAT_CONTRAST:
        return "the frame carries almost no tonal range"
    if float(f.get("sharp", 0)) < MIN_ABS_SHARP:
        return "almost no detail anywhere: badly out of focus or motion blur"
    if group_size > 1 and not is_best:
        if vec[0] < REJECT_REL_SHARP:
            return "much softer than a better frame in the same burst"
        if vec[6] and vec[7] <= 0.01 and best_eyes >= 0.5:
            return "eyes look closed and another frame in the burst has them open"
    return None


def suggest(items: list[dict], feats: dict[str, dict], model=None,
            window: float = BURST_WINDOW_S, loose: int = LOOSE_HASH, strict: int = STRICT_HASH) -> dict:
    """
    Groups and per-photo suggestions.

    ``items``: ``{path, name, t}`` for each photo; ``feats``: path -> extract() dict
    (photos without features are skipped). ``model`` is a cull_store.PickModel, or
    None for the plain heuristic. Returns ``{"groups": [...], "photos": {path: {...}}}``;
    each photo has ``score``, ``heuristic``, ``action`` (pick | reject | none),
    ``reasons`` and ``vec``.
    """
    usable = [dict(i, hash=feats[i["path"]]["hash"]) for i in items if i["path"] in feats]
    groups = group_photos(usable, window, loose, strict)
    photos: dict[str, dict] = {}
    out_groups = []
    for gi, paths in enumerate(groups):
        n = len(paths)
        sharps = {p: sharp_effective(feats[p]) for p in paths}
        top = max(sharps.values()) or 1.0
        scored = []
        for p in paths:
            rel = math.sqrt(sharps[p] / top) if top > 0 else 1.0
            vec = feature_vector(feats[p], rel)
            h = heuristic_score(vec)
            s = model.blend(h, vec) if model is not None else h
            scored.append((p, vec, h, s))
        best_path = max(scored, key=lambda t: t[3])[0] if n > 1 else None
        best_eyes = next((v[7] for p, v, _, _ in scored if p == best_path), 0.0)
        for p, vec, h, s in scored:
            f = feats[p]
            is_best = p == best_path
            bad = _clearly_bad(f, vec, n, is_best, best_eyes)
            reasons = _explain(f, vec, None, n)
            action = "none"
            if is_best and not bad:
                action = "pick"
                reasons.insert(0, "best of the burst" if n > 1 else "")
            elif bad:
                action = "reject"
                reasons.insert(0, bad)
            if model is not None and action == "none" and model.rejects(vec):
                action = "reject"
                reasons.insert(0, "looks like frames you have rejected before")
            photos[p] = {"path": p, "group": gi, "score": round(s, 1), "heuristic": round(h, 1),
                         "action": action, "reasons": [r for r in reasons if r], "vec": vec}
        order = sorted((t[0] for t in scored), key=lambda p: -photos[p]["score"])
        out_groups.append({"id": gi, "paths": paths, "best": best_path, "ranked": order})
    return {"groups": out_groups, "photos": photos}
