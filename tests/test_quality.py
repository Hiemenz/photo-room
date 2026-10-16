"""
Tests for frame assessment.

The central design constraint: a verdict must only appear for frames that
are genuinely unrecoverable. Flagging a good photograph is far worse than
missing a bad one, because the point of the feature is deciding what to
throw away.
"""

import numpy as np
import pytest

import quality
from quality import assess, rank_by_sharpness, sharpness


def noisy(shape=(200, 300), level=0.25, seed=0):
    rng = np.random.default_rng(seed)
    img = 0.5 + level * rng.standard_normal((*shape, 3)).astype(np.float32)
    return np.clip(img, 0.0, 1.0)


# ---------------------------------------------------------------------------
# Unambiguous failures
# ---------------------------------------------------------------------------

def test_a_fully_blown_frame_is_flagged():
    a = assess(np.ones((64, 64, 3), dtype=np.float32))
    assert a["verdict"] == "blown"
    assert a["recoverable"] is False


def test_a_fully_black_frame_is_flagged():
    a = assess(np.zeros((64, 64, 3), dtype=np.float32))
    assert a["verdict"] == "black"
    assert a["recoverable"] is False


def test_a_flat_frame_is_flagged():
    a = assess(np.full((64, 64, 3), 0.5, dtype=np.float32))
    assert a["verdict"] == "flat"


def test_the_reason_names_the_actual_problem():
    assert "white" in assess(np.ones((32, 32, 3), np.float32))["reason"]
    assert "black" in assess(np.zeros((32, 32, 3), np.float32))["reason"]


# ---------------------------------------------------------------------------
# Frames that must NOT be flagged
# ---------------------------------------------------------------------------

def test_a_normal_frame_gets_no_verdict():
    a = assess(noisy())
    assert a["verdict"] is None
    assert a["recoverable"] is True


def test_partial_clipping_is_not_a_verdict():
    """
    Bright skies and specular highlights clip. Only a frame that is mostly
    gone is unrecoverable.
    """
    img = noisy()
    img[:40] = 1.0          # ~20% blown, a bright sky
    a = assess(img)
    assert a["verdict"] is None
    assert a["clipped_high"] > 10


def test_a_low_detail_frame_is_not_flagged():
    """
    A sharp subject against smooth grass scores low for detail. On a real
    shoot the lowest-scoring frame was a perfectly good photograph, so a
    low score must never produce a verdict.
    """
    img = np.full((200, 300, 3), 0.45, dtype=np.float32)
    img[80:120, 100:160] = 0.8          # small subject, large smooth field
    a = assess(img)
    assert a["verdict"] is None
    assert a["sharpness"] < assess(noisy())["sharpness"]


def test_a_dark_but_usable_frame_is_not_flagged():
    """Underexposed is recoverable; black is not."""
    a = assess(np.clip(noisy(level=0.06) * 0.25, 0, 1))
    assert a["verdict"] is None


@pytest.mark.parametrize("fraction", [0.1, 0.3, 0.45])
def test_clipping_below_the_threshold_is_not_a_verdict(fraction):
    img = noisy((200, 300))
    rows = int(200 * fraction)
    img[:rows] = 1.0
    assert assess(img)["verdict"] is None


def test_clipping_above_the_threshold_is_a_verdict():
    img = noisy((200, 300))
    img[:120] = 1.0          # 60%
    assert assess(img)["verdict"] == "blown"


# ---------------------------------------------------------------------------
# Sharpness is a number, not a judgement
# ---------------------------------------------------------------------------

def test_sharpness_is_never_a_verdict():
    """No input may produce a verdict on the basis of detail alone."""
    for level in (0.005, 0.02, 0.1, 0.4):
        img = np.clip(0.5 + level * np.random.default_rng(1).standard_normal((200, 300, 3)), 0, 1)
        a = assess(img.astype(np.float32))
        if a["verdict"] is not None:
            assert a["verdict"] in ("flat",), "only flatness may be a verdict"


def test_blur_lowers_the_sharpness_score():
    from PIL import Image, ImageFilter

    detailed = noisy()
    as_u8 = (detailed * 255).astype(np.uint8)
    blurred = np.asarray(
        Image.fromarray(as_u8).filter(ImageFilter.GaussianBlur(4)), dtype=np.float32
    ) / 255.0

    assert assess(blurred)["sharpness"] < assess(detailed)["sharpness"]


def test_sharpness_finds_a_subject_against_a_smooth_background():
    """
    The peak-tile measure exists so a detailed subject is not diluted by a
    large empty background. A whole-frame average would miss this.
    """
    rng = np.random.default_rng(2)
    img = np.full((240, 320, 3), 0.5, dtype=np.float32)
    img[100:140, 140:180] = np.clip(
        0.5 + 0.3 * rng.standard_normal((40, 40, 3)), 0, 1
    ).astype(np.float32)

    flat = np.full((240, 320, 3), 0.5, dtype=np.float32)
    assert assess(img)["sharpness"] > assess(flat)["sharpness"] * 5


def test_sharpness_is_comparable_across_resolutions():
    """
    A Laplacian's magnitude depends on the pixel grid. Without normalising,
    the editor's preview and the CLI's full decode would disagree about the
    same photograph.
    """
    from PIL import Image

    big = noisy((1400, 2000))
    as_u8 = (big * 255).astype(np.uint8)
    small = np.asarray(
        Image.fromarray(as_u8).resize((1000, 700), Image.BOX), dtype=np.float32
    ) / 255.0

    a, b = assess(big)["sharpness"], assess(small)["sharpness"]
    assert min(a, b) / max(a, b) > 0.7, f"{a:.1f} vs {b:.1f} — resolution dependent"


def test_sharpness_ordering_survives_resampling():
    """Ranking is the actual use; the order must not change with scale."""
    from PIL import Image

    def shrink(img):
        as_u8 = (img * 255).astype(np.uint8)
        return np.asarray(
            Image.fromarray(as_u8).resize((500, 350), Image.BOX), dtype=np.float32
        ) / 255.0

    a, b, c = noisy((700, 1000), 0.05, 1), noisy((700, 1000), 0.15, 2), noisy((700, 1000), 0.35, 3)
    full = [assess(x)["sharpness"] for x in (a, b, c)]
    small = [assess(shrink(x))["sharpness"] for x in (a, b, c)]

    assert full == sorted(full)
    assert small == sorted(small), "ranking flipped when resolution changed"


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def test_ranking_orders_least_detailed_first():
    assessments = {
        "a": {"sharpness": 50.0, "recoverable": True},
        "b": {"sharpness": 10.0, "recoverable": True},
        "c": {"sharpness": 30.0, "recoverable": True},
    }
    assert [n for n, _, _ in rank_by_sharpness(assessments)] == ["b", "c", "a"]


def test_ranking_excludes_unusable_frames():
    """A blown frame has no meaningful detail score; it must not be ranked."""
    assessments = {
        "good": {"sharpness": 20.0, "recoverable": True},
        "blown": {"sharpness": 0.0, "recoverable": False},
    }
    assert [n for n, _, _ in rank_by_sharpness(assessments)] == ["good"]


def test_ranking_reports_percentiles_within_the_shoot():
    assessments = {
        str(i): {"sharpness": float(i * 10), "recoverable": True} for i in range(1, 11)
    }
    ranked = rank_by_sharpness(assessments)
    assert ranked[0][2] == pytest.approx(0.0)
    assert ranked[-1][2] == pytest.approx(90.0)


def test_ranking_handles_an_all_unusable_shoot():
    assert rank_by_sharpness({"x": {"sharpness": 0.0, "recoverable": False}}) == []


def test_ranking_handles_an_empty_shoot():
    assert rank_by_sharpness({}) == []


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------

def test_accepts_uint8_and_float_alike():
    as_float = noisy()
    as_uint8 = (as_float * 255).astype(np.uint8)
    a, b = assess(as_float)["contrast"], assess(as_uint8)["contrast"]
    assert a == pytest.approx(b, abs=0.01)


def test_tiny_images_do_not_raise():
    assert assess(np.full((4, 4, 3), 0.5, dtype=np.float32))["verdict"] is not None


def test_every_assessment_field_is_json_safe():
    a = assess(noisy())
    for key in ("clipped_high", "clipped_low", "contrast", "sharpness"):
        assert type(a[key]) is float, f"{key} is not a plain float"
    assert type(a["recoverable"]) is bool
