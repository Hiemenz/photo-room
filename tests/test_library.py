"""
Tests for listing a library.

After conversion a folder holds both IMG_0001.CR2 and IMG_0001.dng. They
are one photograph, and listing both made every frame appear twice — a
431-frame shoot looked like 862.
"""

from pathlib import Path

import pytest

from process import list_photos


@pytest.fixture
def library(tmp_path):
    day = tmp_path / "2025" / "09" / "06"
    day.mkdir(parents=True)
    for stem in ("IMG_0001", "IMG_0002", "IMG_0003"):
        (day / f"{stem}.CR2").write_bytes(b"raw")
        (day / f"{stem}.dng").write_bytes(b"dng")
    return tmp_path


def test_a_converted_photo_is_listed_once(library):
    assert len(list_photos(library)) == 3


def test_the_dng_is_the_one_listed(library):
    """The DNG carries the edits and embeds the original anyway."""
    assert {p.suffix for p in list_photos(library)} == {".dng"}


def test_an_unconverted_raw_is_still_listed(tmp_path):
    (tmp_path / "IMG_9.CR2").write_bytes(b"raw")
    listed = list_photos(tmp_path)
    assert [p.name for p in listed] == ["IMG_9.CR2"]


def test_a_mixed_library_lists_each_photo_once(tmp_path):
    (tmp_path / "converted.CR2").write_bytes(b"raw")
    (tmp_path / "converted.dng").write_bytes(b"dng")
    (tmp_path / "untouched.CR2").write_bytes(b"raw")

    listed = sorted(p.name for p in list_photos(tmp_path))
    assert listed == ["converted.dng", "untouched.CR2"]


def test_listing_order_is_stable(library):
    assert list_photos(library) == list_photos(library)
    assert list_photos(library) == sorted(list_photos(library))


def test_same_stem_in_different_folders_are_different_photos(tmp_path):
    """Two shoots can both contain IMG_0001."""
    for day in ("a", "b"):
        d = tmp_path / day
        d.mkdir()
        (d / "IMG_0001.dng").write_bytes(b"dng")

    assert len(list_photos(tmp_path)) == 2


def test_the_thumbnail_cache_is_not_a_photo(tmp_path):
    (tmp_path / "real.dng").write_bytes(b"dng")
    cache = tmp_path / ".photo-room-thumbs"
    cache.mkdir()
    (cache / "cached.dng").write_bytes(b"x")

    assert [p.name for p in list_photos(tmp_path)] == ["real.dng"]


def test_non_photos_are_ignored(tmp_path):
    (tmp_path / "photo.dng").write_bytes(b"dng")
    (tmp_path / "notes.txt").write_text("hi")
    (tmp_path / "export.jpg").write_bytes(b"jpeg")

    assert [p.name for p in list_photos(tmp_path)] == ["photo.dng"]


def test_uppercase_extensions_are_found(tmp_path):
    (tmp_path / "A.DNG").write_bytes(b"dng")
    (tmp_path / "B.CR2").write_bytes(b"raw")
    assert len(list_photos(tmp_path)) == 2


def test_an_empty_library_lists_nothing(tmp_path):
    assert list_photos(tmp_path) == []


def test_case_variant_globs_do_not_duplicate(tmp_path):
    """On a case-insensitive filesystem *.dng and *.DNG match the same file."""
    (tmp_path / "x.dng").write_bytes(b"dng")
    listed = list_photos(tmp_path)
    assert len(listed) == len(set(listed)) == 1
