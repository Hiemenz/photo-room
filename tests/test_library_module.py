"""
Tests for the Library module: flags, colour labels, keywords, filter, sort,
XMP field mapping and the API endpoints.

exiftool is replaced by a small stateful double that keeps per-file XMP in
memory, answers batched ``-j`` reads and applies writes, bumping the file's
mtime the way a real in-place write does.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

import editor
import library
from library import (
    filter_items, sort_items, normalize_keywords, normalize_label, xmp_args, parse_entry,
)


def item(name, rating=0, flag="none", label="", keywords=(), camera="", date=""):
    return {"path": name, "name": name, "rating": rating, "flag": flag,
            "label": label, "keywords": list(keywords), "camera": camera, "date": date}


# ---------------------------------------------------------------- XMP mapping

def test_label_normalised_and_validated():
    assert normalize_label("red") == "Red"
    assert normalize_label("none") == ""
    assert normalize_label(None) == ""
    with pytest.raises(ValueError):
        normalize_label("Mauve")


def test_keywords_deduped_trimmed():
    assert normalize_keywords(["  Sunset ", "sunset", "", "Beach  day"]) == ["Sunset", "Beach day"]
    assert normalize_keywords("a, b ,A") == ["a", "b"]


def test_xmp_field_mapping_is_camera_raw_standard():
    assert xmp_args(label="blue") == ["-XMP-xmp:Label=Blue"]
    assert xmp_args(label="") == ["-XMP-xmp:Label="]
    assert xmp_args(keywords=["a", "b"]) == [
        "-XMP-dc:Subject=a", "-XMP-dc:Subject=b"]
    assert xmp_args(keywords=[]) == ["-XMP-dc:Subject="]
    assert xmp_args() == []


def test_parse_entry_maps_fields():
    m = parse_entry({"Rating": 3, "Label": "Green", "Subject": "solo", "Model": " Canon  EOS 5D ",
                     "DateTimeOriginal": "2024:05:06 07:08:09"})
    assert m == {"rating": 3, "label": "Green", "keywords": ["solo"],
                 "camera": "Canon EOS 5D", "date": "2024-05-06 07:08:09"}
    assert parse_entry({"Label": "Approved", "Subject": [1, "x"]})["label"] == ""
    assert parse_entry({"Subject": [1, "x"]})["keywords"] == ["1", "x"]
    assert parse_entry({})["rating"] == 0


# ------------------------------------------------------------------ filtering

@pytest.fixture
def items():
    return [
        item("a.dng", 5, "pick", "Red", ["Sunset", "beach"], "Canon EOS 5D", "2024-05-01 10:00:00"),
        item("b.dng", 3, "none", "Green", ["beach"], "Canon EOS 5D", "2024-05-02 10:00:00"),
        item("c.dng", -1, "reject", "", [], "Nikon Z6", "2024-06-01 09:00:00"),
        item("d.dng", 0, "none", "", ["city"], "Nikon Z6", ""),
    ]


def names(xs):
    return [x["name"] for x in xs]


def test_filter_rating_ops(items):
    assert names(filter_items(items, {"rating_min": 3})) == ["a.dng", "b.dng"]
    assert names(filter_items(items, {"rating_min": 3, "rating_op": "eq"})) == ["b.dng"]
    assert names(filter_items(items, {"rating_min": 0, "rating_op": "le"})) == ["c.dng", "d.dng"]


def test_filter_flag(items):
    assert names(filter_items(items, {"flag": "pick"})) == ["a.dng"]
    assert names(filter_items(items, {"flag": "reject"})) == ["c.dng"]
    assert names(filter_items(items, {"flag": "unflagged"})) == ["b.dng", "d.dng"]


def test_filter_label(items):
    assert names(filter_items(items, {"label": "red"})) == ["a.dng"]
    assert names(filter_items(items, {"label": "none"})) == ["c.dng", "d.dng"]


def test_filter_keyword_camera_date_text(items):
    assert names(filter_items(items, {"keyword": "BEACH"})) == ["a.dng", "b.dng"]
    assert names(filter_items(items, {"camera": "nikon z6"})) == ["c.dng", "d.dng"]
    assert names(filter_items(items, {"date_from": "2024-05-02", "date_to": "2024-05-31"})) == ["b.dng"]
    assert names(filter_items(items, {"date_from": "2024-01-01"})) == ["a.dng", "b.dng", "c.dng"]
    assert names(filter_items(items, {"q": "canon sunset"})) == ["a.dng"]
    assert names(filter_items(items, {"q": "city"})) == ["d.dng"]


def test_filters_combine_with_and(items):
    assert names(filter_items(items, {"keyword": "beach", "rating_min": 4})) == ["a.dng"]
    assert filter_items(items, {"q": "zzz"}) == []


def test_unknown_label_filter_raises(items):
    with pytest.raises(ValueError):
        filter_items(items, {"label": "Mauve"})


# -------------------------------------------------------------------- sorting

def test_sort_name_rating_flag(items):
    assert names(sort_items(items, "name", True)) == ["d.dng", "c.dng", "b.dng", "a.dng"]
    assert names(sort_items(items, "rating", True)) == ["a.dng", "b.dng", "d.dng", "c.dng"]
    assert names(sort_items(items, "flag", True)) == ["a.dng", "b.dng", "d.dng", "c.dng"]


def test_sort_missing_values_last_in_both_directions(items):
    assert names(sort_items(items, "date"))[-1] == "d.dng"
    assert names(sort_items(items, "date", True))[-1] == "d.dng"
    assert names(sort_items(items, "label"))[:2] == ["a.dng", "b.dng"]
    assert names(sort_items(items, "label", True))[:2] == ["b.dng", "a.dng"]


def test_sort_ties_break_on_path_and_bad_key_raises():
    xs = [item("z", 1), item("a", 1), item("m", 1)]
    assert names(sort_items(xs, "rating")) == ["a", "m", "z"]
    with pytest.raises(ValueError):
        sort_items(xs, "nonsense")


def test_facets(items):
    f = library.facets(items)
    assert f["cameras"] == ["Canon EOS 5D", "Nikon Z6"]
    assert {"keyword": "beach", "count": 2} in f["keywords"]
    assert f["labels"]["Red"] == 1 and f["labels"]["Blue"] == 0


# ----------------------------------------------------------------- flag store

def test_flag_store_crud(tmp_path):
    assert not library.get_pick(tmp_path, "a.dng")
    library.set_picks(tmp_path, ["a.dng", "sub/b.dng"], True)
    assert library.get_pick(tmp_path, "a.dng") and library.get_pick(tmp_path, "sub/b.dng")
    library.set_picks(tmp_path, ["a.dng"], False)
    assert not library.get_pick(tmp_path, "a.dng")
    assert library.get_pick(tmp_path, "sub/b.dng")
    assert json.loads((tmp_path / library.STORE_NAME).read_text())["flags"] == {"sub/b.dng": "pick"}


def test_corrupt_store_reads_as_empty(tmp_path):
    (tmp_path / library.STORE_NAME).write_text("{not json")
    assert library.load_store(tmp_path) == {"flags": {}, "pre_reject": {}}


# ------------------------------------------------------------- API + exiftool

class FakeExiftool:
    def __init__(self, root):
        self.root = root
        self.meta: dict[str, dict] = {}
        self.reads = 0
        self.writes: list[list[str]] = []
        self.clock = 1_700_000_000

    def __call__(self, cmd, **kw):
        args = cmd[1:]
        if "-j" in args:
            self.reads += 1
            files = kw["input"].splitlines()
            out = []
            for f in files:
                m = dict(self.meta.get(Path(f).name, {}))
                out.append({"SourceFile": f, **m})
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(out), stderr="")
        self.writes.append(list(args))
        # exiftool writes every trailing file argument (the writer batches photos with identical changes)
        targets = [Path(a) for a in args if not a.startswith("-")]
        for target in targets:
            m = self.meta.setdefault(target.name, {})
            subject_set = False
            for a in args:
                if a.startswith("-XMP-xmp:Rating="):
                    m["Rating"] = int(a.split("=", 1)[1])
                elif a.startswith("-XMP-xmp:Label="):
                    m["Label"] = a.split("=", 1)[1]
                elif a.startswith("-XMP-dc:Subject="):       # repeated assignments replace the list
                    val = a.split("=", 1)[1]
                    if not subject_set:
                        m["Subject"], subject_set = [], True
                    if val:
                        m["Subject"].append(val)
            self.clock += 10
            os.utime(target, (self.clock, self.clock))
        return subprocess.CompletedProcess(cmd, 0, stdout="1 image files updated", stderr="")


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    monkeypatch.setenv("PHOTO_ROOM_EMBED_DNG", "1")  # these fakes model in-place DNG metadata
    for name in ("a.dng", "b.dng", "c.dng"):
        (tmp_path / name).write_bytes(b"dng")
    tool = FakeExiftool(tmp_path)
    tool.meta = {
        "a.dng": {"Model": "Canon EOS 5D", "DateTimeOriginal": "2024-05-01 10:00:00",
                  "Rating": 4, "Subject": ["beach"]},
        "b.dng": {"Model": "Nikon Z6", "DateTimeOriginal": "2024-05-03 10:00:00"},
        "c.dng": {"Model": "Canon EOS 5D", "DateTimeOriginal": "2024-04-01 10:00:00", "Label": "Red"},
    }
    monkeypatch.setattr(subprocess, "run", tool)
    library.clear_cache()
    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        c.tool = tool
        yield c
    library.clear_cache()


def get(c, **q):
    r = c.get("/api/library", query_string=q)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def post(c, url, body):
    return c.post(url, json=body)


def test_library_lists_everything_with_facets(lib):
    d = get(lib)
    assert d["total"] == d["library_total"] == 3
    assert [i["path"] for i in d["items"]] == ["a.dng", "b.dng", "c.dng"]
    assert d["facets"]["cameras"] == ["Canon EOS 5D", "Nikon Z6"]
    assert d["items"][0]["keywords"] == ["beach"] and d["items"][0]["rating"] == 4


def test_library_filter_sort_paginate(lib):
    d = get(lib, camera="Canon EOS 5D", sort="date", dir="desc")
    assert [i["path"] for i in d["items"]] == ["a.dng", "c.dng"]
    page = get(lib, limit=1, offset=1)
    assert [i["path"] for i in page["items"]] == ["b.dng"]
    assert page["total"] == 3 and "facets" not in page


def test_library_bad_params(lib):
    assert lib.get("/api/library?sort=bogus").status_code == 400
    assert lib.get("/api/library?label=Mauve").status_code == 400
    assert lib.get("/api/library?limit=abc").status_code == 400


def test_metadata_read_once_and_cached(lib):
    get(lib)
    n = lib.tool.reads
    get(lib, q="canon")
    assert lib.tool.reads == n


def test_external_change_is_reread(lib, tmp_path):
    get(lib)
    n = lib.tool.reads
    lib.tool.meta["b.dng"]["Rating"] = 2
    os.utime(tmp_path / "b.dng", (1_800_000_000, 1_800_000_000))
    assert get(lib, q="b.dng")["items"][0]["rating"] == 2
    assert lib.tool.reads == n + 1


def test_flag_pick_persists_and_is_filterable(lib, tmp_path):
    assert post(lib, "/api/flag", {"path": "a.dng", "flag": "pick"}).status_code == 200
    assert library.get_pick(tmp_path, "a.dng")
    assert [i["path"] for i in get(lib, flag="pick")["items"]] == ["a.dng"]
    assert lib.tool.writes == []          # pick has no XMP field: nothing written to the file


def test_flag_reject_is_rating_minus_one_and_exclusive_with_pick(lib):
    post(lib, "/api/flag", {"path": "a.dng", "flag": "pick"})
    post(lib, "/api/flag", {"path": "a.dng", "flag": "reject"})
    assert lib.tool.meta["a.dng"]["Rating"] == -1
    it = get(lib, flag="reject")["items"][0]
    assert it["path"] == "a.dng" and it["flag"] == "reject"
    assert get(lib, flag="pick")["total"] == 0
    post(lib, "/api/flag", {"path": "a.dng", "flag": "pick"})
    assert lib.tool.meta["a.dng"]["Rating"] == 4      # the stars it had before the reject
    assert get(lib, flag="pick")["items"][0]["path"] == "a.dng"
    post(lib, "/api/flag", {"path": "a.dng", "flag": "none"})
    assert get(lib, flag="unflagged")["total"] == 3


def test_unrejecting_restores_the_previous_star_rating(lib):
    assert get(lib, q="a.dng")["items"][0]["rating"] == 4
    for _ in range(2):                                   # X twice by accident
        post(lib, "/api/flag", {"path": "a.dng", "flag": "reject"})
        assert lib.tool.meta["a.dng"]["Rating"] == -1
        post(lib, "/api/flag", {"path": "a.dng", "flag": "none"})
        assert lib.tool.meta["a.dng"]["Rating"] == 4
    # a photo that had no stars goes back to none, not to a stale value
    post(lib, "/api/flag", {"path": "b.dng", "flag": "reject"})
    post(lib, "/api/flag", {"path": "b.dng", "flag": "none"})
    assert lib.tool.meta["b.dng"].get("Rating", 0) == 0


def test_flag_validation(lib):
    assert post(lib, "/api/flag", {"path": "a.dng", "flag": "star"}).status_code == 400
    assert post(lib, "/api/flag", {"path": "../x", "flag": "pick"}).status_code == 400
    assert lib.post("/api/flag", data="x", content_type="text/plain").status_code == 415


def test_label_set_and_clear_writes_xmp_label(lib):
    r = post(lib, "/api/label", {"paths": ["a.dng", "b.dng"], "label": "blue"})
    assert r.get_json()["ok"]
    assert lib.tool.meta["a.dng"]["Label"] == "Blue" and lib.tool.meta["b.dng"]["Label"] == "Blue"
    assert any("-XMP-xmp:Label=Blue" in w for w in lib.tool.writes)
    assert get(lib, label="Blue")["total"] == 2
    post(lib, "/api/label", {"path": "a.dng", "label": "none"})
    assert get(lib, label="none")["total"] == 1 and get(lib, label="blue")["total"] == 1
    assert post(lib, "/api/label", {"path": "a.dng", "label": "pink"}).status_code == 400


def test_keywords_add_remove_set_write_dc_subject(lib):
    post(lib, "/api/keywords", {"paths": ["a.dng", "b.dng"], "add": ["Sunset", "beach"]})
    assert lib.tool.meta["a.dng"]["Subject"] == ["beach", "Sunset"]     # beach not duplicated
    assert lib.tool.meta["b.dng"]["Subject"] == ["Sunset", "beach"]
    post(lib, "/api/keywords", {"path": "a.dng", "remove": ["BEACH"]})
    assert lib.tool.meta["a.dng"]["Subject"] == ["Sunset"]
    post(lib, "/api/keywords", {"path": "a.dng", "set": ["x", "y"]})
    assert get(lib, keyword="y")["items"][0]["path"] == "a.dng"
    assert {k["keyword"] for k in get(lib)["facets"]["keywords"]} >= {"x", "y", "Sunset", "beach"}
    assert post(lib, "/api/keywords", {"path": "a.dng"}).status_code == 400


def test_writes_do_not_force_a_full_reread(lib):
    get(lib)
    n = lib.tool.reads
    post(lib, "/api/label", {"path": "a.dng", "label": "red"})
    get(lib, label="red")
    assert lib.tool.reads == n


def test_rate_endpoint_keeps_library_in_step(lib):
    get(lib)
    lib.post("/api/rate", json={"path": "b.dng", "rating": 5})
    assert [i["path"] for i in get(lib, rating_min=5)["items"]] == ["b.dng"]


def test_cross_origin_write_refused(lib):
    r = lib.post("/api/flag", json={"path": "a.dng", "flag": "pick"},
                 headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
