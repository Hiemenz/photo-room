"""
One crop / straighten coordinate system (geometry.py), checked end to end.

Every test here uses a NON-ZERO straighten: at 0 degrees the preview and Camera
Raw coordinates coincide, which is exactly why the double-crop (export), the
drifting crop (save/reopen) and the batch/single mismatch went unnoticed.
"""
import random

import numpy as np
import pytest
from PIL import Image

import editor
import exporter
import geometry

CROP = {"left": 0.1, "top": 0.15, "right": 0.9, "bottom": 0.8}
FULL = {"left": 0.0, "top": 0.0, "right": 1.0, "bottom": 1.0}


def close(a, b, tol=2e-4):
    return all(abs(a[k] - b[k]) <= tol for k in ("left", "top", "right", "bottom"))


# ---------------------------------------------------------------- pure maths

def test_xmp_conversion_round_trips_for_random_angles_crops_and_frames():
    rng = random.Random(7)
    for _ in range(300):
        w, h = rng.randint(120, 6000), rng.randint(120, 4000)
        angle = rng.choice([-1, 1]) * rng.uniform(0.05, 20)
        l, t = rng.uniform(0, 0.4), rng.uniform(0, 0.4)
        crop = {"left": l, "top": t, "right": l + rng.uniform(0.2, 0.6),
                "bottom": t + rng.uniform(0.2, 0.6)}
        back = geometry.xmp_to_ui_crop(geometry.ui_to_xmp_crop(crop, angle, w, h), angle, w, h)
        assert close(back, crop, 1e-9), (w, h, angle)


def test_straighten_only_reads_back_as_no_crop_not_a_phantom_inset():
    for angle in (1.5, 10.0, -7.3):
        xmp = geometry.ui_to_xmp_crop(None, angle, 300, 200)
        assert xmp["left"] > 0 and xmp["right"] < 1          # Lightroom trims the corners
        xmp = {k: round(v, 5) for k, v in xmp.items()}        # XMP keeps 5 decimals
        assert geometry.xmp_to_ui_crop(xmp, angle, 300, 200) == FULL


def test_zero_angle_is_the_identity():
    assert geometry.ui_to_xmp_crop(CROP, 0.0, 300, 200) == CROP
    assert geometry.xmp_to_ui_crop(CROP, 0.0, 300, 200) == CROP


def test_photo_specific_keys_are_dropped_everywhere_a_look_is_shared():
    adj = {"exposure": 1.0, "crop": CROP, "straighten": 4, "xf_vertical": 30, "masks": [{}],
           "spots": [{}], "contrast": 5}
    assert geometry.without_photo_specific(adj) == {"exposure": 1.0, "contrast": 5}


# ------------------------------------------------- real exiftool round trips

@pytest.fixture
def photo(tmp_path):
    p = tmp_path / "a.jpg"
    # Smooth colour ramps: a 1px framing difference is invisible, a wrong crop is not.
    ys, xs = np.mgrid[0:200, 0:300]
    arr = np.stack([xs / 299.0, ys / 199.0, (xs + ys) / 498.0], axis=-1)
    im = Image.fromarray((arr * 255).astype(np.uint8))
    im.save(p, quality=95)
    return p


@pytest.mark.exiftool
@pytest.mark.parametrize("crop", [CROP, None])
@pytest.mark.parametrize("angle", [10.0, -6.5])
def test_save_reload_save_does_not_drift(photo, crop, angle):
    adj = {"color_temp": 5500, "straighten": angle, "crop": crop}
    for _ in range(4):                                   # reopen + save, repeatedly
        editor.save_adjustments(photo, adj, frame_size=(300, 200), source_size=(300, 200))
        back = editor.read_saved_adjustments(photo, source_size=(300, 200))
        assert back["straighten"] == pytest.approx(angle)
        assert close(back["crop"], crop or FULL), back["crop"]
        adj = {**adj, "crop": back["crop"]}


@pytest.mark.exiftool
@pytest.mark.parametrize("crop,angle", [(None, 10.0), (CROP, 10.0), (CROP, -4.0)])
def test_export_matches_preview_geometry_in_size_and_content(photo, tmp_path, crop, angle):
    adj = {"color_temp": 5500, "straighten": angle, "crop": crop}
    editor.save_adjustments(photo, adj, frame_size=(300, 200), source_size=(300, 200))

    base = np.asarray(Image.open(photo).convert("RGB"), dtype=np.float32) / 255.0
    preview = editor.apply_adjustments(base, 5500, {**adj, "color_temp": 5500})
    exported, _ = exporter.render(photo, exporter.ExportOptions(format="png"))

    # The XMP stores 5 decimals, so an edge may land on the neighbouring pixel;
    # the old double crop was off by ~20-30 pixels.
    ph, pw = preview.shape[:2]
    assert abs(exported.size[0] - pw) <= 1 and abs(exported.size[1] - ph) <= 1, (
        f"export {exported.size} framed differently from preview {(pw, ph)}")
    got = np.asarray(exported, dtype=np.float32) / 255.0
    hh, ww = min(ph, got.shape[0]), min(pw, got.shape[1])
    diff = np.abs(got[:hh, :ww] - preview[:hh, :ww]).mean()
    assert diff < 0.01, f"export shows different content than the preview (mean diff {diff:.3f})"


@pytest.mark.exiftool
def test_export_with_a_wrong_double_crop_would_have_been_caught(photo):
    """Guard on the test itself: a doubly-cropped render is measurably smaller."""
    adj = {"color_temp": 5500, "straighten": 10.0, "crop": None}
    base = np.zeros((200, 300, 3), np.float32)
    once = editor.apply_geometry(base, adj).shape
    twice = editor.apply_geometry(
        base, {**adj, "crop": geometry.ui_to_xmp_crop(None, 10.0, 300, 200)}).shape
    assert twice != once
