"""Preset filters: built-ins, amount, groups, the user store and preset files."""

import json

import numpy as np
import pytest

import colormix
import editor
import presetlib as P
import tonecurve


def edit(**kw):
    base = editor.complete_adjustments({}, 5500)
    base.update(kw)
    return base


def test_every_builtin_is_valid_and_uses_only_real_controls():
    controls = {c["key"] for c in editor.CONTROLS + editor.GLOBAL_CONTROLS} | set(colormix.COLOR_DEFAULTS)
    controls |= {"monochrome", "tone_curve", "crop", "lut", "lut_amount", "temp_ratio", "temp_shift"}
    assert len(P.BUILTINS) >= 25
    names = [p["name"] for p in P.BUILTINS]
    assert len(set(names)) == len(names) and len({p["id"] for p in P.BUILTINS}) == len(names)
    for p in P.BUILTINS:
        assert set(p["values"]) <= controls, p["name"]
        assert p["values"] and p["groups"], p["name"]
        for key, v in p["values"].items():          # inside each slider's own range
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                lo, hi = P.key_range(key)
                assert lo <= v <= hi, (p["name"], key)


def test_the_library_covers_the_asked_for_looks():
    cats = {p["category"] for p in P.BUILTINS}
    assert {"B&W", "Warm / Cool", "Faded", "Contrast", "Vivid", "Portrait", "Film"} <= cats
    assert sum(p["category"] == "B&W" for p in P.BUILTINS) >= 4


def test_every_builtin_renders_and_changes_the_picture():
    rng = np.random.default_rng(0)
    img = rng.random((24, 32, 3)).astype(np.float32) * 0.8 + 0.1
    neutral = editor.apply_adjustments(img, 5500, P.neutral_adjustments(5500))
    for p in P.BUILTINS:
        out = editor.apply_adjustments(img, 5500, P.apply_preset(P.neutral_adjustments(5500), p, 1.0, 5500))
        assert np.isfinite(out).all() and 0 <= out.min() and out.max() <= 1
        assert np.abs(out - neutral).max() > 0.01, p["name"]


def test_amount_zero_is_a_no_op_and_one_is_the_preset():
    base = edit(contrast=10, exposure=0.5)
    p = P._BUILTIN_BY_ID["builtin:high-contrast"]
    assert P.apply_preset(base, p, 0.0, 5500) == base
    full = P.apply_preset(base, p, 1.0, 5500)
    assert full["contrast"] == 45 and full["blacks"] == -20 and full["exposure"] == 0.5     # untouched key stays
    half = P.apply_preset(base, p, 0.5, 5500)
    assert half["contrast"] == pytest.approx(27.5, abs=0.1)        # halfway from the current 10 to 45
    assert half["blacks"] == pytest.approx(-10)


def test_apply_does_not_modify_its_input():
    base = edit(contrast=10)
    before = json.dumps(base, sort_keys=True)
    P.apply_preset(base, P._BUILTIN_BY_ID["builtin:film-warm"], 0.6, 5500)
    assert json.dumps(base, sort_keys=True) == before


def test_tone_curves_blend_and_land_exactly_at_the_ends():
    p = P._BUILTIN_BY_ID["builtin:matte"]
    base = edit()
    full = P.apply_preset(base, p, 1.0, 5500)["tone_curve"]
    assert full["points"]["rgb"][0][1] == 32
    half = P.apply_preset(base, p, 0.5, 5500)["tone_curve"]
    assert half["points"]["rgb"][0][1] == pytest.approx(16, abs=1)         # black point half way lifted
    assert tonecurve.is_identity(P.apply_preset(base, p, 0.0, 5500).get("tone_curve"))


def test_grading_hue_goes_the_short_way_round_the_wheel():
    p = {"values": {"grade_shadow_hue": 10, "grade_shadow_sat": 50}}
    out = P.apply_preset(edit(grade_shadow_hue=350, grade_shadow_sat=20), p, 0.5, 5500)
    assert out["grade_shadow_hue"] == pytest.approx(0, abs=1) or out["grade_shadow_hue"] == pytest.approx(360, abs=1)
    fresh = P.apply_preset(edit(), p, 0.3, 5500)          # nothing to blend from: take the preset's hue
    assert fresh["grade_shadow_hue"] == 10 and fresh["grade_shadow_sat"] == pytest.approx(15)


def test_black_and_white_part_way_drains_colour_instead_of_flipping():
    bw = P._BUILTIN_BY_ID["builtin:b-w-classic"]
    part = P.apply_preset(edit(), bw, 0.5, 5500)
    assert part["monochrome"] is False and part["saturation"] == pytest.approx(-50)
    assert P.apply_preset(edit(), bw, 1.0, 5500)["monochrome"] is True
    assert P.apply_preset(edit(monochrome=True), {"values": {"monochrome": False}}, 1.0, 5500)["monochrome"] is False


def test_white_balance_is_relative_to_the_photos_own_camera_setting():
    warm = P._BUILTIN_BY_ID["builtin:warm"]
    out = P.apply_preset(edit(color_temp=4000), warm, 1.0, 4000)
    assert out["color_temp"] == round(4000 * 1.10)
    absolute = {"values": {"temp_ratio": 1.2}}
    assert P.apply_preset(edit(), absolute, 1.0, 3000)["color_temp"] == 3600       # 1.2 x this photo's baseline


def test_a_preset_with_a_lut_sets_the_film_style_at_the_amount():
    p = P._BUILTIN_BY_ID["builtin:warm-negative-lut"]
    out = P.apply_preset(edit(), p, 0.4, 5500)
    assert out["lut"] == "builtin-warm-negative" and out["lut_amount"] == pytest.approx(40)
    cleared = P.apply_preset(edit(lut="builtin-warm-negative", lut_amount=80), {"values": {"lut": ""}}, 0.5, 5500)
    assert cleared["lut_amount"] == pytest.approx(40) and cleared["lut"] == "builtin-warm-negative"
    assert P.apply_preset(edit(lut="x"), {"values": {"lut": ""}}, 1.0, 5500)["lut"] == ""


# ---------------------------------------------------------------------------
# Saving from an edit
# ---------------------------------------------------------------------------

def rich_edit():
    return edit(exposure=0.4, contrast=20, vibrance=15, clarity=12, color_temp=6500, tint=5,
                crop={"left": 0.1, "top": 0.1, "right": 0.9, "bottom": 0.9}, straighten=2.0,
                grade_shadow_hue=210, grade_shadow_sat=30, sat_red=-20,
                tone_curve={"points": {"rgb": [[0, 10], [255, 255]]}},
                lut="builtin-cool-chrome", lut_amount=60)


def test_default_groups_leave_out_crop_and_white_balance():
    p = P.from_adjustments("Mine", rich_edit(), None, 5500)
    assert set(p["groups"]) == set(P.DEFAULT_GROUPS)
    for gone in ("crop", "straighten", "temp_ratio", "tint", "color_temp"):
        assert gone not in p["values"]
    assert p["values"]["exposure"] == 0.4 and p["values"]["grade_shadow_sat"] == 30
    assert p["values"]["lut"] == "builtin-cool-chrome" and p["values"]["lut_amount"] == 60
    assert p["values"]["tone_curve"]["points"]["rgb"][0] == [0.0, 10.0]


def test_only_the_chosen_groups_are_saved():
    p = P.from_adjustments("Tone only", rich_edit(), ["tone"], 5500)
    assert set(p["values"]) == set(P.TONE) and p["groups"] == ["tone"]
    assert "contrast" in p["values"] and "vibrance" not in p["values"]
    only_curves = P.from_adjustments("Curves", rich_edit(), ["curves"], 5500)
    assert set(only_curves["values"]) == {"tone_curve"}


def test_white_balance_and_crop_are_saved_when_asked_for():
    p = P.from_adjustments("WB", rich_edit(), ["wb", "crop"], 5000)
    assert p["values"]["temp_ratio"] == pytest.approx(1.3) and p["values"]["tint"] == 5
    assert p["values"]["crop"]["left"] == pytest.approx(0.1) and p["values"]["straighten"] == 2.0


def test_saving_validates_name_and_groups():
    with pytest.raises(P.PresetError, match="needs a name"):
        P.from_adjustments("  ", rich_edit(), None)
    with pytest.raises(P.PresetError, match="unknown preset group"):
        P.from_adjustments("x", rich_edit(), ["tone", "nonsense"])
    with pytest.raises(P.PresetError, match="at least one"):
        P.from_adjustments("x", rich_edit(), [])


def test_applying_a_saved_preset_reproduces_the_edit_on_another_photo():
    src = rich_edit()
    p = P.from_adjustments("Mine", src, None, 5500)
    other = edit(exposure=-1.0, clarity=0, color_temp=4200)
    out = P.apply_preset(other, p, 1.0, 4200)
    for key in ("exposure", "contrast", "vibrance", "clarity", "grade_shadow_hue", "sat_red"):
        assert out[key] == pytest.approx(src[key]), key
    assert out["color_temp"] == 4200                                  # white balance not part of the preset
    assert out["lut"] == "builtin-cool-chrome" and out["lut_amount"] == 60
    assert "crop" not in P.from_adjustments("Mine", src, None)["values"]


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def test_user_presets_save_list_rename_delete():
    p = P.from_adjustments("My Look", rich_edit(), None, 5500)
    P.save(p)
    assert [x["name"] for x in P.list_user()] == ["My Look"]
    assert P.get(p["id"])["values"]["contrast"] == 20
    renamed = P.rename(p["id"], "Better Look")
    assert renamed["id"] != p["id"] and [x["name"] for x in P.list_user()] == ["Better Look"]
    assert P.delete(renamed["id"]) is True and P.delete(renamed["id"]) is False
    assert P.list_user() == []
    assert len(P.list_all()) == len(P.BUILTINS)


def test_builtins_are_protected_and_ids_cannot_escape_the_folder():
    with pytest.raises(P.PresetError, match="cannot be deleted"):
        P.delete("builtin:warm")
    with pytest.raises(P.PresetError, match="cannot be renamed"):
        P.rename("builtin:warm", "x")
    for bad in ("user:../../x", "user:", "other:x", "../x"):
        with pytest.raises(P.PresetError):
            P.get(bad)


def test_a_damaged_preset_file_is_ignored_not_fatal():
    d = P.preset_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "broken.json").write_text("{not json")
    (d / "empty.json").write_text(json.dumps({"name": "x", "values": {"bogus": 1}}))
    P.save(P.from_adjustments("Fine", rich_edit(), ["tone"], 5500))
    assert [p["name"] for p in P.list_user()] == ["Fine"]


def test_values_are_clamped_and_unknown_keys_reported():
    clean, dropped = P.clean_values({"contrast": 900, "exposure": -50, "evil": 1, "grade_mid_hue": 999})
    assert clean == {"contrast": 100, "exposure": -5.0, "grade_mid_hue": 360} and dropped == ["evil"]
    for bad in ({"contrast": "abc"}, {"contrast": float("nan")}, {"lut": "../x"}, {"crop": {"left": 2}}, [1]):
        with pytest.raises(P.PresetError):
            P.clean_values(bad)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def test_export_and_import_round_trip_as_json():
    a = P.from_adjustments("A one", rich_edit(), None, 5500)
    b = P.from_adjustments("B two", rich_edit(), ["tone", "grading"], 5500)
    P.save(a), P.save(b)
    text = P.export_json([a["id"], b["id"], "builtin:warm"])
    assert json.loads(text)["format"] == "photo-room-preset"
    presets, skipped = P.import_file(text.encode())
    assert [p["name"] for p in presets] == ["A one", "B two", "Warm"] and skipped == []
    assert presets[0]["values"] == a["values"] and presets[1]["groups"] == ["tone", "grading"]


@pytest.mark.parametrize("data,fragment", [
    (b"", "not a preset file"),
    (b"[1, 2]", "not a photo-room preset file"),
    (json.dumps({"format": "photo-room-preset", "version": 99, "presets": []}).encode(), "newer version"),
    (json.dumps({"format": "photo-room-preset", "version": 1, "presets": []}).encode(), "no presets"),
    (json.dumps({"format": "photo-room-preset", "version": 1, "presets": [{"name": "x", "values": {"nope": 1}}]}).encode(),
     "no usable settings"),
    (b"x" * (P.MAX_FILE_BYTES + 1), "2 MB"),
])
def test_bad_preset_files_are_refused_with_a_reason(data, fragment):
    with pytest.raises(P.PresetError, match=fragment):
        P.import_file(data)


LR_XMP = b"""<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description rdf:about="" xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
  crs:PresetType="Normal" crs:Version="15.4" crs:ProcessVersion="11.0" crs:HasSettings="True"
  crs:Exposure2012="+0.50" crs:Contrast2012="+25" crs:Highlights2012="-30" crs:Vibrance="+20"
  crs:Clarity2012="+10" crs:Temperature="5200" crs:Tint="+4" crs:ConvertToGrayscale="False"
  crs:SaturationAdjustmentBlue="-25" crs:HueAdjustmentOrange="+5" crs:SplitToningShadowHue="215"
  crs:SplitToningShadowSaturation="30" crs:ColorGradeMidtoneSat="10" crs:ParametricShadows="12"
  crs:LensProfileEnable="1" crs:RGBTable="ABCDEF" crs:Look="x">
 <crs:Name><rdf:Alt><rdf:li xml:lang="x-default">Film Pop</rdf:li></rdf:Alt></crs:Name>
 <crs:ToneCurvePV2012><rdf:Seq><rdf:li>0, 12</rdf:li><rdf:li>128, 140</rdf:li><rdf:li>255, 255</rdf:li></rdf:Seq></crs:ToneCurvePV2012>
 <crs:ToneCurvePV2012Red><rdf:Seq><rdf:li>0, 0</rdf:li><rdf:li>255, 250</rdf:li></rdf:Seq></crs:ToneCurvePV2012Red>
</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>"""


def test_lightroom_xmp_preset_maps_what_it_can_and_reports_the_rest():
    presets, skipped = P.import_file(LR_XMP, "Film Pop.xmp")
    (p,) = presets
    v = p["values"]
    assert p["name"] == "Film Pop"
    assert v["exposure"] == 0.5 and v["contrast"] == 25 and v["highlights"] == -30 and v["vibrance"] == 20
    assert v["clarity"] == 10 and v["tint"] == 4 and v["monochrome"] is False
    assert v["sat_blue"] == -25 and v["hue_orange"] == 5
    assert v["grade_shadow_hue"] == 215 and v["grade_shadow_sat"] == 30 and v["grade_mid_sat"] == 10
    assert v["tone_curve"]["parametric"]["shadows"] == 12
    assert v["tone_curve"]["points"]["rgb"][0] == [0.0, 12.0] and v["tone_curve"]["points"]["red"][-1] == [255.0, 250.0]
    # absolute white balance, lens profile, embedded LUT/look: named, never guessed
    assert {"Temperature", "LensProfileEnable", "RGBTable", "Look"} <= set(skipped)
    assert "Exposure2012" not in skipped
    assert {"tone", "colour", "curves", "grading", "effects", "wb"} >= set(p["groups"]) >= {"tone", "colour", "curves"}


def test_xml_with_entities_or_no_settings_is_refused():
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x:xmpmeta xmlns:x="adobe:ns:meta/">&a;</x:xmpmeta>'
    with pytest.raises(P.PresetError, match="DOCTYPE"):
        P.import_file(bomb)
    with pytest.raises(P.PresetError, match="No Camera Raw settings"):
        P.import_file(b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/></x:xmpmeta>')
    with pytest.raises(P.PresetError, match="could not be read"):
        P.import_file(b"<x:xmpmeta")


def test_legacy_presets_from_the_style_database_are_carried_over(tmp_path):
    import style
    db = tmp_path / "old.duckdb"
    style.save_preset("Old punchy", {"contrast": 35, "vibrance": 30, "color_temp": 6000, "_baseline_temp": 5000,
                                      "edit_version": 2, "crop": {"left": .1, "top": 0, "right": 1, "bottom": 1}}, db_path=db)
    assert P.import_legacy(db) == 1 and P.import_legacy(db) == 0
    (p,) = P.list_user()
    assert p["name"] == "Old punchy" and p["values"]["contrast"] == 35
    assert p["values"]["temp_ratio"] == pytest.approx(1.2, abs=0.01) and "crop" not in p["values"]
