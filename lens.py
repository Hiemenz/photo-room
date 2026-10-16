"""
Lens corrections: profile (lensfun), manual distortion and vignetting,
chromatic aberration and defringe.

Order inside one pass, on the original frame:
    profile or manual vignetting -> distortion (and lateral CA) -> CA -> defringe

Flat keys, so presets / copy-paste / history carry them:

    lens_profile      bool       use the lensfun profile for this camera + lens
    lens_distortion   -100..100  manual; positive straightens barrel distortion
    lens_vignette     -100..100  manual; positive lightens the corners
    ca_remove         bool       remove lateral chromatic aberration
    defringe_purple   0..20      desaturate purple fringes at edges
    defringe_green    0..20      desaturate green fringes at edges

lensfun (the optional ``lensfunpy`` package) is never required: without it,
or when no profile matches, the manual sliders and the generic CA fix are used
and nothing raises.
"""
from __future__ import annotations

import json
import math
import subprocess
import threading
from pathlib import Path

import numpy as np

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

CONTROLS = [
    {"key": "lens_distortion", "label": "Distortion", "min": -100, "max": 100, "step": 1, "default": 0},
    {"key": "lens_vignette",   "label": "Vignetting", "min": -100, "max": 100, "step": 1, "default": 0},
    {"key": "defringe_purple", "label": "Purple fringe", "min": 0, "max": 20, "step": 1, "default": 0},
    {"key": "defringe_green",  "label": "Green fringe",  "min": 0, "max": 20, "step": 1, "default": 0},
]
TOGGLES = [
    {"key": "lens_profile", "label": "Use lens profile (lensfun)", "default": False},
    {"key": "ca_remove", "label": "Remove chromatic aberration", "default": False},
]
DEFAULTS = {c["key"]: c["default"] for c in CONTROLS + TOGGLES}

DISTORTION_K = 0.25     # radial term at slider +-100 (corner radius = 1)
VIGNETTE_GAIN = 1.5     # corner brightening at slider +100
CA_SCALE_RANGE = 0.005  # generic CA search: +-0.5% radial scale of red/blue


def _num(adj: dict, key: str) -> float:
    try:
        v = float(adj.get(key, DEFAULTS[key]))
    except (TypeError, ValueError):
        return float(DEFAULTS[key])
    return v if math.isfinite(v) else float(DEFAULTS[key])


def is_neutral(adj: dict) -> bool:
    return not (
        adj.get("lens_profile") or adj.get("ca_remove")
        or any(abs(_num(adj, c["key"])) > 1e-9 for c in CONTROLS)
    )


# ---------------------------------------------------------------------------
# lensfun
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_db = None
_db_failed = False
_meta_cache: dict[tuple, dict | None] = {}


def _lensfunpy():
    try:
        import lensfunpy
        return lensfunpy
    except Exception:
        return None


def lensfun_available() -> bool:
    return _lensfunpy() is not None


def _database():
    global _db, _db_failed
    with _lock:
        if _db is None and not _db_failed:
            mod = _lensfunpy()
            try:
                _db = mod.Database() if mod else None
            except Exception:
                _db = None
            _db_failed = _db is None
        return _db


def exif_meta(path: Path) -> dict | None:
    """Camera / lens / shooting data from EXIF, or None if unreadable."""
    try:
        stat = Path(path).stat()
        key = (str(path), stat.st_mtime_ns)
    except OSError:
        return None
    with _lock:
        if key in _meta_cache:
            return _meta_cache[key]
    meta = None
    try:
        from process import EXIFTOOL_TIMEOUT
        out = subprocess.run(
            ["exiftool", "-json", "-n", "-Make", "-Model", "-LensModel", "-LensID",
             "-FocalLength", "-FNumber", "-SubjectDistance", "-ScaleFactor35efl", str(path)],
            capture_output=True, text=True, timeout=EXIFTOOL_TIMEOUT)
        row = json.loads(out.stdout)[0]
        lens = row.get("LensModel") or (row.get("LensID") if isinstance(row.get("LensID"), str) else None)
        if row.get("Make") and row.get("Model") and lens and row.get("FocalLength"):
            meta = {
                "maker": str(row["Make"]), "model": str(row["Model"]), "lens": str(lens),
                "focal": float(row["FocalLength"]),
                "aperture": float(row.get("FNumber") or 0) or 8.0,
                "distance": float(row.get("SubjectDistance") or 0) or 1000.0,
                "crop": float(row.get("ScaleFactor35efl") or 0) or None,
            }
    except Exception:
        meta = None
    with _lock:
        _meta_cache[key] = meta
    return meta


def find_profile(meta: dict | None):
    """(camera, lens) from the lensfun database, or None when there is no match."""
    if not meta:
        return None
    db = _database()
    if db is None:
        return None
    try:
        cams = db.find_cameras(meta["maker"], meta["model"])
        if not cams:
            return None
        lenses = db.find_lenses(cams[0], meta["maker"], meta["lens"]) or db.find_lenses(cams[0], None, meta["lens"])
        if not lenses:
            return None
        return cams[0], lenses[0]
    except Exception:
        return None


def attach_meta(path: Path, adj: dict) -> dict:
    """
    Copy of ``adj`` carrying the EXIF needed for the lens profile (as
    ``_lens_meta``). Returns ``adj`` unchanged unless the profile is on, so
    the common case costs nothing and runs no subprocess.
    """
    if not adj.get("lens_profile") or not lensfun_available():
        return adj
    meta = exif_meta(path)
    if not meta:
        return adj
    out = dict(adj)
    out["_lens_meta"] = meta
    return out


def describe(path: Path) -> dict:
    """What the UI shows about lens support for this photo."""
    info = {"lensfun": lensfun_available(), "profile": None, "camera": None}
    if not info["lensfun"]:
        return info
    meta = exif_meta(path)
    found = find_profile(meta)
    if found:
        cam, lens = found
        info["camera"] = f"{cam.maker} {cam.model}"
        info["profile"] = f"{lens.maker} {lens.model}"
    return info


def _apply_profile(img: np.ndarray, adj: dict):
    """
    Apply the lensfun profile. Returns (image, did_correct_ca) or None when
    no profile applies, so the caller falls back to the manual controls.
    """
    meta = adj.get("_lens_meta")
    mod_pkg = _lensfunpy()
    if not meta or mod_pkg is None or cv2 is None:
        return None
    found = find_profile(meta)
    if not found:
        return None
    cam, lens = found
    h, w = img.shape[:2]
    crop = meta.get("crop") or cam.crop_factor
    mod = mod_pkg.Modifier(lens, crop, w, h)
    mod.initialize(meta["focal"], meta["aperture"], meta["distance"], pixel_format=np.float32)
    out = np.ascontiguousarray(img, dtype=np.float32).copy()
    mod.apply_color_modification(out)

    ca_done = False
    sub = mod.apply_subpixel_distortion()
    if sub is not None:
        sub = np.asarray(sub, dtype=np.float32)
        if sub.ndim == 3 and sub.shape[2] == 6:
            sub = sub.reshape(h, w, 3, 2)
        chans = [cv2.remap(out[:, :, c], np.ascontiguousarray(sub[:, :, c, :]), None,
                           cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE) for c in range(3)]
        out = np.stack(chans, axis=2)
        ca_done = True
    else:
        coords = mod.apply_geometry_distortion()
        if coords is not None:
            out = cv2.remap(out, np.ascontiguousarray(coords, dtype=np.float32), None,
                            cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return np.clip(out, 0.0, 1.0), ca_done


# ---------------------------------------------------------------------------
# Manual corrections
# ---------------------------------------------------------------------------

def _radius_grid(h: int, w: int):
    """Pixel grids and normalised radius (1 at the corners)."""
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    norm = math.hypot(cx, cy) or 1.0
    return xs, ys, cx, cy, norm


def manual_distortion(img: np.ndarray, amount: float) -> np.ndarray:
    if abs(amount) < 1e-9 or cv2 is None:
        return img
    h, w = img.shape[:2]
    xs, ys, cx, cy, norm = _radius_grid(h, w)
    k = amount / 100.0 * DISTORTION_K
    r2 = ((xs - cx) ** 2 + (ys - cy) ** 2) / (norm * norm)
    f = 1.0 - k * r2
    map_x = (cx + (xs - cx) * f).astype(np.float32)
    map_y = (cy + (ys - cy) * f).astype(np.float32)
    return cv2.remap(np.ascontiguousarray(img, dtype=np.float32), map_x, map_y,
                     cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def manual_vignette(img: np.ndarray, amount: float) -> np.ndarray:
    if abs(amount) < 1e-9:
        return img
    h, w = img.shape[:2]
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    r2 = ((xs - cx) ** 2 + (ys - cy) ** 2) / (cx * cx + cy * cy or 1.0)
    gain = 1.0 + (amount / 100.0) * VIGNETTE_GAIN * r2
    return np.clip(img * gain[:, :, None], 0.0, 1.0).astype(np.float32)


# ---------------------------------------------------------------------------
# Chromatic aberration
# ---------------------------------------------------------------------------

def _scale_matrix(s: float, w: int, h: int) -> np.ndarray:
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    return np.array([[s, 0, (1 - s) * cx], [0, s, (1 - s) * cy]], dtype=np.float64)


def _grad(plane: np.ndarray) -> np.ndarray:
    gx = cv2.Sobel(plane, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(plane, cv2.CV_32F, 0, 1, ksize=3)
    return np.hypot(gx, gy)


def estimate_ca(img: np.ndarray) -> tuple[float, float]:
    """
    Radial scale (about the centre) that registers the red and blue planes to
    green, found by maximising edge overlap on a shrunken copy. (1, 1) means
    no shift worth correcting.
    """
    if cv2 is None:
        return 1.0, 1.0
    h, w = img.shape[:2]
    k = min(1.0, 700.0 / max(h, w))
    small = img if k >= 1.0 else cv2.resize(img, (int(w * k), int(h * k)), interpolation=cv2.INTER_AREA)
    sh, sw = small.shape[:2]
    g_edges = _grad(np.ascontiguousarray(small[:, :, 1]))
    if float(g_edges.max()) < 0.05:
        return 1.0, 1.0

    def best_for(channel: int) -> float:
        plane = np.ascontiguousarray(small[:, :, channel])

        ref = g_edges - g_edges.mean()
        ref_norm = float(np.sqrt((ref * ref).sum())) or 1.0

        def score(s: float) -> float:
            # Normalised correlation of the edge maps: 1 when they coincide.
            warped = cv2.warpAffine(plane, _scale_matrix(s, sw, sh), (sw, sh),
                                    flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            g = _grad(warped)
            g = g - g.mean()
            return float((g * ref).sum() / ((float(np.sqrt((g * g).sum())) or 1.0) * ref_norm))

        coarse = np.linspace(1 - CA_SCALE_RANGE, 1 + CA_SCALE_RANGE, 21)
        best = max(coarse, key=score)
        step = coarse[1] - coarse[0]
        fine = np.linspace(best - step, best + step, 11)
        best = float(max(fine, key=score))
        # Require a real gain over doing nothing, or noise picks a shift.
        return best if score(best) - score(1.0) > 0.003 else 1.0

    return best_for(0), best_for(2)


def remove_ca(img: np.ndarray) -> np.ndarray:
    if cv2 is None:
        return img
    sr, sb = estimate_ca(img)
    if sr == 1.0 and sb == 1.0:
        return img
    h, w = img.shape[:2]
    out = img.copy()
    for ch, s in ((0, sr), (2, sb)):
        if s != 1.0:
            out[:, :, ch] = cv2.warpAffine(
                np.ascontiguousarray(img[:, :, ch]), _scale_matrix(s, w, h), (w, h),
                flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return out


def defringe(img: np.ndarray, purple: float, green: float) -> np.ndarray:
    """Desaturate purple / green pixels that sit next to a strong edge."""
    if cv2 is None or (purple <= 0 and green <= 0):
        return img
    src = np.ascontiguousarray(img, dtype=np.float32)
    lum = src @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    edge = _grad(lum)
    near_edge = cv2.dilate((edge > 0.15).astype(np.uint8), np.ones((7, 7), np.uint8)).astype(np.float32)
    near_edge = cv2.GaussianBlur(near_edge, (5, 5), 0)
    hsv = cv2.cvtColor(src, cv2.COLOR_RGB2HSV)       # H in degrees for float input
    hue, sat = hsv[:, :, 0], hsv[:, :, 1]
    strength = np.zeros_like(sat)
    if purple > 0:
        strength = np.maximum(strength, np.where((hue >= 250) & (hue <= 320), purple / 20.0, 0.0))
    if green > 0:
        strength = np.maximum(strength, np.where((hue >= 70) & (hue <= 170), green / 20.0, 0.0))
    hsv[:, :, 1] = sat * (1.0 - np.clip(strength * near_edge, 0.0, 1.0))
    return np.clip(cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB), 0.0, 1.0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def apply(img: np.ndarray, adj: dict) -> np.ndarray:
    """All lens corrections for one image. Never raises; returns ``img`` untouched on failure."""
    if is_neutral(adj):
        return img
    out = img
    ca_done = False
    try:
        profiled = None
        if adj.get("lens_profile"):
            try:
                profiled = _apply_profile(out, adj)
            except Exception:
                profiled = None
        if profiled is not None:
            out, ca_done = profiled
        else:
            out = manual_vignette(out, _num(adj, "lens_vignette"))
        # Manual distortion also tops up a profile: it is a user override.
        out = manual_distortion(out, _num(adj, "lens_distortion"))
    except Exception:
        out = img
    try:
        if adj.get("ca_remove") and not ca_done:
            out = remove_ca(out)
        out = defringe(out, _num(adj, "defringe_purple"), _num(adj, "defringe_green"))
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# XMP
# ---------------------------------------------------------------------------

def xmp_arguments(adj: dict) -> list[str]:
    return [
        f"-XMP-crs:LensProfileEnable={1 if adj.get('lens_profile') else 0}",
        f"-XMP-crs:LensManualDistortionAmount={int(round(_num(adj, 'lens_distortion')))}",
        f"-XMP-crs:VignetteAmount={int(round(_num(adj, 'lens_vignette')))}",
        f"-XMP-crs:AutoLateralCA={1 if adj.get('ca_remove') else 0}",
        f"-XMP-crs:DefringePurpleAmount={int(round(_num(adj, 'defringe_purple')))}",
        f"-XMP-crs:DefringeGreenAmount={int(round(_num(adj, 'defringe_green')))}",
    ]


XMP_TAGS = {
    "lens_profile": "LensProfileEnable",
    "lens_distortion": "LensManualDistortionAmount",
    "lens_vignette": "VignetteAmount",
    "ca_remove": "AutoLateralCA",
    "defringe_purple": "DefringePurpleAmount",
    "defringe_green": "DefringeGreenAmount",
}
BOOL_KEYS = ("lens_profile", "ca_remove")
