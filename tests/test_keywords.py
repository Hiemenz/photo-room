"""
Keyword list: hierarchy (lr:HierarchicalSubject next to dc:Subject), counts,
synonyms, the include-on-export flag, and rename / merge / delete across
photos through their XMP sidecars.

The pure parts run everywhere; the sidecar round trips use the real exiftool
(marker ``exiftool``) because the point is that Lightroom-compatible XMP lands
on disk.
"""
import json
import shutil
from pathlib import Path

import pytest
from PIL import Image

import editor
import exifpool
import keywordlib as kl
import libindex
import library

needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")


def it(path, kws, hier=()):
    return {"path": path, "keywords": list(kws), "hier": list(hier)}


# ------------------------------------------------------------------ pure

def test_parse_hier_and_xmp_args():
    assert kl.parse_hier({"HierarchicalSubject": ["France|Paris", "france|paris", " A | B "]}) == \
        ["France|Paris", "A|B"]
    assert kl.parse_hier({"HierarchicalSubject": "Solo"}) == ["Solo"]
    assert kl.parse_hier({}) == []
    assert library.xmp_args(keywords=["Paris"], hier=["France|Paris"]) == [
        "-XMP-dc:Subject=Paris", "-XMP-lr:HierarchicalSubject=France|Paris"]
    assert library.xmp_args(keywords=[], hier=[]) == ["-XMP-dc:Subject=", "-XMP-lr:HierarchicalSubject="]


def test_tree_counts_include_children_and_discover_lightroom_hierarchy(tmp_path):
    items = [it("1", ["Paris"], ["France|Paris"]), it("2", ["Lyon", "France"], ["France|Lyon", "France"]),
             it("3", ["Beach"], ["Beach"]), it("4", ["paris"], ["France|Paris"])]
    rows = kl.tree(items, kl.merged_state(tmp_path, items))
    by = {r["key"]: r for r in rows}
    assert [r["key"] for r in rows] == ["beach", "france", "lyon", "paris"]       # parents first, depth-first
    assert by["paris"]["parent"] == "France" and by["paris"]["depth"] == 1
    assert by["paris"]["path"] == "France > Paris"
    assert (by["france"]["count"], by["france"]["total"]) == (1, 3)               # own photos vs with children
    assert by["paris"]["count"] == 2                                              # case-insensitive


def test_saved_parent_wins_and_cycles_are_refused(tmp_path):
    kl.save_state(tmp_path, {"paris": {"name": "Paris", "parent": "idf", "synonyms": [], "export": True},
                             "idf": {"name": "IDF", "parent": "", "synonyms": [], "export": True}})
    st = kl.merged_state(tmp_path, [it("1", ["Paris"], ["France|Paris"])])
    assert st["paris"]["parent"] == "idf"
    st = kl.merged_state(tmp_path, [it("1", ["A"], ["A|B", "B|A"])])             # contradictory sidecars
    assert not (st["a"]["parent"] == "b" and st["b"]["parent"] == "a")


def test_paths_for_prefers_tree_and_keeps_foreign_paths():
    st = {"paris": {"name": "Paris", "parent": "france", "synonyms": [], "export": True},
          "france": {"name": "France", "parent": "", "synonyms": [], "export": True}}
    assert kl.paths_for(st, ["Paris", "Dog", "Cat"], ["Pets|Cat"]) == ["France|Paris", "Dog", "Pets|Cat"]


def test_validate_name():
    assert kl.validate_name("  Big   sky ") == "Big sky"
    for bad in ("", "a|b", "a,b", "x\x01"):
        with pytest.raises(ValueError):
            kl.validate_name(bad)


def test_plan_rename_collision_needs_merge_and_blocks_cycles():
    st = {k: {"name": k.title(), "parent": p, "synonyms": s, "export": True}
          for k, p, s in (("a", "", []), ("b", "a", ["bee"]), ("c", "", ["see"]))}
    with pytest.raises(ValueError, match="already exists"):
        kl.plan_rename(st, "b", "C")
    with pytest.raises(ValueError, match="No such"):
        kl.plan_rename(st, "zz", "q")
    with pytest.raises(ValueError, match="own children"):
        kl.plan_rename(st, "a", "B", merge=True)
    plan = kl.plan_rename(st, "b", "C", merge=True)
    assert plan["moved"] == {"b": "c"} and "b" not in plan["state"]
    assert plan["state"]["c"]["synonyms"] == ["see", "bee"] and plan["state"]["c"]["parent"] == "a"
    plain = kl.plan_rename(st, "a", "Alpha")
    assert plain["state"]["alpha"]["name"] == "Alpha" and plain["state"]["b"]["parent"] == "alpha"
    assert st["a"]["name"] == "A"                                                  # planning never mutates


def test_plan_delete_moves_children_up():
    st = {"a": {"name": "A", "parent": "", "synonyms": [], "export": True},
          "b": {"name": "B", "parent": "a", "synonyms": [], "export": True},
          "c": {"name": "C", "parent": "b", "synonyms": [], "export": True}}
    plan = kl.plan_delete(st, "b")
    assert plan["state"]["c"]["parent"] == "a" and "b" not in plan["state"]
    ups = kl.photo_updates([it("1", ["C", "B"], ["A|B|C", "A|B"]), it("2", ["X"])], plan["moved"],
                           plan["state"], {"c"})
    assert ups == {"1": {"keywords": ["C"], "hier": ["A|C"]}}


# ------------------------------------------------------------------ real exiftool

def make_photos(root: Path, names):
    for n in names:
        (root / n).parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), (10, 20, 30)).save(root / n, format="TIFF")


def sidecar(path: Path) -> dict:
    xmp = path.with_suffix(".xmp")
    if not xmp.exists():
        return {}
    r = exifpool.run(["-j", "-XMP-dc:Subject", "-XMP-lr:HierarchicalSubject"], input=str(xmp))
    return json.loads(r.stdout)[0]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(libindex, "DEBOUNCE", 0.05)
    monkeypatch.setattr(libindex, "MAX_DELAY", 0.3)
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    make_photos(tmp_path, ["a.dng", "b.dng", "c.dng"])
    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        c.root = tmp_path
        c.idx = editor._idx()
        yield c
    libindex.shutdown_all()
    exifpool.shutdown()


def tag(c, paths, *words):
    r = c.post("/api/keywords", json={"paths": list(paths), "add": list(words)})
    assert r.status_code == 200, r.get_json()


def listing(c):
    return {r["key"]: r for r in c.get("/api/keywords/list").get_json()["keywords"]}


@needs_exiftool
@pytest.mark.exiftool
def test_hierarchy_written_alongside_subject_and_read_back(client):
    c = client
    assert c.post("/api/keywords/create", json={"name": "France"}).status_code == 200
    assert c.post("/api/keywords/create", json={"name": "Paris", "parent": "France"}).status_code == 200
    tag(c, ["a.dng", "b.dng"], "Paris")
    tag(c, ["b.dng"], "Beach")
    assert c.idx.flush()
    st = sidecar(c.root / "a.dng")
    assert st["Subject"] == "Paris" and st["HierarchicalSubject"] == "France|Paris"
    assert sidecar(c.root / "b.dng")["HierarchicalSubject"] == ["France|Paris", "Beach"]
    rows = listing(c)
    assert rows["paris"]["count"] == 2 and rows["france"]["total"] == 2 and rows["france"]["count"] == 0
    assert rows["beach"]["count"] == 1
    # a fresh index (restart) reads the hierarchy back from the sidecars
    libindex.shutdown_all()
    c.idx = editor._idx()
    c.idx.scan()
    assert c.idx.get("a.dng")["hier"] == ["France|Paris"]


@needs_exiftool
@pytest.mark.exiftool
def test_lightroom_sidecar_hierarchy_shows_in_list(client):
    c = client
    r = exifpool.run(["-XMP-dc:Subject=Paris", "-XMP-lr:HierarchicalSubject=France|Paris",
                      "-o", str(c.root / "c.xmp"), "/dev/null"])
    assert (c.root / "c.xmp").exists(), r.stderr
    c.idx.scan()
    rows = listing(c)
    assert rows["paris"]["parent"] == "France" and rows["france"]["total"] == 1


@needs_exiftool
@pytest.mark.exiftool
def test_rename_rewrites_every_sidecar_and_children_paths(client):
    c = client
    c.post("/api/keywords/create", json={"name": "France"})
    c.post("/api/keywords/create", json={"name": "Paris", "parent": "France"})
    tag(c, ["a.dng"], "Paris")
    tag(c, ["b.dng"], "France")
    tag(c, ["c.dng"], "Dog")
    dry = c.post("/api/keywords/rename", json={"keyword": "France", "new_name": "Frankreich", "dry_run": True})
    assert dry.get_json()["photos"] == 2 and "france" in listing(c)               # nothing changed yet
    r = c.post("/api/keywords/rename", json={"keyword": "france", "new_name": "Frankreich"})
    assert r.status_code == 200, r.get_json()
    assert c.idx.flush()
    assert sidecar(c.root / "a.dng")["HierarchicalSubject"] == "Frankreich|Paris"   # child path follows the parent
    assert sidecar(c.root / "b.dng")["Subject"] == "Frankreich"
    assert sidecar(c.root / "c.dng")["Subject"] == "Dog"
    rows = listing(c)
    assert "france" not in rows and rows["paris"]["parent"] == "Frankreich"
    assert json.loads((c.root / kl.STATE_NAME).read_text())["keywords"]["paris"]["parent"] == "frankreich"


@needs_exiftool
@pytest.mark.exiftool
def test_rename_collision_refused_then_merge(client):
    c = client
    tag(c, ["a.dng", "b.dng"], "Sea")
    tag(c, ["b.dng", "c.dng"], "Ocean")
    r = c.post("/api/keywords/rename", json={"keyword": "Sea", "new_name": "ocean"})
    assert r.status_code == 409
    assert c.idx.get("a.dng")["keywords"] == ["Sea"]                               # untouched
    r = c.post("/api/keywords/rename", json={"keyword": "Sea", "new_name": "ocean", "merge": True})
    assert r.status_code == 200 and r.get_json()["merged"] is True
    assert c.idx.flush()
    assert c.idx.get("b.dng")["keywords"] == ["Ocean"]                             # no duplicate
    assert sidecar(c.root / "a.dng")["Subject"] == "Ocean"
    assert listing(c)["ocean"]["count"] == 3 and "sea" not in listing(c)


@needs_exiftool
@pytest.mark.exiftool
def test_delete_keeps_children_and_other_keywords(client):
    c = client
    c.post("/api/keywords/create", json={"name": "France"})
    c.post("/api/keywords/create", json={"name": "Paris", "parent": "France"})
    tag(c, ["a.dng"], "France", "Paris", "Night")
    assert c.post("/api/keywords/delete", json={"keyword": "zzz"}).status_code == 400
    assert c.post("/api/keywords/delete", json={"keyword": "France", "dry_run": True}).get_json()["photos"] == 1
    assert "france" in listing(c)
    assert c.post("/api/keywords/delete", json={"keyword": "France"}).status_code == 200
    assert c.idx.flush()
    st = sidecar(c.root / "a.dng")
    assert st["Subject"] == ["Paris", "Night"] and st["HierarchicalSubject"] == ["Paris", "Night"]
    assert listing(c)["paris"]["parent"] == ""


@needs_exiftool
@pytest.mark.exiftool
def test_update_parent_synonyms_export_and_validation(client):
    c = client
    tag(c, ["a.dng"], "Paris", "France")
    assert c.post("/api/keywords/update", json={"keyword": "Paris", "parent": "France",
                                                 "synonyms": "Paname, City of Light", "export": False}).status_code == 200
    assert c.idx.flush()
    assert sidecar(c.root / "a.dng")["HierarchicalSubject"] == ["France|Paris", "France"]
    row = listing(c)["paris"]
    assert row["synonyms"] == ["Paname", "City of Light"] and row["export"] is False
    assert kl.unexported(c.root) == frozenset({"Paris"})
    # a keyword cannot become its own descendant
    assert c.post("/api/keywords/update", json={"keyword": "France", "parent": "Paris"}).status_code == 400
    assert c.post("/api/keywords/update", json={"keyword": "France", "parent": "Nowhere"}).status_code == 400
    assert c.post("/api/keywords/rename", json={"keyword": "France", "new_name": "a|b"}).status_code == 400
    assert c.post("/api/keywords/create", json={"name": "paris"}).status_code == 400


@needs_exiftool
@pytest.mark.exiftool
def test_virtual_copy_keywords_follow_a_rename(client):
    import catalog
    c = client
    tag(c, ["a.dng"], "Sea")
    vc = catalog.create_virtual(c.root, "a.dng", keywords=["Sea", "Sky"])
    r = c.post("/api/keywords/rename", json={"keyword": "Sea", "new_name": "Ocean"})
    assert r.get_json()["virtual_copies"] == 1
    assert catalog.get_virtual(c.root, vc["id"])["keywords"] == ["Ocean", "Sky"]


@needs_exiftool
@pytest.mark.exiftool
def test_export_drops_keywords_flagged_not_to_export(tmp_path):
    import exporter
    dest = tmp_path / "out.jpg"
    Image.new("RGB", (8, 8)).save(dest, "JPEG")
    exifpool.run([*library.xmp_args(keywords=["Public", "Private"], hier=["Secret|Private", "Public"]),
                  "-overwrite_original", str(dest)])
    kl.strip_for_export(dest, {"private"}, lambda a: exifpool.run(a, timeout=exporter.EXIFTOOL_TIMEOUT))
    r = json.loads(exifpool.run(["-j", "-XMP-dc:Subject", "-XMP-lr:HierarchicalSubject", str(dest)]).stdout)[0]
    assert r["Subject"] == "Public" and r["HierarchicalSubject"] == "Public"
    exifpool.shutdown()


@needs_exiftool
@pytest.mark.exiftool
def test_xmp_args_really_replace_existing_keywords(tmp_path):
    """Regression: '-Subject=' then '-Subject+=' left the old keywords in place on real exiftool."""
    side = tmp_path / "s.xmp"
    exifpool.run([*library.xmp_args(keywords=["A", "B"], hier=["X|A"]), "-o", str(side), "/dev/null"])
    exifpool.run([*library.xmp_args(keywords=["C"], hier=["C"]), "-overwrite_original", str(side)])
    got = json.loads(exifpool.run(["-j", "-XMP-dc:Subject", "-XMP-lr:HierarchicalSubject", str(side)]).stdout)[0]
    assert got["Subject"] == "C" and got["HierarchicalSubject"] == "C"
    exifpool.run([*library.xmp_args(keywords=[], hier=[]), "-overwrite_original", str(side)])
    got = json.loads(exifpool.run(["-j", "-XMP-dc:Subject", str(side)]).stdout)[0]
    assert "Subject" not in got
    exifpool.shutdown()
