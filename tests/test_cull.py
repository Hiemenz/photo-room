"""Smart culling: features, burst grouping, scoring and suggestions (cull.py), on synthetic frames."""

import numpy as np
import pytest
from PIL import Image
import io

import cull


def textured(seed=1, h=600, w=800, blur=0.0, gain=1.0, offset=0.0):
    rng = np.random.default_rng(seed)
    base = rng.uniform(0, 255, (h // 8, w // 8, 3)).astype(np.float32)
    img = np.kron(base, np.ones((8, 8, 1), np.float32))
    img = img + rng.normal(0, 18, img.shape)
    if blur:
        import cv2
        img = cv2.GaussianBlur(img, (0, 0), blur)
    return np.clip(img * gain + offset, 0, 255).astype(np.uint8)


def feats_of(rgb):
    return cull.extract(rgb)


def test_sharp_frame_scores_above_blurred_copy():
    sharp, soft = feats_of(textured()), feats_of(textured(blur=3.0))
    assert sharp["sharp"] > 5 * soft["sharp"]


def test_focus_region_is_not_dragged_down_by_smooth_background():
    full = textured()
    half = full.copy()
    half[:, :560] = 128                       # most of the frame is flat "sky"
    f_full, f_half = feats_of(full), feats_of(half)
    assert f_half["sharp"] > 0.5 * f_full["sharp"]       # the whole-frame variance would be far lower
    assert f_half["sharp_global"] < 0.5 * f_full["sharp_global"]
    assert f_half["focus"][0] > 0.6                      # the focus region is on the textured side


def test_exposure_and_clipping_from_the_histogram():
    blown = feats_of(textured(gain=1.0, offset=200))
    ok = feats_of(textured(gain=0.5, offset=60))
    assert blown["clip_high"] > 0.3 and cull.exposure_ok(blown) < 0.3
    assert ok["clip_high"] < 0.01 and cull.exposure_ok(ok) > 0.8


def test_hash_is_stable_under_noise_and_differs_between_scenes():
    a = textured(seed=1)
    noisy = np.clip(a.astype(int) + np.random.default_rng(5).integers(-4, 5, a.shape), 0, 255).astype(np.uint8)
    other = textured(seed=2)
    ha, hn, ho = (int(feats_of(x)["hash"], 16) for x in (a, noisy, other))
    assert cull.hamming(ha, hn) <= cull.STRICT_HASH
    assert cull.hamming(ha, ho) > cull.LOOSE_HASH


def test_extract_accepts_jpeg_bytes_and_reports_no_faces_on_texture():
    buf = io.BytesIO()
    Image.fromarray(textured()).save(buf, format="JPEG", quality=80)
    f = cull.extract(buf.getvalue())
    assert f["v"] == cull.FEATURE_VERSION and f["faces"] == 0 and f["eyes_open"] == -1.0


def test_parse_time_formats():
    assert cull.parse_time("2024-05-01 10:00:05") - cull.parse_time("2024-05-01 10:00:00") == 5
    assert cull.parse_time("2024:05:01 10:00:00") == cull.parse_time("2024-05-01 10:00:00")
    assert cull.parse_time("") is None and cull.parse_time("nope") is None


def H(bits_flipped, base=0):
    return f"{base ^ ((1 << bits_flipped) - 1):016x}"


def test_grouping_by_time_and_hash():
    items = [
        {"path": "a", "t": 100.0, "hash": H(0)},
        {"path": "b", "t": 102.0, "hash": H(5)},       # close in time, similar
        {"path": "c", "t": 103.0, "hash": H(40)},      # close in time, different scene
        {"path": "d", "t": 500.0, "hash": H(40)},      # far in time, same scene as c: only strict match joins
        {"path": "e", "t": 505.0, "hash": H(42)},      # 5 s after d and similar
    ]
    groups = cull.group_photos(items)
    assert groups == [["a", "b"], ["c"], ["d", "e"]]


def test_near_identical_frames_join_across_a_longer_gap_but_not_forever():
    base = [{"path": "a", "t": 0.0, "hash": H(0)}, {"path": "b", "t": 40.0, "hash": H(3)},
            {"path": "c", "t": 4000.0, "hash": H(3)}]
    assert cull.group_photos(base) == [["a", "b"], ["c"]]


def test_without_capture_times_only_near_identical_frames_group():
    items = [{"path": "a", "t": None, "hash": H(0)}, {"path": "b", "t": None, "hash": H(3)},
             {"path": "c", "t": None, "hash": H(12)}]
    assert cull.group_photos(items) == [["a", "b"], ["c"]]


def burst(frames):
    """frames: name -> rgb. Returns (items, feats) as suggest() wants them."""
    items = [{"path": n, "name": n, "t": 1000.0 + i} for i, n in enumerate(frames)]
    return items, {n: feats_of(rgb) for n, rgb in frames.items()}


def test_suggest_picks_the_sharpest_and_rejects_the_clearly_bad():
    items, feats = burst({
        "sharp.cr2": textured(seed=1),
        "soft.cr2": textured(seed=1, blur=3.0),
        "blown.cr2": textured(seed=1, offset=230),
    })
    # the three share a scene (same hash) by construction; force that explicitly
    for f in feats.values():
        f["hash"] = feats["sharp.cr2"]["hash"]
    res = cull.suggest(items, feats)
    assert len(res["groups"]) == 1
    p = res["photos"]
    assert res["groups"][0]["best"] == "sharp.cr2" and p["sharp.cr2"]["action"] == "pick"
    assert p["soft.cr2"]["action"] == "reject" and "softer" in p["soft.cr2"]["reasons"][0]
    assert p["blown.cr2"]["action"] == "reject" and "blown" in p["blown.cr2"]["reasons"][0]
    assert p["sharp.cr2"]["score"] > p["soft.cr2"]["score"]
    assert all(v["reasons"] for v in p.values())


def test_a_single_good_frame_gets_no_suggestion_and_a_single_bad_frame_is_rejected():
    items, feats = burst({"good.cr2": textured(seed=1), "dark.cr2": textured(seed=9, gain=0.0)})
    feats["dark.cr2"]["hash"] = "ffffffffffffffff"
    feats["good.cr2"]["hash"] = "0000000000000000"
    res = cull.suggest(items, feats)
    assert res["photos"]["good.cr2"]["action"] == "none"
    assert res["photos"]["dark.cr2"]["action"] == "reject"


def test_closed_eyes_lose_to_an_open_eyed_frame_of_equal_sharpness():
    base = feats_of(textured())
    a = dict(base, faces=1, eyes_open=1.0, face_sharp=-1.0)
    b = dict(base, faces=1, eyes_open=0.0, face_sharp=-1.0)
    items = [{"path": "a", "name": "a", "t": 1.0}, {"path": "b", "name": "b", "t": 2.0}]
    res = cull.suggest(items, {"a": a, "b": b})
    assert res["groups"][0]["best"] == "a"
    assert res["photos"]["b"]["action"] == "reject" and "eyes" in res["photos"]["b"]["reasons"][0]


def test_haar_detection_does_not_crash_and_returns_defaults_on_a_flat_frame():
    gray = np.full((300, 400), 90, np.uint8)
    out = cull.detect_faces(gray)
    assert out == {"faces": 0, "eyes_open": -1.0, "face_sharp": -1.0}


def test_suggestions_are_pure():
    items, feats = burst({"a": textured(seed=1), "b": textured(seed=1, blur=2.0)})
    import copy
    before = copy.deepcopy(feats)
    cull.suggest(items, feats)
    assert feats == before
