"""Lightroom .lrcat import, against a small synthetic catalog (no real catalogs available)."""

import hashlib
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

import catalog
import editor
import library
import lrimport

needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")

DEVELOP = '''s = {
	AutoLateralCA = 0,
	Contrast2012 = 25,
	CropBottom = 0.9,
	CropLeft = 0.1,
	CropRight = 0.95,
	CropTop = 0.05,
	CropAngle = -1.5,
	Exposure2012 = 0.65,
	HasCrop = true,
	HueAdjustmentRed = -12,
	ProcessVersion = "11.0",
	Temperature = 4800,
	Tint = 7,
	ToneCurvePV2012 = { 0, 0, 128, 140, 255, 255, },
	Vibrance = 30,
	WhiteBalance = "Custom",
	GrainAmount = 20,
	Version = "15.4",
}'''


def make_catalog(path: Path, root: str, extra_schema_drift: bool = False):
    db = sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE AgLibraryRootFolder (id_local INTEGER PRIMARY KEY, absolutePath TEXT, name TEXT);
    CREATE TABLE AgLibraryFolder (id_local INTEGER PRIMARY KEY, pathFromRoot TEXT, rootFolder INTEGER);
    CREATE TABLE AgLibraryFile (id_local INTEGER PRIMARY KEY, baseName TEXT, extension TEXT, folder INTEGER, originalFilename TEXT);
    CREATE TABLE Adobe_images (id_local INTEGER PRIMARY KEY, rootFile INTEGER, rating REAL, pick REAL,
                               colorLabels TEXT, masterImage INTEGER, copyName TEXT);
    CREATE TABLE Adobe_imageDevelopSettings (id_local INTEGER PRIMARY KEY, image INTEGER, text TEXT);
    CREATE TABLE AgLibraryKeyword (id_local INTEGER PRIMARY KEY, name TEXT, parent INTEGER);
    CREATE TABLE AgLibraryKeywordImage (id_local INTEGER PRIMARY KEY, image INTEGER, tag INTEGER);
    CREATE TABLE AgLibraryCollection (id_local INTEGER PRIMARY KEY, name TEXT, parent INTEGER, creationId TEXT);
    CREATE TABLE AgLibraryCollectionImage (id_local INTEGER PRIMARY KEY, collection INTEGER, image INTEGER);
    CREATE TABLE AgLibraryFolderStackImage (id_local INTEGER PRIMARY KEY, stack INTEGER, image INTEGER, position INTEGER);
    """)
    db.execute("INSERT INTO AgLibraryRootFolder VALUES (1, ?, 'photos')", (root,))
    db.execute("INSERT INTO AgLibraryFolder VALUES (1, 'trip/', 1)")
    files = [(1, "IMG_0001", "CR2"), (2, "IMG_0002", "CR2"), (3, "IMG_0003", "CR2"), (4, "GONE", "CR2")]
    for fid, base, ext in files:
        db.execute("INSERT INTO AgLibraryFile VALUES (?,?,?,1,?)", (fid, base, ext, f"{base}.{ext}"))
    # id, file, rating, pick, label, master, copyName
    db.executemany("INSERT INTO Adobe_images VALUES (?,?,?,?,?,?,?)", [
        (10, 1, 4.0, 1.0, "Red", None, None),
        (20, 2, None, -1.0, "", None, None),
        (30, 3, 2.0, 0.0, "Purple", None, None),
        (40, 4, 5.0, 0.0, "", None, None),               # not on disk
        (11, 1, 5.0, 0.0, "Blue", 10, "Copy 1"),         # virtual copy of 10
    ])
    db.execute("INSERT INTO Adobe_imageDevelopSettings VALUES (1, 10, ?)", (DEVELOP,))
    db.execute("INSERT INTO Adobe_imageDevelopSettings VALUES (2, 11, ?)", ("s = { Exposure2012 = -1.0, }",))
    db.executemany("INSERT INTO AgLibraryKeyword VALUES (?,?,?)", [
        (1, None, None), (2, "Places", 1), (3, "Italy", 2), (4, "Rome", 3), (5, "family", 1)])
    db.executemany("INSERT INTO AgLibraryKeywordImage VALUES (?,?,?)", [(1, 10, 4), (2, 10, 5), (3, 30, 3)])
    db.executemany("INSERT INTO AgLibraryCollection VALUES (?,?,?,?)", [
        (1, "2023", None, "com.adobe.ag.library.group"),
        (2, "Best", 1, "com.adobe.ag.library.collection"),
        (3, "Rated", None, "com.adobe.ag.library.smart_collection"),
        (4, "Plain", None, "com.adobe.ag.library.collection")])
    db.executemany("INSERT INTO AgLibraryCollectionImage VALUES (?,?,?)", [(1, 2, 10), (2, 2, 11), (3, 2, 30), (4, 4, 40)])
    db.executemany("INSERT INTO AgLibraryFolderStackImage VALUES (?,?,?,?)", [(1, 1, 20, 1), (2, 1, 30, 2)])
    if extra_schema_drift:
        db.execute("ALTER TABLE Adobe_images ADD COLUMN somethingNew TEXT")
    db.commit()
    db.close()


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "library"
    (root / "trip").mkdir(parents=True)
    for n in ("IMG_0001", "IMG_0002", "IMG_0003"):
        (root / "trip" / f"{n}.CR2").write_bytes(n.encode())
    return root


def digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def exif(path: Path, *tags):
    out = subprocess.run(["exiftool", "-s", "-s", "-s", "-sep", "; ", *tags, str(path)],
                         capture_output=True, text=True, timeout=30).stdout
    return [line for line in out.split("\n") if line]


# ------------------------------------------------------------------ parsing

def test_lua_table_parses_scalars_nested_and_comments():
    s = lrimport.parse_develop_text(DEVELOP)
    assert s["Exposure2012"] == 0.65 and s["HasCrop"] is True and s["WhiteBalance"] == "Custom"
    assert s["ToneCurvePV2012"] == [0, 0, 128, 140, 255, 255]
    nested = lrimport.parse_develop_text('x = { A = { ["k"] = 1, 2 }, -- c\n B = "q\\"z" }')
    assert nested["B"] == 'q"z' and nested["A"]["_array"] == [2] and nested["A"]["k"] == 1


def test_damaged_or_compressed_text_is_tolerated():
    assert lrimport.parse_develop_text("s = { Exposure2012 = 1.5, Contrast2012 = ") == {"Exposure2012": 1.5}
    assert lrimport.parse_develop_text("garbage") == {}
    import zlib
    assert lrimport.parse_develop_text(zlib.compress(b"s = { Tint = 3 }"))["Tint"] == 3
    assert lrimport.parse_develop_text(None) == {}


def test_settings_map_and_unsupported_are_listed():
    tags, adj, unsupported = lrimport.map_settings(lrimport.parse_develop_text(DEVELOP))
    assert adj["exposure"] == 0.65 and adj["color_temp"] == 4800 and adj["straighten"] == -1.5
    assert adj["crop"] == {"left": 0.1, "top": 0.05, "right": 0.95, "bottom": 0.9}
    assert adj["hue_red"] == -12
    assert tags["ColorTemperature"] == "4800" and tags["WhiteBalance"] == "Custom"
    assert unsupported == ["GrainAmount", "ToneCurvePV2012"]       # AutoLateralCA = 0 is a default


# ------------------------------------------------------------------ matching and report

def test_dry_run_report_counts_and_never_writes(tmp_path, lib):
    cat = tmp_path / "Lightroom Catalog.lrcat"
    make_catalog(cat, "/old/place/photos/")
    before = digest(cat)
    plan = lrimport.build_plan(cat, lib, ["/old/place/photos=" + str(lib)])
    r = plan["report"]
    assert (r["photos_in_catalog"], r["matched"], r["unmatched"]) == (4, 3, 1)
    assert r["match_methods"] == {"remap": 3} and "GONE.CR2" in r["unmatched_files"][0]
    assert r["virtual_copies"] == 1 and r["collections"] == 2 and r["collection_sets_flattened"] == 1
    assert r["smart_collections_skipped"] == ["Rated"] and r["stacks"] == 1
    assert r["rejected"] == 1 and r["picked"] == 1 and r["with_keywords"] == 2
    assert r["unsupported_settings"] == {"GrainAmount": 1, "ToneCurvePV2012": 1}
    text = lrimport.format_report(r)
    assert "GrainAmount" in text and "Rated" in text
    assert not list(lib.rglob("*.xmp")) and not (lib / catalog.STORE_NAME).exists()
    assert digest(cat) == before and list(tmp_path.glob("*-journal")) == []


def test_unmapped_paths_match_by_library_relative_folder(tmp_path, lib):
    cat = tmp_path / "c.lrcat"
    make_catalog(cat, "D:\\Pictures\\photos\\")      # a Windows path, no remap
    r = lrimport.build_plan(cat, lib)["report"]
    assert r["matched"] == 3 and r["match_methods"] == {"library-relative": 3}


def test_folder_argument_finds_newest_catalog_and_skips_backups(tmp_path):
    (tmp_path / "Backups" / "x").mkdir(parents=True)
    make_catalog(tmp_path / "Backups" / "x" / "old.lrcat", "/a/")
    make_catalog(tmp_path / "main.lrcat", "/a/")
    assert lrimport.find_catalog(tmp_path).name == "main.lrcat"
    with pytest.raises(FileNotFoundError):
        lrimport.find_catalog(tmp_path / "Backups" / "nothing")


def test_schema_differences_and_missing_tables_are_tolerated(tmp_path, lib):
    cat = tmp_path / "c.lrcat"
    make_catalog(cat, str(lib) + "/..//library/photos/")
    db = sqlite3.connect(cat)
    for t in ("AgLibraryKeyword", "AgLibraryCollection", "AgLibraryFolderStackImage", "Adobe_imageDevelopSettings"):
        db.execute(f"DROP TABLE {t}")
    db.execute("ALTER TABLE Adobe_images RENAME COLUMN copyName TO copyLabel")
    db.commit()
    db.close()
    r = lrimport.build_plan(cat, lib)["report"]
    assert r["matched"] == 3 and r["collections"] == 0 and r["stacks"] == 0 and r["with_develop_settings"] == 0


def test_not_a_catalog_is_a_clear_error(tmp_path, lib):
    bad = tmp_path / "x.lrcat"
    bad.write_bytes(b"this is not sqlite")
    with pytest.raises(ValueError, match="not a readable Lightroom catalog|no Adobe_images"):
        lrimport.build_plan(bad, lib)
    other = tmp_path / "y.lrcat"
    sqlite3.connect(other).execute("CREATE TABLE t (a)").connection.close()
    with pytest.raises(ValueError, match="no Adobe_images"):
        lrimport.build_plan(other, lib)


def test_bad_remap_is_rejected():
    with pytest.raises(ValueError):
        lrimport.parse_remaps(["nonsense"])


# ------------------------------------------------------------------ applying

@needs_exiftool
def test_apply_writes_sidecars_store_collections_and_virtual_copies(tmp_path, lib):
    cat = tmp_path / "Lightroom Catalog.lrcat"
    make_catalog(cat, str(lib) + "/")
    raws = {p.name: digest(p) for p in lib.rglob("*.CR2")}
    plan = lrimport.build_plan(cat, lib)
    done = lrimport.apply_plan(plan)
    assert done["sidecars_written"] == 3 and not done.get("errors"), done
    assert {p.name: digest(p) for p in lib.rglob("*.CR2")} == raws          # raws untouched

    side = lib / "trip" / "IMG_0001.xmp"
    assert exif(side, "-XMP-xmp:Rating", "-XMP-xmp:Label") == ["4", "Red"]
    assert sorted(exif(side, "-XMP-dc:Subject")[0].split("; ")) == ["Rome", "family"]
    assert "Places|Italy|Rome" in exif(side, "-XMP-lr:HierarchicalSubject")[0]
    assert "family" in exif(side, "-XMP-lr:HierarchicalSubject")[0]
    assert exif(side, "-XMP-crs:Exposure2012", "-XMP-crs:ColorTemperature", "-XMP-crs:HasCrop") == ["0.65", "4800", "True"]
    assert exif(lib / "trip" / "IMG_0002.xmp", "-XMP-xmp:Rating") == ["-1"]      # reject

    assert library.get_pick(lib, "trip/IMG_0001.CR2") and not library.get_pick(lib, "trip/IMG_0002.CR2")
    store = catalog.load(lib)
    vcs = list(store["virtual"].values())
    assert len(vcs) == 1 and vcs[0]["source"] == "trip/IMG_0001.CR2" and vcs[0]["rating"] == 5
    assert vcs[0]["label"] == "Blue" and vcs[0]["adjustments"] == {"exposure": -1.0}
    cols = {c["name"]: c for c in store["collections"]}
    assert set(cols) == {"2023 / Best"}                                       # Plain's photo is not on disk
    assert set(cols["2023 / Best"]["photos"]) == {"trip/IMG_0001.CR2", vcs[0]["id"], "trip/IMG_0003.CR2"}
    rec = (lib / lrimport.IMPORT_RECORD).read_text()
    assert "trip/IMG_0002.CR2" in rec and "Rated" in rec

    # second run: nothing duplicated, existing sidecars kept
    again = lrimport.apply_plan(lrimport.build_plan(cat, lib))
    assert again["sidecars_kept"] == 3 and again["virtual_copies_existing"] == 1
    assert len(catalog.load(lib)["virtual"]) == 1
    assert len(catalog.load(lib)["collections"]) == 1


@needs_exiftool
def test_existing_sidecar_is_kept_unless_forced_and_foreign_tags_survive(tmp_path, lib):
    cat = tmp_path / "c.lrcat"
    make_catalog(cat, str(lib) + "/")
    side = lib / "trip" / "IMG_0001.xmp"
    subprocess.run(["exiftool", "-XMP-xmp:Rating=1", "-XMP-crs:Tint=-3", "-XMP-xmp:Nickname=keepme",
                    "-o", str(side), "/dev/null"], capture_output=True, timeout=30)
    original = side.read_bytes()
    plan = lrimport.build_plan(cat, lib)
    assert plan["report"]["sidecars_kept"] == 1
    lrimport.apply_plan(plan)
    assert side.read_bytes() == original

    lrimport.apply_plan(lrimport.build_plan(cat, lib, force=True))
    assert exif(side, "-XMP-xmp:Rating", "-XMP-xmp:Nickname") == ["4", "keepme"]
    assert exif(side, "-XMP-crs:Tint") == ["7"]


def test_sidecar_write_uses_bounded_exiftool_calls(tmp_path, lib, monkeypatch):
    cat = tmp_path / "c.lrcat"
    make_catalog(cat, str(lib) + "/")
    calls = []

    def fake_run(args, input=None, timeout=None, binary=False, files=None):
        calls.append((args, timeout, files))
        if "-o" in args:
            Path(args[args.index("-o") + 1]).write_text("x")
        return subprocess.CompletedProcess(["exiftool"], 0, stdout="", stderr="")

    monkeypatch.setattr(lrimport.exifpool, "run", fake_run)
    lrimport.apply_plan(lrimport.build_plan(cat, lib))
    assert len(calls) == 3 and all(t == lrimport.exifpool.DEFAULT_TIMEOUT for _, t, _ in calls)
    first = next(a for a, _, f in calls if "-XMP-xmp:Label=Red" in a)
    assert "-XMP-lr:HierarchicalSubject+=Places|Italy|Rome" in first


# ------------------------------------------------------------------ CLI and API

def test_cli_dry_run_and_needs_yes(tmp_path, lib, capsys):
    cat = tmp_path / "c.lrcat"
    make_catalog(cat, str(lib) + "/")
    assert lrimport.main([str(cat), "--library", str(lib), "--dry-run"]) == 0
    assert "Dry run" in capsys.readouterr().out and not list(lib.rglob("*.xmp"))
    assert lrimport.main([str(cat), "--library", str(lib)]) == 1             # not a tty, no --yes
    assert "Nothing written" in capsys.readouterr().out and not list(lib.rglob("*.xmp"))
    assert lrimport.main([str(tmp_path / "missing"), "--library", str(lib)]) == 2


@needs_exiftool
def test_cli_yes_applies(tmp_path, lib, capsys):
    cat = tmp_path / "c.lrcat"
    make_catalog(cat, "/old/")
    assert lrimport.main([str(tmp_path), "--library", str(lib), "--remap", f"/old={lib}", "--yes"]) == 0
    assert (lib / "trip" / "IMG_0001.xmp").exists()


def test_api_reports_then_applies(tmp_path, lib, monkeypatch):
    cat = tmp_path / "c.lrcat"
    make_catalog(cat, str(lib) + "/")
    monkeypatch.setattr(editor, "LIBRARY_ROOT", lib)
    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        r = c.post("/api/import/lightroom", json={"path": str(cat)}).get_json()
        assert r["report"]["matched"] == 3 and r["applied"] is None and "Photos in catalog" in r["text"]
        assert not list(lib.rglob("*.xmp"))
        assert "error" in c.post("/api/import/lightroom", json={}).get_json()
        assert "error" in c.post("/api/import/lightroom", json={"path": str(tmp_path / "nope")}).get_json()
