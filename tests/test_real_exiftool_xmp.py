"""
Develop settings, written and read back with the REAL exiftool.

The rest of the suite fakes exiftool, which proves our argument lists are what
we meant but not that exiftool accepts them. Here every setting the app writes
goes through the sidecar writer (``editor.save_adjustments``) into a real
``.xmp`` and is read back by exiftool, asserting the values, the Camera Raw
XML shape, and that exiftool emits no warning or error for the file.

Run with ``pytest -m exiftool`` (CI does).
"""

import json
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import colormix
import editor
import library
import masks
import tonecurve

pytestmark = pytest.mark.exiftool

CRS = "http://ns.adobe.com/camera-raw-settings/1.0/"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"

# exiftool keeps Camera Raw's own name for what the app calls ColorTemperature.
ALIAS = {"ColorTemperature": "Temperature"}


def exiftool(*args: str, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(["exiftool", *args], capture_output=True, text=True, check=check)


def read_crs(path: Path) -> dict:
    """Every XMP-crs tag in ``path`` as exiftool reports it (structs kept)."""
    out = exiftool("-j", "-struct", "-XMP-crs:all", str(path)).stdout
    entry = json.loads(out)[0]
    entry.pop("SourceFile", None)
    return entry


def assert_clean(path: Path) -> None:
    """Valid XML, valid crs namespace, and not one exiftool warning or error."""
    res = exiftool("-validate", "-warning", "-error", "-a", "-G1", str(path))
    assert res.stderr.strip() == "", res.stderr
    assert "Validate" in res.stdout and "OK" in res.stdout, res.stdout
    assert "Warning" not in res.stdout and "Error" not in res.stdout, res.stdout
    root = ET.parse(path).getroot()          # well-formed XML
    assert root.tag == "{adobe:ns:meta/}xmpmeta"
    desc = root.find(f".//{{{RDF}}}Description")
    assert desc is not None
    assert any(el.tag.startswith("{" + CRS + "}") for el in desc), "no crs: elements"
    assert "xmlns:crs='" + CRS + "'" in path.read_text(), "crs namespace missing"


# ---------------------------------------------------------------------------
# A photo with every setting the app can write
# ---------------------------------------------------------------------------

CURVE = {
    "points": {
        "rgb": [[0, 0], [64, 52], [192, 204], [255, 255]],
        "red": [[0, 0], [128, 140], [255, 255]],
        "green": [[0, 8], [255, 255]],
        "blue": [[0, 0], [100, 90.4], [100.2, 95], [255, 250]],
    },
    "parametric": {"highlights": 12, "lights": -7, "darks": 5, "shadows": -20,
                   "split1": 30, "split2": 55, "split3": 80},
}

MASKS = [
    {"type": "linear", "name": "Sky", "x": 0.5, "y": 0.3, "angle": 20, "feather": 60,
     "adj": {"exposure": -0.7, "contrast": 10, "highlights": -30, "shadows": 5, "temp": -8,
             "tint": 3, "saturation": 12, "clarity": 7, "texture": 4, "dehaze": 9,
             "sharpness": 6}},
    {"type": "radial", "name": "Face", "x": 0.4, "y": 0.45, "rx": 0.2, "ry": 0.3,
     "angle": 15, "feather": 40, "invert": True, "adj": {"exposure": 0.45, "saturation": -10}},
    {"type": "brush", "name": "Grass",
     "strokes": [{"pts": [0.1, 0.1, 0.2, 0.15, 0.3, 0.2], "size": 0.05, "feather": 30,
                  "flow": 80, "density": 90, "erase": False},
                 {"pts": [0.7, 0.7, 0.75, 0.72], "size": 0.03, "feather": 50,
                  "flow": 100, "density": 100, "erase": True}],
     "adj": {"exposure": 0.25, "clarity": -15}},
]

SPOTS = [
    {"x": 0.25, "y": 0.5, "r": 0.02, "sx": 0.35, "sy": 0.55, "feather": 40,
     "opacity": 80, "mode": "heal"},
    {"x": 0.75, "y": 0.25, "r": 0.03, "sx": 0.6, "sy": 0.25, "feather": 10,
     "opacity": 100, "mode": "clone"},
]

BASIC = {
    "color_temp": 4800, "tint": 12, "exposure": 0.65, "contrast": 15, "highlights": -40,
    "shadows": 25, "whites": 8, "blacks": -6, "vibrance": 18, "saturation": -4,
    "clarity": 11, "texture": 9, "dehaze": 14, "sharpness": 35, "luminance_nr": 22,
}

COLOUR = {
    **{f"hue_{b}": i + 1 for i, b in enumerate(colormix.BANDS)},
    **{f"sat_{b}": -(i + 2) for i, b in enumerate(colormix.BANDS)},
    **{f"lum_{b}": 3 * i - 9 for i, b in enumerate(colormix.BANDS)},
    **{f"gray_{b}": 2 * i - 5 for i, b in enumerate(colormix.BANDS)},
    "grade_shadow_hue": 210, "grade_shadow_sat": 30, "grade_shadow_lum": -5,
    "grade_mid_hue": 40, "grade_mid_sat": 12, "grade_mid_lum": 4,
    "grade_highlight_hue": 55, "grade_highlight_sat": 22, "grade_highlight_lum": 6,
    "grade_global_hue": 300, "grade_global_sat": 9, "grade_global_lum": -3,
    "grade_blending": 65, "grade_balance": -20,
}

GEOMETRY = {
    "straighten": 0.0, "crop": {"left": 0.1, "top": 0.05, "right": 0.9, "bottom": 0.95},
    "xf_vertical": -12, "xf_horizontal": 7, "xf_rotate": 1.5, "xf_aspect": -3,
    "xf_scale": 105, "xf_x": 2.5, "xf_y": -1.25,
    "lens_profile": True, "lens_distortion": 8, "lens_vignette": -12,
    "ca_remove": True, "defringe_purple": 20, "defringe_green": 10,
}

FULL = {**BASIC, **COLOUR, **GEOMETRY, "monochrome": True,
        "tone_curve": CURVE, "masks": MASKS, "spots": SPOTS}


@pytest.fixture
def raw(tmp_path):
    """A stand-in raw: only its name matters, the sidecar is what is written."""
    p = tmp_path / "IMG_0001.CR2"
    p.write_bytes(b"not really a raw")
    return p


@pytest.fixture
def saved(raw):
    editor.save_adjustments(raw, dict(FULL), source_size=(6000, 4000))
    return raw.with_suffix(".xmp")


def num(v):
    return float(v)


def test_the_original_raw_is_never_touched(raw):
    editor.save_adjustments(raw, dict(FULL), source_size=(6000, 4000))
    assert raw.read_bytes() == b"not really a raw"


def test_everything_written_is_a_known_crs_tag_with_no_warnings(saved):
    """exiftool silently drops tags it does not know (it only warns); catch that."""
    assert_clean(saved)
    written = set(re.findall(r"<crs:(\w+)[ >]", saved.read_text()))
    wanted = {a.split("=")[0].split(":")[-1] for a in editor._xmp_arguments(
        {**FULL, "_frame_aspect": 1.5, "_frame": (6000, 4000)}) if a.startswith("-XMP-crs:")}
    wanted = {ALIAS.get(t, t) for t in wanted}
    assert wanted - written == set(), f"requested but absent from the sidecar: {sorted(wanted - written)}"


def test_the_writer_gives_exiftool_nothing_to_warn_about(raw):
    """Re-save over an existing sidecar and read exiftool's own stderr."""
    editor.save_adjustments(raw, dict(FULL), source_size=(6000, 4000))
    args = editor._xmp_arguments({**FULL, "_frame_aspect": 1.5, "_frame": (6000, 4000)})
    res = exiftool(*args, "-overwrite_original", str(raw.with_suffix(".xmp")), check=False)
    assert res.returncode == 0
    assert res.stderr.strip() == "", res.stderr


def test_an_unknown_tag_name_is_an_error_not_a_silent_drop(raw, monkeypatch):
    real = editor._xmp_arguments
    monkeypatch.setattr(editor, "_xmp_arguments",
                        lambda adj, process_version=None: real(adj, process_version) + ["-XMP-crs:NoSuchTag=1"])
    with pytest.raises(RuntimeError, match="not defined"):
        editor.write_sidecar(raw, dict(FULL))


def test_basic_settings_round_trip(saved):
    crs = read_crs(saved)
    assert crs["ColorTemperature"] == 4800
    assert crs["ProcessVersion"] == 11.0 and crs["WhiteBalance"] == "Custom"
    for tag, key in [("Tint", "tint"), ("Exposure2012", "exposure"), ("Contrast2012", "contrast"),
                     ("Highlights2012", "highlights"), ("Shadows2012", "shadows"),
                     ("Whites2012", "whites"), ("Blacks2012", "blacks"), ("Vibrance", "vibrance"),
                     ("Saturation", "saturation"), ("Clarity2012", "clarity"),
                     ("Texture", "texture"), ("Dehaze", "dehaze"), ("Sharpness", "sharpness"),
                     ("LuminanceSmoothing", "luminance_nr")]:
        assert num(crs[tag]) == pytest.approx(BASIC[key]), tag
    assert str(crs["ConvertToGrayscale"]) in ("True", "true", "1", "Yes")
    back = editor.read_saved_adjustments(saved.with_suffix(".CR2"))
    for key, want in BASIC.items():
        assert back[key] == pytest.approx(want), key
    assert back["monochrome"] is True


def test_tone_curve_is_written_exactly_as_lightroom_writes_it(saved):
    """
    Camera Raw stores each curve as ``<crs:ToneCurvePV2012><rdf:Seq>`` with one
    ``<rdf:li>x, y</rdf:li>`` per point, and the parametric values and splits
    as plain integer elements.
    """
    root = ET.parse(saved).getroot()
    desc = root.find(f".//{{{RDF}}}Description")
    for tag, pts in [("ToneCurvePV2012", CURVE["points"]["rgb"]),
                     ("ToneCurvePV2012Red", CURVE["points"]["red"]),
                     ("ToneCurvePV2012Green", CURVE["points"]["green"]),
                     ("ToneCurvePV2012Blue", CURVE["points"]["blue"])]:
        el = desc.find(f"{{{CRS}}}{tag}")
        assert el is not None, tag
        seq = el.find(f"{{{RDF}}}Seq")
        assert seq is not None and el.text is None or not (el.text or "").strip()
        items = [li.text for li in seq.findall(f"{{{RDF}}}li")]
        assert all(len(list(li)) == 0 for li in seq), "points must be plain text items"
        # Camera Raw's points are whole numbers; two that round onto one x collapse.
        want = [", ".join(f"{int(round(v))}" for v in p)
                for p in ([[0, 0], [100, 95], [255, 250]] if tag.endswith("Blue") else pts)]
        assert items == want, tag
        assert all(re.fullmatch(r"\d+(\.\d+)?, \d+(\.\d+)?", i) for i in items)
    for tag, key in [("ParametricHighlights", "highlights"), ("ParametricLights", "lights"),
                     ("ParametricDarks", "darks"), ("ParametricShadows", "shadows"),
                     ("ParametricShadowSplit", "split1"), ("ParametricMidtoneSplit", "split2"),
                     ("ParametricHighlightSplit", "split3")]:
        assert desc.find(f"{{{CRS}}}{tag}").text == str(CURVE["parametric"][key])
    assert desc.find(f"{{{CRS}}}ToneCurveName2012").text == "Custom"


def test_tone_curve_reads_back_through_exiftool(saved):
    crs = read_crs(saved)
    assert crs["ToneCurvePV2012"] == ["0, 0", "64, 52", "192, 204", "255, 255"]
    assert crs["ToneCurvePV2012Blue"] == ["0, 0", "100, 95", "255, 250"]
    back = editor.read_saved_adjustments(saved.with_suffix(".CR2"))["tone_curve"]
    for ch in ("rgb", "red", "green"):
        assert back["points"][ch] == tonecurve.normalise(CURVE)["points"][ch]
    assert back["points"]["blue"] == [[0, 0], [100, 95], [255, 250]]
    assert back["parametric"] == pytest.approx(tonecurve.normalise(CURVE)["parametric"])


def test_a_neutral_curve_clears_the_curve_from_the_file(raw):
    editor.save_adjustments(raw, dict(FULL), source_size=(6000, 4000))
    editor.save_adjustments(raw, {**FULL, "tone_curve": None}, source_size=(6000, 4000))
    sidecar = raw.with_suffix(".xmp")
    assert_clean(sidecar)
    crs = read_crs(sidecar)
    assert not any(k.startswith("ToneCurvePV2012") for k in crs)
    assert crs["ToneCurveName2012"] == "Linear"
    assert "tone_curve" not in editor.read_saved_adjustments(raw)


def test_hsl_mixer_and_colour_grading_round_trip(saved):
    crs = read_crs(saved)
    for key, tag in colormix.XMP_TAGS.items():
        assert tag in crs, f"{key} -> {tag} never reached the file"
        assert num(crs[tag]) == COLOUR[key], tag
    # The four Split Toning names are Camera Raw's, not ColorGrade*.
    assert crs["SplitToningShadowHue"] == 210 and crs["SplitToningHighlightSaturation"] == 22
    assert crs["SplitToningBalance"] == -20
    back = editor.read_saved_adjustments(saved.with_suffix(".CR2"))
    for key, want in COLOUR.items():
        if want != colormix.COLOR_DEFAULTS[key]:
            assert back[key] == want, key


def test_crop_straighten_transform_and_lens_round_trip(raw):
    adj = {**FULL, "straighten": 0.0}
    editor.save_adjustments(raw, adj, source_size=(6000, 4000))
    crs = read_crs(raw.with_suffix(".xmp"))
    assert str(crs["HasCrop"]) in ("True", "true")
    assert (crs["CropLeft"], crs["CropTop"], crs["CropRight"], crs["CropBottom"]) == (0.1, 0.05, 0.9, 0.95)
    assert crs["CropAngle"] == 0
    for key, tag in [("xf_vertical", "PerspectiveVertical"), ("xf_horizontal", "PerspectiveHorizontal"),
                     ("xf_rotate", "PerspectiveRotate"), ("xf_aspect", "PerspectiveAspect"),
                     ("xf_scale", "PerspectiveScale"), ("xf_x", "PerspectiveX"),
                     ("xf_y", "PerspectiveY"), ("lens_distortion", "LensManualDistortionAmount"),
                     ("lens_vignette", "VignetteAmount"), ("defringe_purple", "DefringePurpleAmount"),
                     ("defringe_green", "DefringeGreenAmount")]:
        assert num(crs[tag]) == pytest.approx(GEOMETRY[key]), tag
    assert crs["LensProfileEnable"] == 1 and crs["AutoLateralCA"] == 1
    back = editor.read_saved_adjustments(raw)
    assert back["crop"] == pytest.approx(GEOMETRY["crop"])
    for key in ("xf_vertical", "xf_horizontal", "xf_rotate", "xf_aspect", "xf_scale", "xf_x",
                "xf_y", "lens_distortion", "lens_vignette", "defringe_purple", "defringe_green"):
        assert back[key] == pytest.approx(GEOMETRY[key]), key
    assert back["lens_profile"] is True and back["ca_remove"] is True


def test_straighten_writes_crop_angle_and_the_inscribed_crop(raw):
    editor.save_adjustments(raw, {**BASIC, "straighten": 5.0}, frame_size=(300, 200),
                            source_size=(6000, 4000))
    sidecar = raw.with_suffix(".xmp")
    assert_clean(sidecar)
    crs = read_crs(sidecar)
    assert crs["CropAngle"] == 5.0 and str(crs["HasCrop"]) in ("True", "true")
    assert 0 < crs["CropLeft"] < crs["CropRight"] < 1 and 0 < crs["CropTop"] < crs["CropBottom"] < 1
    assert editor.read_saved_adjustments(raw)["straighten"] == 5.0


def test_masks_are_lightroom_structs_and_read_back(saved, raw):
    crs = read_crs(saved)
    groups = crs["MaskGroupBasedCorrections"]
    assert [g["CorrectionName"] for g in groups] == ["Sky", "Face"]
    sky = groups[0]
    assert sky["What"] == "Correction" and str(sky["CorrectionActive"]) in ("True", "true")
    assert sky["LocalExposure2012"] == -0.7 and sky["LocalDehaze"] == 9
    assert sky["LocalTemperature"] == -8 and sky["LocalSharpness"] == 6
    grad = sky["CorrectionMasks"][0]
    assert grad["What"] == "Mask/Gradient"
    assert {"ZeroX", "ZeroY", "FullX", "FullY"} <= set(grad)
    face = groups[1]["CorrectionMasks"][0]
    assert face["What"] == "Mask/CircularGradient"
    assert (face["Left"], face["Top"], face["Right"], face["Bottom"]) == (0.2, 0.15, 0.6, 0.75)
    assert str(face["MaskInverted"]) in ("True", "true")

    paint = crs["PaintBasedCorrections"]
    assert len(paint) == 1 and paint[0]["CorrectionName"] == "Grass"
    strokes = paint[0]["CorrectionMasks"]
    assert [s["What"] for s in strokes] == ["Mask/Brush", "Mask/Brush"]
    assert strokes[0]["Dabs"] == ["m 0.1 0.1", "d 0.2 0.15", "d 0.3 0.2"]
    assert strokes[0]["Radius"] == 0.05 and strokes[0]["Flow"] == 0.8
    assert strokes[1]["MaskValue"] == 0          # the eraser stroke

    back = editor.read_saved_adjustments(raw)["masks"]
    assert [m["type"] for m in back] == ["linear", "radial", "brush"]
    assert back[0]["adj"]["exposure"] == pytest.approx(-0.7)
    assert back[1]["invert"] is True and back[1]["rx"] == pytest.approx(0.2)


def test_masks_with_no_content_clear_stale_ones(raw):
    editor.save_adjustments(raw, dict(FULL), source_size=(6000, 4000))
    editor.save_adjustments(raw, {**FULL, "masks": []}, source_size=(6000, 4000))
    sidecar = raw.with_suffix(".xmp")
    assert_clean(sidecar)
    crs = read_crs(sidecar)
    assert "MaskGroupBasedCorrections" not in crs and "PaintBasedCorrections" not in crs


def test_retouch_areas_are_structs_and_read_back(saved, raw):
    areas = read_crs(saved)["RetouchAreas"]
    assert [a["SpotType"] for a in areas] == ["heal", "clone"]
    assert [a["Method"] for a in areas] == ["healing", "gaussian"]
    assert areas[0]["SourceX"] == 0.35 and areas[0]["OffsetY"] == pytest.approx(0.05)
    assert areas[0]["Opacity"] == 0.8
    mask = areas[0]["Masks"][0]
    assert mask["What"] == "Mask/EllipseGradient"
    assert (mask["X"], mask["Y"], mask["SizeX"]) == (0.25, 0.5, 0.02)
    back = editor.read_saved_adjustments(raw)["spots"]
    assert [s["mode"] for s in back] == ["heal", "clone"]
    assert back[0]["x"] == pytest.approx(0.25) and back[0]["sx"] == pytest.approx(0.35)


# ---------------------------------------------------------------------------
# Ratings, labels, keywords
# ---------------------------------------------------------------------------

def test_rating_label_and_keywords_in_the_sidecar(raw):
    editor.save_adjustments(raw, dict(FULL), source_size=(6000, 4000))
    sidecar = raw.with_suffix(".xmp")
    editor.write_rating(raw, 4)
    library.write_xmp(sidecar, label="Red", keywords=["harbour", "dusk, blue hour", "ferry|boat"])
    assert_clean(sidecar)

    out = json.loads(exiftool("-j", "-XMP-xmp:Rating", "-XMP-xmp:Label", "-XMP-dc:Subject",
                              str(sidecar)).stdout)[0]
    assert out["Rating"] == 4 and out["Label"] == "Red"
    assert sorted(out["Subject"]) == sorted(["harbour", "dusk, blue hour", "ferry|boat"])
    assert editor.read_rating(raw) == 4
    # And the develop settings written earlier survived the later writes.
    assert read_crs(sidecar)["ColorTemperature"] == 4800


def test_a_rejected_rating_is_minus_one(raw):
    editor.write_rating(raw, -1)
    out = exiftool("-s3", "-XMP-xmp:Rating", str(raw.with_suffix(".xmp"))).stdout.strip()
    assert out == "-1"
    assert editor.read_rating(raw) == -1


def test_library_writes_for_a_raw_go_to_the_sidecar_not_the_raw(raw):
    """A label or keyword on a CR2 must never rewrite the CR2 itself."""
    library.write_xmp(raw, label="Blue", keywords=["x"])
    assert raw.read_bytes() == b"not really a raw"
    sidecar = raw.with_suffix(".xmp")
    assert sidecar.exists()
    got = library.read_metadata([raw])[str(raw)]
    assert got["label"] == "Blue" and got["keywords"] == ["x"]


def test_library_reads_the_rating_the_editor_wrote_to_the_sidecar(raw):
    editor.write_rating(raw, 3)
    assert library.read_metadata([raw])[str(raw)]["rating"] == 3
