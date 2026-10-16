"""
Tests for the editor's exposure as a local web server.

It manages a photograph library and is reachable from any page open in the
same browser. A cross-origin form POST with Content-Type text/plain skips
the CORS preflight entirely, and request.get_json(force=True) parsed it
regardless — so any site visited while the editor was running could reject
photographs, strip develop settings, or overwrite a whole shoot.
"""

import ast
from pathlib import Path

import pytest

import editor


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    (tmp_path / "photo.dng").write_bytes(b"dng")
    return tmp_path


@pytest.fixture
def client(library):
    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        yield c


WRITE_ROUTES = [
    ("/api/rate", {"path": "photo.dng", "rating": 5}),
    ("/api/reset", {"path": "photo.dng"}),
    ("/api/save", {"path": "photo.dng", "adjustments": {}}),
    ("/api/recover", {"path": "photo.dng"}),
    ("/api/batch_apply", {"paths": ["photo.dng"], "adjustments": {}}),
    ("/api/preset_delete", {"name": "x"}),
]


@pytest.mark.parametrize("route,body", WRITE_ROUTES)
def test_a_foreign_origin_cannot_change_anything(client, route, body):
    res = client.post(route, json=body, headers={"Origin": "https://evil.example"})
    assert res.status_code == 403
    assert "another site" in res.get_json()["error"]


@pytest.mark.parametrize("route,body", WRITE_ROUTES)
def test_the_editors_own_page_still_works(client, route, body, fake_rawpy, fake_exiftool):
    """The check must not break the application it protects."""
    res = client.post(route, json=body, headers={"Origin": "http://localhost"})
    assert res.status_code != 403


def test_a_text_plain_body_is_refused(client):
    """
    This is the shape that skips the CORS preflight. Requiring a JSON
    content type means a cross-origin attempt must ask permission first,
    and no permission is granted.
    """
    res = client.post(
        "/api/rate",
        data='{"path":"photo.dng","rating":-1}',
        content_type="text/plain",
    )
    assert res.status_code == 415


def test_a_form_encoded_body_is_refused(client):
    res = client.post(
        "/api/reset",
        data={"path": "photo.dng"},
        content_type="application/x-www-form-urlencoded",
    )
    assert res.status_code == 415


def test_reading_is_not_blocked_by_origin(client):
    """A GET changes nothing, and blocking it would break nothing useful."""
    res = client.get("/api/files", headers={"Origin": "https://evil.example"})
    assert res.status_code == 200


def test_a_request_without_an_origin_is_allowed(client, fake_rawpy, fake_exiftool):
    """
    Browsers attach Origin to every cross-origin request; a command-line
    client sends none. Refusing those would break scripting for no gain.
    """
    res = client.post("/api/rate", json={"path": "photo.dng", "rating": 3})
    assert res.status_code != 403


def test_no_route_parses_a_body_regardless_of_content_type():
    """
    force=True was what let the text/plain form POST through. Checked
    structurally so it cannot return in a new route.
    """
    source = Path("editor.py").read_text()
    offenders = []

    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "get_json":
            continue
        if any(k.arg == "force" and getattr(k.value, "value", False) for k in node.keywords):
            offenders.append(node.lineno)

    assert not offenders, f"get_json(force=True) at lines {offenders}"


def test_the_server_binds_only_to_loopback():
    """Binding to all interfaces would expose a photo library to the network."""
    source = Path("editor.py").read_text()
    assert 'app.run(host="127.0.0.1"' in source
    assert '0.0.0.0' not in source


def test_no_command_is_run_through_a_shell():
    """
    Every subprocess call passes a list, so a filename can never be
    interpreted as shell syntax.
    """
    for module in ("editor.py", "process.py", "ingest.py", "thumbs.py", "uploader.py"):
        source = Path(module).read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "shell" and getattr(kw.value, "value", False):
                        pytest.fail(f"{module}:{node.lineno} runs a command through a shell")


@pytest.mark.parametrize("attack", [
    "../../../../etc/passwd",
    "/etc/passwd",
    "sub/../../escape.dng",
    "....//....//etc/passwd",
])
def test_paths_outside_the_library_are_still_refused(client, attack):
    res = client.post("/api/open", json={"path": attack})
    assert res.status_code == 400


def test_a_symlink_out_of_the_library_is_refused(client, library):
    """resolve() follows symlinks, so the check sees the real destination."""
    outside = library.parent / "secret.dng"
    outside.write_bytes(b"secret")
    (library / "innocent.dng").symlink_to(outside)

    res = client.post("/api/open", json={"path": "innocent.dng"})
    assert res.status_code == 400
    assert "outside" in res.get_json()["error"].lower()
