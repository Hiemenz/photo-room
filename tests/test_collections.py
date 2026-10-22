"""
Collections, smart collections, virtual copies and their API endpoints.

Library metadata comes from the same exiftool double the library tests use;
rendering uses the fake rawpy decoder.
"""

import json
from pathlib import Path

import pytest

import catalog
import editor
import exporter
import library
from exporter import ExportOptions
from test_library_module import FakeExiftool


def item(path, rating=0, flag="none", label="", keywords=(), camera="", date=""):
    return {"path": path, "name": Path(path).name, "rating": rating, "flag": flag,
            "label": label, "keywords": list(keywords), "camera": camera, "date": date}


# ------------------------------------------------------------ collection CRUD

def test_create_rename_delete_manual_collection(tmp_path):
    col = catalog.create_collection(tmp_path, "  Best   of ")
    assert col["name"] == "Best of" and col["photos"] == [] and not col["smart"]
    catalog.rename_collection(tmp_path, col["id"], "Keepers")
    assert [c["name"] for c in catalog.list_collections(tmp_path)] == ["Keepers"]
    catalog.delete_collection(tmp_path, col["id"])
    assert catalog.list_collections(tmp_path) == []


def test_collection_names_are_unique_and_required(tmp_path):
    a = catalog.create_collection(tmp_path, "A")
    with pytest.raises(ValueError):
        catalog.create_collection(tmp_path, "a")
    with pytest.raises(ValueError):
        catalog.create_collection(tmp_path, "   ")
    b = catalog.create_collection(tmp_path, "B")
    with pytest.raises(ValueError):
        catalog.rename_collection(tmp_path, b["id"], "A")
    catalog.rename_collection(tmp_path, a["id"], "A")     # renaming to itself is fine


def test_unknown_collection_raises(tmp_path):
    for fn in (catalog.delete_collection, catalog.get_collection):
        with pytest.raises(KeyError):
            fn(tmp_path, "nope")
    with pytest.raises(KeyError):
        catalog.add_photos(tmp_path, "nope", ["a.dng"])


def test_add_remove_photos_dedupes_and_persists(tmp_path):
    col = catalog.create_collection(tmp_path, "C")
    catalog.add_photos(tmp_path, col["id"], ["a.dng", "b.dng", "a.dng"])
    catalog.add_photos(tmp_path, col["id"], ["b.dng", "c.dng"])
    assert catalog.get_collection(tmp_path, col["id"])["photos"] == ["a.dng", "b.dng", "c.dng"]
    catalog.remove_photos(tmp_path, col["id"], ["b.dng", "zzz.dng"])
    stored = json.loads((tmp_path / catalog.STORE_NAME).read_text())
    assert stored["collections"][0]["photos"] == ["a.dng", "c.dng"]


def test_collection_items_only_returns_members_present(tmp_path):
    col = catalog.create_collection(tmp_path, "C", photos=["a", "gone"])
    items = [item("a"), item("b")]
    assert [i["path"] for i in catalog.collection_items(col, items)] == ["a"]


def test_corrupt_store_reads_as_empty(tmp_path):
    (tmp_path / catalog.STORE_NAME).write_text("{nope")
    assert catalog.load(tmp_path) == {"collections": [], "virtual": {}, "stacks": {}}
    catalog.create_collection(tmp_path, "Fresh")     # and is recoverable


# ------------------------------------------------------- smart-collection rules

@pytest.fixture
def pool():
    return [
        item("a", 5, "pick", "Red", ["beach", "Sunset"], "Canon EOS 5D", "2024-05-01 10:00:00"),
        item("b", 3, "none", "Blue", ["beach"], "Nikon Z6", "2024-05-03 10:00:00"),
        item("c", -1, "reject", "", [], "Canon EOS 5D", "2024-04-01 10:00:00"),
        item("d", 0, "none", "", ["city"], "", ""),
    ]


def paths(xs):
    return [x["path"] for x in xs]


def smart(pool, rules, match="all"):
    return paths(catalog.smart_matches(pool, catalog.normalize_rules(rules), match))


def test_rating_rules(pool):
    assert smart(pool, [{"field": "rating", "op": "ge", "value": 3}]) == ["a", "b"]
    assert smart(pool, [{"field": "rating", "op": "eq", "value": 0}]) == ["d"]
    assert smart(pool, [{"field": "rating", "op": "le", "value": 0}]) == ["c", "d"]


def test_flag_label_keyword_camera_rules(pool):
    assert smart(pool, [{"field": "flag", "value": "pick"}]) == ["a"]
    assert smart(pool, [{"field": "flag", "value": "unflagged"}]) == ["b", "d"]
    assert smart(pool, [{"field": "label", "value": "red"}]) == ["a"]
    assert smart(pool, [{"field": "label", "value": "none"}]) == ["c", "d"]
    assert smart(pool, [{"field": "keyword", "op": "has", "value": "SUNSET"}]) == ["a"]
    assert smart(pool, [{"field": "keyword", "op": "not_has", "value": "beach"}]) == ["c", "d"]
    assert smart(pool, [{"field": "camera", "op": "is", "value": "nikon z6"}]) == ["b"]
    assert smart(pool, [{"field": "camera", "op": "contains", "value": "canon"}]) == ["a", "c"]


def test_date_rules_ignore_undated(pool):
    assert smart(pool, [{"field": "date", "op": "after", "value": "2024-04-30"}]) == ["a", "b"]
    assert smart(pool, [{"field": "date", "op": "before", "value": "2024-05-02"}]) == ["a", "c"]
    assert smart(pool, [{"field": "date", "op": "on", "value": "2024-05-03"}]) == ["b"]


def test_all_versus_any(pool):
    rules = [{"field": "keyword", "op": "has", "value": "beach"},
             {"field": "rating", "op": "ge", "value": 5}]
    assert smart(pool, rules, "all") == ["a"]
    assert smart(pool, rules, "any") == ["a", "b"]


@pytest.mark.parametrize("rule", [
    {"field": "colour", "value": "x"},
    {"field": "rating", "op": "contains", "value": 3},
    {"field": "rating", "value": "lots"},
    {"field": "rating", "value": 9},
    {"field": "flag", "value": "maybe"},
    {"field": "label", "value": "Mauve"},
    {"field": "keyword", "value": "  "},
    {"field": "date", "op": "on", "value": "May 1st"},
    "not a rule",
])
def test_bad_rules_rejected(rule):
    with pytest.raises(ValueError):
        catalog.normalize_rule(rule)


def test_smart_collection_needs_rules_and_is_not_manually_editable(tmp_path):
    with pytest.raises(ValueError):
        catalog.create_collection(tmp_path, "S", smart=True, rules=[])
    col = catalog.create_collection(
        tmp_path, "S", smart=True, rules=[{"field": "rating", "op": "ge", "value": 4}])
    with pytest.raises(ValueError):
        catalog.add_photos(tmp_path, col["id"], ["a"])
    catalog.update_rules(tmp_path, col["id"], [{"field": "flag", "value": "pick"}], "any")
    got = catalog.get_collection(tmp_path, col["id"])
    assert got["rules"] == [{"field": "flag", "op": "is", "value": "pick"}] and got["match"] == "any"


def test_smart_collection_tracks_library_changes(tmp_path, pool):
    col = catalog.create_collection(
        tmp_path, "Good", smart=True, rules=[{"field": "rating", "op": "ge", "value": 3}])
    assert paths(catalog.collection_items(col, pool)) == ["a", "b"]
    pool[3]["rating"] = 4
    assert paths(catalog.collection_items(col, pool)) == ["a", "b", "d"]


# ------------------------------------------------------------- virtual copies

def test_virtual_copy_is_independent_of_source_and_siblings(tmp_path):
    one = catalog.create_virtual(tmp_path, "s/IMG_1.dng", {"exposure": 0.5},
                                 rating=3, label="Red", keywords=["x"])
    two = catalog.create_virtual(tmp_path, "s/IMG_1.dng", {"exposure": 0.5})
    assert (one["copy_no"], two["copy_no"]) == (1, 2)
    assert one["id"] != two["id"] and one["id"].startswith("s/IMG_1.dng#vc-")
    catalog.update_virtual(tmp_path, one["id"], adjustments={"exposure": -1.0},
                           rating=5, label="Blue", keywords=["y", "z"], flag="pick")
    other = catalog.get_virtual(tmp_path, two["id"])
    assert other["adjustments"] == {"exposure": 0.5}
    assert (other["rating"], other["label"], other["keywords"], other["flag"]) == (0, "", [], "none")
    mine = catalog.get_virtual(tmp_path, one["id"])
    assert (mine["rating"], mine["label"], mine["keywords"]) == (5, "Blue", ["y", "z"])


def test_copy_of_a_copy_points_at_the_raw(tmp_path):
    one = catalog.create_virtual(tmp_path, "a.dng")
    nested = catalog.create_virtual(tmp_path, one["id"])
    assert nested["source"] == "a.dng"


def test_virtual_items_inherit_camera_and_date_but_own_metadata(tmp_path):
    src = [item("a.dng", 4, camera="Canon", date="2024-05-01 10:00:00")]
    v = catalog.create_virtual(tmp_path, "a.dng", rating=2, label="Green", keywords=["k"], flag="pick")
    [it] = catalog.virtual_items(tmp_path, src)
    assert it["path"] == v["id"] and it["name"] == "a (copy 1).dng" and it["virtual"]
    assert (it["rating"], it["label"], it["keywords"], it["flag"]) == (2, "Green", ["k"], "pick")
    assert (it["camera"], it["date"], it["source"]) == ("Canon", "2024-05-01 10:00:00", "a.dng")
    catalog.update_virtual(tmp_path, v["id"], rating=-1)
    assert catalog.virtual_items(tmp_path, src)[0]["flag"] == "reject"


def test_orphaned_copy_is_hidden_when_raw_is_gone(tmp_path):
    catalog.create_virtual(tmp_path, "gone.dng")
    assert catalog.virtual_items(tmp_path, [item("other.dng")]) == []


def test_deleting_a_copy_leaves_the_raw_and_cleans_collections(tmp_path):
    raw = tmp_path / "a.dng"
    raw.write_bytes(b"raw bytes")
    v = catalog.create_virtual(tmp_path, "a.dng")
    col = catalog.create_collection(tmp_path, "C", photos=["a.dng", v["id"]])
    catalog.delete_virtual(tmp_path, v["id"])
    assert raw.read_bytes() == b"raw bytes"
    assert catalog.get_collection(tmp_path, col["id"])["photos"] == ["a.dng"]
    with pytest.raises(KeyError):
        catalog.get_virtual(tmp_path, v["id"])


def test_update_rejects_unknown_fields(tmp_path):
    v = catalog.create_virtual(tmp_path, "a.dng")
    with pytest.raises(ValueError):
        catalog.update_virtual(tmp_path, v["id"], source="b.dng")


# --------------------------------------------------------------------- export

def test_export_names_and_renders_the_copy_separately(tmp_path, monkeypatch):
    from PIL import Image
    src = tmp_path / "IMG_0001.jpg"
    Image.new("RGB", (64, 48), (120, 120, 120)).save(src)
    monkeypatch.setattr(exporter, "_saved_adjustments", lambda p, *a: {"exposure": 0.0})
    v = catalog.create_virtual(tmp_path, "IMG_0001.jpg", {"exposure": 2.0})
    opts = ExportOptions(format="png", keep_metadata=False)
    original = exporter.export_photo(src, tmp_path / "out", opts)
    copy = exporter.export_photo(src, tmp_path / "out", opts, virtual=v)
    assert original.name == "IMG_0001.png" and copy.name == "IMG_0001-copy1.png"
    a = Image.open(original).convert("L").getpixel((5, 5))
    b = Image.open(copy).convert("L").getpixel((5, 5))
    assert b > a + 20, "the copy must be rendered with its own adjustments"
    assert src.exists()


def test_export_copy_numbers_do_not_collide(tmp_path, monkeypatch):
    from PIL import Image
    src = tmp_path / "IMG_0002.jpg"
    Image.new("RGB", (32, 32), (90, 90, 90)).save(src)
    monkeypatch.setattr(exporter, "_saved_adjustments", lambda p, *a: {})
    v1 = catalog.create_virtual(tmp_path, "IMG_0002.jpg", {})
    v2 = catalog.create_virtual(tmp_path, "IMG_0002.jpg", {})
    opts = ExportOptions(format="jpeg", keep_metadata=False, template="{name}_{seq:02d}")
    names = {exporter.export_photo(src, tmp_path / "o", opts, seq=i, virtual=v).name
             for i, v in enumerate([v1, v2], 1)}
    assert names == {"IMG_0002-copy1_01.jpg", "IMG_0002-copy2_02.jpg"}


# ------------------------------------------------------------------------ API

@pytest.fixture
def lib(tmp_path, monkeypatch, fake_rawpy):
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
    import subprocess
    monkeypatch.setattr(subprocess, "run", tool)
    library.clear_cache()
    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        c.tool = tool
        c.root = tmp_path
        yield c
    library.clear_cache()


def test_collection_endpoints_crud_and_counts(lib):
    r = lib.post("/api/collections/create", json={"name": "Trip"})
    assert r.status_code == 200
    cid = r.get_json()["collection"]["id"]
    assert lib.post("/api/collections/create", json={"name": "trip"}).status_code == 400
    r = lib.post("/api/collections/add", json={"id": cid, "photos": ["a.dng", "b.dng"]})
    assert [c["count"] for c in r.get_json()["collections"]] == [2]
    assert lib.post("/api/collections/add", json={"id": cid, "photos": ["../x.dng"]}).status_code == 400
    assert lib.post("/api/collections/add", json={"id": "nope", "photos": ["a.dng"]}).status_code == 404
    lib.post("/api/collections/remove", json={"id": cid, "photos": ["a.dng"]})
    lib.post("/api/collections/rename", json={"id": cid, "name": "Holiday"})
    got = lib.get("/api/collections").get_json()["collections"]
    assert got[0]["name"] == "Holiday" and got[0]["count"] == 1
    r = lib.get("/api/library", query_string={"collection": cid}).get_json()
    assert [i["path"] for i in r["items"]] == ["b.dng"]
    assert lib.post("/api/collections/delete", json={"id": cid}).get_json()["collections"] == []
    assert lib.get("/api/library", query_string={"collection": cid}).status_code == 404
    assert (lib.root / "a.dng").exists()


def test_smart_collection_endpoint_and_library_filter(lib):
    r = lib.post("/api/collections/create", json={
        "name": "Canon", "smart": True,
        "rules": [{"field": "camera", "op": "contains", "value": "canon"}]})
    cid = r.get_json()["collection"]["id"]
    assert r.get_json()["collections"][0]["count"] == 2
    items = lib.get("/api/library", query_string={"collection": cid}).get_json()["items"]
    assert [i["path"] for i in items] == ["a.dng", "c.dng"]
    assert lib.post("/api/collections/create", json={
        "name": "Bad", "smart": True, "rules": [{"field": "rating", "value": 99}]}).status_code == 400
    assert lib.post("/api/collections/add", json={"id": cid, "photos": ["a.dng"]}).status_code == 400
    lib.post("/api/collections/rules", json={
        "id": cid, "rules": [{"field": "rating", "op": "ge", "value": 4}]})
    items = lib.get("/api/library", query_string={"collection": cid}).get_json()["items"]
    assert [i["path"] for i in items] == ["a.dng"]


def test_virtual_copy_endpoints(lib):
    r = lib.post("/api/virtual/create", json={"path": "a.dng", "adjustments": {"exposure": 1.0}})
    assert r.status_code == 200, r.get_json()
    v = r.get_json()["virtual"]
    assert v["rating"] == 4 and v["keywords"] == ["beach"]      # seeded from the source
    items = lib.get("/api/library").get_json()
    assert items["total"] == 4 and items["library_total"] == 4
    copy = next(i for i in items["items"] if i.get("virtual"))
    assert copy["path"] == v["id"] and copy["name"] == "a (copy 1).dng"

    # Its own rating / label / keywords / flag; the raw's are untouched.
    assert lib.post("/api/rate", json={"path": v["id"], "rating": 1}).status_code == 200
    assert lib.post("/api/label", json={"paths": [v["id"]], "label": "Green"}).status_code == 200
    assert lib.post("/api/keywords", json={"paths": [v["id"], "b.dng"], "add": ["new"]}).status_code == 200
    assert lib.post("/api/flag", json={"paths": [v["id"]], "flag": "pick"}).status_code == 200
    by = {i["path"]: i for i in lib.get("/api/library").get_json()["items"]}
    assert (by[v["id"]]["rating"], by[v["id"]]["label"], by[v["id"]]["flag"]) == (1, "Green", "pick")
    assert by[v["id"]]["keywords"] == ["beach", "new"]
    assert (by["a.dng"]["rating"], by["a.dng"]["label"], by["a.dng"]["flag"]) == (4, "", "none")
    assert by["b.dng"]["keywords"] == ["new"]
    # No exiftool write ever targeted the copy.
    assert all(Path(w[-1]).name != "a (copy 1).dng" for w in lib.tool.writes)

    # Saving the copy's edits does not touch the raw's XMP.
    writes_before = len(lib.tool.writes)
    r = lib.post("/api/save", json={"path": v["id"], "adjustments": {"exposure": -0.5}})
    assert r.status_code == 200 and len(lib.tool.writes) == writes_before
    assert catalog.get_virtual(lib.root, v["id"])["adjustments"] == {"exposure": -0.5}

    # Deleting removes the copy only.
    assert lib.post("/api/virtual/delete", json={"path": v["id"]}).status_code == 200
    assert (lib.root / "a.dng").read_bytes() == b"dng"
    assert lib.get("/api/library").get_json()["total"] == 3
    assert lib.post("/api/virtual/delete", json={"path": v["id"]}).status_code == 404
    assert lib.post("/api/virtual/delete", json={"path": "a.dng"}).status_code == 400


def test_virtual_copy_of_missing_photo_is_refused(lib):
    assert lib.post("/api/virtual/create", json={"path": "nope.dng"}).status_code == 400


def test_virtual_copy_can_join_collections_and_filter(lib):
    v = lib.post("/api/virtual/create", json={"path": "a.dng", "adjustments": {"exposure": 1}}
                 ).get_json()["virtual"]
    cid = lib.post("/api/collections/create", json={"name": "C", "photos": [v["id"]]}
                   ).get_json()["collection"]["id"]
    items = lib.get("/api/library", query_string={"collection": cid}).get_json()["items"]
    assert [i["path"] for i in items] == [v["id"]]
    lib.post("/api/virtual/delete", json={"path": v["id"]})
    assert lib.get("/api/collections").get_json()["collections"][0]["count"] == 0


def test_view_preview_renders_copy_with_its_own_settings(lib):
    v = lib.post("/api/virtual/create", json={"path": "a.dng", "adjustments": {"exposure": 2.0}}
                 ).get_json()["virtual"]
    bright = lib.post("/api/view_preview", json={"path": v["id"]})
    assert bright.status_code == 200, bright.get_json()
    assert bright.get_json()["preview"].startswith("data:image/jpeg;base64,")
    lib.post("/api/save", json={"path": v["id"], "adjustments": {"exposure": -2.0}})
    dark = lib.post("/api/view_preview", json={"path": v["id"]}).get_json()["preview"]
    assert dark != bright.get_json()["preview"]
    assert lib.post("/api/view_preview", json={"path": "../etc/passwd"}).status_code == 400
    assert lib.post("/api/view_preview", json={"path": "x.dng#vc-deadbeef"}).status_code in (400, 404)


def test_open_virtual_copy_uses_its_settings_and_own_history(lib):
    v = lib.post("/api/virtual/create", json={"path": "a.dng", "adjustments": {"exposure": 1.5}}
                 ).get_json()["virtual"]
    r = lib.post("/api/open", json={"path": v["id"]})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["virtual"] and body["source"] == "saved"
    assert body["adjustments"]["exposure"] == 1.5 and body["rating"] == 4
    # History is kept for the copy, in its own file, not the raw's.
    names = {p.name for p in lib.root.iterdir()}
    assert f"a.dng.{v['vid']}.photoroom.json" in names
    assert "a.dng.photoroom.json" not in names
    r = lib.post("/api/history/reset_to_import", json={"path": v["id"]})
    assert r.status_code == 200, r.get_json()
    lib.post("/api/virtual/delete", json={"path": v["id"]})
    assert f"a.dng.{v['vid']}.photoroom.json" not in {p.name for p in lib.root.iterdir()}
    assert (lib.root / "a.dng").exists()


def test_cross_origin_collection_write_refused(lib):
    r = lib.post("/api/collections/create", json={"name": "x"},
                 headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_every_path_taking_endpoint_accepts_a_virtual_copy_id(lib):
    """Overlay, upright, spot suggestion and recover resolve a copy to its raw;
    batch apply writes a copy's settings to the catalog, not the raw's XMP."""
    v = lib.post("/api/virtual/create", json={"path": "a.dng", "adjustments": {"exposure": 1.0}}
                 ).get_json()["virtual"]["id"]
    bodies = {
        "/api/mask_overlay": {"path": v, "mask": {"type": "radial", "x": .5, "y": .5, "rx": .2, "ry": .2}},
        "/api/upright": {"path": v, "mode": "auto"},
        "/api/spot/suggest": {"path": v, "x": 0.5, "y": 0.5},
    }
    for url, body in bodies.items():
        r = lib.post(url, json=body)
        assert "No such file" not in str(r.get_json()), (url, r.get_json())
    writes_before = len(lib.tool.writes)
    r = lib.post("/api/batch_apply", json={"paths": [v, "b.dng"], "adjustments": {"contrast": 9}})
    body = r.get_json()
    assert v in body["applied"], body
    assert catalog.get_virtual(lib.root, v)["adjustments"]["contrast"] == 9
    assert all(Path(w[-1]).name != "a.dng" for w in lib.tool.writes[writes_before:])
