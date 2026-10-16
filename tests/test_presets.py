"""Tests for named presets."""

from pathlib import Path

import pytest

import style
from style import delete_preset, get_preset, list_presets, save_preset


@pytest.fixture
def db(tmp_path):
    return tmp_path / "presets.duckdb"


LOOK = {
    "exposure": 0.5, "contrast": 35, "vibrance": 30,
    "clarity": 20, "saturation": -5,
}


def test_a_saved_preset_appears_in_the_list(db):
    save_preset("Punchy", LOOK, db_path=db)
    assert list_presets(db) == ["Punchy"]


def test_presets_round_trip(db):
    save_preset("Punchy", LOOK, db_path=db)
    got = get_preset("Punchy", db_path=db)
    for key, value in LOOK.items():
        assert got[key] == pytest.approx(value)


def test_saving_the_same_name_replaces_it(db):
    save_preset("Look", {"contrast": 10}, db_path=db)
    save_preset("Look", {"contrast": 60}, db_path=db)

    assert list_presets(db) == ["Look"]
    assert get_preset("Look", db_path=db)["contrast"] == pytest.approx(60)


def test_presets_are_listed_alphabetically(db):
    for name in ("Zebra", "Alpha", "Mid"):
        save_preset(name, LOOK, db_path=db)
    assert list_presets(db) == ["Alpha", "Mid", "Zebra"]


def test_an_unknown_preset_returns_none(db):
    assert get_preset("nope", db_path=db) is None


def test_a_nameless_preset_is_refused(db):
    for name in ("", "   "):
        with pytest.raises(ValueError, match="needs a name"):
            save_preset(name, LOOK, db_path=db)


def test_names_are_trimmed(db):
    save_preset("  Spaced  ", LOOK, db_path=db)
    assert list_presets(db) == ["Spaced"]


def test_deleting_a_preset(db):
    save_preset("Temp", LOOK, db_path=db)
    assert delete_preset("Temp", db_path=db) is True
    assert list_presets(db) == []


def test_deleting_an_unknown_preset_reports_false(db):
    assert delete_preset("ghost", db_path=db) is False


def test_listing_survives_a_missing_database(tmp_path):
    """A first run with no store must list nothing, not raise."""
    assert list_presets(tmp_path / "nope" / "missing.duckdb") == []


# ---------------------------------------------------------------------------
# White balance travels as a ratio
# ---------------------------------------------------------------------------

def test_temperature_is_stored_relative_to_the_source_photo(db):
    """
    An absolute Kelvin value would force the source photo's colour onto every
    other frame. A ratio keeps the intent — "warmer than this camera saw it".
    """
    save_preset("Warm", {"color_temp": 6000, "_baseline_temp": 5000}, db_path=db)

    on_cool = get_preset("Warm", baseline_temp=4000, db_path=db)["color_temp"]
    on_warm = get_preset("Warm", baseline_temp=8000, db_path=db)["color_temp"]

    assert on_cool == pytest.approx(4800, abs=2)
    assert on_warm == pytest.approx(9600, abs=2)
    assert on_warm / 8000 == pytest.approx(on_cool / 4000, rel=0.01)


def test_a_neutral_temperature_preset_leaves_white_balance_alone(db):
    save_preset("Neutral", {"color_temp": 5000, "_baseline_temp": 5000}, db_path=db)
    assert get_preset("Neutral", baseline_temp=3800, db_path=db)["color_temp"] == pytest.approx(3800, abs=2)


def test_the_baseline_marker_is_not_stored_as_an_adjustment(db):
    save_preset("X", {"contrast": 20, "_baseline_temp": 5000}, db_path=db)
    assert "_baseline_temp" not in get_preset("X", db_path=db)


# ---------------------------------------------------------------------------
# Framing is excluded
# ---------------------------------------------------------------------------

def test_crop_is_never_stored_in_a_preset(db):
    """Someone else's crop is almost always wrong for a different frame."""
    save_preset("Look", {**LOOK, "crop": {"left": 0.2, "top": 0.1, "right": 0.9, "bottom": 0.8}},
                db_path=db)
    assert "crop" not in get_preset("Look", db_path=db)


def test_straighten_is_never_stored_in_a_preset(db):
    save_preset("Look", {**LOOK, "straighten": 7.5}, db_path=db)
    assert "straighten" not in get_preset("Look", db_path=db)


def test_tonal_settings_survive_the_exclusions(db):
    save_preset("Look", {**LOOK, "crop": {"left": 0.2}, "straighten": 3.0}, db_path=db)
    got = get_preset("Look", db_path=db)
    assert got["contrast"] == pytest.approx(35)
    assert got["vibrance"] == pytest.approx(30)


def test_monochrome_is_carried(db):
    """Unlike the learned model, an explicit preset may absolutely mean B&W."""
    save_preset("Mono", {"monochrome": True, "contrast": 40}, db_path=db)
    assert get_preset("Mono", db_path=db)["monochrome"] is True


# ---------------------------------------------------------------------------
# Presets and learned style share a store but not a table
# ---------------------------------------------------------------------------

def test_presets_do_not_pollute_the_learning_data(db):
    save_preset("Look", LOOK, db_path=db)
    assert style.load_training_data(db) == []


def test_recorded_edits_do_not_appear_as_presets(db):
    style.record_edit(
        Path("x.CR2"),
        {"median_lum": 0.4, "p10": 0.1, "p90": 0.8, "dynamic_range": 0.7,
         "log_iso": 1.0, "rb_ratio": 1.5, "clipped_high": 0.0, "clipped_low": 0.0},
        {"contrast": 0.0}, {"contrast": 20.0},
        db_path=db,
    )
    assert list_presets(db) == []
