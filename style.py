#!/usr/bin/env python3
"""
Learns your editing style from the edits you save.

Every save records what the photo looked like and what you chose. The model
learns the *correction* to the built-in automatic values rather than the
values themselves, which matters for two reasons: with no data the
correction is zero and behaviour is identical to the plain heuristic, and
with a little data it only has to learn "you push contrast a bit further
than the default" rather than re-derive exposure from scratch.

Storage is a single DuckDB file. The training set is one row per edit — a
few megabytes even after tens of thousands of edits.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import editversion
import geometry

try:
    import duckdb
except ImportError:  # pragma: no cover - exercised only without the dependency
    duckdb = None


def resolve_db_path() -> Path:
    """
    Where the learned-style store lives.

    PHOTO_ROOM_STYLE_DB redirects it. Without that, running the editor
    against test photographs records their edits as if they were the
    photographer's own, quietly teaching the model from throwaway data.
    """
    override = os.environ.get("PHOTO_ROOM_STYLE_DB")
    return Path(override) if override else Path.home() / ".photo-room" / "style.duckdb"


DB_PATH = resolve_db_path()

# Targets the model predicts a correction for. Monochrome is deliberately
# excluded: converting a photo to black and white is an intent, not a
# tonal preference, and guessing it wrong is far more jarring than a
# slightly-off contrast value.
LEARNED_KEYS = [
    "exposure", "contrast", "highlights", "shadows",
    "whites", "blacks", "tint", "vibrance", "saturation",
    "temp_ratio",     # temperature as a multiple of the camera's own WB
]

FEATURE_KEYS = [
    "median_lum", "p10", "p90", "dynamic_range",
    "log_iso", "rb_ratio", "clipped_high", "clipped_low",
]

# How strongly to trust the model early on. The prediction is scaled by
# n / (n + PRIOR_STRENGTH), so a handful of edits nudge the defaults
# rather than overwriting them.
PRIOR_STRENGTH = 12

# Ridge penalty. High, because the feature count is close to the sample
# count for a long time and an overconfident fit is worse than a flat one.
RIDGE_LAMBDA = 5.0

MIN_SAMPLES = 3

# Clamps matching the editor's slider ranges, so a learned correction can
# never push a value somewhere the UI could not.
LIMITS = {
    "exposure": (-5.0, 5.0),
    "contrast": (-100, 100),
    "highlights": (-100, 100),
    "shadows": (-100, 100),
    "whites": (-100, 100),
    "blacks": (-100, 100),
    "tint": (-150, 150),
    "vibrance": (-100, 100),
    "saturation": (-100, 100),
    "temp_ratio": (0.4, 2.5),
}

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------

def extract_features(rgb: np.ndarray, iso: int, cam_mul: np.ndarray) -> dict:
    """
    Describe a photo in the terms that plausibly drive an editing decision:
    how bright it is, how much range it has, how noisy, and how warm.
    """
    sample = rgb[::4, ::4].astype(np.float32) / 255.0
    lum = 0.2126 * sample[:, :, 0] + 0.7152 * sample[:, :, 1] + 0.0722 * sample[:, :, 2]

    p10, p50, p90 = (float(v) for v in np.percentile(lum, (10, 50, 90)))
    rb = float(cam_mul[0] / cam_mul[2]) if len(cam_mul) > 2 and cam_mul[2] > 0 else 1.0

    return {
        "median_lum": p50,
        "p10": p10,
        "p90": p90,
        "dynamic_range": p90 - p10,
        "log_iso": float(np.log2(max(50, iso) / 100.0)),
        "rb_ratio": rb,
        "clipped_high": float((lum >= 0.99).mean()),
        "clipped_low": float((lum <= 0.01).mean()),
    }


def _feature_vector(features: dict) -> np.ndarray:
    return np.array([float(features.get(k, 0.0)) for k in FEATURE_KEYS], dtype=np.float64)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _connect(db_path: Path | None = None):
    if duckdb is None:
        raise RuntimeError("duckdb is not installed; run: uv add duckdb")
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE IF NOT EXISTS presets (
            name TEXT PRIMARY KEY,
            created_at TIMESTAMP,
            adjustments JSON
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS edits (
            id BIGINT,
            recorded_at TIMESTAMP,
            photo TEXT,
            features JSON,
            auto JSON,
            chosen JSON,
            residual JSON
        )
    """)
    return con


def record_edit(
    photo: Path,
    features: dict,
    auto: dict,
    chosen: dict,
    db_path: Path | None = None,
) -> None:
    """
    Store one training example: what the automatic pass proposed, and what
    the photographer actually kept.
    """
    # Stamp which colour maths the edit was made under (rows recorded before
    # edit versions existed have no stamp, see load_training_data).
    chosen = {**chosen, "edit_version": chosen.get("edit_version") or editversion.CURRENT}
    residual = {}
    for key in LEARNED_KEYS:
        if key not in chosen:
            continue
        # The heuristic emits only exposure, shadows, highlights, temp and
        # tint. Requiring a key in both dicts meant contrast, whites, blacks,
        # vibrance and saturation were never recorded at all — half the
        # learnable settings, silently dropped before any model saw them.
        baseline = auto.get(key, 0.0)
        try:
            residual[key] = float(chosen[key]) - float(baseline)
        except (TypeError, ValueError):
            continue

    if not residual:
        return

    with _lock:
        con = _connect(db_path)
        try:
            next_id = con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM edits").fetchone()[0]
            con.execute(
                "INSERT INTO edits VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    next_id,
                    datetime.now(timezone.utc),
                    str(photo),
                    json.dumps(features),
                    json.dumps(auto),
                    json.dumps(chosen),
                    json.dumps(residual),
                ],
            )
        finally:
            con.close()


def load_training_data(db_path: Path | None = None) -> list[tuple[dict, dict]]:
    """Return [(features, residual)] for every recorded edit."""
    with _lock:
        con = _connect(db_path)
        try:
            rows = con.execute("SELECT features, residual, chosen FROM edits ORDER BY id").fetchall()
        finally:
            con.close()
    out = []
    for f, r, c in rows:
        residual = json.loads(r)
        version = int((json.loads(c) or {}).get("edit_version") or editversion.LEGACY)
        if version < editversion.CURRENT and "exposure" in residual:
            # Exposure was a gain on gamma-coded values: the same look needs
            # about 2.2x the stops in linear light.
            residual["exposure"] = float(residual["exposure"]) * editversion.EXPOSURE_FACTOR
        out.append((json.loads(f), residual))
    return out


def clear_history(db_path: Path | None = None) -> int:
    """Forget everything learned. Returns how many edits were discarded."""
    with _lock:
        con = _connect(db_path)
        try:
            n = con.execute("SELECT COUNT(*) FROM edits").fetchone()[0]
            con.execute("DELETE FROM edits")
        finally:
            con.close()
    return int(n)


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

# Framing, perspective, spots and masks belong to one photograph, so a preset
# never carries them (geometry.PHOTO_SPECIFIC_KEYS, shared with batch apply).
PRESET_EXCLUDED = geometry.PHOTO_SPECIFIC_KEYS


def save_preset(name: str, adjustments: dict, db_path: Path | None = None) -> None:
    """Store a named set of adjustments, replacing one of the same name."""
    name = name.strip()
    if not name:
        raise ValueError("A preset needs a name")

    stored = geometry.without_photo_specific(adjustments, drop_private=True)

    # Temperature is kept as a ratio of the source photo's own white balance
    # so the preset means the same thing under different light.
    baseline = float(adjustments.get("_baseline_temp") or adjustments.get("color_temp") or 5500)
    if "color_temp" in stored and baseline > 0:
        stored["temp_ratio"] = float(stored.pop("color_temp")) / baseline

    with _lock:
        con = _connect(db_path)
        try:
            con.execute("DELETE FROM presets WHERE name = ?", [name])
            con.execute(
                "INSERT INTO presets VALUES (?, ?, ?)",
                [name, datetime.now(timezone.utc), json.dumps(stored)],
            )
        finally:
            con.close()


def list_presets(db_path: Path | None = None) -> list[str]:
    try:
        with _lock:
            con = _connect(db_path)
            try:
                rows = con.execute("SELECT name FROM presets ORDER BY name").fetchall()
            finally:
                con.close()
        return [r[0] for r in rows]
    except Exception:
        return []


def get_preset(name: str, baseline_temp: float = 5500, db_path: Path | None = None) -> dict | None:
    """Return a preset's adjustments, resolved against this photo's own WB."""
    with _lock:
        con = _connect(db_path)
        try:
            row = con.execute(
                "SELECT adjustments FROM presets WHERE name = ?", [name]
            ).fetchone()
        finally:
            con.close()
    if not row:
        return None

    # Presets saved before geometry/masks were excluded may still carry them.
    adjustments = geometry.without_photo_specific(json.loads(row[0]), drop_private=True)
    ratio = adjustments.pop("temp_ratio", None)
    if ratio is not None:
        adjustments["color_temp"] = int(round(float(baseline_temp) * float(ratio)))
    return adjustments


def delete_preset(name: str, db_path: Path | None = None) -> bool:
    with _lock:
        con = _connect(db_path)
        try:
            before = con.execute("SELECT COUNT(*) FROM presets WHERE name = ?", [name]).fetchone()[0]
            con.execute("DELETE FROM presets WHERE name = ?", [name])
        finally:
            con.close()
    return bool(before)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

class StyleModel:
    """
    One ridge regression per adjustment, predicting the correction to apply
    to the automatic value.

    Ridge with an intercept degrades gracefully as the sample count falls:
    with very few edits the feature weights shrink toward zero and the
    intercept carries the prediction, which amounts to "this photographer
    tends to add about this much contrast" — usually the right first guess.
    """

    def __init__(self):
        self.weights: dict[str, np.ndarray] = {}
        self.intercepts: dict[str, float] = {}
        self.mean = np.zeros(len(FEATURE_KEYS))
        self.scale = np.ones(len(FEATURE_KEYS))
        self.n_samples = 0
        self.per_key_counts: dict[str, int] = {}

    @property
    def is_trained(self) -> bool:
        return self.n_samples >= MIN_SAMPLES and bool(self.weights)

    @property
    def confidence(self) -> float:
        """0..1 — how much of the learned correction to actually apply."""
        if self.n_samples < MIN_SAMPLES:
            return 0.0
        return self.n_samples / (self.n_samples + PRIOR_STRENGTH)

    def fit(self, samples: list[tuple[dict, dict]]) -> "StyleModel":
        self.n_samples = len(samples)
        if self.n_samples < MIN_SAMPLES:
            return self

        X = np.array([_feature_vector(f) for f, _ in samples], dtype=np.float64)

        # Standardise so the ridge penalty falls evenly across features that
        # live on very different scales (ISO in stops vs a 0..1 luminance).
        self.mean = X.mean(axis=0)
        self.scale = X.std(axis=0)
        self.scale[self.scale < 1e-9] = 1.0
        Xs = (X - self.mean) / self.scale

        n_features = Xs.shape[1]
        penalty = RIDGE_LAMBDA * np.eye(n_features)

        for key in LEARNED_KEYS:
            present = [i for i, (_, r) in enumerate(samples) if key in r]
            self.per_key_counts[key] = len(present)
            if len(present) < MIN_SAMPLES:
                continue

            y = np.array([samples[i][1][key] for i in present], dtype=np.float64)
            # The intercept is the mean residual; the weights explain the rest.
            intercept = float(y.mean())
            centred = y - intercept

            Xk = Xs[present]
            gram_k = Xk.T @ Xk + penalty
            try:
                w = np.linalg.solve(gram_k, Xk.T @ centred)
            except np.linalg.LinAlgError:  # pragma: no cover - singular is rare
                w = np.zeros(n_features)

            self.weights[key] = w
            self.intercepts[key] = intercept

        return self

    def predict(self, features: dict) -> dict:
        """Return the correction to add to each automatic value."""
        if not self.is_trained:
            return {}

        x = (_feature_vector(features) - self.mean) / self.scale
        confidence = self.confidence

        out = {}
        for key, w in self.weights.items():
            raw = self.intercepts[key] + float(w @ x)
            out[key] = raw * confidence
        return out


def load_model(db_path: Path | None = None) -> StyleModel:
    try:
        samples = load_training_data(db_path)
    except Exception:
        return StyleModel()
    return StyleModel().fit(samples)


# ---------------------------------------------------------------------------
# Applying what was learned
# ---------------------------------------------------------------------------

def apply_style(auto: dict, features: dict, model: StyleModel) -> dict:
    """
    Blend the learned correction into a set of automatic adjustments.

    Temperature is corrected as a ratio of the camera's own white balance
    rather than in absolute Kelvin, so what is learned transfers between
    scenes shot under different light.
    """
    corrections = model.predict(features)
    if not corrections:
        return dict(auto)

    out = dict(auto)
    for key, delta in corrections.items():
        if key == "temp_ratio":
            base = float(auto.get("color_temp", 5500))
            lo, hi = LIMITS["temp_ratio"]
            ratio = float(np.clip(1.0 + delta, lo, hi))
            out["color_temp"] = int(round(base * ratio))
            continue

        # The heuristic only emits exposure, shadows, highlights, temp and
        # tint. Contrast, whites, blacks, vibrance and saturation have no
        # automatic value at all, so they start from neutral — otherwise
        # everything learned about them would be silently dropped.
        lo, hi = LIMITS[key]
        value = float(out.get(key, 0.0)) + delta
        out[key] = float(np.clip(value, lo, hi))

    # Exposure stays on the quarter-stop grid the heuristic uses.
    if "exposure" in out:
        out["exposure"] = round(float(out["exposure"]) * 4) / 4
    for key in ("contrast", "highlights", "shadows", "whites",
                "blacks", "tint", "vibrance", "saturation"):
        if key in out:
            out[key] = int(round(float(out[key])))

    return out


def summarize(db_path: Path | None = None) -> dict:
    """What the model has learned so far, for display."""
    try:
        samples = load_training_data(db_path)
    except Exception as exc:
        return {"error": str(exc), "n_samples": 0}

    model = StyleModel().fit(samples)
    tendencies = {}
    for key in LEARNED_KEYS:
        values = [r[key] for _, r in samples if key in r]
        if len(values) >= MIN_SAMPLES:
            tendencies[key] = {
                "mean_correction": round(float(np.mean(values)), 3),
                "consistency": round(
                    float(1.0 / (1.0 + np.std(values))), 3
                ),
                "samples": len(values),
            }

    return {
        "n_samples": len(samples),
        "trained": model.is_trained,
        "confidence": round(model.confidence, 3),
        "tendencies": tendencies,
    }
