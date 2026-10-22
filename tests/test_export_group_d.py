"""
Export group D: Export with Previous, post-export actions, contact-sheet PDF,
more filename tokens, destination options and the optional AVIF / JPEG XL formats.

Uses the real exiftool (skipped when absent, as in the other export tests);
rawpy is faked as elsewhere.
"""

import io
import sys
import time

import pytest
from PIL import Image

import contactsheet_pdf
import editor
import export_presets
import exporter
import postexport
from test_export import make_photo
from test_export import pytestmark  # noqa: F401  (skip without exiftool)
from test_export_dialog import client, lib, start, wait  # noqa: F401  (fixtures)


# ---------------------------------------------------------------------------
# Post-export actions
# ---------------------------------------------------------------------------

def marker_action(marker):
    code = "import sys; open(sys.argv[1], 'w').write('|'.join(sys.argv[2:]))"
    return {"type": "command", "argv": [sys.executable, "-c", code, str(marker)]}


def test_command_gets_files_as_plain_arguments_without_a_shell(tmp_path):
    nasty = tmp_path / "a b; touch pwned.jpg"
    nasty.write_bytes(b"x")
    marker = tmp_path / "m.txt"
    res = postexport.run_action(postexport.normalize(marker_action(marker)), [str(nasty)])
    assert res["ok"], res
    assert marker.read_text() == str(nasty)       # one argument, not interpreted
    assert not (tmp_path / "pwned.jpg").exists()


def test_files_placeholder_and_folder(tmp_path):
    action = postexport.normalize({"type": "command", "argv": ["prog", "--to", "{folder}", "{files}", "--end"]})
    argv = postexport.build_argv({**action, "argv": [sys.executable, *action["argv"][1:]]}, ["/o/a.jpg", "/o/b.jpg"])
    assert argv[1:] == ["--to", "/o", "/o/a.jpg", "/o/b.jpg", "--end"]


def test_string_command_is_split_into_words_not_run_by_a_shell():
    action = postexport.normalize({"type": "command", "argv": 'gimp --new-instance "a b"'})
    assert action["argv"] == ["gimp", "--new-instance", "a b"]


@pytest.mark.parametrize("bad", [{"type": "rm"}, {"type": "command", "argv": []},
                                 {"type": "command", "argv": [1]}, {"type": "open", "app": "a\x00b"}, "gimp"])
def test_bad_actions_are_rejected(bad):
    with pytest.raises(ValueError):
        postexport.normalize(bad)


def test_unknown_program_fails_cleanly_without_raising(tmp_path):
    res = postexport.run_action({"type": "command", "argv": ["/no/such/prog-xyz"]}, [str(tmp_path / "a.jpg")])
    assert res["ok"] is False and "not found" in res["message"]


def test_command_is_stopped_after_the_timeout(tmp_path):
    action = {"type": "command", "argv": [sys.executable, "-c", "import time; time.sleep(30)"]}
    start_t = time.time()
    res = postexport.run_action(action, [str(tmp_path / "a.jpg")], timeout=1)
    assert res["ok"] is False and "did not finish" in res["message"]
    assert time.time() - start_t < 10


def test_failing_command_is_reported(tmp_path):
    action = {"type": "command", "argv": [sys.executable, "-c", "import sys; sys.exit(3)"]}
    res = postexport.run_action(action, [str(tmp_path / "a.jpg")])
    assert res["ok"] is False and "status 3" in res["message"]


def test_job_runs_the_post_action_with_the_written_files(client, tmp_path):
    marker = tmp_path / "marker.txt"
    job = wait(client, start(client, tmp_path / "out", format="jpeg", post_action=marker_action(marker)))
    assert job["state"] == "done" and job["post"]["ok"], job["post"]
    assert marker.read_text() == job["written"][0]


def test_bad_post_action_is_rejected_before_exporting(client, tmp_path):
    res = client.post("/api/export", json={"scope": "current", "path": "IMG_0001.CR2", "dest": str(tmp_path / "o"),
                                           "post_action": {"type": "nope"}})
    assert res.status_code == 400
    assert not (tmp_path / "o").exists()


# ---------------------------------------------------------------------------
# Filename tokens
# ---------------------------------------------------------------------------

def test_rating_camera_keyword_and_text_tokens(client, tmp_path):
    job = wait(client, start(client, tmp_path / "out", format="jpeg",
                             template="{text}_{rating}_{camera}_{keyword}_{name}", custom_text="Wedding"))
    assert job["state"] == "done", job
    assert (tmp_path / "out" / "Wedding_4_T-1000_ferry_IMG_0001.jpg").exists()


def test_text_token_cannot_escape_the_folder(client, tmp_path):
    res = client.post("/api/export/preview", json={
        "scope": "current", "path": "IMG_0001.CR2", "dest": str(tmp_path / "o"), "template": "{text}{name}",
        "custom_text": "../../etc/"})
    name = res.get_json()["names"][0]["name"]
    assert "/" not in name and name.endswith("IMG_0001.jpg")


def test_unknown_token_still_rejected(client, tmp_path):
    res = client.post("/api/export/preview", json={"scope": "current", "path": "IMG_0001.CR2",
                                                    "dest": str(tmp_path), "template": "{lens}"})
    assert res.status_code == 400 and "rating" in res.get_json()["error"]


def test_tokens_are_listed_in_defaults(client):
    tokens = client.get("/api/export/defaults").get_json()["tokens"]
    assert {"{rating}", "{camera}", "{keyword}", "{text}"} <= set(tokens)


# ---------------------------------------------------------------------------
# Destination options
# ---------------------------------------------------------------------------

def test_subfolder_destination(client, tmp_path):
    job = wait(client, start(client, tmp_path / "out", format="jpeg", dest_mode="subfolder", subfolder="Edited"))
    assert job["state"] == "done" and job["dest"] == str(tmp_path / "out" / "Edited")
    assert (tmp_path / "out" / "Edited" / "IMG_0001.jpg").exists()


@pytest.mark.parametrize("sub", ["", "../x", "a/b", ".hidden", ".."])
def test_bad_subfolder_is_rejected(client, tmp_path, sub):
    res = client.post("/api/export", json={"scope": "current", "path": "IMG_0001.CR2", "dest": str(tmp_path / "o"),
                                           "dest_mode": "subfolder", "subfolder": sub})
    assert res.status_code == 400


def test_same_as_original_writes_beside_each_photo_and_never_overwrites(client, lib):
    body = {"scope": "folder", "format": "jpeg", "dest_mode": "original", "template": "{name}"}
    first = client.post("/api/export", json=body).get_json()
    done = wait(client, first)
    assert done["state"] == "done" and len(done["written"]) == 3
    assert all(p.startswith(str(lib)) for p in done["written"])
    assert (lib / "IMG_0002.jpg").exists() and (lib / "IMG_0002.CR2").exists()
    again = wait(client, client.post("/api/export", json=body).get_json())
    assert (lib / "IMG_0002-1.jpg").exists() and again["state"] == "done"


def test_same_as_original_preview_names_per_folder(client, lib):
    data = client.post("/api/export/preview", json={"scope": "folder", "format": "jpeg",
                                                    "dest_mode": "original"}).get_json()
    assert [n["name"] for n in data["names"]] == ["IMG_0001.jpg", "IMG_0002.jpg", "IMG_0003.jpg"]


def test_ask_mode_behaves_like_a_chosen_folder(client, tmp_path):
    job = wait(client, start(client, tmp_path / "out", format="jpeg", dest_mode="ask"))
    assert job["state"] == "done" and (tmp_path / "out" / "IMG_0001.jpg").exists()


def test_library_guard_still_applies_to_a_subfolder(client, lib):
    res = client.post("/api/export", json={"scope": "current", "path": "IMG_0001.CR2", "dest": str(lib),
                                           "dest_mode": "subfolder", "subfolder": "x"})
    assert res.status_code == 400 and "library" in res.get_json()["error"]


# ---------------------------------------------------------------------------
# Export with Previous
# ---------------------------------------------------------------------------

def test_previous_is_unavailable_before_any_export(client):
    assert client.get("/api/export/last").get_json()["settings"] is None
    assert client.get("/api/export/defaults").get_json()["has_previous"] is False
    assert client.post("/api/export/previous", json={"scope": "current", "path": "IMG_0001.CR2"}).status_code == 404


def test_export_with_previous_repeats_settings_on_new_photos(client, tmp_path):
    wait(client, start(client, tmp_path / "out", format="jpeg", quality=71, long_edge=60,
                       template="p_{name}", dest_mode="subfolder", subfolder="Web"))
    last = client.get("/api/export/last").get_json()["settings"]
    assert last["quality"] == 71 and last["template"] == "p_{name}" and last["subfolder"] == "Web"
    assert client.get("/api/export/defaults").get_json()["has_previous"] is True

    res = client.post("/api/export/previous", json={"scope": "selected", "paths": ["IMG_0002.CR2", "IMG_0003.CR2"]})
    assert res.status_code == 202, res.get_json()
    done = wait(client, res.get_json())
    assert done["state"] == "done"
    out = tmp_path / "out" / "Web"
    assert (out / "p_IMG_0002.jpg").exists() and (out / "p_IMG_0003.jpg").exists()
    assert max(Image.open(out / "p_IMG_0002.jpg").size) == 60


def test_cli_previous_flag_and_new_flags(lib, tmp_path, monkeypatch):
    import process

    def cli(*argv):
        monkeypatch.setattr(sys, "argv", ["process.py", "export", *argv])
        process.main()

    out = tmp_path / "cli"
    cli(str(lib / "IMG_0001.CR2"), "-o", str(out), "--format", "jpeg", "--subfolder", "S",
        "--template", "{text}-{rating}-{name}", "--text", "T")
    assert (out / "S" / "T-4-IMG_0001.jpg").exists()
    cli(str(lib / "IMG_0002.CR2"), "--previous")
    assert (out / "S" / "T-4-IMG_0002.jpg").exists()
    cli(str(lib / "IMG_0003.CR2"), "--same-as-original", "--format", "png")
    assert (lib / "IMG_0003.png").exists()


def test_cli_post_command_runs_without_a_shell(lib, tmp_path, monkeypatch):
    import process

    marker = tmp_path / "cli-marker.txt"
    script = tmp_path / "record.py"
    script.write_text("import sys; open(sys.argv[1], 'w').write('|'.join(sys.argv[2:]))")
    monkeypatch.setattr(sys, "argv", ["process.py", "export", str(lib / "IMG_0001.CR2"), "-o", str(tmp_path / "o"),
                                      "--post-command", sys.executable, str(script), str(marker)])
    process.main()
    assert marker.read_text() == str(tmp_path / "o" / "IMG_0001.jpg")


def test_cli_contact_sheet(lib, tmp_path, monkeypatch):
    import process
    import thumbs

    monkeypatch.setattr(thumbs, "build", lambda p, fb, size: fake_thumb(p))
    out = tmp_path / "sheet.pdf"
    monkeypatch.setattr(sys, "argv", ["process.py", "export", str(lib), "--contact-sheet", str(out),
                                      "--sheet-columns", "3"])
    process.main()
    assert out.read_bytes().startswith(b"%PDF")


# ---------------------------------------------------------------------------
# Contact sheet
# ---------------------------------------------------------------------------

def fake_thumb(path):
    buf = io.BytesIO()
    Image.new("RGB", (320, 213), (90, 120, 200)).save(buf, "JPEG")
    return buf.getvalue()


def test_contact_sheet_paginates(tmp_path):
    from pathlib import Path
    paths = [Path(f"IMG_{i:04d}.CR2") for i in range(60)]
    pages = contactsheet_pdf.build_pages(paths, fake_thumb, columns=4, title="Shoot")
    assert len(pages) >= 2
    out = contactsheet_pdf.write_pdf(tmp_path / "s.pdf", paths, fake_thumb, columns=4)
    assert out.read_bytes().startswith(b"%PDF")


def test_contact_sheet_rejects_bad_input():
    from pathlib import Path
    with pytest.raises(ValueError):
        contactsheet_pdf.build_pages([], fake_thumb)
    with pytest.raises(ValueError):
        contactsheet_pdf.build_pages([Path("a.CR2")], fake_thumb, columns=0)
    with pytest.raises(ValueError):
        contactsheet_pdf.build_pages([Path("a.CR2")], fake_thumb, page="a0")


def test_a_failed_thumbnail_leaves_a_placeholder_not_a_failed_sheet():
    from pathlib import Path

    def broken(path):
        raise OSError("no thumb")

    assert len(contactsheet_pdf.build_pages([Path("a.CR2")], broken)) == 1


def test_contact_sheet_endpoint_writes_a_pdf(client, tmp_path):
    res = client.post("/api/export/contact-sheet", json={
        "scope": "selected", "paths": ["IMG_0001.CR2", "IMG_0002.CR2", "IMG_0003.CR2"],
        "dest": str(tmp_path / "sheets"), "columns": 3, "title": "Test"})
    data = res.get_json()
    assert res.status_code == 200, data
    assert data["count"] == 3 and data["path"].endswith("contact-sheet.pdf")
    assert open(data["path"], "rb").read(4) == b"%PDF"


def test_contact_sheet_endpoint_validates(client, tmp_path):
    res = client.post("/api/export/contact-sheet", json={"scope": "selected", "paths": [],
                                                          "dest": str(tmp_path)})
    assert res.status_code == 400
    res = client.post("/api/export/contact-sheet", json={"scope": "current", "path": "IMG_0001.CR2",
                                                          "dest": str(tmp_path), "page": "a0"})
    assert res.status_code == 400


# ---------------------------------------------------------------------------
# AVIF / JPEG XL
# ---------------------------------------------------------------------------

def test_missing_plugin_gives_an_actionable_error(monkeypatch):
    monkeypatch.setattr(exporter, "format_available", lambda f: f not in ("avif", "jxl"))
    with pytest.raises(ValueError, match="pillow-avif-plugin"):
        exporter.ExportOptions(format="avif").validate()
    with pytest.raises(ValueError, match="pillow-jxl-plugin"):
        exporter.ExportOptions(format="jxl").validate()


def test_defaults_only_offer_installed_formats(client, monkeypatch):
    monkeypatch.setattr(exporter, "format_available", lambda f: f != "jxl")
    fmts = client.get("/api/export/defaults").get_json()["formats"]
    assert "avif" in fmts and "jxl" not in fmts and "jpeg" in fmts


@pytest.mark.parametrize("fmt,ext", [("avif", ".avif"), ("jxl", ".jxl")])
def test_plugin_formats_export_when_installed(client, tmp_path, fmt, ext):
    if not exporter.format_available(fmt):
        pytest.skip(f"{fmt} plugin not installed")
    job = wait(client, start(client, tmp_path / "out", format=fmt, quality=80, keep_metadata=False))
    assert job["state"] == "done", job
    assert (tmp_path / "out" / f"IMG_0001{ext}").stat().st_size > 0
