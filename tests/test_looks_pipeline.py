"""Film styles in the render pipeline: where the LUT sits, caching, spaces, missing LUTs."""

import time

import numpy as np
import pytest

import colour
import editor
import lut as L
import stagecache


@pytest.fixture
def img():
    rng = np.random.default_rng(0)
    ramp = np.linspace(0.05, 0.95, 96, dtype=np.float32)
    base = np.stack([np.tile(ramp, (64, 1)), np.tile(ramp[::-1], (64, 1)), rng.random((64, 96)).astype(np.float32)], -1)
    return np.ascontiguousarray(base)


def adj(**kw):
    out = editor.complete_adjustments({}, 5500)
    out.update(kw)
    return out


def render(img, **kw):
    return editor.apply_adjustments(img, 5500, adj(**kw))


def test_the_lut_runs_on_the_finished_colour_edit(img):
    """tone -> saturation -> mixer -> grading, THEN the LUT (detail off so nothing follows it)."""
    edit = dict(exposure=0.4, contrast=25, saturation=15, sat_red=-30, grade_shadow_hue=220, grade_shadow_sat=30)
    without = render(img, **edit)
    with_lut = render(img, **edit, lut="builtin-teal-and-orange", lut_amount=100)
    assert np.allclose(with_lut, L.apply(without, L.get("builtin-teal-and-orange")), atol=1e-6)
    assert np.abs(with_lut - without).max() > 0.02


def test_the_lut_runs_before_the_detail_stage(img):
    edit = dict(clarity=60, sharpness=80)
    lut_id = "builtin-cool-chrome"
    got = render(img, **edit, lut=lut_id)
    lut_then_detail = editor._apply_detail(L.apply(render(img), L.get(lut_id)), adj(**edit))
    detail_then_lut = L.apply(editor._apply_detail(render(img), adj(**edit)), L.get(lut_id))
    assert np.allclose(got, np.clip(lut_then_detail, 0, 1), atol=1e-5)
    assert np.abs(got - detail_then_lut).max() > 1e-4


def test_intensity_blends_with_the_unfiltered_picture(img):
    full = render(img, lut="builtin-faded-matte", lut_amount=100)
    none = render(img, lut="builtin-faded-matte", lut_amount=0)
    half = render(img, lut="builtin-faded-matte", lut_amount=50)
    assert np.allclose(none, render(img), atol=1e-7)
    assert np.allclose(half, (full + none) / 2, atol=1e-6)


def test_no_lut_and_an_empty_lut_id_change_nothing(img):
    assert np.array_equal(render(img), render(img, lut="", lut_amount=70))


def test_black_and_white_keeps_the_luts_tint(img):
    """Colour is drained after grading, so a toning LUT has to come last to survive."""
    mono = render(img, monochrome=True)
    toned = render(img, monochrome=True, lut="builtin-sepia-print")
    assert np.abs(mono[..., 0] - mono[..., 2]).max() < 1e-6
    assert (toned[..., 0] - toned[..., 2]).mean() > 0.03                  # warm: red above blue
    assert np.allclose(toned, L.apply(mono, L.get("builtin-sepia-print")), atol=1e-6)


@pytest.mark.parametrize("space", ["adobergb", "prophoto"])
def test_the_lut_looks_right_through_the_linear_export_path(img, space):
    """The exporter hands over 16-bit linear light in the output space; the LUT must still land on encoded pixels."""
    lin = colour.to_linear(img, space)
    base = adj(lut="builtin-warm-negative", contrast=15, _linear_input=True, _space=space)
    plain = dict(base, lut="")
    with_lut = editor.apply_adjustments(lin, 5500, base)
    want = L.apply(editor.apply_adjustments(lin, 5500, plain), L.get("builtin-warm-negative"), space)
    assert np.allclose(with_lut, want, atol=1e-6)


def test_changing_only_the_intensity_does_not_redo_the_lookup(img, monkeypatch):
    calls = []
    real = L.apply
    monkeypatch.setattr(L, "apply", lambda *a, **k: calls.append(1) or real(*a, **k))
    cache = stagecache.StageCache(200 * 1024 * 1024)
    monkeypatch.setattr(editor, "_stage_cache", cache)
    run = lambda **kw: editor.apply_adjustments(img, 5500, adj(**kw), cache_id="photo")     # noqa: E731
    first = run(lut="builtin-cool-chrome", lut_amount=100)
    assert len(calls) == 1
    half = run(lut="builtin-cool-chrome", lut_amount=50)
    assert len(calls) == 1                                  # the slider only re-blends the cached look
    assert np.allclose(half, (first + run(lut="builtin-cool-chrome", lut_amount=0)) / 2, atol=1e-6)
    run(lut="builtin-teal-and-orange", lut_amount=50)
    assert len(calls) == 2                                  # another LUT is a real change
    run(lut="builtin-teal-and-orange", lut_amount=50, exposure=0.3)
    assert len(calls) == 3                                  # and so is an upstream edit
    # What the cache returns equals what a cold render returns
    assert np.allclose(run(lut="builtin-cool-chrome", lut_amount=100),
                       editor.apply_adjustments(img, 5500, adj(lut="builtin-cool-chrome")), atol=1e-7)


def test_lut_settings_belong_to_the_detail_stage_and_everything_after():
    assert {"lut", "lut_amount"} <= stagecache.STAGE_FIELDS[stagecache.DETAIL]
    a = stagecache.stage_keys(adj(lut="x"), 5500)
    b = stagecache.stage_keys(adj(lut="y"), 5500)
    assert a[stagecache.GRADE] == b[stagecache.GRADE] and a[stagecache.DETAIL] != b[stagecache.DETAIL]


def test_a_lut_this_machine_lacks_fails_loudly_for_export_and_softly_for_preview(img):
    with pytest.raises(L.LutNotFound, match="LUT library"):
        render(img, lut="0123456789ab")
    trimmed, warnings = editor._without_missing_lut(adj(lut="0123456789ab"))
    assert trimmed["lut"] == "" and "0123456789ab" in warnings[0]
    same, none = editor._without_missing_lut(adj(lut="builtin-cool-chrome"))
    assert none == [] and same["lut"] == "builtin-cool-chrome"


def test_edit_strength_scales_the_saved_intensity_and_counts_as_an_edit():
    out = editor.effective_adjustments(adj(lut="builtin-cool-chrome", lut_amount=80, strength=50))
    assert out["lut_amount"] == pytest.approx(40)
    assert editor.adjustments_have_edits(adj(lut="builtin-cool-chrome"), 5500)
    assert not editor.adjustments_have_edits(adj(lut="builtin-cool-chrome", lut_amount=0), 5500)
    assert not editor.adjustments_have_edits(adj(), 5500)


def test_preview_budget_one_lookup_of_a_fast_preview_is_well_under_a_second():
    """The 800 px drag preview is ~0.43 MP; the lookup (cached afterwards) must stay far inside the budget."""
    small = np.random.default_rng(1).random((533, 800, 3)).astype(np.float32)
    lut = L.get("builtin-warm-negative")
    start = time.perf_counter()
    L.apply(small, lut)
    assert time.perf_counter() - start < 1.5
