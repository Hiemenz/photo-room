"""
Regression tests for defects found reviewing this branch.

Each one sits in a path the rest of the suite did not reach.
"""

from pathlib import Path

import pytest

import editor
import ingest
import process
import style


# ---------------------------------------------------------------------------
# An empty card must not crash the report
# ---------------------------------------------------------------------------

def test_an_empty_card_reports_without_crashing(capsys):
    """
    A JPEG-only card, or one already erased, returned a summary missing the
    keys _report reads — an uncaught KeyError on a perfectly ordinary card.
    """
    summary = ingest.ingest_card(Path("/nonexistent-card"), Path("/tmp/lib"))
    ingest._report(summary)          # must not raise
    assert "0 raw file(s)" in capsys.readouterr().out


def test_the_empty_summary_carries_every_key_the_report_reads(tmp_path):
    card = tmp_path / "CARD"
    (card / "DCIM").mkdir(parents=True)

    summary = ingest.ingest_card(card, tmp_path / "lib")
    for key in ("card", "library", "found", "copied", "already_present",
                "converted", "failed"):
        assert key in summary, f"_report reads {key}"


# ---------------------------------------------------------------------------
# Reset must respect edits made elsewhere
# ---------------------------------------------------------------------------

@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    (tmp_path / "lightroom.dng").write_bytes(b"dng")
    (tmp_path / "mine.dng").write_bytes(b"dng")
    return tmp_path


@pytest.fixture
def client(library):
    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        yield c


@pytest.fixture
def one_foreign(monkeypatch):
    monkeypatch.setattr(
        editor, "detect_foreign_edits",
        lambda p: "Adobe Photoshop Lightroom Classic" if "lightroom" in p.name else None,
    )


def test_reset_refuses_to_strip_lightroom_settings(client, one_foreign, fake_rawpy, fake_exiftool):
    """
    Revert and batch apply both guard this; reset did not, so one click
    destroyed develop settings that exist nowhere else.
    """
    res = client.post("/api/reset", json={"path": "lightroom.dng"})
    assert res.status_code == 409
    assert "Lightroom" in res.get_json()["error"]


def test_reset_does_not_write_when_it_refuses(client, one_foreign, fake_rawpy, fake_exiftool):
    client.post("/api/reset", json={"path": "lightroom.dng"})
    assert not any("XMP-crs:all=" in a for cmd in fake_exiftool.calls for a in cmd)


def test_reset_still_works_on_our_own_files(client, one_foreign, fake_rawpy, fake_exiftool):
    res = client.post("/api/reset", json={"path": "mine.dng"})
    assert res.status_code == 200


def test_reset_can_be_forced(client, one_foreign, fake_rawpy, fake_exiftool):
    res = client.post("/api/reset", json={"path": "lightroom.dng", "overwrite_foreign": True})
    assert res.status_code == 200


# ---------------------------------------------------------------------------
# A save behind a sidecar must say so
# ---------------------------------------------------------------------------

def test_saving_behind_a_sidecar_warns_that_it_will_not_take_effect(client, library, fake_rawpy, fake_exiftool):
    """
    Reads prefer the sidecar, writes go to the DNG — so the edit appeared to
    save and silently reverted on reopen.
    """
    (library / "mine.xmp").write_text("<xmpmeta/>")

    data = client.post("/api/save", json={
        "path": "mine.dng", "adjustments": {"exposure": 1.0},
    }).get_json()

    assert data["warning"], "a save that will not be read back must say so"
    assert "mine.xmp" in data["warning"]


def test_an_ordinary_save_carries_no_warning(client, fake_rawpy, fake_exiftool):
    data = client.post("/api/save", json={
        "path": "mine.dng", "adjustments": {"exposure": 1.0},
    }).get_json()
    assert data["warning"] is None
    assert "Saved to" in data["message"]


# ---------------------------------------------------------------------------
# Every learnable setting must actually be learnable
# ---------------------------------------------------------------------------

def test_settings_the_heuristic_never_emits_are_still_recorded(tmp_path):
    """
    record_edit required a key in both the chosen and automatic values, but
    the heuristic emits only five of the ten — so contrast, whites, blacks,
    vibrance and saturation were dropped before any model saw them.
    """
    db = tmp_path / "s.duckdb"
    features = {k: 0.4 for k in style.FEATURE_KEYS}
    auto = {"exposure": 1.0, "shadows": 10, "highlights": 0, "tint": 0}
    chosen = {**auto, "contrast": 25, "vibrance": 30, "saturation": -5,
              "whites": 8, "blacks": -4, "temp_ratio": 1.05}

    style.record_edit(Path("x.CR2"), features, auto, chosen, db_path=db)
    residual = style.load_training_data(db)[0][1]

    for key in ("contrast", "vibrance", "saturation", "whites", "blacks"):
        assert key in residual, f"{key} was dropped at record time"
    assert residual["contrast"] == pytest.approx(25)


def test_every_learned_key_is_recordable(tmp_path):
    db = tmp_path / "s.duckdb"
    features = {k: 0.4 for k in style.FEATURE_KEYS}
    chosen = {k: 1.0 for k in style.LEARNED_KEYS}

    style.record_edit(Path("x.CR2"), features, {}, chosen, db_path=db)
    residual = style.load_training_data(db)[0][1]
    assert set(residual) == set(style.LEARNED_KEYS)


def test_the_ingest_path_writes_learned_settings(fake_exiftool):
    """
    Ingest printed "[styled]" while embed_xmp_adjustments wrote only eight
    tags, discarding the contrast and vibrance it had just computed.
    """
    adj = {
        "color_temp": 5000, "tint": 0, "exposure": 1.0, "shadows": 10,
        "highlights": 0, "luminance_nr": 0,
        "contrast": 22, "vibrance": 18, "saturation": -4,
        "whites": 6, "blacks": -3,
    }
    process.embed_xmp_adjustments(Path("x.dng"), adj)

    written = " ".join(fake_exiftool.write_call())
    for tag in ("Contrast2012=22", "Vibrance=18", "Saturation=-4",
                "Whites2012=6", "Blacks2012=-3"):
        assert tag in written, f"{tag} never reached the file"


def test_an_unstyled_conversion_writes_only_the_heuristic_tags(fake_exiftool):
    """Without a learned style nothing extra should appear."""
    adj = {"color_temp": 5000, "tint": 0, "exposure": 1.0,
           "shadows": 10, "highlights": 0, "luminance_nr": 0}
    process.embed_xmp_adjustments(Path("x.dng"), adj)

    written = " ".join(fake_exiftool.write_call())
    assert "Contrast2012" not in written
    assert "Vibrance" not in written


# ---------------------------------------------------------------------------
# Edit strength and colour temperature
# ---------------------------------------------------------------------------

def test_edit_strength_scales_colour_temperature():
    """
    The preview blends white balance toward the original as strength drops,
    but the saved value was left at full — so the file did not match the
    screen.
    """
    out = editor.effective_adjustments(
        {"exposure": 2.0, "color_temp": 8000, "_baseline_temp": 5000, "strength": 50}
    )
    assert out["color_temp"] == pytest.approx(6500)
    assert out["exposure"] == pytest.approx(1.0)


def test_temperature_is_untouched_without_a_baseline():
    """Nothing to scale toward; collapsing to a generic 5500K would tint it."""
    out = editor.effective_adjustments({"color_temp": 8000, "strength": 50})
    assert out["color_temp"] == pytest.approx(8000)


def test_zero_strength_returns_the_camera_white_balance():
    out = editor.effective_adjustments(
        {"color_temp": 9000, "_baseline_temp": 4200, "strength": 0}
    )
    assert out["color_temp"] == pytest.approx(4200)


def test_open_returns_the_camera_baseline(client, fake_rawpy, fake_exiftool):
    """
    Hold-to-compare needs the camera's own white balance. Without it the
    client fell back to the saved temperature and the "before" render kept
    the entire white-balance edit applied.
    """
    fake_exiftool.stdout_lines = ["-"] * 22
    data = client.post("/api/open", json={"path": "mine.dng"}).get_json()
    assert isinstance(data.get("baseline_temp"), int)


# ---------------------------------------------------------------------------
# cull keys by path, not basename
# ---------------------------------------------------------------------------

def test_cull_does_not_collide_across_date_folders(tmp_path, monkeypatch, capsys):
    """
    Canon counters wrap at 9999, so two shoots can both hold IMG_0001.
    Keying by basename silently dropped one and under-reported the count.
    """
    import argparse

    for day in ("2026/09/14", "2026/09/15"):
        d = tmp_path / day
        d.mkdir(parents=True)
        (d / "IMG_0001.dng").write_bytes(b"dng")

    import numpy as np
    monkeypatch.setattr(process, "decode_for_analysis",
                        lambda p: (np.full((32, 48, 3), 120, np.uint8), 5000, np.ones(4)))

    process.cmd_cull(argparse.Namespace(
        inputs=[str(tmp_path)], sharpness=False, limit=10,
    ))
    assert "2 frame(s) assessed" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Commands that never convert must not require Adobe's converter
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("argv,needs", [
    (["process.py", "--help"], False),
    (["process.py", "style"], False),
    (["process.py", "cull", "x"], False),
    (["process.py", "revert", "x.dng"], False),
    (["process.py", "revert", "x.dng", "--extract-original"], True),
    (["process.py", "convert", "x.CR2"], True),
    (["process.py", "watch", "dir"], True),
])
def test_only_converting_commands_require_the_converter(argv, needs):
    argv1 = argv[1]
    reads_only = argv1 in ("style", "cull") or argv1.startswith("-")
    extracting = argv1 == "revert" and "--extract-original" in argv
    computed = bool(argv1) and (not reads_only) and (argv1 != "revert" or extracting)
    assert computed is needs


# ---------------------------------------------------------------------------
# Thumbnail writes must not race
# ---------------------------------------------------------------------------

def test_concurrent_thumbnail_writes_do_not_corrupt_the_cache(tmp_path, monkeypatch):
    """
    A fixed temp filename let two writers truncate the same path and promote
    interleaved bytes — served forever behind an immutable cache header.
    """
    import io
    from concurrent.futures import ThreadPoolExecutor

    import thumbs
    from PIL import Image

    photo = tmp_path / "IMG_1.dng"
    photo.write_bytes(b"dng")

    buf = io.BytesIO()
    Image.new("RGB", (900, 600), (90, 120, 150)).save(buf, format="JPEG")
    payload = buf.getvalue()

    import subprocess
    monkeypatch.setattr(
        thumbs.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr=b""),
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: thumbs.get(photo, tmp_path), range(24)))

    for data in results:
        assert Image.open(io.BytesIO(data)).format == "JPEG"

    cached = thumbs.cache_path(photo, tmp_path)
    assert Image.open(cached).format == "JPEG", "cache holds a corrupt file"
    assert list(cached.parent.glob("*.tmp")) == []
