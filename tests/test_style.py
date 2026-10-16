"""Tests for the style learning model."""

from pathlib import Path

import numpy as np
import pytest

import style
from style import (
    LEARNED_KEYS,
    MIN_SAMPLES,
    StyleModel,
    apply_style,
    extract_features,
    record_edit,
)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "style.duckdb"


NEUTRAL = {k: 0.0 for k in LEARNED_KEYS} | {"temp_ratio": 1.0}


def features(median=0.4, iso_stops=1.0):
    return {
        "median_lum": median, "p10": median * 0.3, "p90": min(1.0, median * 1.8),
        "dynamic_range": median * 1.5, "log_iso": iso_stops, "rb_ratio": 1.5,
        "clipped_high": 0.0, "clipped_low": 0.0,
    }


def teach(db, n, chosen_overrides, vary=True, seed=0):
    """Record n edits where the photographer consistently makes the same choice."""
    rng = np.random.default_rng(seed)
    for i in range(n):
        median = float(rng.uniform(0.15, 0.6)) if vary else 0.4
        chosen = dict(NEUTRAL)
        for key, value in chosen_overrides.items():
            chosen[key] = value(median) if callable(value) else value
        record_edit(Path(f"p{i}.CR2"), features(median), dict(NEUTRAL), chosen, db_path=db)


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

def test_features_cover_every_declared_key():
    rgb = np.full((64, 64, 3), 120, dtype=np.uint8)
    f = extract_features(rgb, 400, np.array([2.0, 1.0, 1.5, 1.0]))
    assert set(f) == set(style.FEATURE_KEYS)
    assert all(np.isfinite(v) for v in f.values())


def test_features_track_brightness():
    dark = extract_features(np.full((32, 32, 3), 30, np.uint8), 100, np.ones(4))
    bright = extract_features(np.full((32, 32, 3), 220, np.uint8), 100, np.ones(4))
    assert dark["median_lum"] < bright["median_lum"]


def test_features_detect_clipping():
    blown = extract_features(np.full((32, 32, 3), 255, np.uint8), 100, np.ones(4))
    assert blown["clipped_high"] == pytest.approx(1.0)
    assert blown["clipped_low"] == pytest.approx(0.0)


def test_iso_is_log_scaled():
    """Doubling ISO should move the feature by one, not by thousands."""
    rgb = np.full((16, 16, 3), 120, np.uint8)
    a = extract_features(rgb, 400, np.ones(4))["log_iso"]
    b = extract_features(rgb, 800, np.ones(4))["log_iso"]
    assert b - a == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------

def test_untrained_model_predicts_nothing():
    assert StyleModel().predict(features()) == {}


def test_untrained_model_leaves_adjustments_untouched():
    """With no history the output must be byte-for-byte the plain heuristic."""
    auto = {"exposure": 1.0, "shadows": 20, "highlights": -10, "color_temp": 5200}
    assert apply_style(auto, features(), StyleModel()) == auto


def test_below_minimum_samples_predicts_nothing(db):
    teach(db, MIN_SAMPLES - 1, {"contrast": 25})
    model = style.load_model(db)
    assert not model.is_trained
    assert model.predict(features()) == {}


def test_confidence_grows_with_data(db):
    confidences = []
    for n in (5, 20, 60):
        fresh = db.with_name(f"s{n}.duckdb")
        teach(fresh, n, {"contrast": 25})
        confidences.append(style.load_model(fresh).confidence)

    assert confidences == sorted(confidences)
    assert confidences[0] < 0.5, "must stay cautious with few samples"
    assert confidences[-1] > 0.7, "should grow confident with plenty"
    assert all(c < 1.0 for c in confidences), "never fully trusts the model"


# ---------------------------------------------------------------------------
# Learning
# ---------------------------------------------------------------------------

def test_learns_a_consistent_preference(db):
    teach(db, 50, {"contrast": 20})
    prediction = style.load_model(db).predict(features())
    assert prediction["contrast"] > 10, "should have learned the tendency"
    assert prediction["contrast"] < 20, "but stay shrunk toward the default"


def test_prediction_moves_in_the_direction_taught(db):
    teach(db, 40, {"vibrance": -30})
    assert style.load_model(db).predict(features())["vibrance"] < 0


def test_learns_a_conditional_rule(db):
    """Lifting shadows more on darker frames is a rule about features."""
    teach(db, 60, {"shadows": lambda m: 60 * (0.6 - m)})
    model = style.load_model(db)

    dark = model.predict(features(median=0.15))["shadows"]
    bright = model.predict(features(median=0.6))["shadows"]
    assert dark > bright + 2, "prediction ignored how dark the photo is"


def test_inconsistent_edits_produce_small_corrections(db):
    """Random choices carry no style; the model must not invent one."""
    rng = np.random.default_rng(1)
    for i in range(50):
        chosen = dict(NEUTRAL)
        chosen["contrast"] = float(rng.uniform(-60, 60))
        record_edit(Path(f"p{i}.CR2"), features(), dict(NEUTRAL), chosen, db_path=db)

    prediction = style.load_model(db).predict(features())
    assert abs(prediction.get("contrast", 0)) < 12


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------

def test_applies_to_keys_the_heuristic_never_emits(db):
    """
    The heuristic produces no contrast or vibrance at all. If apply_style
    only touched keys already present, everything learned about them would
    be silently dropped.
    """
    teach(db, 50, {"contrast": 25, "vibrance": 30})
    auto = {"exposure": 1.0, "shadows": 0, "highlights": 0, "color_temp": 5000}

    out = apply_style(auto, features(), style.load_model(db))
    assert out["contrast"] > 5, "learned contrast was dropped"
    assert out["vibrance"] > 5, "learned vibrance was dropped"


def test_temperature_is_learned_as_a_ratio(db):
    """A warmer preference must scale with the camera's own white balance."""
    teach(db, 50, {"temp_ratio": 1.2})
    model = style.load_model(db)

    cool = apply_style({"color_temp": 4000}, features(), model)["color_temp"]
    warm = apply_style({"color_temp": 8000}, features(), model)["color_temp"]

    assert cool > 4000 and warm > 8000
    assert warm / 8000 == pytest.approx(cool / 4000, rel=0.05)


def test_exposure_stays_on_the_quarter_stop_grid(db):
    teach(db, 40, {"exposure": 0.7})
    out = apply_style({"exposure": 0.5}, features(), style.load_model(db))
    assert (out["exposure"] * 4) == pytest.approx(round(out["exposure"] * 4))


def test_integer_sliders_stay_integers(db):
    teach(db, 40, {"contrast": 21, "shadows": 13})
    out = apply_style({"shadows": 5}, features(), style.load_model(db))
    for key in ("contrast", "shadows"):
        assert isinstance(out[key], int), f"{key} must be a whole slider value"


def test_learned_values_cannot_exceed_slider_limits(db):
    """An extreme, consistent preference must still land inside the UI range."""
    teach(db, 80, {"contrast": 100, "saturation": 100})
    out = apply_style({"exposure": 0.0}, features(), style.load_model(db))
    assert -100 <= out["contrast"] <= 100
    assert -100 <= out["saturation"] <= 100


def test_monochrome_is_never_predicted(db):
    """
    Black and white is an intent, not a tonal preference. Guessing it is
    far more jarring than a slightly-off contrast value.
    """
    assert "monochrome" not in LEARNED_KEYS
    teach(db, 50, {"contrast": 20})
    out = apply_style({"exposure": 0.0}, features(), style.load_model(db))
    assert "monochrome" not in out


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def test_edits_round_trip_through_the_store(db):
    record_edit(
        Path("x.CR2"), features(),
        dict(NEUTRAL), {**NEUTRAL, "contrast": 15},
        db_path=db,
    )
    rows = style.load_training_data(db)
    assert len(rows) == 1
    assert rows[0][1]["contrast"] == pytest.approx(15)


def test_residual_is_the_difference_not_the_raw_choice(db):
    """The model learns a correction, so the residual must subtract auto."""
    auto = {**NEUTRAL, "contrast": 10.0}
    record_edit(Path("x.CR2"), features(), auto, {**NEUTRAL, "contrast": 25.0}, db_path=db)
    assert style.load_training_data(db)[0][1]["contrast"] == pytest.approx(15.0)


def test_edit_matching_auto_exactly_records_a_zero_residual(db):
    """Accepting the suggestion is signal too — it says the default was right."""
    record_edit(Path("x.CR2"), features(), dict(NEUTRAL), dict(NEUTRAL), db_path=db)
    residual = style.load_training_data(db)[0][1]
    assert all(v == pytest.approx(0.0) for v in residual.values())


def test_clear_history_resets_learning(db):
    teach(db, 20, {"contrast": 25})
    assert style.load_model(db).is_trained

    removed = style.clear_history(db)
    assert removed == 20
    assert not style.load_model(db).is_trained


def test_summary_reports_what_was_learned(db):
    teach(db, 30, {"contrast": 20}, vary=False)
    summary = style.summarize(db)

    assert summary["n_samples"] == 30
    assert summary["trained"] is True
    assert summary["tendencies"]["contrast"]["mean_correction"] == pytest.approx(20, abs=1)
    assert summary["tendencies"]["contrast"]["consistency"] > 0.8


def test_summary_on_an_empty_store(db):
    summary = style.summarize(db)
    assert summary["n_samples"] == 0
    assert summary["trained"] is False


def test_model_survives_a_missing_database(tmp_path):
    """A first run with no store must return an untrained model, not raise."""
    model = style.load_model(tmp_path / "nope" / "missing.duckdb")
    assert isinstance(model, StyleModel)
    assert not model.is_trained
