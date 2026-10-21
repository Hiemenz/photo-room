"""
Metadata preservation on export, checked with the REAL exiftool.

No raw files are checked in, so the source is a synthetic photo carrying rich
EXIF (camera, lens, exposure, GPS), IPTC (keywords, caption, copyright) and an
embedded ICC profile, plus an XMP sidecar (rating, label, keywords, and develop
settings that must NOT travel). It goes through ``exporter.export_photo`` to
PNG and JPEG, 8 and 16 bit, with presets, sharpening and a watermark, and
every source tag is looked up on the output with exiftool.

Run with ``pytest -m exiftool`` (CI does).
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import export_presets
import exporter
from exporter import ExportOptions, export_photo

pytestmark = pytest.mark.exiftool

CHECKED_GROUPS = ("EXIF", "GPS", "IPTC", "XMP")

SOURCE_TAGS = [
    # EXIF: camera, lens, exposure, dates
    "-EXIF:Make=Canon", "-EXIF:Model=Canon EOS 5D Mark IV",
    "-EXIF:LensMake=Canon", "-EXIF:LensModel=EF24-70mm f/2.8L II USM",
    "-EXIF:LensInfo=24 70 2.8 2.8", "-EXIF:LensSerialNumber=0000a1b2c3",
    "-EXIF:BodySerialNumber=012345678901",
    "-EXIF:FocalLength=35", "-EXIF:FocalLengthIn35mmFormat=35",
    "-EXIF:FNumber=4.5", "-EXIF:ExposureTime=1/320", "-EXIF:ISO=800",
    "-EXIF:ExposureProgram=Aperture-priority AE", "-EXIF:ExposureCompensation=-2/3",
    "-EXIF:MeteringMode=Multi-segment", "-EXIF:Flash=Off, Did not fire",
    "-EXIF:WhiteBalance=Manual", "-EXIF:ExposureMode=Auto",
    "-EXIF:DateTimeOriginal=2026:09:14 18:42:07", "-EXIF:CreateDate=2026:09:14 18:42:07",
    "-EXIF:OffsetTimeOriginal=-07:00", "-EXIF:SubSecTimeOriginal=123",
    "-EXIF:Artist=Kevin Hiemenz", "-EXIF:Copyright=(c) 2026 Kevin Hiemenz",
    "-EXIF:ImageDescription=Ferry at dusk", "-EXIF:UserComment=shot handheld",
    "-EXIF:Software=Test Firmware 1.2",
    # GPS
    "-GPSLatitude=47.6062", "-GPSLatitudeRef=N", "-GPSLongitude=122.3321",
    "-GPSLongitudeRef=W", "-GPSAltitude=56.5", "-GPSAltitudeRef=Above Sea Level",
    "-GPSImgDirection=123.4", "-GPSImgDirectionRef=True North",
    "-GPSTimeStamp=01:42:07", "-GPSDateStamp=2026:09:15", "-GPSMapDatum=WGS-84",
    # IPTC
    "-IPTC:Keywords=harbour", "-IPTC:Keywords=dusk", "-IPTC:Keywords=ferry",
    "-IPTC:Caption-Abstract=Ferry crossing at dusk", "-IPTC:ObjectName=Ferry",
    "-IPTC:CopyrightNotice=(c) 2026 Kevin Hiemenz", "-IPTC:By-line=Kevin Hiemenz",
    "-IPTC:City=Seattle", "-IPTC:Province-State=Washington",
    "-IPTC:Country-PrimaryLocationName=USA", "-IPTC:Credit=Kevin Hiemenz",
    # XMP
    "-XMP-dc:Title=Ferry", "-XMP-dc:Rights=(c) 2026 Kevin Hiemenz",
    "-XMP-dc:Subject=ferry", "-XMP-dc:Subject=water",
    "-XMP-xmp:Rating=4", "-XMP-photoshop:Country=USA",
]

# Tags that legitimately differ because they describe the file or the render,
# not the photograph. Each has a reason:
EXPECTED_TO_CHANGE = {
    # the output has its own pixel dimensions (resize, straighten, crop)
    "ImageWidth", "ImageHeight", "ExifImageWidth", "ExifImageHeight",
    # orientation is applied to the pixels, then normalised to 1 (see own test)
    "Orientation",
    # thumbnails / previews show the *unedited* frame; deliberately dropped (IFD1)
    "PreviewImage", "PreviewImageLength", "PreviewImageStart", "ThumbnailImage",
    "ThumbnailOffset", "ThumbnailLength", "Compression",
    # the container is re-encoded
    "StripOffsets", "StripByteCounts", "RowsPerStrip", "BitsPerSample",
    "PhotometricInterpretation", "SamplesPerPixel", "PlanarConfiguration",
    "YCbCrSubSampling", "YCbCrPositioning", "ColorSpace", "ComponentsConfiguration",
    "XResolution", "YResolution", "ResolutionUnit",
    # bookkeeping exiftool regenerates on every write
    "XMPToolkit", "Format", "MetadataDate", "ModifyDate", "ExifVersion",
    "FlashpixVersion", "ApplicationRecordVersion", "EnvelopeRecordVersion",
}


def exiftool_json(path: Path, *flags: str) -> dict:
    out = subprocess.run(["exiftool", "-j", "-G0", "-a", "-n", "-struct", *flags, str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)[0]


def tags_of(path: Path) -> dict:
    tags = {}
    for key, value in exiftool_json(path).items():
        group, _, name = key.partition(":")
        if (group in CHECKED_GROUPS and name not in EXPECTED_TO_CHANGE
                and not name.startswith("CurrentIPTCDigest")):
            tags.setdefault(name, value)
    return tags


def _norm(value):
    if isinstance(value, list):
        return sorted(str(v) for v in value)
    if isinstance(value, float):
        return round(value, 4)
    return value


def assert_every_tag_survived(source: Path, out: Path, allow_changed=(), ignore=()) -> None:
    src, dst = tags_of(source), tags_of(out)
    src = {k: v for k, v in src.items() if k not in ignore}
    missing = sorted(set(src) - set(dst))
    assert not missing, f"tags dropped by export: {missing}"
    changed = {k: (src[k], dst[k]) for k in src
               if k not in allow_changed and _norm(src[k]) != _norm(dst[k])}
    assert not changed, f"tags altered by export: {changed}"


def icc_description(path: Path) -> str | None:
    return exiftool_json(path, "-ICC_Profile:ProfileDescription").get("ICC_Profile:ProfileDescription")


def make_source(path: Path, size=(160, 120), orientation: int | None = None,
                icc: bytes | None = None) -> Path:
    """A synthetic JPEG with rich EXIF/GPS/IPTC/XMP and an embedded ICC profile."""
    x = np.linspace(30, 220, size[0], dtype=np.uint8)
    y = np.linspace(30, 200, size[1], dtype=np.uint8)
    arr = np.stack([np.tile(x, (size[1], 1)), np.tile(y[:, None], (1, size[0])),
                    np.full((size[1], size[0]), 90, np.uint8)], axis=-1)
    Image.fromarray(arr).save(path, "JPEG", quality=95,
                              icc_profile=icc or exporter.icc_profile_for("adobergb"))
    extra = [f"-EXIF:Orientation#={orientation}"] if orientation else []
    subprocess.run(["exiftool", "-q", "-overwrite_original", *SOURCE_TAGS, *extra, str(path)],
                   check=True, capture_output=True)
    return path


@pytest.fixture
def source(tmp_path):
    return make_source(tmp_path / "IMG_0001.jpg")


def add_sidecar(source: Path) -> Path:
    """Lightroom-style sidecar: rating/label/keywords travel, develop settings do not."""
    side = source.with_suffix(".xmp")
    subprocess.run(["exiftool", "-q", "-XMP-xmp:Rating=5", "-XMP-xmp:Label=Green",
                    "-XMP-dc:Subject=from-sidecar", "-XMP-crs:Exposure2012=1.5",
                    "-XMP-crs:ProcessVersion=11.0", "-o", str(side), os.devnull],
                   check=True, capture_output=True)
    return side


FINISH = {
    "sharpen": {"for": "screen", "amount": "high"},
    "watermark": {"type": "text", "text": "(c) Kevin", "size": 4, "opacity": 70,
                  "position": "br", "margin": 2},
}

CASES = [
    pytest.param("jpeg", 8, {}, id="jpeg8"),
    pytest.param("jpeg", 8, FINISH, id="jpeg8-sharpen-watermark"),
    pytest.param("png", 8, {}, id="png8"),
    pytest.param("png", 8, FINISH, id="png8-sharpen-watermark"),
    pytest.param("png", 16, {}, id="png16"),
    pytest.param("png", 16, FINISH, id="png16-sharpen-watermark"),
]

# TIFF keeps every tag; WebP has no IPTC block (see test_export_formats_metadata.py).
# exiftool -validate is JPEG-centric and nags about Exif-in-TIFF details, so these
# skip the strict-validation test.
EXTRA_CASES = [
    pytest.param("tiff", 8, {}, id="tiff8"),
    pytest.param("tiff", 16, {}, id="tiff16"),
]


@pytest.mark.parametrize("fmt,depth,finish", CASES + EXTRA_CASES)
def test_every_source_tag_survives(source, tmp_path, fmt, depth, finish):
    dest = export_photo(source, tmp_path / "out",
                        ExportOptions(format=fmt, bit_depth=depth, **finish))
    assert_every_tag_survived(source, dest)
    # The tags we planted are really there, so the comparison is not vacuous.
    got = tags_of(dest)
    for name in ("Make", "Model", "LensModel", "FocalLength", "FNumber", "ExposureTime", "ISO",
                 "GPSLatitude", "GPSLongitude", "GPSAltitude", "Keywords", "CopyrightNotice",
                 "Caption-Abstract", "Rating", "Rights", "Artist", "SubSecTimeOriginal"):
        assert name in got, name
    assert got["Model"] == "Canon EOS 5D Mark IV"
    assert got["GPSLatitude"] == pytest.approx(47.6062, abs=1e-4)
    assert got["GPSLongitude"] == pytest.approx(122.3321, abs=1e-4) and got["GPSLongitudeRef"] == "W"
    assert sorted(got["Keywords"]) == ["dusk", "ferry", "harbour"]


@pytest.mark.parametrize("fmt,depth,finish", CASES)
def test_the_exported_file_is_clean_and_has_the_right_depth(source, tmp_path, fmt, depth, finish):
    dest = export_photo(source, tmp_path / "out",
                        ExportOptions(format=fmt, bit_depth=depth, **finish))
    res = subprocess.run(["exiftool", "-validate", "-warning", "-error", "-a", "-G1", str(dest)],
                         capture_output=True, text=True)
    assert res.stderr.strip() == ""
    assert "Warning" not in res.stdout and "Error" not in res.stdout, res.stdout
    info = exiftool_json(dest)
    if fmt == "png":
        assert info["PNG:BitDepth"] == depth
    with Image.open(dest) as im:
        im.load()
        assert im.size == (160, 120)


@pytest.mark.parametrize("depth", [8, 16])
def test_png_carries_exif_xmp_and_icc_chunks(source, tmp_path, depth):
    dest = export_photo(source, tmp_path / "out", ExportOptions(format="png", bit_depth=depth))
    data = dest.read_bytes()
    kinds = []
    pos = 8
    while pos < len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        kinds.append(data[pos + 4:pos + 8])
        pos += 12 + length
    assert b"eXIf" in kinds, kinds
    assert b"iCCP" in kinds, kinds
    assert b"XML:com.adobe.xmp" in data, "XMP packet missing"
    assert any(k in kinds for k in (b"iTXt", b"tEXt", b"zTXt"))
    assert kinds[0] == b"IHDR" and kinds[-1] == b"IEND"
    # eXIf must come before IDAT for readers that stop early.
    assert kinds.index(b"eXIf") < kinds.index(b"IDAT")
    exif = exiftool_json(dest, "-EXIF:all")
    assert exif["EXIF:Model"] == "Canon EOS 5D Mark IV"
    assert exif["EXIF:ISO"] == 800


@pytest.mark.parametrize("fmt,depth", [("jpeg", 8), ("png", 8), ("png", 16)])
def test_the_source_icc_profile_is_embedded_on_the_output(source, tmp_path, fmt, depth):
    assert icc_description(source) == "Adobe RGB (1998)"
    dest = export_photo(source, tmp_path / "out", ExportOptions(format=fmt, bit_depth=depth))
    assert icc_description(dest) == "Adobe RGB (1998)"
    with Image.open(dest) as im:
        assert im.info.get("icc_profile") == source_icc_bytes(source)


def source_icc_bytes(path: Path) -> bytes:
    with Image.open(path) as im:
        return im.info["icc_profile"]


@pytest.mark.parametrize("space,desc", [("adobergb", "Adobe RGB (1998)"),
                                        ("prophoto", "ProPhoto RGB"), ("srgb", None)])
def test_a_requested_colour_space_replaces_the_profile_not_the_tags(source, tmp_path, space, desc):
    dest = export_photo(source, tmp_path / "out",
                        ExportOptions(format="png", bit_depth=16, colorspace=space))
    got = icc_description(dest)
    assert got and (desc is None or got == desc)
    assert_every_tag_survived(source, dest)


@pytest.mark.parametrize("name", sorted(export_presets.BUILTINS))
def test_built_in_presets_keep_the_metadata(source, tmp_path, name):
    opts = export_presets.options_from_payload(export_presets.BUILTINS[name])
    if opts.colorspace is None:
        pass
    dest = export_photo(source, tmp_path / "out", opts)
    assert_every_tag_survived(source, dest)
    assert icc_description(dest)       # every preset leaves a profile on the file


def test_resize_does_not_lose_tags_and_updates_only_the_dimensions(source, tmp_path):
    dest = export_photo(source, tmp_path / "out",
                        ExportOptions(format="jpeg", long_edge=80, **FINISH))
    assert_every_tag_survived(source, dest)
    with Image.open(dest) as im:
        assert im.size == (80, 60)
    assert exiftool_json(dest)["File:ImageWidth"] == 80


def test_orientation_is_applied_to_the_pixels_then_normalised(tmp_path):
    """The documented exception: the decoder applies Orientation, so the tag becomes 1."""
    src = make_source(tmp_path / "IMG_0002.jpg", orientation=6)
    dest = export_photo(src, tmp_path / "out", ExportOptions(format="png"))
    with Image.open(dest) as im:
        assert im.size == (120, 160)       # rotated into portrait
    info = exiftool_json(dest, "-Orientation")
    assert info.get("EXIF:Orientation", 1) == 1
    assert_every_tag_survived(src, dest)


def test_the_thumbnail_of_the_unedited_frame_is_not_copied(tmp_path):
    src = make_source(tmp_path / "IMG_0003.jpg")
    thumb = tmp_path / "thumb.jpg"
    Image.new("RGB", (16, 12), (200, 10, 10)).save(thumb, "JPEG")
    subprocess.run(["exiftool", "-q", "-overwrite_original", f"-ThumbnailImage<={thumb}", str(src)],
                   check=True, capture_output=True)
    assert "EXIF:ThumbnailImage" in exiftool_json(src, "-b")
    dest = export_photo(src, tmp_path / "out", ExportOptions(format="jpeg"))
    assert "EXIF:ThumbnailImage" not in exiftool_json(dest, "-b")
    assert_every_tag_survived(src, dest)


def test_sidecar_rating_label_keywords_travel_but_develop_settings_do_not(source, tmp_path):
    add_sidecar(source)
    dest = export_photo(source, tmp_path / "out", ExportOptions(format="jpeg"))
    info = exiftool_json(dest)
    assert info["XMP:Rating"] == 5 and info["XMP:Label"] == "Green"
    assert "from-sidecar" in info["XMP:Subject"]
    assert not [k for k in info if k.startswith("XMP:") and k.removeprefix("XMP:") in
                ("Exposure2012", "ProcessVersion")], "develop settings were copied twice"
    # The photo's own pre-existing keywords are still there too.
    assert sorted(tags_of(dest)["Keywords"]) == ["dusk", "ferry", "harbour"]


def test_xmp_crs_in_the_source_is_not_copied_to_the_output(source, tmp_path):
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-XMP-crs:Exposure2012=2",
                    "-XMP-crs:ToneCurvePV2012=0, 0|255, 255", str(source)],
                   check=True, capture_output=True)
    dest = export_photo(source, tmp_path / "out", ExportOptions(format="png"))
    assert not any(k.startswith("XMP:") and "Exposure2012" in k for k in exiftool_json(dest))
    assert_every_tag_survived(source, dest, ignore=("Exposure2012", "ToneCurvePV2012"))


def test_keep_metadata_false_strips_everything_personal(source, tmp_path):
    dest = export_photo(source, tmp_path / "out", ExportOptions(format="jpeg", keep_metadata=False))
    got = tags_of(dest)
    for name in ("Make", "Model", "GPSLatitude", "Keywords", "Artist", "Rating"):
        assert name not in got, name


def test_the_comparison_notices_a_dropped_tag(source, tmp_path):
    dest = export_photo(source, tmp_path / "out", ExportOptions(format="jpeg"))
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-GPSLatitude=", "-GPSLatitudeRef=",
                    "-IPTC:Keywords=", str(dest)], check=True, capture_output=True)
    with pytest.raises(AssertionError, match="dropped"):
        assert_every_tag_survived(source, dest)


def test_unicode_and_special_characters_in_tags_survive(tmp_path):
    src = make_source(tmp_path / "IMG_0004.jpg")
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-charset", "iptc=UTF8", "-IPTC:CodedCharacterSet=UTF8",
                    "-XMP-dc:Description=Café – 東京 \"quoted\" & <tagged>",
                    "-XMP-dc:Creator=Zoë Müller", "-IPTC:City=Zürich", str(src)],
                   check=True, capture_output=True)
    for fmt in ("jpeg", "png"):
        dest = export_photo(src, tmp_path / "out", ExportOptions(format=fmt))
        got = tags_of(dest)
        assert got["Description"] == "Café – 東京 \"quoted\" & <tagged>"
        assert got["Creator"] == ["Zoë Müller"] or got["Creator"] == "Zoë Müller"
        assert got["City"] == "Zürich"
