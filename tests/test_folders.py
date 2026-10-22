"""Folders panel: tree, create, safe move / rename with sidecars and state, undo."""

import sqlite3
import subprocess
from pathlib import Path

import pytest

import catalog
import editor
import folders
import library
import security
from test_library_module import FakeExiftool


def make_photo(root: Path, rel: str, extra=()):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"raw-" + rel.encode())
    stem = p.with_suffix("")
    for suffix in (".xmp", *extra):
        Path(str(stem) + suffix).write_text("x" + suffix)
    for tail in (".photoroom.json", ".masks.json", ".vc-0123abcd.photoroom.json"):
        Path(str(p) + tail).write_text("{}" + tail)
    return p


def names(d: Path):
    return sorted(x.name for x in d.iterdir() if x.is_file() and not x.name.startswith("."))


# ----------------------------------------------------------------- path checks

@pytest.mark.parametrize("bad", ["../x", "/etc", "a/../../b", ".hidden/x", "a\\b", "a\0b",
                                 ".photo-room-catalog.json"])
def test_safe_relpath_refuses_escapes(tmp_path, bad):
    (tmp_path / "a").mkdir()
    with pytest.raises(ValueError):
        security.safe_relpath(tmp_path, bad, must_exist=False)


def test_safe_relpath_refuses_symlink_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "lib"
    root.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(ValueError):
        security.safe_relpath(root, "link")
    assert security.safe_relpath(root, "", allow_root=True) == root.resolve()


@pytest.mark.parametrize("bad", ["", "..", "a/b", ".x", "a:b", "x" * 200, None, 3])
def test_safe_name_refuses(bad):
    with pytest.raises(ValueError):
        security.safe_name(bad)


# ------------------------------------------------------------------------ tree

def test_tree_counts_and_empty_folders(tmp_path):
    make_photo(tmp_path, "a.dng")
    make_photo(tmp_path, "2024/b.dng")
    make_photo(tmp_path, "2024/may/c.dng")
    (tmp_path / "empty").mkdir()
    (tmp_path / ".photo-room-thumbs").mkdir()
    t = {f["path"]: f for f in folders.tree(tmp_path)}
    assert set(t) == {"", "2024", "2024/may", "empty"}
    assert (t[""]["count"], t[""]["total"]) == (1, 3)
    assert (t["2024"]["count"], t["2024"]["total"]) == (1, 2)
    assert t["empty"]["total"] == 0 and t["2024/may"]["depth"] == 2


def test_in_folder_with_and_without_subfolders():
    assert folders.in_folder("a/b.dng", "a", False)
    assert not folders.in_folder("a/x/b.dng", "a", False)
    assert folders.in_folder("a/x/b.dng", "a", True)
    assert folders.in_folder("a/b.dng#vc-1234abcd", "a", False)
    assert folders.in_folder("b.dng", "", False) and folders.in_folder("a/b.dng", "", True)
    assert not folders.in_folder("ab/b.dng", "a", True)


# ----------------------------------------------------------------- create/move

def test_create_folder_never_reuses(tmp_path):
    r = folders.create_folder(tmp_path, "", "Trip")
    assert (tmp_path / "Trip").is_dir() and r["path"] == "Trip"
    with pytest.raises(folders.MoveError):
        folders.create_folder(tmp_path, "", "Trip")
    with pytest.raises(folders.MoveError):
        folders.create_folder(tmp_path, "../", "x")
    with pytest.raises(folders.MoveError):
        folders.create_folder(tmp_path, "", "a/b")


def test_move_photo_takes_every_companion(tmp_path):
    make_photo(tmp_path, "IMG_1.dng", extra=(".CR2",))
    make_photo(tmp_path, "IMG_10.dng")
    (tmp_path / "dest").mkdir()
    r = folders.move_photos(tmp_path, ["IMG_1.dng"], "dest")
    assert r["mapping"]["IMG_1.dng"] == "dest/IMG_1.dng"
    assert names(tmp_path / "dest") == sorted([
        "IMG_1.CR2", "IMG_1.dng", "IMG_1.dng.masks.json", "IMG_1.dng.photoroom.json",
        "IMG_1.dng.vc-0123abcd.photoroom.json", "IMG_1.xmp"])
    # IMG_10 is a different photo and stays put, with its own sidecars
    assert "IMG_10.dng" in names(tmp_path) and "IMG_10.xmp" in names(tmp_path)
    assert not (tmp_path / "IMG_1.xmp").exists()


def test_move_never_overwrites_and_is_all_or_nothing(tmp_path):
    make_photo(tmp_path, "a.dng")
    make_photo(tmp_path, "b.dng")
    make_photo(tmp_path, "dest/b.dng")
    before = (tmp_path / "dest" / "b.dng").read_bytes()
    with pytest.raises(folders.MoveError, match="already exists"):
        folders.move_photos(tmp_path, ["a.dng", "b.dng"], "dest")
    assert (tmp_path / "a.dng").exists() and (tmp_path / "b.dng").exists()
    assert not (tmp_path / "dest" / "a.dng").exists()
    assert (tmp_path / "dest" / "b.dng").read_bytes() == before


def test_move_refuses_outside_and_non_photos(tmp_path):
    make_photo(tmp_path, "a.dng")
    (tmp_path / "notes.txt").write_text("hi")
    (tmp_path / "x").mkdir()
    for paths, dest in ((["a.dng"], "../elsewhere"), (["../a.dng"], ""), (["notes.txt"], "x"),
                        (["a.dng"], "missing"), ([], "x")):
        with pytest.raises(folders.MoveError):
            folders.move_photos(tmp_path, paths, dest)
    assert (tmp_path / "notes.txt").read_text() == "hi"


def test_rollback_when_a_move_fails_midway(tmp_path, monkeypatch):
    make_photo(tmp_path, "a.dng")
    (tmp_path / "dest").mkdir()
    real = folders._move_one
    calls = []

    def flaky(src, dst):
        calls.append(src)
        if len(calls) == 3:
            raise OSError(5, "disk on fire")
        real(src, dst)
    monkeypatch.setattr(folders, "_move_one", flaky)
    with pytest.raises(folders.MoveError):
        folders.move_photos(tmp_path, ["a.dng"], "dest")
    monkeypatch.setattr(folders, "_move_one", real)
    assert list((tmp_path / "dest").iterdir()) == []
    assert (tmp_path / "a.dng").exists() and (tmp_path / "a.xmp").exists()


def test_rename_photo_renames_companions(tmp_path):
    make_photo(tmp_path, "old.dng")
    folders.rename_photo(tmp_path, "old.dng", "new.dng")
    assert names(tmp_path) == sorted(["new.dng", "new.xmp", "new.dng.masks.json", "new.dng.photoroom.json",
                                      "new.dng.vc-0123abcd.photoroom.json"])
    make_photo(tmp_path, "other.dng")
    with pytest.raises(folders.MoveError):
        folders.rename_photo(tmp_path, "other.dng", "new")
    with pytest.raises(folders.MoveError):
        folders.rename_photo(tmp_path, "other.dng", "../evil")


def test_folder_move_and_rename_keep_contents_and_guard_cycles(tmp_path):
    make_photo(tmp_path, "a/p.dng")
    make_photo(tmp_path, "a/sub/q.dng")
    (tmp_path / "b").mkdir()
    with pytest.raises(folders.MoveError):
        folders.move_folder(tmp_path, "a", "a/sub")
    with pytest.raises(folders.MoveError):
        folders.move_folder(tmp_path, "a", "")           # already there
    r = folders.move_folder(tmp_path, "a", "b")
    assert r["mapping"] == {"a/p.dng": "b/a/p.dng", "a/sub/q.dng": "b/a/sub/q.dng"}
    assert (tmp_path / "b/a/sub/q.dng.photoroom.json").exists()
    folders.rename_folder(tmp_path, "b/a", "z")
    assert (tmp_path / "b/z/p.dng.photoroom.json").exists()
    make_photo(tmp_path, "c/x.dng")
    with pytest.raises(folders.MoveError, match="already exists"):
        folders.rename_folder(tmp_path, "c", "b")


# ----------------------------------------------------------------------- state

def seed_state(root: Path):
    entry = catalog.create_virtual(root, "a/p.dng", adjustments={"exposure": 1}, rating=3)
    col = catalog.create_collection(root, "Best")
    catalog.add_photos(root, col["id"], ["a/p.dng", entry["id"], "a/other.dng"])
    library.set_picks(root, ["a/p.dng"], True)
    library.remember_pre_reject(root, {"a/p.dng": 4})
    db = sqlite3.connect(root / ".photo-room-cull.sqlite")
    db.execute("CREATE TABLE features (path TEXT PRIMARY KEY, fkey TEXT, data TEXT)")
    db.execute("CREATE TABLE decisions (id INTEGER PRIMARY KEY, path TEXT)")
    db.execute("CREATE TABLE autoedit (path TEXT PRIMARY KEY)")
    db.execute("CREATE TABLE offsets (path TEXT PRIMARY KEY)")
    db.execute("INSERT INTO features VALUES('a/p.dng','k','{}')")
    db.execute("INSERT INTO decisions(path) VALUES('a/p.dng')")
    db.commit()
    db.close()
    return entry, col


def test_state_follows_the_file_and_undo_restores_it(tmp_path):
    make_photo(tmp_path, "a/p.dng")
    entry, col = seed_state(tmp_path)
    (tmp_path / "b").mkdir()
    folders.move_photos(tmp_path, [entry["id"]], "b")        # a copy id moves its raw
    new_id = "b/p.dng#" + entry["vid"]
    store = catalog.load(tmp_path)
    assert list(store["virtual"]) == [new_id]
    v = store["virtual"][new_id]
    assert v["source"] == "b/p.dng" and v["id"] == new_id and v["adjustments"] == {"exposure": 1}
    assert store["collections"][0]["photos"] == ["b/p.dng", new_id, "a/other.dng"]
    lib = library.load_store(tmp_path)
    assert lib["flags"] == {"b/p.dng": "pick"} and lib["pre_reject"] == {"b/p.dng": 4}
    db = sqlite3.connect(tmp_path / ".photo-room-cull.sqlite")
    assert db.execute("SELECT path FROM features").fetchall() == [("b/p.dng",)]
    assert db.execute("SELECT path FROM decisions").fetchall() == [("b/p.dng",)]
    db.close()
    assert (tmp_path / "b" / "p.dng.vc-0123abcd.photoroom.json").exists()

    undone = folders.undo_last(tmp_path)
    assert (tmp_path / "a/p.dng").exists() and not (tmp_path / "b/p.dng").exists()
    assert list(catalog.load(tmp_path)["virtual"]) == [entry["id"]]
    assert catalog.load(tmp_path)["collections"][0]["photos"] == ["a/p.dng", entry["id"], "a/other.dng"]
    assert library.load_store(tmp_path)["flags"] == {"a/p.dng": "pick"}
    assert undone["label"].startswith("Undid")
    with pytest.raises(folders.MoveError, match="Nothing to undo"):
        folders.undo_last(tmp_path)


def test_folder_rename_remaps_state_and_undo(tmp_path):
    make_photo(tmp_path, "a/p.dng")
    entry, col = seed_state(tmp_path)
    folders.rename_folder(tmp_path, "a", "renamed")
    assert list(catalog.load(tmp_path)["virtual"]) == ["renamed/p.dng#" + entry["vid"]]
    folders.undo_last(tmp_path)
    assert (tmp_path / "a/p.dng.photoroom.json").exists()
    assert list(catalog.load(tmp_path)["virtual"]) == [entry["id"]]


def test_undo_refuses_to_overwrite_and_mkdir_undo_only_removes_empty(tmp_path):
    make_photo(tmp_path, "a.dng")
    (tmp_path / "d").mkdir()
    folders.move_photos(tmp_path, ["a.dng"], "d")
    make_photo(tmp_path, "a.dng")                 # something new took the old place
    with pytest.raises(folders.MoveError, match="already exists"):
        folders.undo_last(tmp_path)
    assert (tmp_path / "d/a.dng").exists()

    root2 = tmp_path / "r2"
    root2.mkdir()
    folders.create_folder(root2, "", "new")
    (root2 / "new" / "keep.txt").write_text("x")
    with pytest.raises(folders.MoveError, match="no longer empty"):
        folders.undo_last(root2)
    assert (root2 / "new" / "keep.txt").exists()


# ------------------------------------------------------------------------- API

@pytest.fixture
def lib(tmp_path, monkeypatch, fake_rawpy):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    monkeypatch.setenv("PHOTO_ROOM_EMBED_DNG", "1")
    make_photo(tmp_path, "a.dng")
    make_photo(tmp_path, "2024/b.dng")
    tool = FakeExiftool(tmp_path)
    tool.meta = {"a.dng": {"Rating": 4}, "2024/b.dng": {"Rating": 2},
                 "2024/a.dng": {"Rating": 4}}
    monkeypatch.setattr(subprocess, "run", tool)
    library.clear_cache()
    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        c.root = tmp_path
        yield c
    library.clear_cache()


def test_api_tree_filter_move_and_undo(lib):
    tree = lib.get("/api/folders").get_json()
    assert {f["path"]: f["total"] for f in tree["folders"]} == {"": 2, "2024": 1}

    got = lib.get("/api/library", query_string={"folder": "2024"}).get_json()
    assert [i["path"] for i in got["items"]] == ["2024/b.dng"]
    got = lib.get("/api/library", query_string={"folder": "", "subfolders": "1"}).get_json()
    assert got["total"] == 2
    got = lib.get("/api/library", query_string={"folder": ""}).get_json()
    assert [i["path"] for i in got["items"]] == ["a.dng"]

    r = lib.post("/api/folders/move", json={"paths": ["a.dng"], "dest": "2024"})
    assert r.status_code == 200 and r.get_json()["undo"]["op"]
    assert (lib.root / "2024/a.dng.photoroom.json").exists()
    items = lib.get("/api/library").get_json()["items"]
    byp = {i["path"]: i for i in items}
    assert set(byp) == {"2024/a.dng", "2024/b.dng"}

    r = lib.post("/api/folders/undo", json={})
    assert r.status_code == 200
    assert {i["path"] for i in lib.get("/api/library").get_json()["items"]} == {"a.dng", "2024/b.dng"}


def test_api_errors_and_guards(lib):
    assert lib.post("/api/folders/move", json={"paths": ["a.dng"], "dest": "../"}).status_code == 400
    assert lib.post("/api/folders/create", json={"parent": "", "name": "2024"}).status_code == 400
    assert lib.post("/api/folders/rename", json={"path": "../a.dng", "name": "x"}).status_code == 400
    assert lib.post("/api/folders/move", json={"paths": ["a.dng"], "dest": "2024"},
                    headers={"Origin": "https://evil.example"}).status_code == 403
    assert lib.post("/api/folders/undo", data="{}", content_type="text/plain").status_code == 400
    assert (lib.root / "a.dng").exists()
    r = lib.post("/api/folders/create", json={"parent": "2024", "name": "new"})
    assert r.status_code == 200 and (lib.root / "2024/new").is_dir()
    assert lib.post("/api/folders/rename", json={"path": "2024/b.dng", "name": "c"}).status_code == 200
    assert (lib.root / "2024/c.dng.photoroom.json").exists()
