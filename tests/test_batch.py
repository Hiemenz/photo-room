"""Tests for applying one edit across many files."""

from pathlib import Path

import pytest

import editor


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    for name in ("a.dng", "b.dng", "c.dng"):
        (tmp_path / name).write_bytes(b"dng")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "d.dng").write_bytes(b"dng")
    return tmp_path


@pytest.fixture
def client(library):
    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        yield c


EDIT = {"exposure": 1.0, "contrast": 30, "shadows": 20, "strength": 100}


def test_applies_to_every_selected_file(client, fake_rawpy, fake_exiftool):
    res = client.post("/api/batch_apply", json={
        "paths": ["a.dng", "b.dng", "sub/d.dng"], "adjustments": EDIT,
    })
    data = res.get_json()

    assert res.status_code == 200
    assert data["ok"] is True
    assert sorted(data["applied"]) == ["a.dng", "b.dng", "sub/d.dng"]
    assert data["failed"] == []


def test_writes_the_adjustments_to_each_file(client, fake_rawpy, fake_exiftool):
    client.post("/api/batch_apply", json={"paths": ["a.dng", "b.dng"], "adjustments": EDIT})

    writes = [c for c in fake_exiftool.calls if any("Exposure2012" in a for a in c)]
    assert len(writes) == 2
    for cmd in writes:
        joined = " ".join(cmd)
        assert "Exposure2012=1.00" in joined
        assert "Contrast2012=30" in joined


def test_unedited_files_are_left_alone(client, library, fake_rawpy, fake_exiftool):
    """Only the selected files may be touched."""
    client.post("/api/batch_apply", json={"paths": ["a.dng"], "adjustments": EDIT})

    written = [c[-1] for c in fake_exiftool.calls if any("Exposure2012" in a for a in c)]
    assert all("c.dng" not in p for p in written)


def test_empty_selection_is_rejected(client):
    res = client.post("/api/batch_apply", json={"paths": [], "adjustments": EDIT})
    assert res.status_code == 400
    assert "No files selected" in res.get_json()["error"]


def test_paths_outside_the_library_are_refused(client, fake_rawpy, fake_exiftool):
    res = client.post("/api/batch_apply", json={
        "paths": ["a.dng", "../../../etc/passwd"], "adjustments": EDIT,
    })
    data = res.get_json()

    assert data["applied"] == ["a.dng"]
    assert len(data["failed"]) == 1
    assert "outside" in data["failed"][0]["error"].lower()


def test_one_failure_does_not_abandon_the_rest(client, fake_rawpy, fake_exiftool, monkeypatch):
    """A single unwritable photo must not cost the whole shoot."""
    real_save = editor.save_adjustments

    def flaky(path, adj):
        if path.name == "b.dng":
            raise RuntimeError("file is read-only")
        return real_save(path, adj)

    monkeypatch.setattr(editor, "save_adjustments", flaky)

    data = client.post("/api/batch_apply", json={
        "paths": ["a.dng", "b.dng", "c.dng"], "adjustments": EDIT,
    }).get_json()

    assert sorted(data["applied"]) == ["a.dng", "c.dng"]
    assert data["ok"] is False
    assert data["failed"][0]["path"] == "b.dng"
    assert "read-only" in data["failed"][0]["error"]


def test_missing_file_is_reported_not_raised(client, fake_rawpy, fake_exiftool):
    data = client.post("/api/batch_apply", json={
        "paths": ["a.dng", "ghost.dng"], "adjustments": EDIT,
    }).get_json()

    assert data["applied"] == ["a.dng"]
    assert "No such file" in data["failed"][0]["error"]


def test_temperature_is_reanchored_to_each_target(client, library, fake_rawpy, fake_exiftool, monkeypatch):
    """
    Kelvin is camera-relative, so the source's warmth travels as a ratio and
    is re-applied against each target's own white balance.

    This previously set a _baseline_temp key and relied on the strength blend
    to use it — a no-op at the default strength of 100, so every target got
    the source's absolute Kelvin verbatim.
    """
    seen = {}
    real_save = editor.save_adjustments

    def capture(path, adj):
        seen[path.name] = adj.get("color_temp")
        return real_save(path, adj)

    monkeypatch.setattr(editor, "save_adjustments", capture)
    # Each target reports a different camera white balance.
    monkeypatch.setattr(editor, "_baseline_temp_of",
                        lambda p: 4000 if p.name == "a.dng" else 6000)

    client.post("/api/batch_apply", json={
        "paths": ["a.dng", "b.dng"],
        "adjustments": {**EDIT, "color_temp": 6600},
        "baseline_temp": 5500,          # source was set 1.2x warm
    })

    assert seen["a.dng"] == pytest.approx(4800, abs=2)
    assert seen["b.dng"] == pytest.approx(7200, abs=2)


def test_batch_apply_does_not_decode_already_open_files(client, library, fake_rawpy, fake_exiftool, monkeypatch):
    """Re-anchoring must not decode a whole shoot to read one number."""
    editor._decode_cache[str((library / "a.dng").resolve())] = (None, 4000, None)

    calls = {"n": 0}
    monkeypatch.setattr(editor, "decode_for_analysis",
                        lambda p: (calls.__setitem__("n", calls["n"] + 1), (None, 5000, None))[1])

    client.post("/api/batch_apply", json={
        "paths": ["a.dng"], "adjustments": {**EDIT, "color_temp": 6000},
        "baseline_temp": 5000,
    })
    assert calls["n"] == 0, "decoded a file whose baseline was already cached"


def test_strength_is_resolved_before_writing(client, fake_rawpy, fake_exiftool):
    """Half strength must write halved values, as it does for a single save."""
    client.post("/api/batch_apply", json={
        "paths": ["a.dng"], "adjustments": {"exposure": 2.0, "contrast": 60, "strength": 50},
    })

    cmd = " ".join(fake_exiftool.write_call())
    assert "Exposure2012=1.00" in cmd
    assert "Contrast2012=30" in cmd


def test_message_counts_successes_and_failures(client, fake_rawpy, fake_exiftool):
    data = client.post("/api/batch_apply", json={
        "paths": ["a.dng", "nope.dng"], "adjustments": EDIT,
    }).get_json()
    assert "Applied to 1 file(s)" in data["message"]
    assert "1 failed" in data["message"]


def test_duplicate_paths_are_applied_once(client, fake_rawpy, fake_exiftool):
    """A repeated path must not be written twice or inflate the count."""
    data = client.post("/api/batch_apply", json={
        "paths": ["a.dng", "b.dng", "a.dng"], "adjustments": EDIT,
    }).get_json()

    assert data["applied"] == ["a.dng", "b.dng"]
    writes = [c for c in fake_exiftool.calls if any("Exposure2012" in a for a in c)]
    assert len(writes) == 2


# ---------------------------------------------------------------------------
# Parallel conversion
# ---------------------------------------------------------------------------

def test_parallel_and_serial_convert_the_same_files(tmp_path, fake_rawpy, fake_exiftool, monkeypatch):
    """
    Threading must not change which files are handled or what is written —
    only how long it takes.
    """
    import argparse

    import process

    sources = []
    for i in range(7):
        p = tmp_path / f"IMG_{i}.CR2"
        p.write_bytes(b"raw")
        sources.append(p)

    handled: list[str] = []

    def fake_process(cr2, output, date_folders, skip_existing, verbose, style_model=None):
        handled.append(cr2.name)
        return Path(output or cr2.parent) / (cr2.stem + ".dng")

    monkeypatch.setattr(process, "process_file", fake_process)

    def run(jobs):
        handled.clear()
        args = argparse.Namespace(
            inputs=[str(tmp_path)], output=str(tmp_path / "out"),
            date_folders=False, skip_existing=False, verbose=False,
            jobs=jobs, upload_google=False, upload_amazon=False,
        )
        process.cmd_convert(args)
        return sorted(handled)

    assert run(1) == run(4) == sorted(p.name for p in sources)


def test_a_failure_in_one_worker_does_not_stop_the_others(tmp_path, fake_rawpy, fake_exiftool, monkeypatch, capsys):
    import argparse

    import process

    for i in range(6):
        (tmp_path / f"IMG_{i}.CR2").write_bytes(b"raw")

    def flaky(cr2, output, date_folders, skip_existing, verbose, style_model=None):
        if cr2.name == "IMG_3.CR2":
            raise RuntimeError("unreadable frame")
        return Path(output) / (cr2.stem + ".dng")

    monkeypatch.setattr(process, "process_file", flaky)

    args = argparse.Namespace(
        inputs=[str(tmp_path)], output=str(tmp_path / "out"),
        date_folders=False, skip_existing=False, verbose=False,
        jobs=4, upload_google=False, upload_amazon=False,
    )
    process.cmd_convert(args)

    out = capsys.readouterr().out
    assert "Converted 5 file(s)" in out
    assert "1 failed" in out


def test_default_job_count_is_sane():
    import process

    assert 1 <= process.DEFAULT_JOBS <= 12
