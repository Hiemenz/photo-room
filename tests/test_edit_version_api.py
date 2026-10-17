"""Edit versions through the HTTP API: old edits keep their look until the user upgrades them."""
import numpy as np
import pytest

import editor
import editversion
from test_editor_api import ALL_SAVED, client, library  # noqa: F401  (fixtures)

LEGACY_LINES = ALL_SAVED                                       # no CreatorTool: written before versions
OURS_V2 = ALL_SAVED + ["photo-room (edit v2)", "-"]
OURS_V1 = ALL_SAVED + ["photo-room (edit v1)", "-"]
ADOBE = ALL_SAVED + ["Adobe Photoshop Lightroom Classic 13.0", "15.4"]


def open_with(client, fake_exiftool, lines):
    fake_exiftool.stdout_lines = lines
    res = client.post("/api/open", json={"path": "IMG_0001.dng"})
    assert res.status_code == 200, res.get_json()
    return res.get_json()


def test_a_saved_edit_without_a_version_is_version_one_and_flagged(client, fake_rawpy, fake_exiftool):
    data = open_with(client, fake_exiftool, LEGACY_LINES)
    assert data["adjustments"]["edit_version"] == 1
    assert data["needs_upgrade"] is True
    assert data["source"] == "saved"


@pytest.mark.parametrize("lines,version", [(OURS_V2, 2), (OURS_V1, 1), (ADOBE, 2)])
def test_a_declared_version_is_respected(client, fake_rawpy, fake_exiftool, lines, version):
    data = open_with(client, fake_exiftool, lines)
    assert data["adjustments"]["edit_version"] == version
    assert data["needs_upgrade"] is (version < 2)


def test_a_fresh_photo_is_current(client, fake_rawpy, fake_exiftool):
    data = open_with(client, fake_exiftool, ["-"] * 22)
    assert data["adjustments"]["edit_version"] == editor.EDIT_VERSION
    assert data["needs_upgrade"] is False


def test_legacy_edits_render_exactly_as_before_and_differ_from_the_new_maths(client, fake_rawpy, fake_exiftool):
    adj = {"exposure": 0.7, "contrast": 15, "shadows": 20}
    old = client.post("/api/preview", json={"path": "IMG_0001.dng",
                                            "adjustments": {**adj, "edit_version": 1}}).get_json()
    new = client.post("/api/preview", json={"path": "IMG_0001.dng",
                                            "adjustments": {**adj, "edit_version": 2}}).get_json()
    assert old["preview"] != new["preview"]
    assert old["histogram"] != new["histogram"]


def test_saving_stamps_the_version(client, fake_exiftool):
    client.post("/api/save", json={"path": "IMG_0001.dng", "adjustments": {"exposure": 0.5, "edit_version": 2}})
    assert fake_exiftool.tags["CreatorTool"] == "photo-room (edit v2)"
    client.post("/api/save", json={"path": "IMG_0001.dng", "adjustments": {"exposure": 0.5, "edit_version": 1}})
    assert fake_exiftool.tags["CreatorTool"] == "photo-room (edit v1)"       # not silently relabelled


def test_upgrade_endpoint_returns_a_version_two_edit_and_changes_nothing_on_disk(client, fake_rawpy, fake_exiftool):
    data = open_with(client, fake_exiftool, LEGACY_LINES)
    writes_before = len([c for c in fake_exiftool.calls if any("=" in a for a in c if a.startswith("-"))])
    res = client.post("/api/upgrade_edit", json={"path": "IMG_0001.dng", "adjustments": data["adjustments"]})
    body = res.get_json()
    assert res.status_code == 200, body
    assert body["adjustments"]["edit_version"] == 2
    assert body["report"]["upgraded"] is True
    assert body["report"]["mean_difference_after"] <= body["report"]["mean_difference_before"]
    assert body["preview"].startswith("data:image/jpeg")
    assert body["adjustments"]["exposure"] != data["adjustments"]["exposure"]          # 0.75 -> about 1.6
    assert len([c for c in fake_exiftool.calls if any("=" in a for a in c if a.startswith("-"))]) == writes_before


def test_a_preset_is_not_merged_into_a_legacy_edit(client, fake_exiftool, monkeypatch):
    monkeypatch.setattr(editor.style, "get_preset", lambda name, baseline_temp=5500: {"exposure": 0.5})
    res = client.post("/api/preset_get", json={"name": "x", "edit_version": 1})
    assert res.status_code == 409 and res.get_json()["needs_upgrade"] is True
    ok = client.post("/api/preset_get", json={"name": "x", "baseline_temp": 5000})
    # an unversioned preset predates edit versions: its exposure is re-mapped
    assert ok.get_json()["adjustments"]["exposure"] == pytest.approx(0.5 * editversion.EXPOSURE_FACTOR)
    assert ok.get_json()["adjustments"]["edit_version"] == 2


def test_history_entries_without_a_version_stay_legacy():
    done = editor.complete_adjustments({"exposure": 0.4, "color_temp": 5000}, 5000)
    assert done["edit_version"] == 1
    assert editor.complete_adjustments({"color_temp": 5000}, 5000)["edit_version"] == 2
