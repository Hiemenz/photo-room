"""
Export tests, run against the real exiftool.

No camera raws are checked in, so the "source photo" is a small synthetic
JPEG/TIFF carrying EXIF, GPS, IPTC and XMP written by exiftool itself. Where
the raw path matters, rawpy is faked as in the other tests. Every test here
that compares metadata fails if a single tag is dropped.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageCms

import exporter
from exporter import ExportOptions, export_photo

pytestmark = pytest.mark.skipif(
    shutil.which("exiftool") is None, reason="exiftool is not installed"
)

REPO = Path(__file__).resolve().parent.parent

TAGS_WRITTEN = [
    "-EXIF:Make=TestCam", "-EXIF:Model=T-1000", "-EXIF:LensModel=50mm Test",
    "-EXIF:ISO=400", "-EXIF:FNumber=2.8", "-EXIF:ExposureTime=1/250",
    "-EXIF:DateTimeOriginal=2026:09:14 12:34:56", "-EXIF:Artist=Kevin Hiemenz",
    "-EXIF:Copyright=(c) 2026 Kevin",
    "-GPSLatitude=47.6062", "-GPSLatitudeRef=N",
    "-GPSLongitude=122.3321", "-GPSLongitudeRef=W", "-GPSAltitude=56",
    "-IPTC:Keywords=harbour", "-IPTC:Keywords=dusk",
    "-IPTC:City=Seattle", "-IPTC:By-line=Kevin Hiemenz",
    "-IPTC:Caption-Abstract=Ferry at dusk",
    "-XMP-dc:Title=Ferry", "-XMP-dc:Subject=ferry", "-XMP-dc:Subject=water",
    "-XMP-xmp:Rating=4", "-XMP-photoshop:Country=USA",
]

# Groups whose tags must survive an export.
CHECKED_GROUPS = ("EXIF", "GPS", "IPTC", "XMP")

# Tags that legitimately differ: they describe the file/render, not the photo.
EXPECTED_TO_CHANGE = {
    "ImageWidth", "ImageHeight", "ExifImageWidth", "ExifImageHeight",
    "Orientation", "PreviewImage", "PreviewImageLength", "PreviewImageStart",
    "ThumbnailImage", "ThumbnailOffset", "ThumbnailLength",
    "StripOffsets", "StripByteCounts", "RowsPerStrip", "BitsPerSample",
    "Compression", "PhotometricInterpretation", "SamplesPerPixel",
    "PlanarConfiguration", "ColorSpace", "YCbCrSubSampling", "XResolution",
    "YResolution", "ResolutionUnit", "XMPToolkit", "Format", "MetadataDate",
    "ModifyDate", "ComponentsConfiguration", "FlashpixVersion",
    "ExifVersion", "YCbCrPositioning", "ApplicationRecordVersion",
    "CodedCharacterSet", "EnvelopeRecordVersion",
}


def read_tags(path: Path) -> dict[str, object]:
    """{tag name: value} for the groups that must survive, via real exiftool."""
    out = subprocess.run(
        ["exiftool", "-j", "-G0", "-a", "-n", "-struct", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    entry = json.loads(out)[0]
    tags = {}
    for key, value in entry.items():
        group, _, name = key.partition(":")
        if group in CHECKED_GROUPS and name not in EXPECTED_TO_CHANGE \
                and not name.startswith("CurrentIPTCDigest"):
            tags.setdefault(name, value)
    return tags


def _norm(value):
    if isinstance(value, list):
        return sorted(str(v) for v in value)
    if isinstance(value, float):
        return round(value, 4)
    return value


def assert_metadata_preserved(before: Path, after: Path):
    """Every tag the source had (in the checked groups) must be on the output."""
    src, out = read_tags(before), read_tags(after)
    src = {k: v for k, v in src.items() if not k.startswith(("Version", "RawFile"))}
    missing = sorted(set(src) - set(out))
    assert not missing, f"tags dropped by export: {missing}"
    changed = {k: (src[k], out[k]) for k in src if _norm(src[k]) != _norm(out[k])}
    assert not changed, f"tags altered by export: {changed}"


def make_photo(path: Path, size=(120, 80)) -> Path:
    ramp = np.linspace(40, 200, size[0], dtype=np.uint8)
    arr = np.tile(ramp, (size[1], 1))
    img = Image.fromarray(np.stack([arr, arr // 2 + 20, 255 - arr], axis=-1))
    kind = "TIFF" if path.suffix.lower() in (".tif", ".tiff") else "JPEG"
    img.save(path, kind)
    subprocess.run(["exiftool", "-q", "-overwrite_original", *TAGS_WRITTEN, str(path)],
                   check=True, capture_output=True)
    return path


@pytest.fixture(params=[".jpg", ".tif"])
def photo(request, tmp_path):
    return make_photo(tmp_path / f"IMG_0001{request.param}")


# ---------------------------------------------------------------------------
# The metadata guarantee
# ---------------------------------------------------------------------------

def test_fixture_really_carries_exif_gps_iptc_and_xmp(photo):
    tags = read_tags(photo)
    for name in ("Make", "GPSLatitude", "City", "Keywords", "Title", "Rating", "Country"):
        assert name in tags, name


@pytest.mark.parametrize("fmt", ["jpeg", "png"])
def test_export_keeps_every_tag(photo, tmp_path, fmt):
    dest = export_photo(photo, tmp_path / "out", ExportOptions(format=fmt))
    assert dest.suffix == (".jpg" if fmt == "jpeg" else ".png")
    assert_metadata_preserved(photo, dest)


def test_png_gets_exif_and_xmp_chunks(photo, tmp_path):
    dest = export_photo(photo, tmp_path / "out", ExportOptions(format="png"))
    data = dest.read_bytes()
    assert b"eXIf" in data, "PNG has no eXIf chunk"
    assert b"XML:com.adobe.xmp" in data, "PNG has no XMP chunk"
    gps = subprocess.run(["exiftool", "-s3", "-GPSLatitude", "-n", str(dest)],
                         capture_output=True, text=True).stdout.strip()
    assert float(gps) == pytest.approx(47.6062, abs=1e-3)


def test_the_comparison_fails_when_a_tag_is_dropped(photo, tmp_path):
    """The guard must bite: strip one group and the check has to complain."""
    dest = export_photo(photo, tmp_path / "out", ExportOptions(format="jpeg"))
    for strip in ("-IPTC:all=", "-GPS:all=", "-XMP-dc:Subject="):
        broken = tmp_path / "broken.jpg"
        shutil.copy(dest, broken)
        subprocess.run(["exiftool", "-q", "-overwrite_original", strip, str(broken)],
                       check=True, capture_output=True)
        with pytest.raises(AssertionError, match="dropped"):
            assert_metadata_preserved(photo, broken)


def test_no_metadata_flag_strips_the_tags(photo, tmp_path):
    dest = export_photo(photo, tmp_path / "out",
                        ExportOptions(format="jpeg", keep_metadata=False))
    tags = read_tags(dest)
    assert "GPSLatitude" not in tags and "City" not in tags and "Make" not in tags


def test_sidecar_rating_travels_but_develop_settings_do_not(tmp_path):
    raw = make_photo(tmp_path / "IMG_0002.jpg")
    sidecar = raw.with_suffix(".xmp")
    subprocess.run(["exiftool", "-q", "-XMP-xmp:Rating=5", "-XMP-dc:Subject=sidecar-kw",
                    "-XMP-crs:Exposure2012=1.5", "-XMP-crs:ColorTemperature=5200",
                    "-o", str(sidecar), "/dev/null"], capture_output=True)
    dest = export_photo(raw, tmp_path / "out", ExportOptions(format="jpeg"))
    out = subprocess.run(["exiftool", "-s3", "-XMP-xmp:Rating", "-XMP-dc:Subject",
                          "-XMP-crs:Exposure2012", str(dest)],
                         capture_output=True, text=True).stdout.split("\n")
    assert out[0] == "5"
    assert "sidecar-kw" in out[1]
    assert "1.5" not in out[2:], "develop settings were copied and would apply twice"


# ---------------------------------------------------------------------------
# Raw sources (rawpy faked), edits and the portable path
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_cr2(tmp_path, fake_rawpy):
    """A 'CR2' whose bytes are a metadata-rich TIFF; rawpy is faked."""
    (tmp_path / "shoot").mkdir()
    src = make_photo(tmp_path / "shoot" / "IMG_0003.tif")
    cr2 = src.with_suffix(".CR2")
    src.rename(cr2)
    return cr2


def test_raw_export_keeps_metadata_and_applies_sidecar_edits(fake_cr2, tmp_path):
    flat = exporter.export_photo(fake_cr2, tmp_path / "flat", ExportOptions(format="png"))

    import editor
    editor.save_adjustments(fake_cr2, {"exposure": 1.5, "color_temp": 5500})
    assert fake_cr2.with_suffix(".xmp").exists()

    bright = exporter.export_photo(fake_cr2, tmp_path / "bright", ExportOptions(format="png"))
    mean = lambda p: np.asarray(Image.open(p).convert("L"), dtype=float).mean()
    assert mean(bright) > mean(flat) + 10

    assert_metadata_preserved(fake_cr2, bright)


def test_saving_edits_on_a_raw_writes_only_a_sidecar(fake_cr2):
    import editor
    before = fake_cr2.read_bytes()
    editor.save_adjustments(fake_cr2, {"exposure": 0.5, "color_temp": 5400})
    assert fake_cr2.read_bytes() == before, "the raw was modified"
    saved = editor.read_saved_adjustments(fake_cr2)
    assert saved["exposure"] == pytest.approx(0.5)
    assert saved["color_temp"] == 5400


def test_rating_a_raw_goes_to_the_sidecar(fake_cr2):
    import editor
    before = fake_cr2.read_bytes()
    editor.write_rating(fake_cr2, 3)
    assert fake_cr2.read_bytes() == before
    assert editor.read_rating(fake_cr2) == 3


@pytest.mark.no_converter
def test_conversion_without_dng_converter_writes_a_sidecar(fake_cr2):
    import process
    assert not process.dng_converter_available()
    before = fake_cr2.read_bytes()
    result = process.process_file(fake_cr2, None, False, True, False)
    assert result == fake_cr2
    assert fake_cr2.read_bytes() == before
    assert fake_cr2.with_suffix(".xmp").exists()
    assert not fake_cr2.with_suffix(".dng").exists()
    import editor
    assert editor.read_saved_adjustments(fake_cr2) is not None


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

def test_long_edge_resizes_without_upscaling(photo, tmp_path):
    small = export_photo(photo, tmp_path / "a", ExportOptions(long_edge=60))
    assert Image.open(small).size == (60, 40)
    same = export_photo(photo, tmp_path / "b", ExportOptions(long_edge=5000))
    assert Image.open(same).size == (120, 80)
    assert_metadata_preserved(photo, small)


def test_jpeg_quality_changes_file_size(photo, tmp_path):
    lo = export_photo(photo, tmp_path / "lo", ExportOptions(quality=20))
    hi = export_photo(photo, tmp_path / "hi", ExportOptions(quality=95))
    assert lo.stat().st_size < hi.stat().st_size


@pytest.mark.parametrize("space,name", [
    ("srgb", "sRGB"), ("adobergb", "Adobe RGB"), ("prophoto", "ProPhoto"),
])
def test_colour_space_embeds_a_valid_profile(fake_cr2, tmp_path, space, name):
    dest = exporter.export_photo(fake_cr2, tmp_path / "o",
                                 ExportOptions(format="jpeg", colorspace=space))
    icc = Image.open(dest).info["icc_profile"]
    import io
    profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
    assert name in ImageCms.getProfileDescription(profile)
    assert_metadata_preserved(fake_cr2, dest)


def test_source_icc_profile_is_kept_for_plain_images(tmp_path):
    src = tmp_path / "IMG_9.jpg"
    Image.new("RGB", (40, 30), (90, 120, 150)).save(
        src, "JPEG", icc_profile=exporter.icc_profile_for("adobergb"))
    for fmt in ("jpeg", "png"):
        dest = export_photo(src, tmp_path / fmt, ExportOptions(format=fmt))
        assert Image.open(dest).info["icc_profile"] == exporter.icc_profile_for("adobergb")


def test_filename_template(photo, tmp_path):
    dest = export_photo(photo, tmp_path / "o",
                        ExportOptions(template="{date}_{seq:03d}_{name}_{width}x{height}"),
                        seq=7)
    assert dest.name == "20260914_007_IMG_0001_120x80.jpg"


def test_existing_files_are_never_overwritten(photo, tmp_path):
    a = export_photo(photo, tmp_path / "o", ExportOptions())
    b = export_photo(photo, tmp_path / "o", ExportOptions())
    assert a != b and a.exists() and b.exists()


@pytest.mark.parametrize("template", ["../escape", "a/b", "{nope}", ""])
def test_bad_templates_are_rejected(photo, tmp_path, template):
    with pytest.raises(ValueError):
        export_photo(photo, tmp_path / "o", ExportOptions(template=template))
    assert not list((tmp_path / "o").glob("*"))


@pytest.mark.parametrize("kwargs", [{"format": "gif"}, {"quality": 0},
                                    {"long_edge": 3}, {"colorspace": "p3"}])
def test_bad_options_are_rejected(kwargs):
    with pytest.raises(ValueError):
        ExportOptions(**kwargs).validate()


def test_cli_export(photo, tmp_path):
    out = tmp_path / "cli"
    result = subprocess.run(
        [sys.executable, str(REPO / "process.py"), "export", str(photo), "-o", str(out),
         "--format", "png", "--long-edge", "64", "--template", "x_{name}"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    dest = out / "x_IMG_0001.png"
    assert dest.exists() and max(Image.open(dest).size) == 64
    assert_metadata_preserved(photo, dest)
