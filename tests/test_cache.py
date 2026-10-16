"""
Tests for the preview caches.

Entries are large — about 16MB for a preview and 23MB for the decode behind
it. Unbounded, browsing a 431-frame shoot would have held 16GB; measured at
1.3GB after only 40 photos before this was bounded.
"""

from pathlib import Path

import pytest

import editor


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    for i in range(20):
        (tmp_path / f"IMG_{i:03d}.dng").write_bytes(b"dng")
    return tmp_path


def open_photo(library, i):
    editor._load_base_render(library / f"IMG_{i:03d}.dng")


def test_cache_never_exceeds_its_limit(library, fake_rawpy):
    for i in range(20):
        open_photo(library, i)

    stats = editor.cache_stats()
    assert stats["previews"] <= editor.MAX_CACHED_PHOTOS
    assert stats["decodes"] <= editor.MAX_CACHED_PHOTOS


def test_both_caches_stay_in_step(library, fake_rawpy):
    """A decode left behind after its preview is evicted would leak the larger half."""
    for i in range(15):
        open_photo(library, i)

    stats = editor.cache_stats()
    assert stats["decodes"] <= stats["previews"] + 1


def test_the_most_recent_photo_is_kept(library, fake_rawpy, monkeypatch):
    for i in range(12):
        open_photo(library, i)

    calls = {"n": 0}
    real = editor.decode_for_analysis
    monkeypatch.setattr(editor, "decode_for_analysis",
                        lambda p: (calls.__setitem__("n", calls["n"] + 1), real(p))[1])

    open_photo(library, 11)          # the one just viewed
    assert calls["n"] == 0, "the current photo was evicted"


def test_the_oldest_photo_is_the_one_dropped(library, fake_rawpy, monkeypatch):
    for i in range(editor.MAX_CACHED_PHOTOS + 3):
        open_photo(library, i)

    calls = {"n": 0}
    real = editor.decode_for_analysis
    monkeypatch.setattr(editor, "decode_for_analysis",
                        lambda p: (calls.__setitem__("n", calls["n"] + 1), real(p))[1])

    open_photo(library, 0)           # the first one opened, long since evicted
    assert calls["n"] == 1


def test_revisiting_a_photo_renews_it(library, fake_rawpy, monkeypatch):
    """
    Arrowing back and forth between two frames must not evict either of
    them just because others were opened in between.
    """
    open_photo(library, 0)
    for i in range(1, editor.MAX_CACHED_PHOTOS):
        open_photo(library, i)
        open_photo(library, 0)       # keep touching the first

    calls = {"n": 0}
    real = editor.decode_for_analysis
    monkeypatch.setattr(editor, "decode_for_analysis",
                        lambda p: (calls.__setitem__("n", calls["n"] + 1), real(p))[1])

    open_photo(library, 0)
    assert calls["n"] == 0, "a repeatedly-viewed photo was evicted"


def test_a_cached_photo_is_not_decoded_again(library, fake_rawpy, monkeypatch):
    open_photo(library, 0)

    def forbidden(path):
        pytest.fail("re-decoded a photo that was still cached")

    monkeypatch.setattr(editor, "decode_for_analysis", forbidden)
    open_photo(library, 0)


def test_cache_stats_report_the_limit():
    assert editor.cache_stats()["limit"] == editor.MAX_CACHED_PHOTOS


def test_the_limit_is_a_sane_size():
    """Small enough to bound memory, large enough for arrow-key browsing."""
    assert 4 <= editor.MAX_CACHED_PHOTOS <= 24
