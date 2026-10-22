"""Auto Sync and Quick Develop: /api/sync_settings and /api/sync_undo."""

import pytest

import editor
import history


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    for n in ("a.dng", "b.dng"):
        (tmp_path / n).write_bytes(b"dng")
    # Each photo has its own saved look and its own camera white balance.
    state = {
        "a.dng": {"exposure": 0.5, "contrast": 10, "color_temp": 4000,
                  "crop": {"left": 0.1, "top": 0.1, "right": 0.9, "bottom": 0.9}},
        "b.dng": {"exposure": -1.0, "contrast": 0, "color_temp": 6000},
    }
    monkeypatch.setattr(editor, "read_saved_adjustments", lambda p, *a, **k: dict(state[p.name]))
    monkeypatch.setattr(editor, "_baseline_temp_of", lambda p: 4000 if p.name == "a.dng" else 6000)
    saved = {}

    def fake_save(path, adj, **kw):
        saved[path.name] = dict(adj)
        state[path.name] = dict(adj)

    monkeypatch.setattr(editor, "save_adjustments", fake_save)
    monkeypatch.setattr(editor, "_save_frames", lambda p, a: (None, None))
    monkeypatch.setattr(editor, "detect_foreign_edits", lambda p: None)
    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        c.saved = saved
        yield c


def post(c, **body):
    body.setdefault("paths", ["a.dng", "b.dng"])
    return c.post("/api/sync_settings", json=body).get_json()


def test_absolute_change_keeps_each_photos_other_settings(lib):
    d = post(lib, changes={"contrast": 40})
    assert d["ok"] and sorted(d["applied"]) == ["a.dng", "b.dng"]
    assert lib.saved["a.dng"]["contrast"] == 40 and lib.saved["b.dng"]["contrast"] == 40
    assert lib.saved["a.dng"]["exposure"] == 0.5
    assert lib.saved["b.dng"]["exposure"] == -1.0


def test_relative_step_is_per_photo_and_clamped(lib):
    post(lib, steps={"exposure": 0.5})
    assert lib.saved["a.dng"]["exposure"] == 1.0
    assert lib.saved["b.dng"]["exposure"] == -0.5
    post(lib, steps={"contrast": 95})
    assert lib.saved["a.dng"]["contrast"] == 100


def test_white_balance_is_re_anchored_per_camera(lib):
    post(lib, changes={"color_temp": 6600}, baseline_temp=5500)   # 1.2x warmer
    assert lib.saved["a.dng"]["color_temp"] == 4800
    assert lib.saved["b.dng"]["color_temp"] == 7200


def test_crop_and_spots_are_never_synced(lib):
    d = post(lib, changes={"crop": {"left": 0}, "spots": [1], "contrast": 5})
    assert d["ignored"] == ["crop", "spots"]
    assert lib.saved["a.dng"]["crop"] == {"left": 0.1, "top": 0.1, "right": 0.9, "bottom": 0.9}
    only = lib.post("/api/sync_settings", json={"paths": ["a.dng"], "changes": {"crop": None}})
    assert only.status_code == 400


def test_one_history_step_per_photo_and_undo(lib, tmp_path):
    d = post(lib, steps={"exposure": 0.33}, label="Exposure +1/3")
    for n in ("a.dng", "b.dng"):
        h = history.load(tmp_path / n)
        assert [e["label"] for e in h["entries"]] == ["Opened with saved edits", "Exposure +1/3"]
    before_b = {"exposure": -1.0}["exposure"]
    u = lib.post("/api/sync_undo", json={"undo": d["undo"]}).get_json()
    assert sorted(u["restored"]) == ["a.dng", "b.dng"]
    assert lib.saved["b.dng"]["exposure"] == before_b
    assert history.load(tmp_path / "a.dng")["index"] == 0


def test_undo_skips_photos_edited_since(lib, tmp_path):
    d = post(lib, steps={"exposure": 0.33})
    post(lib, paths=["a.dng"], steps={"contrast": 5})
    u = lib.post("/api/sync_undo", json={"undo": d["undo"]}).get_json()
    assert u["skipped"] == ["a.dng"] and u["restored"] == ["b.dng"]


def test_foreign_edited_files_are_skipped_and_failures_isolated(lib, monkeypatch):
    monkeypatch.setattr(editor, "detect_foreign_edits", lambda p: "Lightroom" if p.name == "a.dng" else None)
    d = post(lib, paths=["a.dng", "b.dng", "ghost.dng"], changes={"contrast": 1})
    assert d["skipped"][0]["path"] == "a.dng"
    assert d["applied"] == ["b.dng"] and d["failed"][0]["path"] == "ghost.dng"


def test_bad_requests(lib):
    assert lib.post("/api/sync_settings", json={"paths": [], "steps": {"exposure": 1}}).status_code == 400
    assert lib.post("/api/sync_settings", json={"paths": ["a.dng"]}).status_code == 400
    assert lib.post("/api/sync_settings", json={"paths": ["a.dng"], "steps": {"nope": 1}}).status_code == 400
