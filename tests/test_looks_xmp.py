"""LUT and preset use in the XMP sidecar, and through export (fake and real exiftool)."""

import subprocess

import numpy as np
import pytest
from PIL import Image

import editor
import lookxmp
import lut as L


def test_look_block_encodes_and_decodes_every_combination():
    both = {"lut": "builtin-cool-chrome", "lut_amount": 65, "preset_name": "Warm & Cosy = 100%", "preset_amount": 40}
    assert lookxmp.parse(lookxmp.encode(both)) == both
    assert lookxmp.parse(lookxmp.encode({"lut": "0123456789ab"})) == {"lut": "0123456789ab", "lut_amount": 100.0}
    assert lookxmp.parse(lookxmp.encode({"preset_name": "Faded"})) == {"preset_name": "Faded", "preset_amount": 100.0}
    assert lookxmp.encode({}) is None and lookxmp.encode({"lut": ""}) is None


def test_foreign_or_hostile_look_values_are_ignored():
    for junk in (None, "", "Adobe Color", "photo-room:v1?lut=../../etc/passwd", "photo-room:v1?lut=%00"):
        assert "lut" not in lookxmp.parse(junk)
    assert lookxmp.parse("photo-room:v1?preset=" + "x" * 500)["preset_name"] == "x" * 60
    assert lookxmp.parse("photo-room:v1?lut=builtin-a&lutamt=900")["lut_amount"] == 100


def test_arguments_name_the_lut_and_clear_the_block_when_there_is_none():
    args = lookxmp.xmp_arguments({"lut": "builtin-cool-chrome", "lut_amount": 50})
    assert "-XMP-crs:LookName=Cool Chrome" in args and "-XMP-crs:LookAmount=0.500" in args
    assert "-XMP-crs:LookGroup=photo-room" in args
    assert any(a.startswith("-XMP-crs:LookUUID=photo-room:v1?lut=builtin-cool-chrome") for a in args)
    only_preset = lookxmp.xmp_arguments({"preset_name": "Faded Film", "preset_amount": 70})
    assert "-XMP-crs:LookName=Faded Film" in only_preset and "-XMP-crs:LookAmount=0.700" in only_preset
    assert lookxmp.xmp_arguments({}) == ["-XMP-crs:LookName=", "-XMP-crs:LookAmount=",
                                         "-XMP-crs:LookGroup=", "-XMP-crs:LookUUID="]


def test_saving_writes_the_look_and_reading_restores_it(tmp_path, fake_exiftool):
    raw = tmp_path / "IMG_0001.CR2"
    raw.write_bytes(b"raw")
    adj = editor.complete_adjustments({}, 5500)
    adj.update(lut="builtin-teal-and-orange", lut_amount=70, preset_name="Cinematic", preset_amount=85, exposure=0.5)
    editor.save_adjustments(raw, adj)
    written = [a for call in fake_exiftool.calls for a in call if a.startswith("-XMP-crs:LookUUID=")]
    assert written and "lut=builtin-teal-and-orange" in written[-1]
    back = editor.read_saved_adjustments(raw)
    assert back["lut"] == "builtin-teal-and-orange" and back["lut_amount"] == 70
    assert back["preset_name"] == "Cinematic" and back["preset_amount"] == 85 and back["exposure"] == 0.5


def test_a_look_free_save_clears_an_earlier_look(tmp_path, fake_exiftool):
    raw = tmp_path / "IMG_0001.CR2"
    raw.write_bytes(b"raw")
    adj = editor.complete_adjustments({}, 5500)
    editor.save_adjustments(raw, dict(adj, lut="builtin-cool-chrome"))
    editor.save_adjustments(raw, dict(adj, exposure=0.1))
    assert "lut" not in editor.read_saved_adjustments(raw)


# ---------------------------------------------------------------------------
# Real exiftool
# ---------------------------------------------------------------------------

real = pytest.mark.exiftool


@real
def test_real_exiftool_round_trips_the_look_in_a_clean_sidecar(tmp_path):
    from tests.test_real_exiftool_xmp import assert_clean, read_crs
    raw = tmp_path / "IMG_0001.CR2"
    raw.write_bytes(b"not really a raw")
    adj = editor.complete_adjustments({}, 5500)
    adj.update(lut="builtin-warm-negative", lut_amount=55, preset_name="Film Warm & more", preset_amount=90, contrast=12)
    editor.save_adjustments(raw, adj)
    side = raw.with_suffix(".xmp")
    assert_clean(side)
    crs = read_crs(side)
    look = crs["Look"] if "Look" in crs else {k: v for k, v in crs.items() if k.startswith("Look")}
    flat = {k.replace("Look", ""): v for k, v in (look.items() if isinstance(look, dict) else [])}
    assert flat.get("Name") == "Warm Negative" and float(flat.get("Amount")) == pytest.approx(0.55)
    assert flat.get("Group") == "photo-room"
    back = editor.read_saved_adjustments(raw)
    assert back["lut"] == "builtin-warm-negative" and back["lut_amount"] == 55
    assert back["preset_name"] == "Film Warm & more" and back["preset_amount"] == 90 and back["contrast"] == 12
    # and once the look is dropped nothing is left behind
    editor.save_adjustments(raw, dict(adj, lut="", preset_name=""))
    assert_clean(side)
    assert "lut" not in editor.read_saved_adjustments(raw)
    assert not any(k.startswith("Look") for k in read_crs(side))


@real
def test_real_export_bakes_the_lut_into_the_pixels_and_keeps_every_tag(tmp_path):
    from tests.test_real_exiftool_export import (assert_every_tag_survived, make_source, exiftool_json)
    import exporter
    from exporter import ExportOptions, export_photo

    src = make_source(tmp_path / "IMG_0001.jpg")
    plain = export_photo(src, tmp_path / "plain", ExportOptions(format="png", bit_depth=8))

    adj = editor.complete_adjustments({}, 5500)
    adj.update(lut="builtin-teal-and-orange", lut_amount=100, preset_name="Cinematic", preset_amount=100)
    editor.save_adjustments(src, adj)
    side = src.with_suffix(".xmp")
    assert side.exists()
    looked = export_photo(src, tmp_path / "looked", ExportOptions(format="png", bit_depth=8))

    a = np.asarray(Image.open(plain), dtype=np.float64) / 255.0
    b = np.asarray(Image.open(looked), dtype=np.float64) / 255.0
    assert a.shape == b.shape and np.abs(a - b).mean() > 0.01                  # the look is in the pixels
    # exactly the LUT applied to the plain render (sRGB source, so no space conversion)
    want = L.apply(a.astype(np.float32), L.get("builtin-teal-and-orange"))
    assert np.abs(want - b).max() < 0.02
    # every source tag survives; the develop block (Look included) is not copied, so nothing applies twice
    assert_every_tag_survived(src, looked)
    tags = exiftool_json(looked, "-XMP-crs:all")
    assert not [k for k in tags if "Look" in k], tags


def test_exporting_with_a_lut_this_machine_lacks_is_an_error_not_a_silent_skip(tmp_path, fake_exiftool):
    import exporter
    img = tmp_path / "a.jpg"
    Image.fromarray((np.random.default_rng(0).random((20, 30, 3)) * 255).astype(np.uint8)).save(img)
    with pytest.raises(L.LutNotFound):
        exporter.render(img, exporter.ExportOptions(), adjustments={"lut": "0123456789ab", "lut_amount": 100})
