"""The on-disk decode cache: hits skip the decode, and nothing stale can ever be served."""
import numpy as np
import pytest

import decode_cache


@pytest.fixture(autouse=True)
def cache_on(monkeypatch, tmp_path):
    monkeypatch.setenv("PHOTO_ROOM_DECODE_CACHE", "1")
    monkeypatch.setenv("PHOTO_ROOM_CACHE_DIR", str(tmp_path / "cache"))


P = {"wb": "camera", "auto_bright": False, "bps": 8, "space": "srgb", "half": False}


def raw_file(tmp_path, data=b"raw-bytes-1", name="a.CR2"):
    f = tmp_path / name
    f.write_bytes(data)
    return f


def counting(value):
    calls = []

    def decode():
        calls.append(1)
        return np.full((4, 5, 3), value, dtype=np.uint8)
    return decode, calls


def test_second_read_skips_the_decode(tmp_path):
    f = raw_file(tmp_path)
    decode, calls = counting(7)
    a, hit1 = decode_cache.get_or_decode(f, P, decode)
    b, hit2 = decode_cache.get_or_decode(f, P, decode)
    assert (hit1, hit2) == (False, True) and len(calls) == 1
    assert np.array_equal(a, b)


@pytest.mark.parametrize("change", [{"bps": 16}, {"space": "adobergb"}, {"half": True},
                                    {"wb": "daylight"}, {"auto_bright": True}])
def test_every_decode_parameter_is_part_of_the_key(tmp_path, change):
    f = raw_file(tmp_path)
    assert decode_cache.cache_key(f, P) != decode_cache.cache_key(f, {**P, **change})


def test_changed_file_content_misses_even_with_same_name_and_size(tmp_path):
    f = raw_file(tmp_path, b"AAAA")
    k1 = decode_cache.cache_key(f, P)
    f.write_bytes(b"AAAB")
    assert decode_cache.cache_key(f, P) != k1


def test_same_content_other_path_shares_the_entry(tmp_path):
    a, b = raw_file(tmp_path, name="a.CR2"), raw_file(tmp_path, name="b.CR2")
    assert decode_cache.cache_key(a, P) == decode_cache.cache_key(b, P)


def test_disabled_never_touches_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("PHOTO_ROOM_DECODE_CACHE", "0")
    decode, calls = counting(1)
    decode_cache.get_or_decode(raw_file(tmp_path), P, decode)
    decode_cache.get_or_decode(raw_file(tmp_path), P, decode)
    assert len(calls) == 2 and not (tmp_path / "cache").exists()


def test_corrupt_entry_is_replaced(tmp_path):
    f = raw_file(tmp_path)
    decode, calls = counting(3)
    decode_cache.get_or_decode(f, P, decode)
    entry = next((tmp_path / "cache" / "decode").glob("*.npy"))
    entry.write_bytes(b"not an npy file")
    arr, hit = decode_cache.get_or_decode(f, P, decode)
    assert not hit and arr.max() == 3 and len(calls) == 2


def test_size_cap_evicts_older_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("PHOTO_ROOM_DECODE_CACHE_MB", "0.0002")        # ~200 bytes: room for one entry
    files = [raw_file(tmp_path, bytes([i]) * 10, f"{i}.CR2") for i in range(4)]
    for f in files:
        decode_cache.get_or_decode(f, P, counting(1)[0])
    left = {p.name for p in (tmp_path / "cache" / "decode").glob("*.npy")}
    assert left == {decode_cache.cache_key(files[-1], P) + ".npy"}


def test_export_uses_the_cache_and_output_is_identical(tmp_path, monkeypatch, fake_rawpy):
    """Through the real exporter: second export reads the cached decode and produces the same pixels."""
    import exporter
    from PIL import Image
    raw = tmp_path / "IMG_0001.CR2"
    raw.write_bytes(b"fake raw")
    monkeypatch.setattr(exporter, "_saved_adjustments", lambda p, *a, **k: {"contrast": 20})
    monkeypatch.setattr(exporter, "exiftool_available", lambda: False, raising=False)
    opts = exporter.ExportOptions(format="png", keep_metadata=False)
    first = exporter.export_photo(raw, tmp_path / "o1", opts)
    entries = list((tmp_path / "cache" / "decode").glob("*.npy"))
    assert len(entries) == 1
    second = exporter.export_photo(raw, tmp_path / "o2", opts)
    assert np.array_equal(np.asarray(Image.open(first)), np.asarray(Image.open(second)))
