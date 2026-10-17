"""Card watcher: detection, import, verify, safe eject, opt-in delete, locking.

No hardware: cards are temp directories, eject commands are a recording fake.
The rules that matter most: eject is never called after a failure or an
interruption, deletion only follows verification, and nothing is written to the
card unless deletion was asked for.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import cardwatch
import ingest
from cardwatch import Card, Options


@pytest.fixture(autouse=True)
def no_exiftool(monkeypatch):
    monkeypatch.setattr(cardwatch, "_shoot_date", lambda p: ("2026", "09", "14"))


def make_card(root: Path, name="EOS_DIGITAL", n=3, ext=".CR2") -> Path:
    card = root / name
    folder = card / "DCIM" / "100CANON"
    folder.mkdir(parents=True)
    for i in range(n):
        (folder / f"IMG_{i:04d}{ext}").write_bytes(b"raw-%d-" % i * 200)
    return card


def snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()}


class FakeRun:
    """Stands in for subprocess.run; records commands, fails on demand."""

    def __init__(self, fail=(), stderr="boom"):
        self.calls, self.fail, self.stderr = [], fail, stderr

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        bad = any(f in " ".join(cmd) for f in self.fail)
        return SimpleNamespace(returncode=1 if bad else 0, stdout="", stderr=self.stderr if bad else "")


def which_all(name):
    return f"/usr/bin/{name}"


@pytest.fixture
def lib(tmp_path):
    return tmp_path / "library"


@pytest.fixture
def opts(lib):
    return Options(library=lib, notify=False)


def run_card(card_path, opts, ejector=None, **kw):
    ej = ejector or FakeEject()
    res = cardwatch.process_card(Card(card_path, "/dev/sdb1", "EOS"), opts, ejector=ej,
                                 notifier=lambda *a, **k: None, log=lambda *a, **k: None, **kw)
    return res, ej


class FakeEject:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def __call__(self, card):
        self.calls.append(card)
        return {"ok": self.ok, "state": "ejected" if self.ok else "failed",
                "message": "ejected" if self.ok else "busy", "steps": []}


# --- detection -------------------------------------------------------------

def test_finds_only_volumes_with_dcim(tmp_path):
    make_card(tmp_path, "CARD")
    (tmp_path / "USBSTICK" / "docs").mkdir(parents=True)
    cards = cardwatch.find_cards(roots=(str(tmp_path),), mounts_file=None)
    assert [c.mount.name for c in cards] == ["CARD"]


def test_finds_cards_two_levels_deep_under_run_media(tmp_path):
    root = tmp_path / "run" / "media"
    make_card(root / "pi", "EOS")
    cards = cardwatch.find_cards(roots=(str(root),), mounts_file=None)
    assert cards and cards[0].mount.name == "EOS"


def test_proc_mounts_gives_device_and_unescapes_spaces(tmp_path):
    root = tmp_path / "media"
    card = make_card(root, "MY CARD")
    mounts = tmp_path / "mounts"
    mounts.write_text(f"/dev/sdb1 {str(card).replace(' ', chr(92) + '040')} vfat rw 0 0\n"
                      f"/dev/sda1 / ext4 rw 0 0\n")
    cards = cardwatch.find_cards(roots=(str(root),), mounts_file=mounts)
    assert [(c.device, c.mount) for c in cards] == [("/dev/sdb1", card)]


def test_mac_ignores_internal_disks_and_boot_volume(tmp_path):
    make_card(tmp_path, "EOS")
    make_card(tmp_path, "Macintosh HD")
    make_card(tmp_path, "InternalThing")
    info = lambda v: {"Internal": True} if v.name == "InternalThing" else {"DeviceNode": "/dev/disk4s1"}
    cards = cardwatch.find_cards(roots=(str(tmp_path),), platform="darwin", diskutil=info)
    assert [(c.mount.name, c.device) for c in cards] == [("EOS", "/dev/disk4s1")]


# --- import ----------------------------------------------------------------

def test_import_copies_into_date_folders_and_leaves_card_untouched(tmp_path, opts):
    card = make_card(tmp_path)
    before = snapshot(card)
    res, ej = run_card(card, opts)
    assert res.copied == 3 and res.duplicates == 0 and res.clean
    assert sorted(p.name for p in (opts.library / "2026/09/14").glob("*.CR2")) == [
        "IMG_0000.CR2", "IMG_0001.CR2", "IMG_0002.CR2"]
    assert snapshot(card) == before
    assert not list(opts.library.rglob("*.partial"))


def test_layouts(tmp_path, lib):
    card = make_card(tmp_path, n=1)
    cardwatch.import_card(card, lib, "flat")
    assert (lib / "IMG_0000.CR2").exists()
    lib2 = tmp_path / "l2"
    cardwatch.import_card(card, lib2, "{yyyy}/{yyyy}-{mm}-{dd}")
    assert (lib2 / "2026/2026-09-14/IMG_0000.CR2").exists()


@pytest.mark.parametrize("bad", ["../x", "/abs", "{foo}", "a/../b", "{yyyy"])
def test_unsafe_layouts_are_rejected(bad):
    with pytest.raises(cardwatch.CardError):
        cardwatch.resolve_layout(bad)


def test_reinsert_imports_nothing_new_and_dedupes_by_content(tmp_path, opts):
    card = make_card(tmp_path)
    run_card(card, opts)
    # same bytes under another name elsewhere in the library also count
    res, _ = run_card(card, opts)
    assert res.copied == 0 and res.duplicates == 3 and res.clean
    assert len(list(opts.library.rglob("*.CR2"))) == 3


def test_same_name_different_content_never_overwrites(tmp_path, opts):
    (opts.library / "2026/09/14").mkdir(parents=True)
    (opts.library / "2026/09/14/IMG_0000.CR2").write_bytes(b"someone else")
    card = make_card(tmp_path, n=1)
    res, _ = run_card(card, opts)
    assert res.renamed == 1
    assert (opts.library / "2026/09/14/IMG_0000.CR2").read_bytes() == b"someone else"
    assert (opts.library / "2026/09/14/IMG_0000-1.CR2").exists()


def test_jpeg_and_video_imported_junk_and_appledouble_ignored(tmp_path, opts):
    card = make_card(tmp_path, n=1)
    d = card / "DCIM/100CANON"
    (d / "IMG_0100.JPG").write_bytes(b"jpeg")
    (d / "MVI_0001.MP4").write_bytes(b"video")
    (d / "._IMG_0000.CR2").write_bytes(b"apple")
    (d / "NOTES.TXT").write_bytes(b"x")
    res, _ = run_card(card, opts)
    names = sorted(p.name for p in opts.library.rglob("*") if p.is_file() and not p.name.startswith("."))
    assert names == ["IMG_0000.CR2", "IMG_0100.JPG", "MVI_0001.MP4"]
    res2 = cardwatch.import_card(card, opts.library.parent / "raw", suffixes=cardwatch.RAW_ONLY)
    assert res2.found == 1


def test_thousands_of_files(tmp_path, opts):
    card = tmp_path / "BIG"
    (card / "DCIM/100").mkdir(parents=True)
    for i in range(1500):
        (card / f"DCIM/100/IMG_{i:05d}.CR2").write_bytes(i.to_bytes(4, "big") * 8)
    res, ej = run_card(card, opts)
    assert res.copied == 1500 and res.verified == 1500 and len(ej.calls) == 1


def test_not_enough_free_space_copies_nothing_and_does_not_eject(tmp_path, opts):
    card = make_card(tmp_path)
    usage = lambda p: SimpleNamespace(free=1024)
    res, ej = run_card(card, opts, disk_usage=usage)
    assert "not enough free space" in res.problem
    assert not list(opts.library.rglob("*.CR2")) and ej.calls == []


def test_unreadable_file_fails_and_blocks_eject(tmp_path, opts, monkeypatch):
    card = make_card(tmp_path)
    real = ingest.copy_from_card_ex

    def flaky(src, *a, **k):
        if src.name == "IMG_0001.CR2":
            raise OSError("Input/output error")
        return real(src, *a, **k)

    monkeypatch.setattr(ingest, "copy_from_card_ex", flaky)
    res, ej = run_card(card, opts)
    assert res.copied == 2 and [f["file"] for f in res.failed] == ["IMG_0001.CR2"]
    assert not res.clean and ej.calls == []


def test_checksum_mismatch_in_copy_blocks_eject(tmp_path, opts, monkeypatch):
    card = make_card(tmp_path)
    monkeypatch.setattr(ingest, "_sha256", lambda p, chunk=0: os.urandom(8).hex())
    res, ej = run_card(card, opts)
    assert len(res.failed) == 3 and ej.calls == []


def test_corrupt_library_copy_found_by_verify_pass_blocks_eject(tmp_path, opts, monkeypatch):
    card = make_card(tmp_path)
    real = cardwatch.verify_import

    def corrupt_then_verify(res, *a, **k):
        victim = next(iter(res.destinations.values()))
        victim.write_bytes(b"bit rot")
        return real(res, *a, **k)

    monkeypatch.setattr(cardwatch, "verify_import", corrupt_then_verify)
    res, ej = run_card(card, opts)
    assert len(res.verify_failed) == 1 and ej.calls == []


def test_card_removed_mid_copy_blocks_eject(tmp_path, opts, monkeypatch):
    card = make_card(tmp_path)
    real = ingest.copy_from_card_ex
    n = {"i": 0}

    def vanish(src, *a, **k):
        n["i"] += 1
        if n["i"] == 2:
            import shutil
            shutil.rmtree(card)
        return real(src, *a, **k)

    monkeypatch.setattr(ingest, "copy_from_card_ex", vanish)
    res, ej = run_card(card, opts)
    assert not res.clean and ej.calls == []


def test_unreadable_directory_blocks_eject(tmp_path, opts):
    card = make_card(tmp_path)
    bad = card / "DCIM/101LOCKED"
    bad.mkdir()
    (bad / "IMG_9.CR2").write_bytes(b"x")
    bad.chmod(0)
    try:
        if os.access(bad, os.R_OK):
            pytest.skip("running as root; permissions are not enforced")
        res, ej = run_card(card, opts)
        assert res.scan_errors and ej.calls == []
    finally:
        bad.chmod(0o755)


def test_interrupted_import_never_ejects(tmp_path, opts):
    card = make_card(tmp_path)
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 5

    res, ej = run_card(card, opts, should_stop=stop)
    assert res.interrupted and not res.clean and ej.calls == []
    status = json.loads((opts.library / cardwatch.STATUS_NAME).read_text())
    assert status["state"] == "error" and "NOT ejected" in status["message"]


# --- eject policy ----------------------------------------------------------

def test_clean_import_ejects_once(tmp_path, opts):
    card = make_card(tmp_path)
    res, ej = run_card(card, opts)
    assert len(ej.calls) == 1 and res.eject["ok"]
    st = json.loads((opts.library / cardwatch.STATUS_NAME).read_text())
    assert st["state"] == "done" and st["ejected"] and "Safe to remove" in st["message"]


def test_no_eject_option(tmp_path, lib):
    card = make_card(tmp_path)
    res, ej = run_card(card, Options(library=lib, notify=False, eject=False))
    assert res.clean and ej.calls == []


def test_failed_eject_is_reported_not_claimed(tmp_path, opts):
    card = make_card(tmp_path)
    res, ej = run_card(card, opts, ejector=FakeEject(ok=False))
    st = json.loads((opts.library / cardwatch.STATUS_NAME).read_text())
    assert not st["ejected"] and "Safe to remove" not in st["message"] and "busy" in st["message"]


def test_notifications_say_safe_to_remove_only_after_eject(tmp_path, opts):
    sent = []
    notify = lambda t, b, **k: sent.append((t, b))
    o = Options(library=opts.library, notify=True)
    card = make_card(tmp_path)
    cardwatch.process_card(Card(card, "/dev/sdb1", "EOS"), o, ejector=FakeEject(ok=False),
                           notifier=notify, log=lambda *a: None)
    assert not any("safe to remove" in (t + b).lower() for t, b in sent)
    cardwatch.process_card(Card(card, "/dev/sdb1", "EOS"), o, ejector=FakeEject(ok=True),
                           notifier=notify, log=lambda *a: None)
    assert any("Safe to remove" in b for t, b in sent)


# --- eject commands --------------------------------------------------------

def test_linux_eject_unmounts_then_powers_off_the_whole_disk(tmp_path):
    sysblk = tmp_path / "sys"
    disk = sysblk.parent / "devices/block/sdb"
    (disk / "sdb1").mkdir(parents=True)
    (disk / "sdb1/partition").write_text("1")
    sysblk.mkdir()
    (sysblk / "sdb1").symlink_to(disk / "sdb1")
    run = FakeRun()
    r = cardwatch.eject_card(Card(Path("/media/x"), "/dev/sdb1"), "linux", run, which_all, sysblk)
    assert r["ok"] and run.calls == [["udisksctl", "unmount", "-b", "/dev/sdb1"],
                                     ["udisksctl", "power-off", "-b", "/dev/sdb"]]


def test_parent_disk_names(tmp_path):
    assert cardwatch.parent_disk("/dev/mmcblk0p1", tmp_path) == "/dev/mmcblk0"
    assert cardwatch.parent_disk("/dev/sdc2", tmp_path) == "/dev/sdc"


def test_busy_card_is_not_powered_off_or_force_unmounted():
    run = FakeRun(fail=("unmount",), stderr="Error unmounting: target is busy")
    r = cardwatch.eject_card(Card(Path("/media/x"), "/dev/sdb1"), "linux", run, which_all)
    assert not r["ok"] and r["state"] == "failed"
    flat = [" ".join(c) for c in run.calls]
    assert not any("power-off" in c or "umount " in c or " -f" in c or " -l" in c for c in flat)


def test_umount_fallback_when_no_udisks():
    run = FakeRun()
    which = lambda n: None if n == "udisksctl" else f"/bin/{n}"
    r = cardwatch.eject_card(Card(Path("/media/x"), "/dev/sdb1"), "linux", run, which)
    assert run.calls == [["umount", "/media/x"]] and r["state"] == "unmounted" and not r["ok"]


def test_power_off_failure_is_not_reported_as_safe():
    run = FakeRun(fail=("power-off",))
    r = cardwatch.eject_card(Card(Path("/media/x"), "/dev/sdb1"), "linux", run, which_all)
    assert not r["ok"] and r["state"] == "unmounted"


def test_macos_uses_diskutil_eject():
    run = FakeRun()
    r = cardwatch.eject_card(Card(Path("/Volumes/EOS"), "/dev/disk4s1"), "darwin", run, which_all)
    assert r["ok"] and run.calls == [["diskutil", "eject", "/Volumes/EOS"]]


# --- delete after import ---------------------------------------------------

def test_delete_is_off_by_default(tmp_path, opts):
    card = make_card(tmp_path)
    before = snapshot(card)
    run_card(card, opts)
    assert snapshot(card) == before


def test_delete_removes_only_verified_photos_and_logs(tmp_path, lib):
    card = make_card(tmp_path)
    d = card / "DCIM/100CANON"
    (d / "MVI_0001.MP4").write_bytes(b"video")
    (d / "NOTES.TXT").write_bytes(b"keep")
    (card / "MISC").mkdir()
    o = Options(library=lib, notify=False, delete_after_import=True)
    res, ej = run_card(card, o)
    assert len(res.deleted) == 3
    assert sorted(p.name for p in d.iterdir()) == ["MVI_0001.MP4", "NOTES.TXT"]
    assert (card / "MISC").is_dir() and d.is_dir()           # no directory is ever removed
    log = (lib / cardwatch.DELETE_LOG_NAME).read_text()
    assert log.count("deleted ") == 3 and "IMG_0000.CR2" in log
    assert len(ej.calls) == 1


def test_delete_only_after_verify_never_when_anything_failed(tmp_path, lib, monkeypatch):
    card = make_card(tmp_path)
    real = ingest.copy_from_card_ex
    monkeypatch.setattr(ingest, "copy_from_card_ex",
                        lambda s, *a, **k: (_ for _ in ()).throw(OSError("eio")) if s.name.endswith("0002.CR2") else real(s, *a, **k))
    o = Options(library=lib, notify=False, delete_after_import=True)
    res, ej = run_card(card, o)
    assert res.deleted == [] and len(list((card / "DCIM/100CANON").glob("*.CR2"))) == 3 and ej.calls == []


def test_delete_skips_a_file_that_changed_after_verification(tmp_path, lib):
    card = make_card(tmp_path, n=1)
    res = cardwatch.import_card(card, lib)
    cardwatch.verify_import(res)
    f = next(iter(res.files.values()))
    f.path.write_bytes(b"camera wrote more " * 50)
    cardwatch.delete_verified(res, card, log=lambda *a: None)
    assert res.deleted == [] and f.path.exists() and res.delete_failed


def test_delete_dry_run_deletes_nothing(tmp_path, lib):
    card = make_card(tmp_path)
    before = snapshot(card)
    o = Options(library=lib, notify=False, delete_after_import=True, delete_dry_run=True)
    res, _ = run_card(card, o)
    assert len(res.would_delete) == 3 and res.deleted == [] and snapshot(card) == before
    assert "DRY-RUN" in (lib / cardwatch.DELETE_LOG_NAME).read_text()


def test_config_cannot_enable_delete(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"delete_after_import": True, "eject": False, "layout": "flat"}))
    args = cardwatch.build_parser().parse_args(["/x", "--config", str(cfg)])
    o = cardwatch.options_from(args, cardwatch.load_config(cfg))
    assert not o.delete_after_import and not o.eject and o.layout == "flat"


# --- watcher ---------------------------------------------------------------

def test_watcher_debounces_imports_once_and_reimports_after_reinsert(opts):
    present = [Card(Path("/m/A"), "/dev/sdb1")]
    seen = []
    w = cardwatch.Watcher(opts, finder=lambda: list(present),
                          processor=lambda c, o, **k: seen.append(c.key) or SimpleNamespace(clean=True))
    w.poll_once()
    assert seen == []                       # first sighting: wait (debounce)
    w.poll_once(); w.poll_once()
    assert len(seen) == 1                   # imported once, not on every poll
    present.clear(); w.poll_once()          # card removed
    present.append(Card(Path("/m/A"), "/dev/sdb1"))
    w.poll_once(); w.poll_once()
    assert len(seen) == 2                   # re-inserted -> imported again


def test_a_card_that_flickers_is_never_imported(opts):
    flip = iter([True, False, True, False, True, False])
    seen = []
    w = cardwatch.Watcher(opts, finder=lambda: [Card(Path("/m/A"))] if next(flip) else [],
                          processor=lambda *a, **k: seen.append(1))
    for _ in range(6):
        w.poll_once()
    assert seen == []


# --- locking and CLI -------------------------------------------------------

def test_second_instance_on_the_same_library_is_refused(lib):
    with cardwatch.RunLock(lib):
        with pytest.raises(cardwatch.LockHeld):
            with cardwatch.RunLock(lib):
                pass
    with cardwatch.RunLock(lib):            # released afterwards
        pass


def test_main_once_imports_and_ejects_via_fake_commands(tmp_path, monkeypatch, capsys):
    media = tmp_path / "media"
    make_card(media, "EOS")
    lib = tmp_path / "lib"
    monkeypatch.setattr(cardwatch, "find_cards", lambda: [Card(media / "EOS", "/dev/sdb1", "EOS")])
    ran = FakeRun()
    monkeypatch.setattr(cardwatch.subprocess, "run", ran)
    monkeypatch.setattr(cardwatch.shutil, "which", which_all)
    code = cardwatch.main([str(lib), "--once", "--no-notify", "--config", str(tmp_path / "none.json")])
    assert code == 0
    assert len(list(lib.rglob("*.CR2"))) == 3
    assert ["udisksctl", "unmount", "-b", "/dev/sdb1"] in ran.calls
    assert "Safe to remove" in capsys.readouterr().out


def test_cli_dispatches_watch_cards(monkeypatch):
    import photo_room_cli
    got = {}
    monkeypatch.setattr(cardwatch, "main", lambda argv: got.setdefault("argv", argv) and 0)
    monkeypatch.setattr(photo_room_cli, "preflight", lambda: [])
    with pytest.raises(SystemExit) as e:
        photo_room_cli.main(["watch-cards", "/lib", "--no-eject"])
    assert e.value.code == 0 and got["argv"] == ["/lib", "--no-eject"]


def test_notify_commands():
    run = FakeRun()
    assert cardwatch.notify("T", 'say "hi"', "darwin", run, which_all)
    assert run.calls[0][0] == "osascript" and '\\"hi\\"' in run.calls[0][2]
    run = FakeRun()
    assert cardwatch.notify("T", "B", "linux", run, which_all) and run.calls[0][0] == "notify-send"
    assert not cardwatch.notify("T", "B", "linux", run, lambda n: None)
