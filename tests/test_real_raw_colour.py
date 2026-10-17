"""
The colour pipeline on the real CC0 Canon CR2 (see test_real_raw_e2e.py for the sample).

    PHOTO_ROOM_REAL_RAW=1 pytest -m realraw tests/test_real_raw_colour.py
"""
import numpy as np
import pytest
from PIL import Image

import colour
import editor
import editversion
import exporter
import process
from exporter import ExportOptions
from test_real_raw_e2e import raw, sample, _fetch  # noqa: F401  (fixtures)

pytestmark = pytest.mark.realraw


@pytest.fixture(scope="module")
def preview(sample):
    lin16, baseline, _ = process.decode_preview_linear(sample)
    return editor._fit_preview(colour.linear16_to_encoded(lin16)), baseline, lin16


def test_the_linear_decode_is_linear(preview, sample):
    """gamma=(1, 1) really gives scene-linear: a +1 EV edit doubles the decoded light."""
    base, baseline, lin16 = preview
    out = editor.apply_adjustments(base, baseline, {"edit_version": 2, "exposure": 1.0, "color_temp": baseline})
    a, b = colour.to_linear(base), colour.to_linear(out)
    unclipped = (a.max(axis=2) < 0.45)          # not clipped by +1 EV
    assert unclipped.mean() > 0.5
    ratio = b[unclipped] / np.maximum(a[unclipped], 1e-4)
    keep = a[unclipped] > 0.01
    assert np.median(ratio[keep]) == pytest.approx(2.0, rel=2e-3)
    assert np.percentile(ratio[keep], [1, 99]) == pytest.approx([2.0, 2.0], rel=2e-2)


def test_legacy_edits_still_look_the_same_on_the_real_photo(preview, sample):
    import rawpy
    with rawpy.imread(str(sample)) as r:
        old = r.postprocess(use_camera_wb=True, no_auto_bright=True, output_bps=8, half_size=True)
    base, baseline, lin16 = preview
    new = np.rint(colour.to_legacy(colour.linear16_to_encoded(lin16)) * 255)
    assert np.abs(new - old.astype(float)).max() <= 2


def test_8bit_export_has_all_levels_where_the_old_path_banded(raw, tmp_path):
    adj = {"exposure": 2.0, "shadows": 50, "color_temp": process.camera_baseline_temp(raw)}
    img, icc = exporter.render(raw, ExportOptions(long_edge=1200), adjustments=adj)
    green = np.asarray(img)[:, :, 1]
    # the review measured 129 of 256 distinct levels on this edit through the old 8-bit decode
    levels = np.unique(green)
    assert len(levels) == int(levels.max()) - int(levels.min()) + 1      # contiguous: no gaps
    assert len(levels) >= 190


def test_straightened_16bit_export_is_not_an_8bit_picture(raw, tmp_path):
    adj = {"straighten": 1.0, "color_temp": process.camera_baseline_temp(raw)}
    arr, icc = exporter._render_array(raw, ExportOptions(long_edge=1200, bit_depth=16, format="png").validate(),
                                      adjustments=adj)
    q = np.round(arr[:, :, 1] * 65535)
    assert len(np.unique(q)) > 20000                 # the photograph's own tonal range, at 16 bits


def test_export_agrees_with_the_preview_pipeline(raw, preview):
    """Same linear data, same maths: preview and export differ only by resampling."""
    base, baseline, _ = preview
    adj = {"exposure": 0.6, "contrast": 20, "shadows": 30, "color_temp": baseline}
    prev = editor.apply_adjustments(base, baseline, {"edit_version": 2, **adj})
    img, _ = exporter.render(raw, ExportOptions(long_edge=base.shape[1]), adjustments=adj)
    exp = np.asarray(img).astype(np.float32) / 255.0
    h = min(prev.shape[0], exp.shape[0])
    assert abs(float(prev[:h].mean()) - float(exp[:h].mean())) < 0.01


def test_upgrade_on_the_real_photo(preview):
    base, baseline, _ = preview
    old = {"exposure": 0.5, "contrast": 15, "shadows": 30, "highlights": -20, "color_temp": baseline,
           "edit_version": 1}
    up, report = editversion.upgrade(old, baseline, base, lambda b, t, a: editor.apply_adjustments(b, t, a))
    assert report["mean_difference_after"] < report["mean_difference_before"] * 0.5
    assert report["mean_difference_after"] < 8
