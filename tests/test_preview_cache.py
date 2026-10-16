"""
Guards for the M10 preview pipeline: the stage cache keys, cache equivalence
(a cached render must equal a fresh one), memory bounds, stale-request
skipping, fast drag renders and the background thumbnail pool.
"""
import io
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import editor
import stagecache
import thumbs
from stagecache import (DETAIL, FINAL, GEOMETRY, GRADE, MIX, SATURATION, STAGE_FIELDS,
                        TONE, StageCache, stage_keys)

BASE_ADJ = {"exposure": 0.2, "contrast": 5, "saturation": 4, "clarity": 10,
            "hue_orange": 6, "grade_shadow_sat": 12, "crop": {"left": 0.1, "top": 0, "right": 1, "bottom": 1}}

# One representative change per stage-owned setting.
CHANGES = {
    "color_temp": 7000, "tint": 20, "exposure": 0.7, "shadows": 30, "highlights": -30,
    "blacks": 10, "whites": 10, "contrast": 30,
    "tone_curve": {"parametric": {"highlights": 20}},
    "saturation": 30, "vibrance": 30,
    "monochrome": True, "hue_red": 20, "sat_blue": 20, "lum_green": 20, "gray_aqua": 20,
    "grade_highlight_hue": 30, "grade_mid_sat": 20, "grade_global_lum": 10,
    "grade_blending": 80, "grade_balance": 20,
    "clarity": 40, "texture": 20, "dehaze": 20, "sharpness": 50,
    "masks": [{"type": "linear"}], "strength": 80,
}


def stage_of(key):
    """The first stage that owns ``key``."""
    for stage in sorted(STAGE_FIELDS):
        if key in STAGE_FIELDS[stage]:
            return stage
    raise AssertionError(key)


@pytest.mark.parametrize("key", sorted(CHANGES))
def test_changing_a_setting_only_invalidates_its_stage_and_later(key):
    before = stage_keys(BASE_ADJ, 5500)
    after = stage_keys({**BASE_ADJ, key: CHANGES[key]}, 5500)
    owner = stage_of(key)
    for stage in range(GEOMETRY, owner):
        assert before[stage] == after[stage], f"{key} wrongly invalidated {stagecache.STAGE_NAMES[stage]}"
    assert before[owner] != after[owner], f"{key} did not invalidate its own stage"
    assert before[FINAL] != after[FINAL]


def test_every_colour_setting_the_pipeline_reads_is_owned_by_a_stage():
    # If colormix grows a new control, the stage table must learn about it,
    # or it would silently live in the geometry key (safe, but never cached).
    import colormix
    owned = set().union(*STAGE_FIELDS.values())
    assert set(colormix.COLOR_DEFAULTS) <= owned, sorted(set(colormix.COLOR_DEFAULTS) - owned)


@pytest.mark.parametrize("key,value", [("crop", {"left": 0.3, "top": 0, "right": 1, "bottom": 1}),
                                       ("straighten", 3.0), ("spots", [{"x": 0.5}]),
                                       ("lens_profile", True), ("rotate", 90)])
def test_geometry_settings_invalidate_everything(key, value):
    before = stage_keys(BASE_ADJ, 5500)
    after = stage_keys({**BASE_ADJ, key: value}, 5500)
    assert all(a != b for a, b in zip(before, after))


def test_unknown_settings_default_to_invalidating_not_going_stale():
    before = stage_keys(BASE_ADJ, 5500)
    after = stage_keys({**BASE_ADJ, "some_future_slider": 3}, 5500)
    assert all(a != b for a, b in zip(before, after))


def test_baseline_temperature_is_part_of_every_key():
    a, b = stage_keys(BASE_ADJ, 5500), stage_keys(BASE_ADJ, 6500)
    assert all(x != y for x, y in zip(a, b))


def test_noise_reduction_is_ignored_because_the_preview_never_shows_it():
    a = stage_keys(BASE_ADJ, 5500)
    b = stage_keys({**BASE_ADJ, "luminance_nr": 40}, 5500)
    assert a == b


# ---------------------------------------------------------------------------
# Equivalence and reuse
# ---------------------------------------------------------------------------

@pytest.fixture
def photo():
    rng = np.random.default_rng(3)
    h, w = 300, 450
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    img = np.stack([x / w, y / h, (x + y) / (w + h)], axis=2) * 0.8 + 0.1
    return np.clip(img + rng.normal(0, 0.02, img.shape), 0, 1).astype(np.float32)


@pytest.mark.parametrize("mono", [False, True])
def test_cached_render_equals_fresh_render_through_a_slider_drag(photo, mono):
    original = photo.copy()
    adj = {**BASE_ADJ, "monochrome": mono, "sharpness": 30, "strength": 90,
           "sat_orange": 10, "grade_highlight_sat": 20,
           "masks": [{"id": "a", "type": "linear", "x0": 0.5, "y0": 0.2, "x1": 0.5, "y1": 0.8,
                      "adj": {"exposure": 0.5}}]}
    # Walk through settings from every stage, then back, against one cache.
    sequence = [("exposure", v) for v in (0.1, 0.3)] + [("saturation", 20), ("hue_red", 15),
                ("grade_shadow_sat", 25), ("clarity", 30), ("strength", 70), ("exposure", 0.3),
                ("hue_orange", 0), ("crop", {"left": 0, "top": 0.1, "right": 1, "bottom": 1}),
                ("exposure", 0.1)]
    editor._stage_cache.clear()
    for key, value in sequence:
        adj = {**adj, key: value}
        cached = editor.apply_adjustments(photo, 5500, adj, cache_id="p")
        fresh = editor.apply_adjustments(photo, 5500, adj)
        assert np.allclose(cached, fresh, atol=1e-6), key
    assert np.array_equal(photo, original), "the shared base render was modified"
    assert editor._stage_cache.stats()["hits"] > 0


def test_a_late_slider_does_not_redo_earlier_stages(photo, monkeypatch):
    calls = {"tone": 0, "sat": 0, "geo": 0}
    real = (editor._stage_tone, editor._stage_saturation, editor._stage_geometry)
    monkeypatch.setattr(editor, "_stage_tone", lambda *a: calls.__setitem__("tone", calls["tone"] + 1) or real[0](*a))
    monkeypatch.setattr(editor, "_stage_saturation", lambda *a: calls.__setitem__("sat", calls["sat"] + 1) or real[1](*a))
    monkeypatch.setattr(editor, "_stage_geometry", lambda *a: calls.__setitem__("geo", calls["geo"] + 1) or real[2](*a))
    # Straighten makes a new array (worth caching); a bare crop is only a view.
    adj = {**BASE_ADJ, "saturation": 10, "straighten": 2.0}
    editor.apply_adjustments(photo, 5500, adj, cache_id="p")
    assert calls == {"tone": 1, "sat": 1, "geo": 1}
    for v in (10, 20, 30):                                   # drag clarity (detail stage)
        editor.apply_adjustments(photo, 5500, {**adj, "clarity": v}, cache_id="p")
    assert calls == {"tone": 1, "sat": 1, "geo": 1}
    editor.apply_adjustments(photo, 5500, {**adj, "exposure": 1.0}, cache_id="p")   # tone stage
    assert calls["tone"] == 2 and calls["sat"] == 2


def test_hsl_drag_reuses_the_hsv_analysis(photo, monkeypatch):
    import colormix
    made = []
    real = colormix.Analysis
    monkeypatch.setattr(colormix, "Analysis", lambda img: made.append(1) or real(img))
    adj = {**BASE_ADJ, "hue_red": 5}
    for v in (5, 10, 15, 20):
        editor.apply_adjustments(photo, 5500, {**adj, "sat_blue": v}, cache_id="p")
    assert len(made) == 1


def test_cached_arrays_are_read_only(photo):
    editor.apply_adjustments(photo, 5500, BASE_ADJ, cache_id="p")
    arrays = [v for v, _ in editor._stage_cache._items.values() if hasattr(v, "flags")]
    assert arrays and all(not a.flags.writeable for a in arrays)


def test_hsl_without_hue_change_matches_the_full_hsv_roundtrip(photo):
    import colormix
    adj = {"sat_orange": 40, "sat_blue": -30, "lum_red": 20}
    a = colormix.apply_hsl(photo, adj)
    h, s, v = colormix._rgb_to_hsv_numpy(photo)
    chroma = colormix._smoothstep(0.0, colormix.CHROMA_FLOOR * 2, s)
    ms, ml = colormix.band_mix(h, colormix._vec(adj, "sat"), colormix._vec(adj, "lum"))
    expect = colormix._hsv_to_rgb_numpy(h, np.clip(s * (1 + ms * chroma / 100), 0, 1), v)
    expect = np.clip(expect * (1 + ml * chroma / 100 * colormix.LUM_GAIN)[..., None], 0, 1)
    assert np.abs(a - expect).max() < 5e-3


# ---------------------------------------------------------------------------
# Memory bounds
# ---------------------------------------------------------------------------

def test_stage_cache_is_bounded_by_bytes_and_evicts_least_recent():
    cache = StageCache(max_bytes=3000)
    arrays = [np.zeros(250, dtype=np.float32) for _ in range(5)]        # 1000 bytes each
    for i, a in enumerate(arrays[:3]):
        cache.put("p", i, "k", a)
    assert cache.get("p", 0, "k") is not None                           # touch 0
    cache.put("p", 3, "k", arrays[3])                                   # evicts 1, the oldest
    assert cache.get("p", 1, "k") is None
    assert cache.get("p", 0, "k") is not None
    assert cache.stats()["bytes"] <= 3000
    cache.put("p", 9, "k", np.zeros(5000, dtype=np.float32))            # larger than the limit
    assert cache.get("p", 9, "k") is None


def test_stage_cache_total_stays_under_the_limit_while_dragging(photo):
    editor._stage_cache.max_bytes = 6 * photo.nbytes
    for v in range(40):
        editor.apply_adjustments(photo, 5500, {**BASE_ADJ, "exposure": v / 10}, cache_id="p")
    assert editor._stage_cache.stats()["bytes"] <= 6 * photo.nbytes


def test_evicting_a_photo_drops_its_stage_entries():
    editor._stage_cache.put("a", 1, "k", np.zeros(4, dtype=np.float32))
    editor._stage_cache.put("b", 1, "k", np.zeros(4, dtype=np.float32))
    editor._stage_cache.drop_photo("a")
    assert editor._stage_cache.get("a", 1, "k") is None
    assert editor._stage_cache.get("b", 1, "k") is not None


# ---------------------------------------------------------------------------
# Stale requests and fast renders
# ---------------------------------------------------------------------------

@pytest.fixture
def big_photo(monkeypatch):
    base = np.full((1000, 1500, 3), 0.4, dtype=np.float32)
    monkeypatch.setattr(editor, "_load_base_render", lambda path: (base, 5500))
    return Path("/nonexistent/IMG_0001.CR2")


def test_a_request_overtaken_by_a_newer_one_is_skipped(big_photo):
    assert "preview" in editor._render_if_current(big_photo, {}, seq=10)
    editor._render_if_current(big_photo, {}, seq=12)
    assert editor._render_if_current(big_photo, {}, seq=11) == {"stale": True}


def test_a_render_in_progress_stops_between_stages_when_overtaken(big_photo):
    key = str(big_photo.resolve())
    editor._note_seq(key, 5)
    calls = {"n": 0}

    def stale():
        calls["n"] += 1
        return calls["n"] > 2

    with pytest.raises(editor.StaleRender):
        editor.render_with_histogram(big_photo, {"exposure": 1}, should_abort=stale)


def test_requests_without_a_sequence_number_always_render(client_for_preview):
    r = client_for_preview(None)
    assert "preview" in r


def test_fast_render_is_smaller_and_full_render_unchanged(big_photo):
    def size(r):
        data = r["preview"].split(",", 1)[1]
        import base64
        return Image.open(io.BytesIO(base64.b64decode(data))).size
    assert max(size(editor.render_with_histogram(big_photo, {}, fast=True))) == editor.FAST_EDGE
    assert size(editor.render_with_histogram(big_photo, {})) == (1500, 1000)


def test_fast_render_is_not_used_for_spots_or_lens(big_photo):
    def size(r):
        import base64
        return Image.open(io.BytesIO(base64.b64decode(r["preview"].split(",", 1)[1]))).size
    adj = {"spots": [{"x": 0.5, "y": 0.5, "r": 0.05}]}
    assert size(editor.render_with_histogram(big_photo, adj, fast=True)) == (1500, 1000)


@pytest.fixture
def client_for_preview(big_photo, tmp_path, monkeypatch):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    (tmp_path / "IMG_0001.CR2").write_bytes(b"x")
    monkeypatch.setattr(editor, "_safe_path", lambda raw: big_photo)
    client = editor.app.test_client()

    def post(seq):
        body = {"path": "IMG_0001.CR2", "adjustments": {}}
        if seq is not None:
            body["seq"] = seq
        return client.post("/api/preview", json=body, headers={"Origin": "http://localhost"}).get_json()
    return post


def test_preview_endpoint_flags_stale_sequence_numbers(client_for_preview):
    assert "preview" in client_for_preview(100)
    assert client_for_preview(99) == {"stale": True}
    assert "preview" in client_for_preview(101)


# ---------------------------------------------------------------------------
# Background thumbnails
# ---------------------------------------------------------------------------

def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (800, 600), (90, 120, 150)).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture
def fake_preview(monkeypatch):
    payload = _jpeg()
    count = {"n": 0}

    def fake_run(cmd, **kwargs):
        count["n"] += 1
        time.sleep(0.01)
        return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr=b"")

    monkeypatch.setattr(thumbs.subprocess, "run", fake_run)
    return count


def _wait(pool, timeout=20):
    end = time.time() + timeout
    while pool.progress()["active"] and time.time() < end:
        time.sleep(0.02)


def test_prefetch_builds_every_thumbnail_in_the_background(tmp_path, fake_preview):
    files = []
    for i in range(12):
        f = tmp_path / f"IMG_{i:04d}.CR2"
        f.write_bytes(b"raw%d" % i)
        files.append(f)
    pool = thumbs.ThumbPool(tmp_path)
    started = time.time()
    first = pool.prefetch(files)
    assert time.time() - started < 1                  # returns at once, does not wait for the work
    assert first["total"] == 12
    _wait(pool)
    assert pool.progress() == {"total": 12, "done": 12, "failed": 0, "active": False}
    assert all(thumbs.cache_path(f, tmp_path).exists() for f in files)

    before = fake_preview["n"]
    assert pool.prefetch(files)["total"] == 0 or pool.progress()["active"] is False
    assert fake_preview["n"] == before                # nothing is rebuilt


def test_prefetch_counts_failures_and_keeps_going(tmp_path, monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")   # no preview anywhere
    monkeypatch.setattr(thumbs.subprocess, "run", fake_run)
    files = [tmp_path / f"a{i}.CR2" for i in range(3)]
    for f in files:
        f.write_bytes(b"x")
    pool = thumbs.ThumbPool(tmp_path)
    pool.prefetch(files)
    _wait(pool)
    assert pool.progress()["failed"] == 3 and pool.progress()["done"] == 3


def test_requested_thumbnail_is_not_stuck_behind_the_background_queue(tmp_path, monkeypatch):
    gate = threading.Event()
    payload = _jpeg()

    def slow_run(cmd, **kwargs):
        if str(cmd[-1]).endswith("wanted.CR2"):
            return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr=b"")
        gate.wait(5)                                   # background files crawl
        return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr=b"")

    monkeypatch.setattr(thumbs.subprocess, "run", slow_run)
    bg = [tmp_path / f"bg{i}.CR2" for i in range(6)]
    for f in bg:
        f.write_bytes(b"x")
    wanted = tmp_path / "wanted.CR2"
    wanted.write_bytes(b"w")
    pool = thumbs.ThumbPool(tmp_path)
    try:
        pool.prefetch(bg)
        assert len(pool.get_now(wanted, timeout=3)) > 100
    finally:
        gate.set()
        _wait(pool)


def test_thumbs_endpoints_report_progress(tmp_path, monkeypatch, fake_preview):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    monkeypatch.setattr(editor, "_thumb_pool_instance", None)
    monkeypatch.setattr(editor, "list_photos", lambda root: [tmp_path / "a.CR2", tmp_path / "b.CR2"])
    for n in ("a.CR2", "b.CR2"):
        (tmp_path / n).write_bytes(n.encode())
    client = editor.app.test_client()
    r = client.post("/api/thumbs/prefetch", json={}, headers={"Origin": "http://localhost"})
    assert r.status_code == 200 and r.get_json()["total"] == 2
    _wait(editor._thumb_pool())
    assert client.get("/api/thumbs/status").get_json()["done"] == 2
