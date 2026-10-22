"""Tests for card detection and ingest.

The card-safety tests matter most: the card holds the only copy of a shoot
until the import is verified, so nothing here may modify or remove it.
"""

import json
from pathlib import Path

import pytest

import ingest
from ingest import copy_from_card, find_raws, ingest_card, is_camera_card


@pytest.fixture
def card(tmp_path):
    """A DCF-structured card with three raws and some camera clutter."""
    root = tmp_path / "EOS_DIGITAL"
    folder = root / "DCIM" / "100EOS5D"
    folder.mkdir(parents=True)
    for i, name in enumerate(["IMG_0001.CR2", "IMG_0002.CR2", "IMG_0003.CR2"]):
        (folder / name).write_bytes(b"raw-" + bytes([i]) * 128)
    (folder / "IMG_0001.JPG").write_bytes(b"jpeg")
    (root / "MISC").mkdir()
    return root


@pytest.fixture
def no_convert(monkeypatch):
    """Skip the DNG conversion so ingest tests cover copying, not Adobe."""
    monkeypatch.setattr(ingest, "_convert_with_style",
                        lambda raw, model, verbose, reconvert=False: None)


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def test_recognises_a_card_by_its_dcim_folder(card):
    assert is_camera_card(card)


def test_a_plain_folder_is_not_a_card(tmp_path):
    (tmp_path / "photos").mkdir()
    assert not is_camera_card(tmp_path / "photos")


def test_a_hard_drive_of_photos_is_not_a_card(tmp_path):
    """Matching on DCIM, not on content, keeps a photo archive from being ingested."""
    drive = tmp_path / "external_hardrive"
    (drive / "photos" / "polo 2025").mkdir(parents=True)
    (drive / "photos" / "polo 2025" / "IMG_9.CR2").write_bytes(b"raw")
    assert not is_camera_card(drive)


def test_finds_raws_and_ignores_jpegs(card):
    names = [p.name for p in find_raws(card)]
    assert names == ["IMG_0001.CR2", "IMG_0002.CR2", "IMG_0003.CR2"]


def test_finds_raws_from_other_camera_brands(tmp_path):
    root = tmp_path / "CARD"
    (root / "DCIM" / "100").mkdir(parents=True)
    for name in ("a.NEF", "b.ARW", "c.RAF", "d.cr3"):
        (root / "DCIM" / "100" / name).write_bytes(b"raw")
    assert len(find_raws(root)) == 4


def test_empty_card_yields_nothing(tmp_path):
    root = tmp_path / "CARD"
    (root / "DCIM").mkdir(parents=True)
    assert find_raws(root) == []


# ---------------------------------------------------------------------------
# The card is read-only
# ---------------------------------------------------------------------------

def test_ingest_never_modifies_the_card(card, tmp_path, fake_exiftool, no_convert):
    before = snapshot(card)
    ingest_card(card, tmp_path / "lib", date_folders=False)
    assert snapshot(card) == before, "the card was modified during ingest"


def test_ingest_never_deletes_from_the_card(card, tmp_path, fake_exiftool, no_convert):
    ingest_card(card, tmp_path / "lib", date_folders=False)
    assert len(find_raws(card)) == 3


def test_a_failure_still_leaves_the_card_intact(card, tmp_path, fake_exiftool, monkeypatch):
    before = snapshot(card)
    monkeypatch.setattr(ingest, "_convert_with_style", lambda *a, **k: 1 / 0)

    summary = ingest_card(card, tmp_path / "lib", date_folders=False)

    assert summary["failed"], "expected the conversion failure to be reported"
    assert snapshot(card) == before


# ---------------------------------------------------------------------------
# Copying
# ---------------------------------------------------------------------------

def test_copy_preserves_bytes(card, tmp_path):
    src = find_raws(card)[0]
    dest, copied = copy_from_card(src, tmp_path / "lib", date_folders=False)
    assert copied is True
    assert dest.read_bytes() == src.read_bytes()


def test_copy_is_skipped_when_already_present(card, tmp_path):
    src = find_raws(card)[0]
    lib = tmp_path / "lib"
    copy_from_card(src, lib, date_folders=False)
    _, copied_again = copy_from_card(src, lib, date_folders=False)
    assert copied_again is False


def test_checksum_mismatch_leaves_no_file_behind(card, tmp_path, monkeypatch):
    """A corrupted copy must not survive looking like a successful import."""
    src = find_raws(card)[0]
    values = iter(["aaa", "bbb"])
    monkeypatch.setattr(ingest, "_sha256", lambda p, chunk=1 << 20: next(values))

    lib = tmp_path / "lib"
    with pytest.raises(RuntimeError, match="Checksum mismatch"):
        copy_from_card(src, lib, date_folders=False)

    assert not (lib / src.name).exists()
    assert list(lib.glob("*.partial")) == []


def test_partial_files_are_not_left_on_success(card, tmp_path):
    copy_from_card(find_raws(card)[0], tmp_path / "lib", date_folders=False)
    assert list((tmp_path / "lib").glob("*.partial")) == []


def test_copy_sorts_into_date_folders(card, tmp_path, fake_exiftool):
    fake_exiftool.stdout_lines = ["2025:09:06 15:08:53"]
    dest, _ = copy_from_card(find_raws(card)[0], tmp_path / "lib", date_folders=True)
    assert dest.parent == tmp_path / "lib" / "2025" / "09" / "06"


def test_copy_falls_back_when_the_date_is_missing(card, tmp_path, fake_exiftool):
    fake_exiftool.stdout_lines = ["-"]
    dest, _ = copy_from_card(find_raws(card)[0], tmp_path / "lib", date_folders=True)
    assert dest.parent == tmp_path / "lib"


# ---------------------------------------------------------------------------
# Whole run
# ---------------------------------------------------------------------------

def test_ingest_reports_what_it_did(card, tmp_path, fake_exiftool, no_convert):
    summary = ingest_card(card, tmp_path / "lib", date_folders=False)
    assert summary["found"] == 3
    assert summary["copied"] == 3
    assert summary["failed"] == []


def test_rerunning_an_ingest_copies_nothing_new(card, tmp_path, fake_exiftool, no_convert):
    lib = tmp_path / "lib"
    ingest_card(card, lib, date_folders=False)
    second = ingest_card(card, lib, date_folders=False)

    assert second["copied"] == 0
    assert second["already_present"] == 3


def test_one_bad_file_does_not_end_the_import(card, tmp_path, fake_exiftool, monkeypatch):
    real_copy = ingest.copy_from_card_ex

    def flaky(source, library, date_folders=True, verify=True):
        if source.name == "IMG_0002.CR2":
            raise OSError("I/O error reading card")
        return real_copy(source, library, date_folders, verify)

    monkeypatch.setattr(ingest, "copy_from_card_ex", flaky)
    monkeypatch.setattr(ingest, "_convert_with_style", lambda *a, **k: None)

    summary = ingest_card(card, tmp_path / "lib", date_folders=False)

    assert summary["copied"] == 2
    assert len(summary["failed"]) == 1
    assert "IMG_0002" in summary["failed"][0]["file"]


def test_ingest_does_not_upload(card, tmp_path, fake_exiftool, no_convert, monkeypatch):
    """Uploading is an explicit step; ingest must never publish on its own."""
    import uploader

    def forbidden(*a, **k):
        pytest.fail("ingest must not upload")

    monkeypatch.setattr(uploader, "upload_to_google_photos", forbidden)
    monkeypatch.setattr(uploader, "upload_to_amazon_photos", forbidden)

    ingest_card(card, tmp_path / "lib", date_folders=False)


def test_a_manifest_records_the_run(card, tmp_path, fake_exiftool, no_convert):
    lib = tmp_path / "lib"
    ingest_card(card, lib, date_folders=False)

    entries = [
        json.loads(line)
        for line in (lib / ".photo-room-ingests.jsonl").read_text().splitlines()
    ]
    assert entries[-1]["found"] == 3
    assert entries[-1]["copied"] == 3


def test_empty_card_is_handled_without_error(tmp_path, fake_exiftool):
    root = tmp_path / "CARD"
    (root / "DCIM").mkdir(parents=True)
    summary = ingest_card(root, tmp_path / "lib")
    assert summary["found"] == 0
    assert summary["failed"] == []


# ---------------------------------------------------------------------------
# Re-developing a shoot
# ---------------------------------------------------------------------------

def test_an_existing_dng_is_skipped_by_default(card, tmp_path, fake_exiftool, monkeypatch):
    converted = []
    monkeypatch.setattr(
        ingest, "_convert_with_style",
        lambda raw, model, verbose, reconvert=False: (
            converted.append(raw.name) or raw.with_suffix(".dng")
        ),
    )
    lib = tmp_path / "lib"
    ingest_card(card, lib, date_folders=False)

    # The DNGs now exist, so a second run reports them as already converted.
    for name in ("IMG_0001", "IMG_0002", "IMG_0003"):
        (lib / f"{name}.dng").write_bytes(b"dng")

    second = ingest_card(card, lib, date_folders=False)
    assert second["converted"] == 0
    assert second["already_converted"] == 3


def test_no_skip_develops_them_again(card, tmp_path, fake_exiftool, monkeypatch):
    """
    The route for re-developing a shoot once the learned style has improved.
    convert has had --no-skip from the start; ingest had no equivalent.
    """
    monkeypatch.setattr(
        ingest, "_convert_with_style",
        lambda raw, model, verbose, reconvert=False: raw.with_suffix(".dng"),
    )
    lib = tmp_path / "lib"
    ingest_card(card, lib, date_folders=False)
    for name in ("IMG_0001", "IMG_0002", "IMG_0003"):
        (lib / f"{name}.dng").write_bytes(b"dng")

    second = ingest_card(card, lib, date_folders=False, reconvert=True)
    assert second["converted"] == 3
    assert second["already_converted"] == 0


def test_reconvert_replaces_the_existing_dng(tmp_path, fake_exiftool, fake_converter, monkeypatch):
    """A stale DNG left in place would make the converter skip it silently."""
    raw = tmp_path / "IMG_0001.CR2"
    raw.write_bytes(b"raw")
    stale = tmp_path / "IMG_0001.dng"
    stale.write_bytes(b"stale")

    import numpy as np
    monkeypatch.setattr(ingest, "decode_for_analysis",
                        lambda p: (np.full((32, 48, 3), 120, np.uint8), 5000, np.ones(4)))
    monkeypatch.setattr(ingest, "convert_cr2_to_dng",
                        lambda p, out: (stale.write_bytes(b"fresh"), stale)[1])
    monkeypatch.setattr(ingest, "embed_xmp_adjustments", lambda *a, **k: None)

    ingest._convert_with_style(raw, None, False, reconvert=True)
    assert stale.read_bytes() == b"fresh"


def test_the_style_is_reported_only_when_it_reached_a_photograph(card, tmp_path, fake_exiftool, monkeypatch):
    """
    Re-running an ingest that converts nothing had reported "[learned style
    applied]" while applying it to nothing at all.
    """
    class Trained:
        is_trained = True
        n_samples = 30
        confidence = 0.7

    monkeypatch.setattr(ingest.style, "load_model", lambda *a, **k: Trained())
    monkeypatch.setattr(
        ingest, "_convert_with_style",
        lambda raw, model, verbose, reconvert=False: raw.with_suffix(".dng"),
    )

    lib = tmp_path / "lib"
    first = ingest_card(card, lib, date_folders=False)
    assert first["styled"] is True

    for name in ("IMG_0001", "IMG_0002", "IMG_0003"):
        (lib / f"{name}.dng").write_bytes(b"dng")

    second = ingest_card(card, lib, date_folders=False)
    assert second["converted"] == 0
    assert second["styled"] is False, "claimed a style it applied to nothing"


# ---------------------------------------------------------------------------
# Name clashes must never lose or overwrite an original
# ---------------------------------------------------------------------------

def _two_card_clash(tmp_path, same_size=True):
    card = tmp_path / "card"
    a = card / "DCIM" / "100CANON"
    b = card / "DCIM" / "101CANON"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    (a / "IMG_0001.CR2").write_bytes(b"A" * 5000)
    (b / "IMG_0001.CR2").write_bytes(b"B" * 5000)
    (a / "IMG_0002.CR2").write_bytes(b"C" * 6000)
    (b / "IMG_0002.CR2").write_bytes(b"D" * (6000 if same_size else 7000))
    return card


@pytest.mark.parametrize("same_size", [True, False])
@pytest.mark.parametrize("jobs", [1, 4])
def test_colliding_names_are_all_kept(tmp_path, fake_exiftool, no_convert, same_size, jobs):
    card = _two_card_clash(tmp_path, same_size)
    lib = tmp_path / "lib"
    summary = ingest_card(card, lib, date_folders=False, jobs=jobs)

    contents = sorted(p.read_bytes() for p in lib.glob("*.CR2"))
    assert len(contents) == 4
    assert sorted(contents) == sorted(
        p.read_bytes() for p in card.rglob("*.CR2"))
    assert summary["copied"] + len(summary["renamed"]) == 4
    assert len(summary["renamed"]) == 2
    assert summary["already_present"] == 0
    assert summary["failed"] == []
    assert list(lib.glob("*.partial")) == [] and list(lib.glob(".*.partial")) == []


def test_rerun_after_a_clash_copies_nothing(tmp_path, fake_exiftool, no_convert):
    card = _two_card_clash(tmp_path)
    lib = tmp_path / "lib"
    ingest_card(card, lib, date_folders=False)
    second = ingest_card(card, lib, date_folders=False)
    assert second["copied"] == 0 and second["renamed"] == []
    assert second["already_present"] == 4
    assert len(list(lib.glob("*.CR2"))) == 4


def test_existing_library_file_is_never_overwritten(tmp_path):
    src = tmp_path / "IMG_0001.CR2"
    src.write_bytes(b"new" * 100)
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "IMG_0001.CR2").write_bytes(b"old" * 100)  # same size, other bytes
    dest, status = ingest.copy_from_card_ex(src, lib, date_folders=False)
    assert status == "renamed" and dest.name == "IMG_0001-1.CR2"
    assert (lib / "IMG_0001.CR2").read_bytes() == b"old" * 100
    assert dest.read_bytes() == b"new" * 100


def test_summary_line_lists_all_four_counts(tmp_path, fake_exiftool, no_convert, capsys):
    summary = ingest_card(_two_card_clash(tmp_path), tmp_path / "lib", date_folders=False)
    ingest._report(summary)
    out = capsys.readouterr().out
    assert "copied" in out and "skipped as identical" in out
    assert "renamed" in out and "failed" in out


def test_volume_roots_per_platform():
    assert ingest.volume_roots("darwin") == [Path("/Volumes")]
    assert ingest.volume_roots("linux", user="ann") == [
        Path("/media/ann"), Path("/run/media/ann"), Path("/mnt")]


def test_list_volumes_and_find_cards_scan_linux_roots(tmp_path):
    media, run, mnt = tmp_path / "media/ann", tmp_path / "run/ann", tmp_path / "mnt"
    for d in (media / "EOS" / "DCIM", run / "SONY" / "DCIM", mnt / "CARD" / "DCIM",
              mnt / "plain", media / ".hidden" / "DCIM"):
        d.mkdir(parents=True)
    (mnt / "file.txt").write_text("x")
    roots = [media, run, mnt, tmp_path / "missing"]
    names = [v.name for v in ingest.list_volumes(roots)]
    assert names == ["EOS", "SONY", "CARD", "plain"]
    assert [c.name for c in ingest.find_cards(roots)] == ["EOS", "SONY", "CARD"]


def test_list_volumes_default_uses_volume_roots(tmp_path, monkeypatch):
    (tmp_path / "V" / "DCIM").mkdir(parents=True)
    monkeypatch.setattr(ingest, "volume_roots", lambda *a, **k: [tmp_path])
    assert [c.name for c in ingest.find_cards()] == ["V"]
