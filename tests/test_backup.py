"""Library backup / restore: snapshot, sqlite backup API, pruning, zip, raws, safe restore."""
import json
import sqlite3
import zipfile
from datetime import datetime, timedelta

import pytest

import backup
import editor
import photo_room_cli


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("PHOTO_ROOM_CONFIG_DIR", str(tmp_path / "cfg"))


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "lib"
    (root / "sub").mkdir(parents=True)
    (root / ".photo-room-library.json").write_text('{"a": 1}')
    (root / ".photo-room-catalog.json").write_text('{"b": 2}')
    (root / "IMG_1.photoroom.json").write_text('{"c": 3}')
    (root / "sub" / "IMG_2.xmp").write_text("<x/>")
    (root / "IMG_1.CR2").write_bytes(b"raw-one")
    (root / "notes.txt").write_text("ignore me")
    db = sqlite3.connect(root / ".photo-room-cull.sqlite")
    db.execute("create table t(x)")
    db.execute("insert into t values (42)")
    db.commit()
    db.close()
    return root


def test_snapshot_folder(lib, tmp_path):
    r = backup.create_backup(lib, tmp_path / "bk")
    snap = tmp_path / "bk" / r["path"].split("/")[-1]
    m = json.loads((snap / "manifest.json").read_text())
    assert set(m["files"]) == {".photo-room-library.json", ".photo-room-catalog.json",
                               "IMG_1.photoroom.json", "sub/IMG_2.xmp", ".photo-room-cull.sqlite"}
    assert (snap / "state" / "sub" / "IMG_2.xmp").read_text() == "<x/>"
    db = sqlite3.connect(snap / "state" / ".photo-room-cull.sqlite")
    assert db.execute("select x from t").fetchone() == (42,)
    assert not (tmp_path / "bk" / "raws").exists()


def test_zip_and_restore_into_empty(lib, tmp_path):
    r = backup.create_backup(lib, tmp_path / "bk", zip_it=True)
    assert r["path"].endswith(".zip") and zipfile.is_zipfile(r["path"])
    new = tmp_path / "new"
    out = backup.restore_backup(r["path"], new)
    assert len(out["restored"]) == 5 and not out["conflicts"]
    assert (new / "sub" / "IMG_2.xmp").read_text() == "<x/>"


def test_keep_last_n(lib, tmp_path):
    t = datetime(2026, 1, 1)
    for i in range(5):
        backup.create_backup(lib, tmp_path / "bk", keep=3, now=t + timedelta(minutes=i))
    names = [p.name for p in backup.list_backups(tmp_path / "bk")]
    assert len(names) == 3 and names[-1].endswith("20260101-000400")


def test_same_second_does_not_collide(lib, tmp_path):
    t = datetime(2026, 1, 1)
    a = backup.create_backup(lib, tmp_path / "bk", now=t)
    b = backup.create_backup(lib, tmp_path / "bk", now=t)
    assert a["path"] != b["path"]


def test_restore_never_overwrites_without_force(lib, tmp_path):
    r = backup.create_backup(lib, tmp_path / "bk")
    (lib / ".photo-room-library.json").write_text('{"changed": true}')
    out = backup.restore_backup(r["path"], lib)
    # the live DB differs byte-wise from its online-backup copy, so it counts as differing too
    assert ".photo-room-library.json" in out["conflicts"]
    assert json.loads((lib / ".photo-room-library.json").read_text()) == {"changed": True}
    # confirm callback declining keeps it, accepting restores it
    assert ".photo-room-library.json" in backup.restore_backup(
        r["path"], lib, confirm=lambda rel: False)["conflicts"]
    assert ".photo-room-library.json" in backup.restore_backup(
        r["path"], lib, confirm=lambda rel: True)["restored"]
    assert json.loads((lib / ".photo-room-library.json").read_text()) == {"a": 1}


def test_restore_force(lib, tmp_path):
    r = backup.create_backup(lib, tmp_path / "bk")
    (lib / "IMG_1.photoroom.json").write_text("{}")
    out = backup.restore_backup(r["path"], lib, force=True)
    assert "IMG_1.photoroom.json" in out["restored"] and not out["conflicts"]


def test_corrupt_backup_is_refused_before_any_write(lib, tmp_path):
    r = backup.create_backup(lib, tmp_path / "bk")
    snap = tmp_path / "bk" / r["path"].split("/")[-1]
    (snap / "state" / "IMG_1.photoroom.json").write_text("tampered")
    new = tmp_path / "new"
    with pytest.raises(backup.BackupError, match="checksum"):
        backup.restore_backup(snap, new)
    assert not new.exists()


def test_unsafe_manifest_path(tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "manifest.json").write_text(json.dumps({"files": {"../evil": "0" * 64}}))
    with pytest.raises(backup.BackupError, match="unsafe"):
        backup.restore_backup(snap, tmp_path / "lib")


def test_raws_incremental_and_restore(lib, tmp_path):
    bk = tmp_path / "bk"
    r1 = backup.create_backup(lib, bk, include_raws=True)
    assert (r1["raws_copied"], r1["raws_skipped"]) == (1, 0)
    assert (bk / "raws" / "IMG_1.CR2").read_bytes() == b"raw-one"
    r2 = backup.create_backup(lib, bk, include_raws=True)
    assert (r2["raws_copied"], r2["raws_skipped"]) == (0, 1)
    (lib / "IMG_1.CR2").unlink()
    out = backup.restore_backup(r2["path"], lib, include_raws=True)
    assert out["raws"] == ["IMG_1.CR2"] and (lib / "IMG_1.CR2").read_bytes() == b"raw-one"


def test_backup_inside_library_is_not_recursed(lib, tmp_path):
    backup.create_backup(lib, lib / "backups")
    r = backup.create_backup(lib, lib / "backups")
    assert r["files"] == 5


def test_due_and_settings(cfg, lib, tmp_path):
    assert not backup.is_due()
    backup.save_settings({"dest": str(tmp_path / "bk"), "interval_days": 7, "auto": True})
    assert backup.is_due()
    backup.run_configured(lib)
    assert not backup.is_due()
    assert backup.is_due(now=backup.load_settings()["last_backup"] + 8 * 86400)
    with pytest.raises(ValueError):
        backup.save_settings({"keep": 0})
    assert not backup.maybe_auto_backup(lib)   # not due now


def test_api(cfg, lib, tmp_path, monkeypatch):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", lib)
    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        assert c.post("/api/backup/run", json={}).status_code == 400
        r = c.post("/api/backup/run", json={"dest": str(tmp_path / "bk")})
        assert r.status_code == 200 and r.get_json()["files"] == 5
        st = c.get("/api/backup/status").get_json()
        assert st["latest"].startswith("photo-room-backup-") and not st["due"]
        assert c.post("/api/backup/settings", json={"interval_days": "x"}).status_code == 400


def test_cli(lib, tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        photo_room_cli.main(["backup", str(lib), "-o", str(tmp_path / "bk"), "--zip"])
    assert e.value.code == 0
    snap = backup.list_backups(tmp_path / "bk")[0]
    (lib / ".photo-room-catalog.json").write_text("{}")
    with pytest.raises(SystemExit) as e:
        photo_room_cli.main(["restore", str(snap), str(lib)])
    assert e.value.code == 2 and (lib / ".photo-room-catalog.json").read_text() == "{}"
    with pytest.raises(SystemExit) as e:
        photo_room_cli.main(["restore", str(snap), str(lib), "--force"])
    assert e.value.code == 0 and (lib / ".photo-room-catalog.json").read_text() == '{"b": 2}'
