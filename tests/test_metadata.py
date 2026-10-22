"""
Metadata panel: validation, exiftool argument lists, readout parsing, presets and
the API, with exiftool replaced by an in-memory double (the real one is in
test_real_exiftool_metadata.py).
"""

import json
import subprocess
from pathlib import Path

import pytest

import editor
import exifpool
import library
import metadata

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_normalize_trims_and_clears():
    out = metadata.normalize_fields({"title": "  A \t B  ", "caption": " one\r\ntwo \x00", "creator": None})
    assert out == {"title": "A B", "caption": "one\ntwo", "creator": ""}


@pytest.mark.parametrize("bad", [{}, None, {"nope": "x"}, {"title": 3}, {"title": "x" * 2001},
                                 {"copyright_status": "maybe"}])
def test_normalize_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        metadata.normalize_fields(bad)


def test_status_spellings():
    assert metadata.normalize_fields({"copyright_status": "Public Domain"}) == {"copyright_status": "public_domain"}
    assert metadata.normalize_fields({"copyright_status": ""}) == {"copyright_status": "unknown"}


def test_write_args_use_the_lightroom_tags():
    args = metadata.write_args(metadata.normalize_fields({
        "title": "T", "caption": "C", "creator": "Me", "copyright": "(c) Me",
        "copyright_status": "copyrighted", "usage_terms": "None"}))
    assert args == ["-XMP-dc:Title=T", "-XMP-dc:Description=C", "-XMP-dc:Creator=Me", "-XMP-dc:Rights=(c) Me",
                    "-XMP-xmpRights:Marked=True", "-XMP-xmpRights:UsageTerms=None"]
    assert metadata.write_args({"copyright_status": "public_domain"}) == ["-XMP-xmpRights:Marked=False"]
    assert metadata.write_args({"copyright_status": "unknown"}) == ["-XMP-xmpRights:Marked="]


def test_parse_info():
    info = metadata.parse_info({
        "Make": "Canon", "Model": "Canon EOS 1000D", "LensModel": "EF 50mm", "ISO": 800, "ExposureTime": 0.004,
        "FNumber": 2.8, "FocalLength": 50, "DateTimeOriginal": "2026:09:14 18:42:07",
        "GPSLatitude": -33.85, "GPSLongitude": 151.2, "GPSAltitude": 12.4,
        "ImageWidth": 3888, "ImageHeight": 2592})
    assert info["camera"] == "Canon EOS 1000D"
    assert (info["iso"], info["shutter"], info["aperture"], info["focal_length"]) == (800, "1/250 s", "f/2.8", "50 mm")
    assert info["captured"] == "2026-09-14 18:42:07" and info["dimensions"] == "3888 × 2592"
    assert info["gps"]["text"] == "33.85000° S, 151.20000° E, 12 m"


def test_parse_info_of_nothing_is_blank_not_an_error():
    info = metadata.parse_info({})
    assert info["camera"] == "" and info["iso"] is None and info["gps"] is None and info["dimensions"] == ""
    assert metadata.parse_info({"Make": "Nikon", "Model": "D850"})["camera"] == "Nikon D850"
    assert metadata.parse_info({"ExposureTime": 2.5})["shutter"] == "2.5 s"


def test_parse_fields_prefers_xmp_and_falls_back_to_camera_exif():
    assert metadata.parse_fields({"Artist": "Cam", "Copyright": "(c) Cam"})["creator"] == "Cam"
    got = metadata.parse_fields({"Creator": ["A", "B"], "Rights": "R", "Artist": "Cam", "Marked": "True"})
    assert got["creator"] == "A, B" and got["copyright"] == "R" and got["copyright_status"] == "copyrighted"
    assert metadata.parse_fields({"Marked": False})["copyright_status"] == "public_domain"
    assert metadata.parse_fields({"Artist": "Cam"}, fallback=False)["creator"] == ""


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


def test_presets_save_replace_delete(tmp_path):
    saved = metadata.save_preset(tmp_path, "  Copyright ", {"copyright": "(c) Me", "title": "", "copyright_status": "unknown"})
    assert saved == {"copyright": "(c) Me"}                    # empty fields would erase data on apply
    metadata.save_preset(tmp_path, "Copyright", {"creator": "Me"})
    assert metadata.load_presets(tmp_path) == {"Copyright": {"creator": "Me"}}
    assert (tmp_path / metadata.STORE_NAME).exists()
    metadata.delete_preset(tmp_path, "Copyright")
    assert metadata.load_presets(tmp_path) == {}
    with pytest.raises(KeyError):
        metadata.delete_preset(tmp_path, "Copyright")


@pytest.mark.parametrize("name,fields", [("", {"title": "x"}), ("x" * 61, {"title": "x"}),
                                         ("ok", {"title": ""}), ("ok", {"bogus": "x"})])
def test_preset_validation(tmp_path, name, fields):
    with pytest.raises(ValueError):
        metadata.save_preset(tmp_path, name, fields)


def test_a_corrupt_preset_file_reads_as_empty(tmp_path):
    (tmp_path / metadata.STORE_NAME).write_text("{not json")
    assert metadata.load_presets(tmp_path) == {}
    (tmp_path / metadata.STORE_NAME).write_text(json.dumps({"presets": {"a": {"bogus": "x"}, "b": {"title": "t"}}}))
    assert metadata.load_presets(tmp_path) == {"b": {"title": "t"}}


# ---------------------------------------------------------------------------
# API with an exiftool double
# ---------------------------------------------------------------------------


class FakeTool:
    """Remembers XMP-dc/xmpRights assignments per output file and answers -j reads."""

    def __init__(self):
        self.sidecars: dict[str, dict[str, str]] = {}
        self.writes: list[list[str]] = []
        self.fail_on: str | None = None

    def write(self, cmd, **kw):
        self.writes.append(list(cmd))
        target = Path(cmd[cmd.index("-o") + 1] if "-o" in cmd else cmd[-1])
        if self.fail_on and target.name == self.fail_on:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
        tags = self.sidecars.setdefault(str(target), {})
        for arg in cmd:
            if arg.startswith("-XMP-") and "=" in arg:
                name, _, value = arg[1:].partition("=")
                tags[name.split(":")[1]] = value
        if "-o" in cmd:
            target.write_text("<xmp/>")
        return subprocess.CompletedProcess(cmd, 0, stdout="1 image files updated", stderr="")

    def run(self, args, input=None, timeout=0, binary=False, files=None):
        out = []
        for f in files or []:
            if f.endswith(".xmp"):
                t = self.sidecars.get(f, {})
                out.append({"SourceFile": f, **{k: v for k, v in t.items() if v}})
            else:
                out.append({"SourceFile": f, "Make": "Canon", "Model": "Canon EOS", "ISO": 200, "ExposureTime": 0.01})
        return subprocess.CompletedProcess(["exiftool"], 0, stdout=json.dumps(out), stderr="")


@pytest.fixture
def tool(monkeypatch):
    t = FakeTool()
    monkeypatch.setattr(library, "_run_exiftool", lambda args: t.write(["exiftool", *args]))
    monkeypatch.setattr(exifpool, "run", t.run)
    return t


@pytest.fixture
def client(tmp_path, monkeypatch, tool):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    for name in ("a.dng", "b.dng"):
        (tmp_path / name).write_bytes(b"raw")
    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        yield c


def test_read_returns_info_and_empty_fields(client):
    body = client.get("/api/metadata?path=a.dng&path=b.dng").get_json()
    assert [i["path"] for i in body["items"]] == ["a.dng", "b.dng"]
    assert body["items"][0]["info"]["shutter"] == "1/100 s" and body["items"][0]["info"]["iso"] == 200
    assert body["items"][0]["fields"]["title"] == ""


def test_write_creates_a_sidecar_and_never_touches_the_raw(client, tmp_path, tool):
    r = client.post("/api/metadata", json={"paths": ["a.dng", "b.dng"], "fields": {"copyright": "(c) Me"}})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["count"] == 2 and body["previous"] == {"a.dng": {"copyright": ""}, "b.dng": {"copyright": ""}}
    assert (tmp_path / "a.xmp").exists() and (tmp_path / "a.dng").read_bytes() == b"raw"
    assert all(str(tmp_path / "a.dng") not in w[:-2] or "-o" in w for w in tool.writes)
    got = client.get("/api/metadata?path=a.dng").get_json()["items"][0]["fields"]
    assert got["copyright"] == "(c) Me"


def test_previous_values_make_a_working_undo(client):
    client.post("/api/metadata", json={"paths": ["a.dng"], "fields": {"title": "One"}})
    body = client.post("/api/metadata", json={"paths": ["a.dng"], "fields": {"title": "Two"}}).get_json()
    assert body["previous"] == {"a.dng": {"title": "One"}}
    client.post("/api/metadata", json={"per_path": body["previous"]})
    assert client.get("/api/metadata?path=a.dng").get_json()["items"][0]["fields"]["title"] == "One"


def test_a_failing_photo_rolls_the_batch_back(client, tool):
    client.post("/api/metadata", json={"paths": ["a.dng"], "fields": {"title": "Keep"}})
    tool.fail_on = "b.xmp"
    r = client.post("/api/metadata", json={"paths": ["a.dng", "b.dng"], "fields": {"title": "New"}})
    assert r.status_code == 500
    tool.fail_on = None
    assert client.get("/api/metadata?path=a.dng").get_json()["items"][0]["fields"]["title"] == "Keep"


@pytest.mark.parametrize("payload", [
    {"paths": ["a.dng"], "fields": {"bogus": "x"}},
    {"paths": ["a.dng"], "fields": {}},
    {"paths": [], "fields": {"title": "x"}},
    {"paths": ["../../etc/passwd"], "fields": {"title": "x"}},
    {"paths": ["missing.dng"], "fields": {"title": "x"}},
    {"paths": "a.dng", "fields": {"title": "x"}},
    {"fields": {"title": "x"}},
])
def test_bad_writes_are_400(client, payload):
    assert client.post("/api/metadata", json=payload).status_code == 400


def test_reads_cannot_leave_the_library(client):
    assert client.get("/api/metadata?path=../x.dng").status_code == 400
    assert client.get("/api/metadata").status_code == 400


def test_writes_need_the_session_token(client):
    from flask.testing import FlaskClient
    client = FlaskClient(editor.app, editor.app.response_class)
    assert client.post("/api/metadata", json={"paths": ["a.dng"], "fields": {"title": "x"}}).status_code == 403
    assert client.post("/api/metadata/presets", json={"name": "n", "fields": {"title": "x"}}).status_code == 403
    assert client.post("/api/metadata/presets/delete", json={"name": "n"}).status_code == 403


def test_preset_api_flow(client, tmp_path):
    r = client.post("/api/metadata/presets", json={"name": "Rights", "fields": {
        "creator": "Me", "copyright": "(c) Me", "copyright_status": "copyrighted", "title": ""}})
    assert r.get_json()["presets"] == {"Rights": {"creator": "Me", "copyright": "(c) Me", "copyright_status": "copyrighted"}}
    assert client.get("/api/metadata/presets").get_json()["presets"].keys() == {"Rights"}
    client.post("/api/metadata", json={"paths": ["a.dng"], "fields": {"title": "Mine"}})
    r = client.post("/api/metadata/presets/apply", json={"name": "Rights", "paths": ["a.dng", "b.dng"]})
    assert r.status_code == 200 and r.get_json()["count"] == 2
    fields = client.get("/api/metadata?path=a.dng").get_json()["items"][0]["fields"]
    assert fields["title"] == "Mine" and fields["copyright"] == "(c) Me"      # the preset left the title alone
    assert client.post("/api/metadata/presets/apply", json={"name": "Nope", "paths": ["a.dng"]}).status_code == 404
    assert client.post("/api/metadata/presets/delete", json={"name": "Rights"}).status_code == 200
    assert client.post("/api/metadata/presets/delete", json={"name": "Rights"}).status_code == 404
    assert client.post("/api/metadata/presets", json={"name": "", "fields": {"title": "x"}}).status_code == 400
    assert (tmp_path / metadata.STORE_NAME).exists()


def test_a_virtual_copy_shares_its_sources_sidecar(client, tmp_path):
    import catalog
    vid = catalog.create_virtual(tmp_path, "a.dng")["id"]
    r = client.post("/api/metadata", json={"paths": [vid], "fields": {"title": "VC"}})
    assert r.status_code == 200
    assert client.get("/api/metadata?path=a.dng").get_json()["items"][0]["fields"]["title"] == "VC"
