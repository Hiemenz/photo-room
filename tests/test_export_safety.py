"""Export destination validation, no-overwrite claims, template and size limits."""

import os
import stat
from pathlib import Path

import pytest

import editor
import exporter
import export_api
from test_export_dialog import lib, client, start, wait  # noqa: F401
from test_export import pytestmark  # noqa: F401  (skip without exiftool)


def post(client, body):
    return client.post("/api/export", json={"scope": "current", "path": "IMG_0001.CR2", **body})


def test_destination_inside_the_library_is_refused(client, lib):
    for dest in (lib, lib / "out", lib / "a" / ".." / "b"):
        res = post(client, {"dest": str(dest)})
        assert res.status_code == 400, dest
        assert "inside the photo library" in res.get_json()["error"]
    assert not (lib / "out").exists()


def test_dotdot_and_symlink_into_the_library_are_refused(client, lib, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    link = out / "sneaky"
    link.symlink_to(lib)
    assert post(client, {"dest": str(link)}).status_code == 400
    assert post(client, {"dest": str(out / ".." / "shoot")}).status_code == 400


def test_library_destination_needs_an_explicit_flag(client, lib):
    res = post(client, {"dest": str(lib / "out"), "allow_library": True})
    assert res.status_code == 202


def test_relative_destination_never_lands_in_the_library(lib, monkeypatch, tmp_path):
    monkeypatch.setattr(export_api, "default_destination", lambda: str(tmp_path / "default"))
    assert export_api._destination({"dest": "trip"}) == (tmp_path / "default" / "trip").resolve()


def test_any_normal_folder_still_works(client, lib, tmp_path):
    res = post(client, {"dest": str(tmp_path / "my" / "exports")})
    assert res.status_code == 202
    wait(client, res.get_json())
    assert list((tmp_path / "my" / "exports").glob("*.jpg"))


def test_existing_files_are_not_overwritten_by_default(client, lib, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    for _ in range(2):
        wait(client, post(client, {"dest": str(out)}).get_json())
    names = sorted(p.name for p in out.iterdir())
    assert len(names) == 2 and names[0].endswith(".jpg")


def test_overwrite_needs_the_explicit_flag(client, lib, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    wait(client, post(client, {"dest": str(out)}).get_json())
    (first,) = out.iterdir()
    first.write_bytes(b"mine")
    wait(client, post(client, {"dest": str(out), "overwrite": True}).get_json())
    assert len(list(out.iterdir())) == 1 and first.read_bytes() != b"mine"


def test_a_planted_symlink_is_never_written_through(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    (out / "IMG_0001.jpg").symlink_to(victim)
    got = exporter.claim_output(out, "IMG_0001.jpg")
    assert got.name == "IMG_0001-1.jpg"
    assert victim.read_text() == "precious"
    # even with overwrite, a symlink is stepped around
    got2 = exporter.claim_output(out, "IMG_0001.jpg", overwrite=True)
    assert got2.name != "IMG_0001.jpg" and victim.read_text() == "precious"


# -- L1: template widths ----------------------------------------------------

@pytest.mark.parametrize("tpl", [
    "{seq:150000000d}", "{name.__class__}", "{name[0]}", "{name!r}",
    "{seq:>99999999}", "{unknown}", "x" * 300,
])
def test_hostile_templates_are_refused_without_allocating(tpl):
    with pytest.raises(ValueError):
        exporter.output_name(Path("IMG.CR2"), tpl, ".jpg", 1, (10, 10))


@pytest.mark.parametrize("tpl,expect", [
    ("{name}", "IMG.jpg"), ("{name}_{seq:04d}", "IMG_0001.jpg"),
    ("{name}-{width}x{height}", "IMG-10x10.jpg"), ("{seq:03d}-{name}", "001-IMG.jpg"),
])
def test_documented_templates_still_work(tpl, expect):
    assert exporter.output_name(Path("IMG.CR2"), tpl, ".jpg", 1, (10, 10)) == expect


def test_preview_with_a_giant_width_is_a_400_not_a_500(client):
    res = client.post("/api/export/preview",
                      json={"scope": "current", "path": "IMG_0001.CR2",
                            "template": "{seq:150000000d}"})
    assert res.status_code == 400


# -- L2: body size ------------------------------------------------------------

def test_oversized_request_bodies_are_refused(client):
    big = "x" * (editor.app.config["MAX_CONTENT_LENGTH"] + 10)
    res = client.post("/api/keywords", data=big, content_type="application/json")
    assert res.status_code == 413


# -- L4: token file -----------------------------------------------------------

def test_secret_file_is_private_and_written_atomically(tmp_path, monkeypatch):
    import uploader
    target = tmp_path / "cfg" / "token.json"
    uploader._write_secret(target, "one")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700

    def boom(*a):
        raise OSError("crash")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        uploader._write_secret(target, "two")
    assert target.read_text() == "one"
    assert [p.name for p in target.parent.iterdir()] == ["token.json"]
