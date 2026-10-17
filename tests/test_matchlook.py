"""Match reference look: the derived values are plain controls and they close the gap."""

import io

import numpy as np
import pytest
from PIL import Image

import matchlook as M
import presetlib


def scene(seed=0, shape=(160, 240)):
    """Smooth, varied content: a gradient with some colour, so every tonal range is populated."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:shape[0], 0:shape[1]]
    base = np.stack([x / shape[1], y / shape[0], 0.5 + 0.3 * np.sin(x / 20.0)], axis=-1)
    return np.clip(base * 0.8 + rng.random(shape + (3,)) * 0.15, 0, 1).astype(np.float32)


def look(img):
    """A known 'film look': contrast, warm highlights, teal shadows, lifted blacks."""
    out = 0.5 + (img - 0.5) * 1.35
    y = out.mean(axis=2, keepdims=True)
    out = out + (1 - y) ** 2 * np.array([-0.03, 0.01, 0.05]) + y ** 2 * np.array([0.06, 0.02, -0.05])
    return np.clip(out * 0.94 + 0.03, 0, 1).astype(np.float32)


def test_matching_a_known_look_closes_most_of_the_gap():
    target = scene(1)
    ref = look(scene(2))                     # same kind of scene, different pixels
    res = M.derive(target, ref)
    rep = res["report"]
    b, a = rep["distance_before"], rep["distance_after"]
    assert a["tone"] < b["tone"] * 0.4
    assert a["colour_cast"] <= b["colour_cast"] + 0.004
    assert len(rep["steps"]) >= 4
    # The tint is in the shadows and the highlights, not the average: judge it there.
    out = M.render(M.downsize(target), res["values"])

    def tint(img, dark):
        y = img.mean(axis=2)
        sel = y < 0.35 if dark else y > 0.65
        return float((img[..., 2] - img[..., 0])[sel].mean())

    for dark in (True, False):
        ref_t = tint(M.downsize(ref), dark)
        assert abs(tint(out, dark) - ref_t) < abs(tint(M.downsize(target), dark) - ref_t) * 0.7


def test_the_result_is_ordinary_editable_controls():
    res = M.derive(scene(3), look(scene(4)))
    values = res["values"]
    clean, dropped = presetlib.clean_values(values)
    assert dropped == [] and clean.keys() == values.keys()
    allowed = presetlib.GROUPS["curves"] + presetlib.GROUPS["grading"] + presetlib.GROUPS["colour"]
    assert set(values) <= set(allowed)
    pts = values["tone_curve"]["points"]["rgb"]
    assert [p[0] for p in pts] == sorted(p[0] for p in pts) and all(b[1] >= a[1] for a, b in zip(pts, pts[1:]))
    # Applies like any preset, at an amount
    preset = {"values": clean}
    half = presetlib.apply_preset(presetlib.neutral_adjustments(), preset, 0.5, 5500)
    assert half["edit_version"] == 2


def test_a_reference_with_the_same_look_asks_for_nothing_much():
    img = scene(5)
    res = M.derive(img, img.copy())
    v = res["values"]
    assert res["report"]["distance_after"]["tone"] <= 0.01
    assert abs(v.get("saturation", 0)) <= 3
    assert all(v.get(k, 0) <= 3 for k in ("grade_shadow_sat", "grade_mid_sat", "grade_highlight_sat"))
    rgb = v["tone_curve"]["points"]["rgb"]
    assert max(abs(y - x) for x, y in rgb) < 6                      # close to the identity curve


def test_a_black_and_white_reference_drains_the_colour():
    target = scene(6)
    grey = np.repeat(target.mean(axis=2, keepdims=True), 3, axis=2)
    res = M.derive(target, grey)
    assert res["values"].get("saturation", 0) <= -50
    assert res["report"]["distance_after"]["saturation"] < res["report"]["distance_before"]["saturation"]


def test_load_image_handles_formats_orientation_and_garbage():
    buf = io.BytesIO()
    Image.fromarray((scene(7) * 255).astype(np.uint8)).save(buf, "PNG")
    arr = M.load_image(buf.getvalue())
    assert arr.dtype == np.float32 and max(arr.shape[:2]) <= M.EDGE and 0 <= arr.min() and arr.max() <= 1
    with pytest.raises(M.MatchError, match="not an image"):
        M.load_image(b"this is not an image")
    with pytest.raises(M.MatchError, match="8 MB"):
        M.load_image(b"x" * (M.MAX_UPLOAD + 1))
    with pytest.raises(M.MatchError):
        M.derive(np.zeros((4, 4, 3), np.float32), scene(1))
