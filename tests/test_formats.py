"""
Tests that every command agrees on what counts as a photograph.

Before these, ingest accepted eight raw formats while convert and watch
accepted only CR2. Pointing convert at a folder holding a second body's
files converted the CR2s and said nothing at all about the rest.
"""

import argparse
from pathlib import Path

import pytest

import ingest
import process
from process import RAW_SUFFIXES, list_photos


def test_ingest_and_convert_agree_on_formats():
    """A narrower list in one command silently skips files in that command."""
    assert set(ingest.RAW_SUFFIXES) == set(RAW_SUFFIXES)


def test_every_suffix_has_an_upper_and_lower_pattern():
    """Cameras write IMG_0001.CR2; some tools write .cr2."""
    for suffix in RAW_SUFFIXES:
        assert f"**/*{suffix}" in process.RAW_PATTERNS
        assert f"**/*{suffix.upper()}" in process.RAW_PATTERNS


@pytest.mark.parametrize("suffix", [".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2"])
def test_each_raw_format_is_listed(tmp_path, suffix):
    (tmp_path / f"IMG_0001{suffix}").write_bytes(b"raw")
    assert len(list_photos(tmp_path)) == 1


@pytest.mark.parametrize("suffix", [".cr3", ".nef", ".arw"])
def test_convert_accepts_formats_other_than_cr2(tmp_path, suffix, monkeypatch):
    """The DNG Converter handles all of them; only the glob was narrow."""
    (tmp_path / f"IMG_0001{suffix}").write_bytes(b"raw")

    handled = []
    monkeypatch.setattr(
        process, "process_file",
        lambda p, out, *a, **k: handled.append(p.name) or (tmp_path / "x.dng"),
    )

    process.cmd_convert(argparse.Namespace(
        inputs=[str(tmp_path)], output=str(tmp_path / "out"),
        date_folders=False, skip_existing=False, verbose=False, jobs=1,
        upload_google=False, upload_amazon=False,
    ))
    assert handled == [f"IMG_0001{suffix}"]


def test_convert_handles_a_mixed_shoot(tmp_path, monkeypatch, capsys):
    """Two bodies at one event is ordinary, not exotic."""
    for name in ("canon.CR2", "canon_r5.CR3", "nikon.NEF"):
        (tmp_path / name).write_bytes(b"raw")

    monkeypatch.setattr(
        process, "process_file",
        lambda p, out, *a, **k: tmp_path / (p.stem + ".dng"),
    )

    process.cmd_convert(argparse.Namespace(
        inputs=[str(tmp_path)], output=str(tmp_path / "out"),
        date_folders=False, skip_existing=False, verbose=False, jobs=1,
        upload_google=False, upload_amazon=False,
    ))
    assert "Converted 3 file(s)" in capsys.readouterr().out


def test_convert_skips_already_converted_dngs(tmp_path, monkeypatch):
    """Converting a DNG to a DNG is not what pointing at a folder means."""
    (tmp_path / "raw.CR2").write_bytes(b"raw")
    (tmp_path / "done.dng").write_bytes(b"dng")

    handled = []
    monkeypatch.setattr(
        process, "process_file",
        lambda p, out, *a, **k: handled.append(p.name) or (tmp_path / "x.dng"),
    )

    process.cmd_convert(argparse.Namespace(
        inputs=[str(tmp_path)], output=str(tmp_path / "out"),
        date_folders=False, skip_existing=False, verbose=False, jobs=1,
        upload_google=False, upload_amazon=False,
    ))
    assert handled == ["raw.CR2"]


def test_a_non_photograph_is_reported_not_silently_dropped(tmp_path, capsys):
    """Naming what was skipped is the whole point."""
    notes = tmp_path / "notes.txt"
    notes.write_text("hi")

    with pytest.raises(SystemExit):
        process.cmd_convert(argparse.Namespace(
            inputs=[str(notes)], output=None, date_folders=False,
            skip_existing=False, verbose=False, jobs=1,
            upload_google=False, upload_amazon=False,
        ))
    assert "Skipping" in capsys.readouterr().err


def test_libraw_is_new_enough_for_cr3():
    """
    CR3 decoding arrived in LibRaw 0.20. Accepting the extension while the
    decoder underneath cannot read it would fail only at run time.
    """
    import rawpy

    assert rawpy.libraw_version[:2] >= (0, 20), (
        f"LibRaw {rawpy.libraw_version} cannot decode CR3, "
        "but .cr3 is an accepted extension"
    )


# ---------------------------------------------------------------------------
# The learned style applies wherever photographs are converted
# ---------------------------------------------------------------------------

def test_convert_passes_the_style_model_through(tmp_path, monkeypatch):
    """
    Ingest applied the learned style and convert did not, so a shoot copied
    off a card came out in the photographer's own style while the same files
    converted by hand came out on the plain heuristic.
    """
    (tmp_path / "IMG_0001.CR2").write_bytes(b"raw")

    sentinel = object()
    monkeypatch.setattr(process, "_load_style_model", lambda verbose=False: sentinel)

    seen = {}

    def capture(cr2, output, date_folders, skip_existing, verbose, style_model=None):
        seen["model"] = style_model
        return tmp_path / "x.dng"

    monkeypatch.setattr(process, "process_file", capture)
    process.cmd_convert(argparse.Namespace(
        inputs=[str(tmp_path)], output=str(tmp_path / "out"),
        date_folders=False, skip_existing=False, verbose=False, jobs=1,
        upload_google=False, upload_amazon=False,
    ))
    assert seen["model"] is sentinel


def test_the_style_model_reaches_parallel_workers(tmp_path, monkeypatch):
    """Threading must not quietly drop it."""
    for i in range(4):
        (tmp_path / f"IMG_{i}.CR2").write_bytes(b"raw")

    sentinel = object()
    monkeypatch.setattr(process, "_load_style_model", lambda verbose=False: sentinel)

    seen = []

    def capture(cr2, output, date_folders, skip_existing, verbose, style_model=None):
        seen.append(style_model)
        return tmp_path / "x.dng"

    monkeypatch.setattr(process, "process_file", capture)
    process.cmd_convert(argparse.Namespace(
        inputs=[str(tmp_path)], output=str(tmp_path / "out"),
        date_folders=False, skip_existing=False, verbose=False, jobs=3,
        upload_google=False, upload_amazon=False,
    ))
    assert seen and all(m is sentinel for m in seen)


def test_an_untrained_model_is_not_passed(monkeypatch):
    """Nothing learned yet means the plain heuristic, not an empty model."""
    import style

    class Untrained:
        is_trained = False

    monkeypatch.setattr(style, "load_model", lambda *a, **k: Untrained())
    assert process._load_style_model() is None


def test_an_unreadable_style_store_does_not_stop_a_conversion(monkeypatch):
    """A lost style is an inconvenience; a lost shoot is not."""
    import style

    def broken(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(style, "load_model", broken)
    assert process._load_style_model() is None
