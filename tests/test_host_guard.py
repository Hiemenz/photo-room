"""DNS rebinding, forged Host/Origin, session token, and launch-style tests."""

import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from flask.testing import FlaskClient

import editor

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    (tmp_path / "IMG_1.CR2").write_bytes(b"raw")
    return tmp_path


@pytest.fixture
def bare(library):
    """A client with no session token, like a script or foreign page."""
    editor.app.config["TESTING"] = True
    return FlaskClient(editor.app, editor.app.response_class)


@pytest.fixture
def bound(monkeypatch):
    monkeypatch.setitem(editor.app.config, "BOUND_PORT", 5057)


GOOD = {"Host": "127.0.0.1:5057"}


@pytest.mark.parametrize("host", [
    "evil.example:5057", "evil.example", "127.0.0.1.evil.example:5057",
    "localhost.evil.example:5057", "127.0.0.1:6000", "127.0.0.1@evil.example",
    "[::2]:5057", "",
])
@pytest.mark.parametrize("route", ["/api/files", "/", "/api/thumb?path=IMG_1.CR2"])
def test_forged_host_is_refused_on_get(library, bound, host, route):
    c = editor.app.test_client()
    res = c.get(route, headers={"Host": host})
    assert res.status_code == 403


def test_rebinding_post_with_matching_origin_is_refused(library, bound):
    c = editor.app.test_client()
    res = c.post("/api/rate", json={"path": "IMG_1.CR2", "rating": 5},
                 headers={"Host": "evil.example:5057",
                          "Origin": "http://evil.example:5057"})
    assert res.status_code == 403


@pytest.mark.parametrize("host", ["127.0.0.1:5057", "localhost:5057", "[::1]:5057"])
def test_real_hosts_are_accepted(library, bound, host):
    assert editor.app.test_client().get("/api/files", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("origin", [
    "null", "http://evil.example:5057", "http://127.0.0.1:6000",
    "http://127.0.0.1.evil.example:5057", "file://",
])
def test_forged_origin_is_refused_even_with_good_host(library, bound, origin):
    c = editor.app.test_client()
    h = {**GOOD, "Origin": origin}
    assert c.get("/api/files", headers=h).status_code == 403
    assert c.post("/api/rate", json={"path": "IMG_1.CR2", "rating": 5},
                  headers=h).status_code == 403


def test_own_origin_is_accepted(library, bound):
    c = editor.app.test_client()
    res = c.get("/api/files", headers={**GOOD, "Origin": "http://127.0.0.1:5057"})
    assert res.status_code == 200


def test_writes_need_the_session_token(bare, library):
    body = {"path": "IMG_1.CR2", "rating": 5}
    assert bare.post("/api/rate", json=body).status_code == 403
    bad = bare.post("/api/rate", json=body, headers={"X-PhotoRoom-Token": "nope"})
    assert bad.status_code == 403
    assert not (library / "IMG_1.xmp").exists()


def test_index_page_carries_the_token_and_it_works(bare, library):
    html = bare.get("/").get_data(as_text=True)
    token = editor.app.config["PHOTOROOM_TOKEN"]
    assert token in html and len(token) >= 32
    # A GET needs no token (thumbnails are plain <img> loads).
    assert bare.get("/api/files").status_code == 200


# -- launch styles ----------------------------------------------------------

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url, tries=60):
    for _ in range(tries):
        try:
            return urllib.request.urlopen(url, timeout=2).read().decode()
        except Exception:
            time.sleep(0.5)
    raise AssertionError("server did not start")


def test_python_editor_py_exports_use_the_library_not_the_cwd(tmp_path):
    """Run as `python editor.py`, export's root must be the library."""
    import json

    lib = tmp_path / "lib"
    other = tmp_path / "elsewhere"
    lib.mkdir()
    other.mkdir()
    (lib / "IN_LIB.CR2").write_bytes(b"x")
    (other / "OUTSIDE.CR2").write_bytes(b"x")
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "editor.py"), str(lib), "-p", str(port)],
        cwd=other, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        page = _get(f"http://127.0.0.1:{port}/")
        import re
        token = re.search(r'photoroom-token" content="([^"]+)"', page).group(1)
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/export/preview",
            data=json.dumps({"scope": "folder", "dest": str(tmp_path / "out")}).encode(),
            headers={"Content-Type": "application/json", "X-PhotoRoom-Token": token})
        out = json.loads(urllib.request.urlopen(req, timeout=60).read())
        names = json.dumps(out)
        assert "IN_LIB" in names
        assert "OUTSIDE" not in names
    finally:
        proc.terminate()
        proc.wait(10)


def test_import_launch_shares_one_editor_module(library):
    import export_api

    assert export_api._ed() is editor
    assert sys.modules["editor"] is editor


def test_static_assets_follow_the_host_allowlist(library, bound):
    c = editor.app.test_client()
    js = next(p for p in (ROOT / "static" / "js").iterdir())
    url = f"/static/js/{js.name}"
    assert c.get(url, headers=GOOD).status_code == 200
    assert c.get(url, headers={"Host": "evil.example:5057"}).status_code == 403


def test_page_loads_the_token_wrapper_before_any_script(bare, library):
    html = bare.get("/").get_data(as_text=True)
    assert html.index("X-PhotoRoom-Token") < html.index("/static/js/")
