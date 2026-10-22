"""Import dialog backend: templates, sidecars, selection, backup copy, metadata, API and path checks."""
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import cardwatch
import editor
import import_api
import importkit
import ingest
from cardwatch import Card, Options


@pytest.fixture(autouse=True)
def fake_date(monkeypatch):
    monkeypatch.setattr(cardwatch, "_shoot_date", lambda p: ("2026", "09", "14"))
    monkeypatch.setattr(importkit, "frame_info", lambda p, cam=False, tm=False, date_fn=None: {
        "yyyy": "2026", "mm": "09", "dd": "14", "time": "101500", "camera": "EOS-R5"})


@pytest.fixture
def exif_calls(monkeypatch):
    """A stand-in for library._run_exiftool that records arguments and creates the sidecar."""
    import library
    calls = []

    def run(args):
        calls.append(list(args))
        tags = " ".join(a for a in args if a.startswith("-XMP"))
        if "-o" in args:
            Path(args[args.index("-o") + 1]).write_text("<xmp " + tags + "/>")
        else:
            target = Path(args[-1])
            target.write_text(target.read_text() + " " + tags)
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(library, "_run_exiftool", run)
    return calls


def make_card(root, n=3, ext=".CR2", sidecar=()):
    card = root / "card"
    folder = card / "DCIM" / "100CANON"
    folder.mkdir(parents=True)
    for i in range(n):
        (folder / f"IMG_{i:04d}{ext}").write_bytes(b"raw-%d-" % i * 200)
    for i in sidecar:
        (folder / f"IMG_{i:04d}.xmp").write_text(f"<x:xmpmeta source='camera {i}'/>")
    return card


def run(card, lib, **opt):
    return cardwatch.import_card(card, lib, spec=importkit.ImportSpec(**opt))


# ---- templates --------------------------------------------------------------------
def test_rename_and_layout_tokens():
    info = {"yyyy": "2026", "mm": "09", "dd": "14", "time": "101500", "camera": "EOS-R5"}
    assert importkit.render_name("{date}_{camera}_{seq:3}", info, 7, "IMG_1.CR2") == "2026-09-14_EOS-R5_007.CR2"
    assert importkit.render_name("{original}-{time}", info, 1, "IMG_1.CR2") == "IMG_1-101500.CR2"
    assert importkit.render_name("", info, 1, "IMG_1.CR2") == "IMG_1.CR2"
    assert importkit.render_layout("{yyyy}/{camera}/{date}", info) == "2026/EOS-R5/2026-09-14"
    assert importkit.render_name("{seq}", info, 5, "a.dng") == "0005.dng"


def test_hostile_values_cannot_leave_the_folder():
    info = {"yyyy": "2026", "mm": "09", "dd": "14", "time": "0", "camera": "../../etc/x"}
    out = importkit.render_layout("{camera}", info)
    assert ".." not in out.split("/") and "/" not in out
    assert "/" not in importkit.render_name("{camera}", info, 1, "a.CR2")


@pytest.mark.parametrize("bad", ["../x", "/abs", "{foo}", "a/../b", "{yyyy", "{seq}/x"])
def test_bad_layouts(bad):
    with pytest.raises(importkit.TemplateError):
        importkit.resolve_layout(bad)


@pytest.mark.parametrize("bad", ["a/b", "{nope}", "{seq:99}", "{date", "x\\y"])
def test_bad_rename(bad):
    with pytest.raises(importkit.TemplateError):
        importkit.resolve_rename(bad)


# ---- import_card with a spec --------------------------------------------------------
def test_rename_template_numbers_in_card_order_and_keeps_extension(tmp_path):
    card, lib = make_card(tmp_path), tmp_path / "lib"
    res = run(card, lib, layout="flat", rename="{date}_{seq:3}", seq_start=10)
    assert res.copied == 3
    assert sorted(p.name for p in lib.iterdir()) == ["2026-09-14_010.CR2", "2026-09-14_011.CR2", "2026-09-14_012.CR2"]


def test_rename_never_overwrites_a_different_file(tmp_path):
    card, lib = make_card(tmp_path, n=1), tmp_path / "lib"
    lib.mkdir()
    (lib / "shot.CR2").write_bytes(b"someone else")
    res = run(card, lib, layout="flat", rename="shot")
    assert res.renamed == 1
    assert (lib / "shot.CR2").read_bytes() == b"someone else"
    assert (lib / "shot-1.CR2").exists()


def test_reimport_with_rename_is_still_a_duplicate_by_content(tmp_path):
    card, lib = make_card(tmp_path), tmp_path / "lib"
    run(card, lib, layout="flat", rename="{seq}")
    res = run(card, lib, layout="flat", rename="{seq}")
    assert res.copied == 0 and res.duplicates == 3
    assert len(list(lib.glob("*.CR2"))) == 3


def test_only_selected_frames_are_imported(tmp_path):
    card, lib = make_card(tmp_path), tmp_path / "lib"
    pick = {str(card / "DCIM/100CANON/IMG_0001.CR2")}
    res = cardwatch.import_card(card, lib, "flat", only=pick)
    assert res.found == 1 and res.copied == 1
    assert [p.name for p in lib.glob("*.CR2")] == ["IMG_0001.CR2"]


def test_sidecar_travels_with_the_raw_and_follows_a_rename(tmp_path):
    card, lib = make_card(tmp_path, sidecar=(0, 2)), tmp_path / "lib"
    before = (card / "DCIM/100CANON/IMG_0000.xmp").read_text()
    res = run(card, lib, layout="flat", rename="n{seq}")
    assert res.sidecars == 2
    assert (lib / "n0001.xmp").read_text() == before
    assert (lib / "n0003.xmp").exists() and not (lib / "n0002.xmp").exists()
    assert (card / "DCIM/100CANON/IMG_0000.xmp").read_text() == before        # card untouched


def test_sidecar_never_overwrites_an_existing_one_and_can_be_turned_off(tmp_path):
    card, lib = make_card(tmp_path, n=1, sidecar=(0,)), tmp_path / "lib"
    lib.mkdir()
    (lib / "IMG_0000.xmp").write_text("mine")
    run(card, lib, layout="flat")
    assert (lib / "IMG_0000.xmp").read_text() == "mine"
    lib2 = tmp_path / "lib2"
    run(card, lib2, layout="flat", sidecars=False)
    assert not (lib2 / "IMG_0000.xmp").exists()


def test_find_sidecar_variants(tmp_path):
    (tmp_path / "A.CR2").write_bytes(b"x")
    (tmp_path / "A.CR2.xmp").write_text("x")
    assert ingest.find_sidecar(tmp_path / "A.CR2").name == "A.CR2.xmp"


def test_backup_copy_is_made_verified_and_mirrors_layout(tmp_path):
    card, lib, bak = make_card(tmp_path, sidecar=(1,)), tmp_path / "lib", tmp_path / "bak"
    res = run(card, lib, layout="date", backup=bak)
    cardwatch.verify_import(res)
    assert res.clean and len(res.backups) == 3
    for name in ("IMG_0000.CR2", "IMG_0001.CR2", "IMG_0001.xmp"):
        assert (bak / "2026/09/14" / name).read_bytes() == (lib / "2026/09/14" / name).read_bytes()


def test_corrupt_backup_blocks_clean(tmp_path):
    card, lib, bak = make_card(tmp_path, n=1), tmp_path / "lib", tmp_path / "bak"
    res = run(card, lib, layout="flat", backup=bak)
    next(bak.glob("*.CR2")).write_bytes(b"rot")
    cardwatch.verify_import(res)
    assert not res.clean and "backup" in res.verify_failed[0]["error"]
    assert cardwatch.eject_blockers(res, True)


def test_failed_backup_is_a_failure_not_a_silent_skip(tmp_path, monkeypatch):
    card, lib, bak = make_card(tmp_path, n=1), tmp_path / "lib", tmp_path / "bak"
    real = ingest.copy_from_card_ex

    def flaky(src, library, *a, **k):
        if str(k.get("dest_dir", "")).startswith(str(bak)):
            raise OSError("backup disk full")
        return real(src, library, *a, **k)
    monkeypatch.setattr(ingest, "copy_from_card_ex", flaky)
    res = run(card, lib, layout="flat", backup=bak)
    assert res.failed and "backup disk full" in res.failed[0]["error"]


def test_second_run_completes_a_missing_backup(tmp_path):
    card, lib, bak = make_card(tmp_path, n=2), tmp_path / "lib", tmp_path / "bak"
    run(card, lib, layout="flat")
    res = run(card, lib, layout="flat", backup=bak)
    assert res.duplicates == 2 and len(list(bak.glob("*.CR2"))) == 2


@pytest.mark.parametrize("where", ["lib", "lib/sub", "card", "."])
def test_backup_inside_or_around_library_or_source_is_refused(tmp_path, where):
    card, lib = make_card(tmp_path, n=1), tmp_path / "lib"
    lib.mkdir()
    bak = tmp_path if where == "." else tmp_path / where
    with pytest.raises(cardwatch.CardError):
        run(card, lib, layout="flat", backup=bak)


def test_metadata_on_import_goes_to_the_sidecar_and_adds_keywords(tmp_path, exif_calls):
    card, lib = make_card(tmp_path, n=1, sidecar=(0,)), tmp_path / "lib"
    run(card, lib, layout="flat", rating=4, keywords=["wedding", "Wedding", "church"], copyright="(c) Me")
    args = exif_calls[0]
    assert "-XMP-xmp:Rating=4" in args and "-XMP-dc:Subject+=wedding" in args and "-XMP-dc:Subject+=church" in args
    assert args.count("-XMP-dc:Subject+=wedding") == 1 and "-XMP-dc:Rights=(c) Me" in args
    assert "-overwrite_original" in args and args[-1] == str(lib / "IMG_0000.xmp")       # merged into the carried one


def test_metadata_failure_is_a_warning_and_the_photo_still_imports(tmp_path, monkeypatch):
    import library
    monkeypatch.setattr(library, "_run_exiftool", lambda a: subprocess.CompletedProcess(a, 1, "", "boom"))
    card, lib = make_card(tmp_path, n=1), tmp_path / "lib"
    res = run(card, lib, layout="flat", rating=5)
    assert res.copied == 1 and res.warnings and "boom" in res.warnings[0]


def test_duplicates_do_not_get_metadata_again(tmp_path, exif_calls):
    card, lib = make_card(tmp_path, n=1), tmp_path / "lib"
    run(card, lib, layout="flat")
    run(card, lib, layout="flat", rating=1)
    assert exif_calls == []


def test_post_copy_hook_runs_on_new_files_only(tmp_path):
    card, lib = make_card(tmp_path, n=2), tmp_path / "lib"
    seen = []
    run(card, lib, layout="flat", post_copy=seen.append)
    run(card, lib, layout="flat", post_copy=seen.append)
    assert sorted(p.name for p in seen) == ["IMG_0000.CR2", "IMG_0001.CR2"]


def test_plain_folder_source_is_scanned_without_dcim(tmp_path):
    src = tmp_path / "shoot" / "day1"
    src.mkdir(parents=True)
    (src / "A.CR2").write_bytes(b"a" * 100)
    (src / "._A.CR2").write_bytes(b"junk")
    res = cardwatch.import_card(tmp_path / "shoot", tmp_path / "lib", "flat")
    assert res.copied == 1


def test_library_inside_the_source_folder_is_not_reimported(tmp_path):
    src = tmp_path / "all"
    (src / "in").mkdir(parents=True)
    (src / "in" / "A.CR2").write_bytes(b"a" * 100)
    lib = src / "library"
    cardwatch.import_card(src, lib, "flat")
    res = cardwatch.import_card(src, lib, "flat")
    assert res.found == 1 and res.duplicates == 1


def test_watch_cards_options_carry_the_new_settings(tmp_path):
    args = cardwatch.build_parser().parse_args(
        [str(tmp_path / "lib"), "--rename", "{seq}", "--backup", str(tmp_path / "b"), "--rating", "3",
         "--keywords", "a, b", "--no-sidecars", "--seq-start", "5"])
    s = cardwatch.options_from(args, {}).spec()
    assert (s.rename, s.rating, s.keywords, s.sidecars, s.seq_start) == ("{seq}", 3, ["a", "b"], False, 5)


def test_process_card_with_rename_and_backup_ejects_only_after_verifying_both(tmp_path):
    card, lib, bak = make_card(tmp_path), tmp_path / "lib", tmp_path / "bak"
    opts = Options(library=lib, notify=False, rename="{seq}", backup=bak)
    ej = SimpleNamespace(n=0)

    def ejector(c):
        ej.n += 1
        return {"ok": True, "state": "ejected", "message": "ok", "steps": []}
    res = cardwatch.process_card(Card(card, "/dev/sdb1", "EOS"), opts, ejector=ejector,
                                 notifier=lambda *a, **k: None, log=lambda *a, **k: None)
    assert res.clean and ej.n == 1 and len(list(bak.rglob("*.CR2"))) == 3


# ---- ingest shares the same logic ---------------------------------------------------------
def test_ingest_carries_sidecars_and_uses_rename_and_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "_convert_with_style", lambda *a, **k: None)
    card, lib, bak = make_card(tmp_path, sidecar=(0,)), tmp_path / "lib", tmp_path / "bak"
    spec = importkit.ImportSpec(layout="month", rename="{date}_{seq}", backup=bak).validate()
    s = ingest.ingest_card(card, lib, convert=False, spec=spec, jobs=4)
    assert s["copied"] == 3 and not s["failed"]
    assert (lib / "2026/09/2026-09-14_0001.xmp").exists()
    assert (bak / "2026/09/2026-09-14_0003.CR2").exists()


def test_ingest_classic_path_carries_sidecars_too(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "get_shoot_date", lambda p: None)
    card, lib = make_card(tmp_path, sidecar=(1,)), tmp_path / "lib"
    ingest.ingest_card(card, lib, convert=False)
    assert (lib / "IMG_0001.xmp").read_text() == "<x:xmpmeta source='camera 1'/>"


# ---- HTTP API ---------------------------------------------------------------------------------
@pytest.fixture
def api(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setattr(editor, "LIBRARY_ROOT", lib)
    monkeypatch.setattr(import_api, "queue_smart_previews", lambda paths: None)
    monkeypatch.setattr(cardwatch, "find_cards", lambda *a, **k: [])
    editor.app.config["TESTING"] = True
    import_api._job.clear()
    import_api._job["state"] = "idle"
    with editor.app.test_client() as c:
        c.lib = lib
        yield c


def wait_job(c, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get("/api/import/job").get_json()
        if j["state"] not in ("scanning", "importing", "verifying", "ejecting"):
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_scan_lists_frames_and_flags_duplicates(api, tmp_path):
    card = make_card(tmp_path, sidecar=(0,))
    api.post("/api/import/run", json={"source": str(card), "files": ["DCIM/100CANON/IMG_0001.CR2"], "layout": "flat"})
    wait_job(api)
    d = api.post("/api/import/scan", json={"source": str(card)}).get_json()
    by = {f["name"]: f for f in d["frames"]}
    assert len(by) == 3 and by["IMG_0001.CR2"]["duplicate"] == "IMG_0001.CR2"
    assert by["IMG_0000.CR2"]["duplicate"] is None and by["IMG_0000.CR2"]["sidecar"] is True
    assert d["card"] is False


def test_run_imports_selection_with_options_end_to_end(api, tmp_path, exif_calls):
    card = make_card(tmp_path, sidecar=(0,))
    bak = tmp_path / "bak"
    r = api.post("/api/import/run", json={
        "source": str(card), "files": ["DCIM/100CANON/IMG_0000.CR2", "DCIM/100CANON/IMG_0002.CR2"],
        "dest": "Shoots/Wedding", "layout": "flat", "rename": "wed_{seq:2}", "backup": str(bak),
        "rating": 3, "keywords": "bride, church", "copyright": "(c) Me"})
    assert r.get_json()["started"]
    j = wait_job(api)
    assert j["state"] == "done", j
    out = api.lib / "Shoots/Wedding"
    assert sorted(p.name for p in out.glob("*.CR2")) == ["wed_01.CR2", "wed_02.CR2"]
    assert (out / "wed_01.xmp").exists()
    assert sorted(p.name for p in (bak / "Shoots/Wedding").glob("*.CR2")) == ["wed_01.CR2", "wed_02.CR2"]
    assert j["summary"]["copied"] == 2 and j["summary"]["backed_up"] == 2
    assert not list(card.rglob("*.partial")) and len(list(card.rglob("*.CR2"))) == 3


def test_run_rejects_paths_outside_and_bad_templates(api, tmp_path):
    card = make_card(tmp_path)
    rel = "DCIM/100CANON/IMG_0000.CR2"
    for body in (
        {"source": str(card), "files": ["../../etc/passwd"]},
        {"source": str(card), "files": ["/etc/passwd"]},
        {"source": "relative/dir", "files": [rel]},
        {"source": str(api.lib), "files": [rel]},                      # the library itself
        {"source": str(card), "files": [rel], "dest": "../escape"},
        {"source": str(card), "files": [rel], "dest": "{yyyy}"},
        {"source": str(card), "files": [rel], "rename": "a/b"},
        {"source": str(card), "files": [rel], "layout": "../x"},
        {"source": str(card), "files": [rel], "backup": str(api.lib / "inside")},
        {"source": str(card), "files": [rel], "backup": "relative"},
        {"source": str(card), "files": [rel], "rating": 9},
        {"source": str(card), "files": [rel], "preset": {"id": "no-such"}},
        {"source": str(card), "files": []},
    ):
        r = api.post("/api/import/run", json=body)
        assert r.status_code == 400 and r.get_json()["error"], body
    assert not list(api.lib.rglob("*.CR2"))


def test_symlink_escape_from_the_source_is_refused(api, tmp_path):
    card = make_card(tmp_path, n=1)
    secret = tmp_path / "secret.CR2"
    secret.write_bytes(b"s" * 50)
    (card / "DCIM/100CANON/LINK.CR2").symlink_to(secret)
    r = api.post("/api/import/run", json={"source": str(card), "files": ["DCIM/100CANON/LINK.CR2"]})
    assert r.status_code == 400


def test_only_one_job_at_a_time(api, tmp_path):
    card = make_card(tmp_path, n=1)
    import_api._job["state"] = "importing"
    r = api.post("/api/import/run", json={"source": str(card), "files": ["DCIM/100CANON/IMG_0000.CR2"]})
    assert r.status_code == 409


def test_eject_only_for_a_real_card_and_only_when_clean(api, tmp_path, monkeypatch):
    card = make_card(tmp_path, n=1)
    calls = []
    monkeypatch.setattr(cardwatch, "find_cards", lambda *a, **k: [Card(card.resolve(), "/dev/sdb1", "EOS")])
    monkeypatch.setattr(cardwatch, "eject_card",
                        lambda c: calls.append(c) or {"ok": True, "state": "ejected", "message": "ok", "steps": []})
    body = {"source": str(card), "files": ["DCIM/100CANON/IMG_0000.CR2"], "layout": "flat", "eject": True}
    api.post("/api/import/run", json=body)
    j = wait_job(api)
    assert j["ejected"] is True and "Safe to remove" in j["message"] and len(calls) == 1
    calls.clear()                                              # a folder that is not a card is never ejected
    monkeypatch.setattr(cardwatch, "find_cards", lambda *a, **k: [])
    api.post("/api/import/run", json=body)
    j = wait_job(api)
    assert not j["ejected"] and not calls


def test_failed_verification_blocks_eject(api, tmp_path, monkeypatch):
    card = make_card(tmp_path, n=1)
    calls = []
    monkeypatch.setattr(cardwatch, "find_cards", lambda *a, **k: [Card(card.resolve(), "/dev/sdb1", "EOS")])
    monkeypatch.setattr(cardwatch, "eject_card", lambda c: calls.append(c))
    monkeypatch.setattr(cardwatch, "sha256", lambda p: str(p))            # source and copy never agree
    api.post("/api/import/run", json={"source": str(card), "files": ["DCIM/100CANON/IMG_0000.CR2"],
                                      "layout": "flat", "eject": True})
    j = wait_job(api)
    assert j["state"] == "done_with_errors" and not calls and "NOT ejected" in j["message"]


def test_browse_lists_folders_only_and_sources_reports_presets(api, tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "f.txt").write_text("x")
    d = api.get(f"/api/import/browse?path={tmp_path}").get_json()
    assert "a" in d["dirs"] and ".hidden" not in d["dirs"] and "f.txt" not in d["dirs"]
    assert api.get("/api/import/browse?path=/no/such/dir").status_code == 400
    s = api.get("/api/import/sources").get_json()
    assert s["library"] == str(api.lib.resolve()) and isinstance(s["presets"], list)


def test_thumb_refuses_files_outside_the_source(api, tmp_path):
    card = make_card(tmp_path, n=1)
    assert api.get(f"/api/import/thumb?source={card}&rel=../../etc/passwd").status_code == 404
    assert api.get(f"/api/import/thumb?source={card}&rel=DCIM/100CANON/IMG_0000.CR2").status_code in (200, 204)
