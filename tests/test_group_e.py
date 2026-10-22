"""Collection sets, Quick / Target collection, the Map endpoint and the move-folder behind the drag UI."""

import subprocess
from pathlib import Path

import pytest

import catalog
import editor
import folders
import libindex
import library
import map_api
from test_library_module import FakeExiftool


# ------------------------------------------------------------ collection sets

def test_sets_nest_and_list_depth_first(tmp_path):
    trips = catalog.create_collection(tmp_path, "Trips", is_set=True)
    asia = catalog.create_collection(tmp_path, "Asia", is_set=True, parent=trips["id"])
    catalog.create_collection(tmp_path, "Japan", parent=asia["id"], photos=["a.dng"])
    catalog.create_collection(tmp_path, "Loose")
    order = catalog.tree_order(catalog.list_collections(tmp_path))
    assert [(c["name"], c["depth"]) for c in order] == [("Trips", 0), ("Asia", 1), ("Japan", 2), ("Loose", 0)]
    # persisted
    assert catalog.load(tmp_path)["collections"][2]["parent"] == asia["id"]


def test_set_rules_cycles_and_same_name_in_other_set(tmp_path):
    a = catalog.create_collection(tmp_path, "A", is_set=True)
    b = catalog.create_collection(tmp_path, "B", is_set=True, parent=a["id"])
    plain = catalog.create_collection(tmp_path, "Plain")
    with pytest.raises(ValueError):
        catalog.move_collection(tmp_path, a["id"], b["id"])         # into its own descendant
    with pytest.raises(ValueError):
        catalog.move_collection(tmp_path, a["id"], a["id"])
    with pytest.raises(ValueError):
        catalog.move_collection(tmp_path, b["id"], plain["id"])     # not a set
    with pytest.raises(ValueError):
        catalog.add_photos(tmp_path, a["id"], ["x.dng"])            # sets hold no photos
    assert catalog.collection_items(a, [{"path": "x.dng"}]) == []
    # the same name is fine in a different set, not beside itself
    catalog.create_collection(tmp_path, "Plain", parent=a["id"])
    with pytest.raises(ValueError):
        catalog.create_collection(tmp_path, "plain")
    with pytest.raises(KeyError):
        catalog.create_collection(tmp_path, "X", parent="nope")


def test_move_collection_and_delete_set_moves_children_up(tmp_path):
    s = catalog.create_collection(tmp_path, "S", is_set=True)
    c = catalog.create_collection(tmp_path, "C", photos=["a.dng"])
    catalog.move_collection(tmp_path, c["id"], s["id"])
    assert catalog.get_collection(tmp_path, c["id"])["parent"] == s["id"]
    catalog.delete_collection(tmp_path, s["id"])
    kept = catalog.get_collection(tmp_path, c["id"])
    assert "parent" not in kept and kept["photos"] == ["a.dng"]
    catalog.move_collection(tmp_path, c["id"], None)                # already top level: harmless


# ------------------------------------------------------- quick / target

def test_quick_collection_is_protected_and_toggles(tmp_path):
    assert catalog.target_id(tmp_path) is None
    res = catalog.toggle_in_target(tmp_path, ["a.dng", "b.dng"])
    assert res["action"] == "added" and res["collection"]["quick"]
    qid = res["collection"]["id"]
    assert catalog.target_id(tmp_path) == qid
    assert catalog.toggle_in_target(tmp_path, ["a.dng", "c.dng"])["action"] == "added"   # c is new
    assert catalog.get_collection(tmp_path, qid)["photos"] == ["a.dng", "b.dng", "c.dng"]
    assert catalog.toggle_in_target(tmp_path, ["a.dng", "b.dng", "c.dng"])["action"] == "removed"
    assert catalog.get_collection(tmp_path, qid)["photos"] == []
    for fn in (lambda: catalog.delete_collection(tmp_path, qid),
               lambda: catalog.rename_collection(tmp_path, qid, "Mine"),
               lambda: catalog.create_collection(tmp_path, "quick collection")):
        with pytest.raises(ValueError):
            fn()


def test_target_collection_choice_persists_and_resets(tmp_path):
    catalog.toggle_in_target(tmp_path, ["a.dng"])
    mine = catalog.create_collection(tmp_path, "Mine")
    smart = catalog.create_collection(tmp_path, "Smart", smart=True, rules=[{"field": "rating", "op": "ge", "value": 3}])
    assert catalog.set_target(tmp_path, mine["id"]) == mine["id"]
    assert catalog.load(tmp_path)["target"] == mine["id"]
    catalog.toggle_in_target(tmp_path, ["b.dng"])
    assert catalog.get_collection(tmp_path, mine["id"])["photos"] == ["b.dng"]
    with pytest.raises(ValueError):
        catalog.set_target(tmp_path, smart["id"])
    catalog.delete_collection(tmp_path, mine["id"])                 # a deleted target falls back to Quick
    assert catalog.target_id(tmp_path) == catalog.quick_collection(tmp_path)["id"]
    catalog.clear_collection(tmp_path, catalog.quick_collection(tmp_path)["id"])
    assert catalog.quick_collection(tmp_path)["photos"] == []


# ------------------------------------------------------------ GPS parsing/map

def test_parse_meta_reads_coordinates():
    m = libindex.parse_meta({"GPSLatitude": 51.5, "GPSLongitude": -0.12})
    assert (m["lat"], m["lon"]) == (51.5, -0.12) and m["gps"]
    m = libindex.parse_meta({"Model": "X"}, {"GPSLatitude": -33.9, "GPSLongitude": 151.2})   # from the sidecar
    assert (m["lat"], m["lon"]) == (-33.9, 151.2)
    m = libindex.parse_meta({"GPSLatitude": 95, "GPSLongitude": 10})
    assert m["lat"] is None and m["gps"]
    assert libindex.parse_meta({"Model": "X"})["lon"] is None


def test_gps_box_filter_including_antimeridian():
    items = [{"path": "a", "name": "a", "rating": 0, "flag": "none", "label": "", "keywords": [], "camera": "",
              "date": "", "lat": 10.0, "lon": 20.0},
             {"path": "b", "name": "b", "rating": 0, "flag": "none", "label": "", "keywords": [], "camera": "",
              "date": "", "lat": 10.0, "lon": -179.5},
             {"path": "c", "name": "c", "rating": 0, "flag": "none", "label": "", "keywords": [], "camera": "",
              "date": "", "lat": None, "lon": None}]
    paths = lambda box: [i["path"] for i in library.filter_items(items, {"gps_box": box})]
    assert paths("0,10,20,30") == ["a"]
    assert paths("0,170,20,-170") == ["b"]
    assert paths("") == ["a", "b", "c"]
    for bad in ("1,2,3", "50,0,10,10", "a,b,c,d"):
        with pytest.raises(ValueError):
            library.filter_items(items, {"gps_box": bad})


def test_cluster_groups_by_cell():
    items = [{"path": f"p{i}", "name": f"p{i}", "lat": 51.50 + i * 0.001, "lon": -0.12} for i in range(3)]
    items.append({"path": "far", "name": "far", "lat": -33.9, "lon": 151.2})
    cl = map_api.cluster(items, 1.0)
    assert [c["count"] for c in cl] == [3, 1]
    assert cl[0]["osm"].startswith("https://www.openstreetmap.org/?mlat=51.5")
    assert len(map_api.cluster(items, 0.0005)) == 4
    s, w, n, e = (float(x) for x in cl[0]["box"].split(","))
    assert s < 51.5 < n and w < -0.12 < e


@pytest.fixture
def lib(tmp_path, monkeypatch, fake_rawpy):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    monkeypatch.setenv("PHOTO_ROOM_EMBED_DNG", "1")
    for name in ("a.dng", "b.dng", "c.dng"):
        (tmp_path / name).write_bytes(b"dng")
    tool = FakeExiftool(tmp_path)
    tool.meta = {
        "a.dng": {"Model": "Canon", "GPSLatitude": 51.5, "GPSLongitude": -0.12},
        "b.dng": {"Model": "Canon", "GPSLatitude": 51.5002, "GPSLongitude": -0.1203},
        "c.dng": {"Model": "Canon"},
    }
    monkeypatch.setattr(subprocess, "run", tool)
    library.clear_cache()
    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        c.root = tmp_path
        yield c
    library.clear_cache()


def test_map_endpoint_and_gps_box_in_library(lib):
    d = lib.get("/api/map", query_string={"zoom": 8}).get_json()
    assert d["geotagged"] == 2 and d["total"] == 3
    assert [c["count"] for c in d["clusters"]] == [2]
    assert d["bounds"]["south"] == 51.5
    box = d["clusters"][0]["box"]
    r = lib.get("/api/library", query_string={"gps_box": box}).get_json()
    assert sorted(i["name"] for i in r["items"]) == ["a.dng", "b.dng"]
    assert lib.get("/api/library", query_string={"gps_box": "bad"}).status_code == 400
    assert lib.get("/api/map", query_string={"zoom": "x"}).status_code == 400
    assert lib.get("/api/map", query_string={"collection": "nope"}).status_code == 404


def test_collection_api_sets_target_and_quick(lib):
    s = lib.post("/api/collections/create", json={"name": "Set", "set": True}).get_json()["collection"]
    r = lib.post("/api/collections/create", json={"name": "Inner", "parent": s["id"], "photos": ["a.dng"]})
    inner = r.get_json()["collection"]
    cols = r.get_json()["collections"]
    assert [(c["name"], c["depth"]) for c in cols] == [("Set", 0), ("Inner", 1)]
    assert lib.post("/api/collections/add", json={"id": s["id"], "photos": ["a.dng"]}).status_code == 400
    assert lib.post("/api/collections/move", json={"id": inner["id"], "parent": ""}).status_code == 200
    assert lib.post("/api/collections/move", json={"id": s["id"], "parent": inner["id"]}).status_code == 400
    # B
    r = lib.post("/api/collections/target_toggle", json={"photos": ["b.dng", "c.dng"]}).get_json()
    assert r["action"] == "added" and r["collection"] == "Quick Collection"
    quick = next(c for c in r["collections"] if c.get("quick"))
    assert r["target"] == quick["id"] and quick["count"] == 2
    r = lib.post("/api/collections/target", json={"id": inner["id"]}).get_json()
    assert r["target"] == inner["id"]
    assert lib.post("/api/collections/target", json={"id": s["id"]}).status_code == 400
    assert lib.post("/api/collections/target_toggle", json={"photos": ["../x"]}).status_code == 400
    r = lib.get("/api/library", query_string={"collection": quick["id"]}).get_json()
    assert sorted(i["name"] for i in r["items"]) == ["b.dng", "c.dng"]
    assert lib.post("/api/collections/delete", json={"id": quick["id"]}).status_code == 400
    r = lib.post("/api/collections/clear", json={"id": quick["id"]}).get_json()
    assert next(c for c in r["collections"] if c.get("quick"))["count"] == 0
    r = lib.post("/api/collections/delete", json={"id": s["id"]}).get_json()
    assert all(c["name"] != "Set" for c in r["collections"])


# ------------------------------------------------------- move folder (UI backing)

def test_move_folder_carries_collections(tmp_path):
    for rel in ("2024/may/a.dng", "dest/keep.dng"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"raw")
    col = catalog.create_collection(tmp_path, "C", photos=["2024/may/a.dng"])
    r = folders.move_folder(tmp_path, "2024/may", "dest")
    assert (tmp_path / "dest" / "may" / "a.dng").exists()
    assert catalog.get_collection(tmp_path, col["id"])["photos"] == ["dest/may/a.dng"]
    with pytest.raises(folders.MoveError):
        folders.move_folder(tmp_path, "dest", "dest/may")           # into itself
    folders.undo_last(tmp_path)
    assert (tmp_path / "2024" / "may" / "a.dng").exists()
    assert r["label"]


def test_static_assets_wired():
    html = (Path(__file__).parent.parent / "templates" / "editor.html").read_text()
    for needle in ("library-view.js", "map-view.js", 'id="map-dlg"', 'id="lib-cell"', 'id="loupe-ov"',
                   'id="col-set"', 'id="lib-target"'):
        assert needle in html
