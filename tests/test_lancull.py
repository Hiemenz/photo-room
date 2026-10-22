"""LAN cull server: pairing, allowlist, writes, expiry, traversal; editor host guard unchanged."""

import io

import pytest
from PIL import Image

import editor
import lancull
import security

PORT = 5055
H = {"Host": f"192.168.1.20:{PORT}"}


class FakeBackend:
    def __init__(self, root):
        self.root = root
        self.folder = ""
        self.marks = []
        self.rels = ["a/one.CR2", "a/two.CR2"]

    def items(self):
        return [{"rel": r, "name": r.split("/")[-1], "rating": 0, "flag": "none", "group": 0} for r in self.rels]

    def preview(self, rel):
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(buf, "JPEG")
        return buf.getvalue()

    def mark(self, rel, flag, rating):
        self.marks.append((rel, flag, rating))


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    gate = lancull.Gate(idle_seconds=60, clock=clock)
    be = FakeBackend(tmp_path)
    app = lancull.create_app(be, gate, port=PORT)
    return app, gate, be, clock


def paired(env):
    app, gate, *_ = env
    c = app.test_client()
    r = c.get(f"/?t={gate.token}", headers=H)
    assert r.status_code == 303 and "pr_cull" in r.headers["Set-Cookie"]
    assert "HttpOnly" in r.headers["Set-Cookie"] and "SameSite=Strict" in r.headers["Set-Cookie"]
    return c


def ids(c):
    return [i["id"] for i in c.get("/api/items", headers=H).get_json()["items"]]


def test_token_required(env):
    app, *_ = env
    c = app.test_client()
    assert c.get("/", headers=H).status_code == 401
    assert c.get("/api/items", headers=H).status_code == 401
    assert c.post("/api/mark", json={"id": "x", "flag": "pick"}, headers=H).status_code == 401
    assert c.get("/?t=wrong", headers=H).status_code == 401
    assert env[2].marks == []


def test_token_is_one_time_and_page_served_after(env):
    app, gate, *_ = env
    tok = gate.token
    c = paired(env)
    assert c.get("/", headers=H).status_code == 200
    # a second phone cannot reuse the token
    assert app.test_client().get(f"/?t={tok}", headers=H).status_code == 401


def test_rate_limit(env):
    app, gate, be, clock = env
    gate.idle = 600
    c = app.test_client()
    codes = [c.get("/?t=bad", headers=H).status_code for _ in range(lancull.MAX_FAILS)]
    assert set(codes) == {401}
    assert c.get("/?t=bad", headers=H).status_code == 429
    # even the right token is refused while blocked, then works after the window
    assert c.get(f"/?t={gate.token}", headers=H).status_code == 429
    clock.t += lancull.FAIL_WINDOW_S + 1
    assert c.get(f"/?t={gate.token}", headers=H).status_code == 303


def test_allowlist(env):
    c = paired(env)
    for path in ("/static/x", "/api/cull/apply", "/api/library", "/api/export", "/editor", "/api/mark/x", "/file?path=a"):
        assert c.get(path, headers=H).status_code == 404, path
    assert c.post("/api/items", headers=H).status_code == 404
    assert c.delete("/", headers=H).status_code == 404
    assert c.put("/api/mark", json={}, headers=H).status_code == 404


def test_host_and_origin_checks(env):
    c = paired(env)
    assert c.get("/api/items", headers={"Host": f"evil.example:{PORT}"}).status_code == 403
    assert c.get("/api/items", headers={"Host": "192.168.1.20:9999"}).status_code == 403
    assert c.get("/api/items", headers={**H, "Origin": "http://evil.example"}).status_code == 403
    assert c.get("/api/items", headers={**H, "Origin": f"http://192.168.1.20:{PORT}"}).status_code == 200


def test_mark_writes_only_ratings_and_flags(env):
    app, gate, be, _ = env
    c = paired(env)
    a, b = ids(c)
    assert c.post("/api/mark", json={"id": a, "flag": "pick"}, headers=H).status_code == 200
    assert c.post("/api/mark", json={"id": b, "rating": 5}, headers=H).status_code == 200
    assert c.post("/api/mark", json={"id": b, "flag": "none", "rating": 3}, headers=H).status_code == 200
    assert be.marks == [("a/one.CR2", "pick", None), ("a/two.CR2", None, 5), ("a/two.CR2", "none", 3)]
    for bad in ({"id": a}, {"id": a, "flag": "delete"}, {"id": a, "rating": 6}, {"id": a, "rating": True},
                {"id": a, "rating": "5"}, {"id": a, "flag": "pick", "path": "/etc/passwd"},
                {"id": "../../etc/passwd", "flag": "pick"}, {"id": 3, "flag": "pick"}, [1]):
        assert c.post("/api/mark", json=bad, headers=H).status_code in (400, 404), bad
    assert c.post("/api/mark", data="id=1", headers=H).status_code == 415
    assert len(be.marks) == 3


def test_preview_by_opaque_id_only(env):
    c = paired(env)
    a, _ = ids(c)
    r = c.get(f"/api/preview/{a}", headers=H)
    assert r.status_code == 200 and r.mimetype == "image/jpeg"
    for bad in ("..%2f..%2fetc%2fpasswd", "a%2Fone.CR2", "IMG_1.CR2", "0" * 16):
        assert c.get(f"/api/preview/{bad}", headers=H).status_code == 404


def test_idle_expiry_and_exit_kill_access(env):
    app, gate, be, clock = env
    c = paired(env)
    clock.t = 30
    assert c.get("/api/items", headers=H).status_code == 200      # activity resets the timer
    clock.t = 80
    assert c.get("/api/items", headers=H).status_code == 200
    clock.t = 80 + 61
    assert c.get("/api/items", headers=H).status_code == 410
    assert c.get("/", headers=H).status_code == 410
    assert gate.token == "" and not gate.sessions


def test_expire_on_exit(env):
    app, gate, *_ = env
    tok = gate.token
    gate.expire()
    assert app.test_client().get(f"/?t={tok}", headers=H).status_code == 410


def test_devices_option():
    g = lancull.Gate(devices=2)
    t = g.token
    assert g.redeem(t) and g.redeem(t) and g.redeem(t) is None


def test_safe_join(tmp_path):
    (tmp_path / "ok").mkdir()
    (tmp_path / "ok" / "a.CR2").write_bytes(b"x")
    outside = tmp_path.parent / (tmp_path.name + "_out")
    outside.mkdir()
    (outside / "s.CR2").write_bytes(b"x")
    (tmp_path / "link.CR2").symlink_to(outside / "s.CR2")
    assert security.safe_join(tmp_path, "ok/a.CR2") == (tmp_path / "ok" / "a.CR2").resolve()
    for bad in ("../x", "ok/../../x", "/etc/passwd", "ok/nothere", "ok", "link.CR2", "a\0b", "..\\x", "", None):
        assert security.safe_join(tmp_path, bad) is None, bad


def test_real_backend_writes_through_editor_paths(tmp_path, monkeypatch):
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "IMG_1.CR2").write_bytes(b"raw")
    calls = []
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    monkeypatch.setattr(editor, "_flag_paths", lambda paths, flag: calls.append(("flag", [p.name for p in paths], flag)))
    monkeypatch.setattr(editor, "_set_fields", lambda u: calls.append(("fields", u)))
    be = lancull.LibraryBackend(tmp_path, "s")
    be.mark("s/IMG_1.CR2", "pick", 4)
    assert calls == [("flag", ["IMG_1.CR2"], "pick"), ("fields", {"s/IMG_1.CR2": {"rating": 4}})]
    for bad in ("../x", "/etc/passwd", "s/missing.CR2"):
        with pytest.raises(FileNotFoundError):
            be.mark(bad, "pick", None)
    with pytest.raises(ValueError):
        lancull.LibraryBackend(tmp_path, "../..")


def test_editor_still_refuses_non_local_hosts(tmp_path, monkeypatch):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    monkeypatch.setitem(editor.app.config, "BOUND_PORT", 5000)
    c = editor.app.test_client()
    for host in ("192.168.1.20:5000", "myphone.local:5000", "evil.example:5000"):
        assert c.get("/api/library", headers={"Host": host}).status_code == 403
    assert not security.host_allowed("192.168.1.20:5000", 5000)
    assert not security.host_allowed("192.168.1.20", None)
    assert security.host_allowed("127.0.0.1:5000", 5000)
