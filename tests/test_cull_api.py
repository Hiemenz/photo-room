"""Cull and Auto edit over HTTP: analysis jobs, suggestions, an explicit undoable apply, learning, Auto edit."""

import io
import json
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import editor
import library
import thumbs
from test_cull import textured

DATES = {"IMG_0001.CR2": "2024-05-01 10:00:00", "IMG_0002.CR2": "2024-05-01 10:00:01",
         "IMG_0003.CR2": "2024-05-01 10:00:02", "IMG_0004.CR2": "2024-05-01 12:30:00"}


def jpeg(arr):
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="JPEG", quality=88)
    return buf.getvalue()


@pytest.fixture
def lib(tmp_path, monkeypatch, fake_rawpy, fake_exiftool):
    """Three frames of one scene (sharp, soft, blown) a second apart, plus one unrelated frame."""
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    frames = {"IMG_0001.CR2": textured(seed=1), "IMG_0002.CR2": textured(seed=1, blur=3.0),
              "IMG_0003.CR2": textured(seed=1, offset=235), "IMG_0004.CR2": textured(seed=7, gain=0.6)}
    for name, arr in frames.items():
        (tmp_path / name).write_bytes(jpeg(arr))
    monkeypatch.setattr(thumbs, "_embedded_preview", lambda p: (Path(p).read_bytes(), 1))
    meta = {n: {"DateTimeOriginal": d} for n, d in DATES.items()}

    def tool(cmd, **kw):
        if "-j" in cmd:
            out = [{"SourceFile": f, **meta.get(Path(f).name, {})} for f in kw["input"].splitlines()]
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(out), stderr="")
        return fake_exiftool(cmd, **kw)

    monkeypatch.setattr(subprocess, "run", tool)
    monkeypatch.setattr(editor.subprocess, "run", tool)
    import process
    monkeypatch.setattr(process.subprocess, "run", tool)
    library.clear_cache()
    editor._base_cache.clear()
    editor._decode_cache.clear()
    yield tmp_path
    library.clear_cache()


@pytest.fixture
def client(lib):
    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        yield c


def wait(client, job_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/cull/job?id={job_id}").get_json()
        if j["state"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def analyse_and_suggest(client, **body):
    r = client.post("/api/cull/analyze", json={"scope": "all", **body}).get_json()
    job = wait(client, r["job"])
    assert job["state"] == "done", job
    return client.post("/api/cull/suggest", json={"scope": "all", **body}).get_json()


def flags(client):
    return {i["name"]: i["flag"] for i in client.get("/api/library").get_json()["items"]}


def test_analysis_job_caches_features_and_second_run_is_instant(client):
    r = client.post("/api/cull/analyze", json={"scope": "all"}).get_json()
    assert r["missing"] == 4
    assert wait(client, r["job"])["result"]["analysed"] == 4
    again = client.post("/api/cull/analyze", json={"scope": "all"}).get_json()
    assert again["missing"] == 0 and again["cached"] == 4


def test_suggest_groups_the_burst_and_explains_every_photo(client):
    res = analyse_and_suggest(client)
    assert res["missing"] == 0
    groups = [[p["name"] for p in g["photos"]] for g in res["groups"]]
    assert groups == [["IMG_0001.CR2", "IMG_0002.CR2", "IMG_0003.CR2"], ["IMG_0004.CR2"]]
    burst = {p["name"]: p for p in res["groups"][0]["photos"]}
    assert burst["IMG_0001.CR2"]["best"] and burst["IMG_0001.CR2"]["action"] == "pick"
    assert burst["IMG_0002.CR2"]["action"] == "reject" and burst["IMG_0003.CR2"]["action"] == "reject"
    assert all(p["reasons"] and len(p["vec"]) == 8 for g in res["groups"] for p in g["photos"])


def test_suggesting_changes_nothing(client, lib):
    before = flags(client)
    analyse_and_suggest(client)
    assert flags(client) == before == {n: "none" for n in DATES}
    assert not any(p.suffix == ".xmp" for p in lib.iterdir())


def test_scope_selection_and_validation(client):
    r = client.post("/api/cull/analyze", json={"scope": "selection", "paths": ["IMG_0001.CR2"]}).get_json()
    assert r["missing"] == 1
    assert client.post("/api/cull/analyze", json={"scope": "selection"}).status_code == 400
    assert client.post("/api/cull/analyze", json={"scope": "bogus"}).status_code == 400
    assert client.post("/api/cull/suggest", json={"scope": "all", "window": "x"}).status_code == 400


def choices_from(res, override=None):
    out = []
    for g in res["groups"]:
        for p in g["photos"]:
            out.append({"path": p["path"], "suggested": p["action"], "action": (override or {}).get(p["name"], p["action"]),
                        "group": g["id"], "gsize": len(g["photos"]), "vec": p["vec"]})
    return out


def test_apply_is_explicit_logged_and_undoable(client):
    res = analyse_and_suggest(client)
    r = client.post("/api/cull/apply", json={"choices": choices_from(res)}).get_json()
    assert r["changed"] == 3 and r["counts"] == {"pick": 1, "reject": 2, "none": 0} and r["logged"] == 4
    assert flags(client) == {"IMG_0001.CR2": "pick", "IMG_0002.CR2": "reject", "IMG_0003.CR2": "reject",
                             "IMG_0004.CR2": "none"}
    acc = client.get("/api/cull/stats").get_json()["accuracy"]
    assert acc["decisions"] == 4 and acc["overall"] == 1.0
    u = client.post("/api/cull/undo", json={"batch": r["batch"]}).get_json()
    assert u["restored"] == 3
    assert flags(client) == {n: "none" for n in DATES}
    assert client.get("/api/cull/stats").get_json()["accuracy"]["decisions"] == 0      # undone: not trained on
    assert client.post("/api/cull/undo", json={"batch": r["batch"]}).status_code == 409


def test_user_override_lowers_agreement_and_is_remembered(client):
    res = analyse_and_suggest(client)
    ch = choices_from(res, {"IMG_0002.CR2": "pick", "IMG_0001.CR2": "none"})
    client.post("/api/cull/apply", json={"choices": ch})
    assert flags(client)["IMG_0002.CR2"] == "pick"
    acc = client.get("/api/cull/stats").get_json()["accuracy"]
    assert acc["overall"] == 0.5 and acc["overridden_suggestions"]["pick"] == 1


def test_apply_validates_input(client):
    assert client.post("/api/cull/apply", json={"choices": []}).status_code == 400
    bad = {"path": "IMG_0001.CR2", "action": "delete", "suggested": "pick"}
    assert client.post("/api/cull/apply", json={"choices": [bad]}).status_code == 400
    out = {"path": "../x.CR2", "action": "pick", "suggested": "pick"}
    assert client.post("/api/cull/apply", json={"choices": [out]}).status_code == 400


def test_applying_a_reject_keeps_the_stars_for_un_rejecting(client):
    client.post("/api/rate", json={"path": "IMG_0002.CR2", "rating": 4})
    res = analyse_and_suggest(client)
    client.post("/api/cull/apply", json={"choices": choices_from(res)})
    assert flags(client)["IMG_0002.CR2"] == "reject"
    client.post("/api/flag", json={"paths": ["IMG_0002.CR2"], "flag": "none"})
    item = next(i for i in client.get("/api/library").get_json()["items"] if i["name"] == "IMG_0002.CR2")
    assert item["rating"] == 4


def test_export_reset_and_clear(client, lib):
    res = analyse_and_suggest(client)
    client.post("/api/cull/apply", json={"choices": choices_from(res)})
    ex = client.get("/api/cull/export")
    assert "attachment" in ex.headers["Content-Disposition"]
    body = json.loads(ex.data)
    assert len(body["decisions"]) == 4 and body["format"] == "photo-room-cull-export"
    assert (lib / ".photo-room-cull.sqlite").exists()
    assert client.post("/api/cull/reset", json={}).get_json()["ok"]
    assert client.get("/api/cull/stats").get_json()["accuracy"]["decisions"] == 0
    assert client.post("/api/cull/analyze", json={"scope": "all"}).get_json()["missing"] == 0     # cache kept
    client.post("/api/cull/clear", json={})
    assert client.post("/api/cull/analyze", json={"scope": "all"}).get_json()["missing"] == 4


def test_requires_json_and_host_guard(client):
    assert client.post("/api/cull/apply", data="x", content_type="text/plain").status_code in (400, 403, 415)


# ----------------------------------------------------------------------------- Auto edit

def history_of(client, path):
    return client.post("/api/history", json={"path": path}).get_json()["history"]


def test_autoedit_preview_writes_nothing(client):
    r = client.post("/api/cull/autoedit/preview", json={"paths": ["IMG_0001.CR2"]})
    assert r.status_code == 200, r.get_json()
    ph = r.get_json()["photos"][0]
    assert ph["before"].startswith("data:image/jpeg") and ph["after"].startswith("data:image/jpeg")
    assert "color_temp" in ph["changes"] or "exposure" in ph["changes"]
    assert history_of(client, "IMG_0001.CR2")["entries"] == []
    assert client.post("/api/cull/autoedit/preview", json={"paths": []}).status_code == 400
    assert client.post("/api/cull/autoedit/preview", json={"paths": [f"x{i}" for i in range(9)]}).status_code == 400


def test_autoedit_apply_is_one_history_step_each_and_undoable(client):
    paths = ["IMG_0001.CR2", "IMG_0004.CR2"]
    job = wait(client, client.post("/api/cull/autoedit/apply", json={"paths": paths}).get_json()["job"])
    assert job["state"] == "done" and job["result"]["applied"] == 2 and not job["result"]["failed"], job
    for p in paths:
        h = history_of(client, p)
        assert [e["label"] for e in h["entries"]] == ["Auto edit"] and h["has_original"]
    batch = job["result"]["batch"]
    u = client.post("/api/cull/autoedit/undo", json={"batch": batch}).get_json()
    assert u["restored"] == 2 and not u["failed"]
    assert client.post("/api/cull/autoedit/undo", json={"batch": batch}).status_code == 409


def test_autoedit_learns_the_corrections_made_afterwards(client):
    paths = list(DATES)
    job = wait(client, client.post("/api/cull/autoedit/apply", json={"paths": paths}).get_json()["job"])
    assert job["result"]["applied"] == 4
    # Untouched Auto edits teach nothing.
    assert client.get("/api/cull/stats").get_json()["offset_model"]["photos"] == 0
    for p in paths:                                    # the user brightens every one by half a stop
        entry = history_of(client, p)["entries"][-1]["id"]
        adj = dict(client.post("/api/history/jump", json={"path": p, "id": entry}).get_json()["adjustments"])
        adj["exposure"] = round(adj["exposure"] + 0.5, 2)
        client.post("/api/history/record", json={"path": p, "label": "My tweak", "adjustments": adj})
    typ = client.get("/api/cull/stats").get_json()["offset_model"]["typical"]
    assert typ["exposure"]["samples"] == 4 and 0.45 < typ["exposure"]["offset"] < 0.55
    prev = client.post("/api/cull/autoedit/preview", json={"paths": [paths[0]]}).get_json()["photos"][0]
    assert 0 < prev["learned"]["exposure"] < 0.5          # shifted toward the user's taste, but shrunk
    # a reset forgets it
    client.post("/api/cull/reset", json={})
    prev = client.post("/api/cull/autoedit/preview", json={"paths": [paths[0]]}).get_json()["photos"][0]
    assert prev["learned"] == {}
