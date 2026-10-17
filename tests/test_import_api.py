"""In-app import: streamed upload, checksum-verified copy, no overwrite, content-hash dedupe."""

import hashlib

import pytest

import editor
import import_api
import ingest
import smartpreview


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    monkeypatch.setattr(import_api, "_has_date", lambda src: False)
    import_api._pending_new.clear()
    editor.app.config["TESTING"] = True
    return tmp_path


@pytest.fixture
def client(lib):
    with editor.app.test_client() as c:
        yield c


def up(client, name, data):
    return client.post(f"/api/import/upload?name={name}", data=data,
                       content_type="application/octet-stream")


def test_new_file_is_copied_byte_for_byte(client, lib):
    data = b"raw-bytes-" * 1000
    r = up(client, "IMG_0001.CR2", data).get_json()
    assert r["status"] == "copied" and r["path"] == "IMG_0001.CR2"
    assert (lib / "IMG_0001.CR2").read_bytes() == data
    assert not [p for p in lib.rglob("*.partial")]


def test_same_content_under_another_name_is_a_duplicate_and_writes_nothing(client, lib):
    up(client, "IMG_0001.CR2", b"same")
    r = up(client, "COPY_OF_IT.CR2", b"same").get_json()
    assert r["status"] == "duplicate" and r["path"] == "IMG_0001.CR2"
    assert sorted(p.name for p in lib.glob("*.CR2")) == ["IMG_0001.CR2"]


def test_different_content_with_the_same_name_never_overwrites(client, lib):
    up(client, "IMG_0001.CR2", b"first")
    r = up(client, "IMG_0001.CR2", b"second").get_json()
    assert r["status"] == "renamed" and r["path"] == "IMG_0001-1.CR2"
    assert (lib / "IMG_0001.CR2").read_bytes() == b"first"
    assert (lib / "IMG_0001-1.CR2").read_bytes() == b"second"


def test_reimporting_the_same_batch_is_idempotent(client, lib):
    for _ in range(3):
        up(client, "A.DNG", b"a")
        up(client, "B.DNG", b"b")
    assert sorted(p.name for p in lib.glob("*.DNG")) == ["A.DNG", "B.DNG"]


def test_unsupported_types_and_bad_names_are_refused_gently(client, lib):
    r = up(client, "notes.txt", b"hello").get_json()
    assert r["status"] == "skipped" and "not a supported raw" in r["reason"]
    assert up(client, ".hidden.CR2", b"x").status_code == 400
    assert up(client, "x.CR2", b"").status_code == 422
    assert list(lib.iterdir()) == []


def test_path_tricks_in_the_name_land_inside_the_library(client, lib):
    r = up(client, "..%2F..%2Fetc%2Fevil.CR2", b"x").get_json()
    assert r["status"] == "copied" and r["path"] == "evil.CR2"
    assert (lib / "evil.CR2").exists()


def test_checksum_mismatch_discards_the_copy(client, lib, monkeypatch):
    real = ingest._sha256
    calls = {"n": 0}

    def flaky(path, chunk=1 << 20):
        calls["n"] += 1
        return real(path, chunk) if calls["n"] % 2 else "0" * 64

    monkeypatch.setattr(ingest, "_sha256", flaky)
    r = up(client, "BAD.CR2", b"payload").get_json()
    assert r["status"] == "failed" and "Checksum mismatch" in r["reason"]
    assert list(lib.iterdir()) == []


def test_not_enough_space_is_reported_with_a_next_step(client, lib, monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(10, 9, 1))
    r = up(client, "BIG.CR2", b"x" * 100)
    assert r.status_code == 507 and "free" in r.get_json()["error"].lower()


def test_upload_needs_the_session_token(lib):
    with editor.app.test_client() as c:
        r = c.post("/api/import/upload?name=A.CR2", data=b"x",
                   headers={"X-PhotoRoom-Token": "wrong"})
    assert r.status_code == 403
    assert not list(lib.iterdir())


def test_big_bodies_pass_the_json_cap(client, lib):
    assert editor.app.config["MAX_CONTENT_LENGTH"] == 8 * 1024 * 1024
    data = b"z" * (9 * 1024 * 1024)
    assert up(client, "BIG.DNG", data).get_json()["status"] == "copied"
    assert (lib / "BIG.DNG").stat().st_size == len(data)


def test_finish_rescans_and_queues_smart_previews(client, lib, monkeypatch):
    built = []
    monkeypatch.setattr(smartpreview, "ensure", lambda p, root: built.append(p.name))
    client.get("/api/library")                          # first scan: the library is empty
    up(client, "N1.DNG", b"n1")
    res = client.post("/api/import/finish", json={}).get_json()
    assert res["new"] == ["N1.DNG"] and res["scan"]["added"] == 1
    import_api._build_q.join()
    assert built == ["N1.DNG"]
    items = client.get("/api/library").get_json()["items"]
    assert [i["path"] for i in items] == ["N1.DNG"]


def test_smart_preview_endpoint(client, lib):
    (lib / "P.DNG").write_bytes(b"p")
    assert client.get("/api/smartpreview?path=P.DNG").status_code == 404
    assert client.get("/api/smartpreview/status?path=P.DNG").get_json() == {"ready": False}
    jpg, meta = smartpreview.entry_paths(lib / "P.DNG", lib)
    jpg.parent.mkdir(parents=True)
    jpg.write_bytes(b"\xff\xd8jpeg")
    meta.write_text("{}")
    r = client.get("/api/smartpreview?path=P.DNG")
    assert r.status_code == 200 and r.mimetype == "image/jpeg"
    assert client.get("/api/smartpreview?path=../x").status_code == 400
    assert client.get("/api/smartpreview/status?path=P.DNG").get_json() == {"ready": True}
    assert client.get("/api/smartpreview/status?path=../x").status_code == 400


def test_clean_name():
    assert import_api.clean_name("a/b\\c.CR2") == "c.CR2"
    assert import_api.clean_name("") is None and import_api.clean_name(".x.CR2") is None
