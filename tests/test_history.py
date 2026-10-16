"""History, snapshots, persistence and their HTTP endpoints."""

import json

import pytest

import editor
import history


@pytest.fixture
def photo(tmp_path):
    p = tmp_path / "IMG_0001.dng"
    p.write_bytes(b"x")
    return p


def A(**kw):
    return {"exposure": 0.0, "contrast": 0, **kw}


# ---- record / jump / trim -------------------------------------------------

def test_record_appends_with_label_and_timestamp(photo):
    h, ok = history.record(photo, "Exposure +1", A(exposure=1), now=100.0)
    assert ok
    assert h["entries"][0]["label"] == "Exposure +1"
    assert h["entries"][0]["ts"] == 100.0
    assert h["index"] == 0


def test_identical_step_is_not_recorded_twice(photo):
    history.record(photo, "a", A(exposure=1))
    _, ok = history.record(photo, "b", A(exposure=1))
    assert not ok
    assert len(history.load(photo)["entries"]) == 1


def test_transient_keys_are_not_stored(photo):
    h, _ = history.record(photo, "a", A(_baseline_temp=5000))
    assert "_baseline_temp" not in h["entries"][0]["adjustments"]


def test_jump_moves_index_without_deleting(photo):
    history.record(photo, "one", A(exposure=1))
    h, _ = history.record(photo, "two", A(exposure=2))
    first = h["entries"][0]["id"]
    h, adj = history.jump(photo, first)
    assert adj["exposure"] == 1
    assert h["index"] == 0
    assert len(h["entries"]) == 2


def test_recording_after_jump_discards_the_abandoned_future(photo):
    history.record(photo, "one", A(exposure=1))
    h, _ = history.record(photo, "two", A(exposure=2))
    history.jump(photo, h["entries"][0]["id"])
    h, _ = history.record(photo, "three", A(exposure=3))
    assert [e["label"] for e in h["entries"]] == ["one", "three"]
    assert h["index"] == 1


def test_jump_to_unknown_id_raises(photo):
    with pytest.raises(KeyError):
        history.jump(photo, "nope")


def test_history_is_trimmed_to_the_limit(photo, monkeypatch):
    monkeypatch.setattr(history, "MAX_ENTRIES", 5)
    for i in range(9):
        h, _ = history.record(photo, f"s{i}", A(exposure=i))
    assert len(h["entries"]) == 5
    assert h["entries"][0]["label"] == "s4"
    assert h["index"] == 4


# ---- snapshots ------------------------------------------------------------

def test_snapshot_create_list_apply_delete(photo):
    h = history.create_snapshot(photo, "Warm", A(exposure=0.5))
    sid = h["snapshots"][0]["id"]
    assert history.summary(h)["snapshots"][0]["name"] == "Warm"
    assert history.get_snapshot(photo, sid)["exposure"] == 0.5
    h, removed = history.delete_snapshot(photo, sid)
    assert removed and h["snapshots"] == []
    with pytest.raises(KeyError):
        history.get_snapshot(photo, sid)


def test_snapshot_same_name_replaces(photo):
    history.create_snapshot(photo, "Look", A(exposure=1))
    h = history.create_snapshot(photo, "Look", A(exposure=2))
    assert len(h["snapshots"]) == 1
    assert h["snapshots"][0]["adjustments"]["exposure"] == 2


def test_snapshot_needs_a_name(photo):
    with pytest.raises(ValueError):
        history.create_snapshot(photo, "   ", A())


def test_delete_missing_snapshot_reports_false(photo):
    assert history.delete_snapshot(photo, "zzz")[1] is False


# ---- persistence ----------------------------------------------------------

def test_round_trip_through_the_sidecar(photo):
    history.set_original(photo, A(exposure=0.25))
    history.record(photo, "x", A(exposure=1))
    history.create_snapshot(photo, "S", A(exposure=2))
    on_disk = history.history_path(photo)
    assert on_disk.name == "IMG_0001.dng.photoroom.json"
    again = history.load(photo)
    assert again["original"]["exposure"] == 0.25
    assert again["entries"][0]["adjustments"]["exposure"] == 1
    assert again["snapshots"][0]["name"] == "S"
    assert not list(photo.parent.glob("*.tmp"))


def test_set_original_only_keeps_the_first(photo):
    history.set_original(photo, A(exposure=1))
    history.set_original(photo, A(exposure=9))
    assert history.load(photo)["original"]["exposure"] == 1


def test_corrupt_sidecar_loads_as_empty(photo):
    history.history_path(photo).write_text("{not json")
    assert history.load(photo)["entries"] == []


def test_history_never_touches_the_photo(photo):
    history.record(photo, "x", A(exposure=1))
    assert photo.read_bytes() == b"x"


# ---- HTTP endpoints -------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch, fake_rawpy, fake_exiftool):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    (tmp_path / "IMG_0001.dng").write_bytes(b"fake-dng")
    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        yield c


def post(client, url, **body):
    body.setdefault("path", "IMG_0001.dng")
    return client.post(url, json=body)


def test_open_seeds_history_and_import_state(client):
    data = post(client, "/api/open").get_json()
    assert data["history"]["has_original"]
    assert [e["label"] for e in data["history"]["entries"]] == ["Import"]
    assert "exposure" in data["import_adjustments"]


def test_reopen_does_not_duplicate_history(client):
    post(client, "/api/open")
    data = post(client, "/api/open").get_json()
    assert len(data["history"]["entries"]) == 1


def test_record_then_jump_back(client):
    opened = post(client, "/api/open").get_json()
    adj = dict(opened["adjustments"], exposure=1.5)
    rec = post(client, "/api/history/record", label="Exposure 1.5", adjustments=adj).get_json()
    assert rec["recorded"] and len(rec["history"]["entries"]) == 2
    first = rec["history"]["entries"][0]["id"]
    jumped = post(client, "/api/history/jump", id=first).get_json()
    assert jumped["adjustments"]["exposure"] == opened["adjustments"]["exposure"]
    assert jumped["history"]["index"] == 0
    assert jumped["preview"]
    assert len(jumped["history"]["entries"]) == 2   # non-destructive


def test_jump_unknown_is_404(client):
    post(client, "/api/open")
    assert post(client, "/api/history/jump", id="nope").status_code == 404


def test_reset_to_import_restores_and_is_undoable(client):
    opened = post(client, "/api/open").get_json()
    post(client, "/api/history/record", label="E",
         adjustments=dict(opened["adjustments"], exposure=2.0))
    data = post(client, "/api/history/reset_to_import").get_json()
    assert data["adjustments"]["exposure"] == opened["import_adjustments"]["exposure"]
    assert data["history"]["entries"][-1]["label"] == "Reset to import"
    assert len(data["history"]["entries"]) == 3


def test_snapshot_endpoints(client):
    opened = post(client, "/api/open").get_json()
    adj = dict(opened["adjustments"], contrast=40)
    h = post(client, "/api/snapshots/create", name="Punchy", adjustments=adj).get_json()["history"]
    sid = h["snapshots"][0]["id"]
    applied = post(client, "/api/snapshots/apply", id=sid, name="Punchy").get_json()
    assert applied["adjustments"]["contrast"] == 40
    assert applied["history"]["entries"][-1]["label"] == "Snapshot: Punchy"
    gone = post(client, "/api/snapshots/delete", id=sid).get_json()
    assert gone["ok"] and gone["history"]["snapshots"] == []
    assert post(client, "/api/snapshots/apply", id=sid).status_code == 404


def test_snapshot_without_name_is_400(client):
    post(client, "/api/open")
    r = post(client, "/api/snapshots/create", name="", adjustments={"exposure": 1})
    assert r.status_code == 400


def test_endpoints_reject_paths_outside_library(client):
    for url in ("/api/history", "/api/history/record", "/api/snapshots/create"):
        assert post(client, url, path="../../etc/passwd").status_code == 400


def test_endpoints_require_json(client):
    r = client.post("/api/history", data="path=x", content_type="text/plain")
    assert r.status_code == 415


def test_history_sidecar_is_not_listed_as_a_photo(client, tmp_path):
    post(client, "/api/open")
    assert (tmp_path / "IMG_0001.dng.photoroom.json").exists()
    assert client.get("/api/files").get_json()["files"] == ["IMG_0001.dng"]


def test_editor_page_has_history_and_compare_ui(client):
    html = client.get("/").get_data(as_text=True)
    for marker in ('id="history-list"', 'id="snapshot-list"', 'id="reset-import"',
                   'id="ba-divider"', 'data-ba="split"', 'data-ba="side"'):
        assert marker in html
