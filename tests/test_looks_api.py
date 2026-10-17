"""The preset panel, LUT library and match-reference endpoints."""

import base64
import io
import json

import numpy as np
import pytest
from PIL import Image

import editor
import history
import lut as L
import presetlib as P


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    for name in ("IMG_0001.dng", "IMG_0002.dng", "IMG_0003.dng"):
        (tmp_path / name).write_bytes(b"fake-dng")
    return tmp_path


@pytest.fixture
def client(library, fake_rawpy, fake_exiftool, monkeypatch):
    editor.app.config["TESTING"] = True
    # A thumbnail stand-in: the app builds these from embedded raw previews.
    buf = io.BytesIO()
    Image.fromarray(fake_rawpy["rgb"]).save(buf, "JPEG")

    class Pool:
        def get_now(self, path, size=160, timeout=120):
            return buf.getvalue()

    monkeypatch.setattr(editor, "_thumb_pool", lambda: Pool())
    with editor.app.test_client() as c:
        yield c


def current(client, path="IMG_0001.dng"):
    return client.post("/api/open", json={"path": path}).get_json()


def jpeg_size(data):
    return Image.open(io.BytesIO(data)).size


def test_the_preset_list_has_builtins_groups_and_luts(client):
    d = client.get("/api/looks/presets").get_json()
    assert len(d["presets"]) >= 25 and {"id", "name", "category", "builtin", "groups", "rev"} <= set(d["presets"][0])
    defaults = {g["id"] for g in d["groups"] if g["default"]}
    assert defaults == set(P.DEFAULT_GROUPS) and "crop" not in defaults and "wb" not in defaults
    assert len([x for x in d["luts"] if x["builtin"]]) >= 5


def test_save_a_preset_with_chosen_groups_then_apply_it_elsewhere(client):
    edit = current(client)["adjustments"]
    edit.update(contrast=33, vibrance=21, clarity=9, crop={"left": .1, "top": .1, "right": .9, "bottom": .9})
    saved = client.post("/api/looks/preset/save", json={
        "name": "Mine", "adjustments": edit, "groups": ["tone", "colour"], "baseline_temp": 5500}).get_json()
    assert saved["ok"] and "Mine" in [p["name"] for p in saved["presets"]]
    again = client.post("/api/looks/preset/save", json={"name": "Mine", "adjustments": edit})
    assert again.status_code == 409 and again.get_json()["exists"]
    assert client.post("/api/looks/preset/save", json={"name": "Mine", "adjustments": edit, "groups": ["tone", "colour"], "overwrite": True}).status_code == 200

    stored = P.get(saved["id"])
    assert stored["values"]["contrast"] == 33 and "clarity" not in stored["values"] and "crop" not in stored["values"]

    other = current(client, "IMG_0002.dng")["adjustments"]
    out = client.post("/api/looks/preset/apply", json={
        "id": saved["id"], "amount": 100, "adjustments": other, "baseline_temp": 5500}).get_json()
    assert out["adjustments"]["contrast"] == 33 and out["adjustments"]["clarity"] == other["clarity"]
    assert out["adjustments"]["preset_name"] == "Mine" and out["label"] == "Preset: Mine"


def test_amount_scales_the_preset_and_is_in_the_history_label(client):
    base = current(client)["adjustments"]
    base["contrast"] = 0
    r = client.post("/api/looks/preset/apply", json={
        "id": "builtin:high-contrast", "amount": 40, "adjustments": base, "baseline_temp": 5500}).get_json()
    assert r["adjustments"]["contrast"] == pytest.approx(18, abs=0.1)
    assert r["label"] == "Preset: High Contrast (40%)" and r["adjustments"]["preset_amount"] == 40
    for bad in (-1, 101, "x"):
        assert client.post("/api/looks/preset/apply", json={
            "id": "builtin:high-contrast", "amount": bad, "adjustments": base}).status_code == 400


def test_old_colour_maths_edits_are_refused_not_silently_mixed(client):
    base = current(client)["adjustments"]
    base["edit_version"] = 1
    r = client.post("/api/looks/preset/apply", json={"id": "builtin:vivid", "amount": 100, "adjustments": base})
    assert r.status_code == 409 and "Upgrade edit" in r.get_json()["error"]


def test_hover_preview_renders_without_changing_anything(client, library):
    base = current(client)["adjustments"]
    steps = len(history.load(library / "IMG_0001.dng")["entries"])
    plain = client.post("/api/preview", json={"path": "IMG_0001.dng", "adjustments": base}).get_json()["preview"]
    r = client.post("/api/looks/preset/preview", json={
        "path": "IMG_0001.dng", "id": "builtin:b-w-high-contrast", "amount": 100,
        "adjustments": base, "baseline_temp": 5500}).get_json()
    assert r["preview"].startswith("data:image/jpeg") and r["preview"] != plain and "histogram" in r
    assert not list(library.glob("*.xmp"))
    assert len(history.load(library / "IMG_0001.dng")["entries"]) == steps
    stale = client.post("/api/looks/preset/preview", json={
        "path": "IMG_0001.dng", "id": "builtin:vivid", "amount": 50, "adjustments": base,
        "baseline_temp": 5500, "seq": 1}).get_json()
    assert "preview" in stale or stale.get("stale")


def test_thumbnails_are_cached_jpegs_made_from_the_photos_thumbnail(client):
    q = "path=IMG_0001.dng&amount=100&baseline_temp=5500"
    a = client.get(f"/api/looks/preset/thumb?{q}&id=builtin:cinematic")
    b = client.get(f"/api/looks/preset/thumb?{q}&id=builtin:b-w-classic")
    assert a.status_code == 200 and a.mimetype == "image/jpeg" and a.data != b.data
    assert "immutable" in a.headers["Cache-Control"]
    assert jpeg_size(a.data) == (64, 64)
    assert client.get(f"/api/looks/preset/thumb?{q}&id=builtin:nope").status_code == 400
    assert client.get(f"/api/looks/lut/thumb?{q}&id=builtin-cool-chrome").status_code == 200
    assert client.get(f"/api/looks/lut/thumb?{q}&id=0123456789ab").status_code == 404


def test_thumbnail_requests_cannot_leave_the_library(client):
    r = client.get("/api/looks/preset/thumb?path=../../etc/passwd&id=builtin:vivid")
    assert r.status_code == 400


def test_batch_apply_merges_into_each_photos_own_edit_with_one_history_step(client, library):
    for name, contrast in (("IMG_0001.dng", 10), ("IMG_0002.dng", -20)):
        adj = current(client, name)["adjustments"]
        adj.update(contrast=contrast, exposure=0.5)
        client.post("/api/save", json={"path": name, "adjustments": adj})
    r = client.post("/api/looks/preset/apply_batch", json={
        "id": "builtin:vivid", "amount": 100, "paths": ["IMG_0001.dng", "IMG_0002.dng"]}).get_json()
    assert r["ok"] and len(r["applied"]) == 2 and "Preset: Vivid" in r["message"]
    for name in ("IMG_0001.dng", "IMG_0002.dng"):
        h = history.load(library / name)
        assert h["entries"][-1]["label"] == "Preset: Vivid"
        assert h["entries"][-1]["adjustments"]["vibrance"] == 40
        assert h["entries"][-1]["adjustments"]["exposure"] == 0.5            # their own edit survives
        assert h["entries"][-2]["adjustments"]["vibrance"] != 40              # one step to undo


def test_batch_apply_reports_failures_and_skips_without_stopping(client):
    r = client.post("/api/looks/preset/apply_batch", json={
        "id": "builtin:vivid", "amount": 100, "paths": ["IMG_0001.dng", "ghost.dng", "../x.dng"]}).get_json()
    assert len(r["applied"]) == 1 and len(r["failed"]) == 2 and not r["ok"]
    assert client.post("/api/looks/preset/apply_batch", json={"id": "builtin:vivid", "paths": []}).status_code == 400


def test_preset_files_export_and_import(client):
    client.post("/api/looks/preset/save", json={"name": "Shareable", "adjustments": {"contrast": 12}, "groups": ["tone"]})
    pid = "user:shareable"
    r = client.get(f"/api/looks/preset/export?ids={pid},builtin:warm")
    assert r.status_code == 200 and "attachment" in r.headers["Content-Disposition"]
    data = json.loads(r.data)
    assert [p["name"] for p in data["presets"]] == ["Shareable", "Warm"]

    client.post("/api/looks/preset/delete", json={"id": pid})
    up = client.post("/api/looks/preset/import", json={
        "filename": "x.json", "data": base64.b64encode(r.data).decode()}).get_json()
    assert up["ok"] and up["imported"] == ["Shareable", "Warm"]
    names = [p["name"] for p in up["presets"] if not p["builtin"]]
    assert names == ["Shareable", "Warm"]
    again = client.post("/api/looks/preset/import", json={"data": base64.b64encode(r.data).decode()}).get_json()
    assert "Shareable (imported)" in again["imported"]                       # never silently overwrites yours


def test_importing_a_lightroom_preset_reports_what_it_skipped(client):
    from tests.test_presetlib import LR_XMP
    r = client.post("/api/looks/preset/import", json={"filename": "Film Pop.xmp", "data": base64.b64encode(LR_XMP).decode()})
    d = r.get_json()
    assert d["imported"] == ["Film Pop"] and "Temperature" in d["skipped"]
    bad = client.post("/api/looks/preset/import", json={"data": base64.b64encode(b"junk").decode()})
    assert bad.status_code == 400 and "not a preset file" in bad.get_json()["error"]
    assert client.post("/api/looks/preset/import", json={"data": "!!!"}).status_code == 400


def test_rename_and_delete_user_presets_only(client):
    client.post("/api/looks/preset/save", json={"name": "Old", "adjustments": {"contrast": 5}, "groups": ["tone"]})
    r = client.post("/api/looks/preset/rename", json={"id": "user:old", "name": "New"}).get_json()
    assert r["id"] == "user:new"
    assert client.post("/api/looks/preset/delete", json={"id": "builtin:warm"}).status_code == 400
    assert client.post("/api/looks/preset/delete", json={"id": "user:new"}).get_json()["ok"]


# ---------------------------------------------------------------------------
# LUT library
# ---------------------------------------------------------------------------

CUBE = "TITLE \"Inverted\"\nLUT_3D_SIZE 2\n" + "\n".join(
    f"{1 - r} {1 - g} {1 - b}" for b in (0, 1) for g in (0, 1) for r in (0, 1)) + "\n"


def test_import_apply_preview_and_delete_a_lut(client):
    r = client.post("/api/looks/lut/import", json={"name": "Invert", "text": CUBE}).get_json()
    lut_id = r["lut"]["id"]
    assert r["ok"] and "Invert" in r["message"] and lut_id in [x["id"] for x in r["luts"]]

    base = current(client)["adjustments"]
    plain = client.post("/api/preview", json={"path": "IMG_0001.dng", "adjustments": base}).get_json()["preview"]
    with_lut = client.post("/api/preview", json={"path": "IMG_0001.dng",
                                                 "adjustments": {**base, "lut": lut_id, "lut_amount": 100}}).get_json()
    assert with_lut["preview"] != plain and "warnings" not in with_lut

    gone = client.post("/api/looks/lut/delete", json={"id": lut_id}).get_json()
    assert gone["ok"] and lut_id not in [x["id"] for x in gone["luts"]]
    missing = client.post("/api/preview", json={"path": "IMG_0001.dng",
                                                "adjustments": {**base, "lut": lut_id}}).get_json()
    assert missing["preview"] == plain and "LUT library" in missing["warnings"][0]


def test_bad_luts_are_refused_with_the_reason(client):
    r = client.post("/api/looks/lut/import", json={"text": "LUT_3D_SIZE 2\n0 0 0\n"})
    assert r.status_code == 400 and "expected 8 data rows" in r.get_json()["error"]
    assert client.post("/api/looks/lut/import", json={"text": ""}).status_code == 400
    assert client.post("/api/looks/lut/delete", json={"id": "builtin-cool-chrome"}).status_code == 400


def test_lut_rename_and_export(client):
    lid = client.post("/api/looks/lut/import", json={"name": "A", "text": CUBE}).get_json()["lut"]["id"]
    assert client.post("/api/looks/lut/rename", json={"id": lid, "name": "B"}).get_json()["lut"]["name"] == "B"
    exp = client.get(f"/api/looks/lut/export?id={lid}")
    assert exp.status_code == 200 and b"LUT_3D_SIZE 2" in exp.data and ".cube" in exp.headers["Content-Disposition"]


# ---------------------------------------------------------------------------
# Match reference look
# ---------------------------------------------------------------------------

def reference_png():
    ramp = np.linspace(0.1, 1, 80, dtype=np.float32)
    img = np.stack([np.tile(ramp, (60, 1)) * 0.8 + 0.2, np.tile(ramp, (60, 1)) * 0.7, np.tile(ramp, (60, 1)) * 0.5], -1)
    buf = io.BytesIO()
    Image.fromarray((img * 255).astype(np.uint8)).save(buf, "PNG")
    return buf.getvalue()


def test_match_reference_returns_an_editable_preset_and_a_report(client):
    r = client.post("/api/looks/match", json={
        "path": "IMG_0001.dng", "reference_data": base64.b64encode(reference_png()).decode()}).get_json()
    values = r["preset"]["values"]
    assert "tone_curve" in values and r["report"]["steps"]
    assert r["report"]["distance_after"]["tone"] <= r["report"]["distance_before"]["tone"]
    saved = client.post("/api/looks/preset/save", json={"name": "Matched A", "preset": r["preset"]}).get_json()
    assert saved["ok"] and P.get(saved["id"])["values"].keys() == values.keys()
    applied = client.post("/api/looks/preset/apply", json={
        "preset": r["preset"], "amount": 50, "adjustments": current(client)["adjustments"]}).get_json()
    assert "tone_curve" in applied["adjustments"]


def test_match_reference_from_another_library_photo_and_bad_input(client):
    ok = client.post("/api/looks/match", json={"path": "IMG_0001.dng", "reference_path": "IMG_0002.dng"})
    assert ok.status_code == 200
    assert client.post("/api/looks/match", json={"path": "IMG_0001.dng"}).status_code == 400
    bad = client.post("/api/looks/match", json={"path": "IMG_0001.dng", "reference_data": base64.b64encode(b"nope").decode()})
    assert bad.status_code == 400 and "not an image" in bad.get_json()["error"]
    assert client.post("/api/looks/match", json={"path": "../x", "reference_data": "AAAA"}).status_code == 400


def test_writes_need_the_session_token_like_every_other_route(library):
    from flask.testing import FlaskClient
    bare = FlaskClient(editor.app, editor.app.response_class)
    for route in ("/api/looks/preset/save", "/api/looks/preset/delete", "/api/looks/lut/import",
                  "/api/looks/lut/delete", "/api/looks/preset/apply_batch", "/api/looks/preset/import"):
        assert bare.post(route, json={}).status_code == 403, route
