"""Doctor logic with every tool faked, so it runs the same everywhere."""

import socket
import types

import photo_room_cli as cli


def fake_run(stdout):
    return lambda *a, **k: types.SimpleNamespace(stdout=stdout, returncode=0)


def test_exiftool_missing_fails_with_fix():
    c = cli.check_exiftool(which=lambda n: None)
    assert c.status == cli.FAIL and ("apt" in c.fix or "brew" in c.fix)


def test_exiftool_versions():
    ok = lambda v: cli.check_exiftool(which=lambda n: "/x/exiftool", run=fake_run(v + "\n"))
    assert ok("13.55").status == cli.PASS
    assert ok("12.76").status == cli.WARN
    assert ok("11.88").status == cli.FAIL
    assert ok("garbage").status == cli.FAIL


def test_exiftool_fix_per_platform(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    assert "brew" in cli.check_exiftool(which=lambda n: None).fix
    monkeypatch.setattr(cli.sys, "platform", "linux")
    assert "apt" in cli.check_exiftool(which=lambda n: None).fix


def test_python_version():
    assert cli.check_python((3, 13, 1)).status == cli.PASS
    c = cli.check_python((3, 11, 4))
    assert c.status == cli.FAIL and "uv python install 3.12" in c.fix


def test_missing_import_is_fail_or_warn():
    def boom(name):
        raise ImportError("no " + name)
    assert cli.check_import("rawpy", "rawpy", boom).status == cli.FAIL
    assert cli.check_import("x", "x", boom, required=False).status == cli.WARN
    assert cli.check_lensfun(boom).status == cli.WARN


def test_port_in_use():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    port = s.getsockname()[1]
    try:
        assert cli.check_port(port).status == cli.WARN
    finally:
        s.close()
    assert cli.check_port(port).status == cli.PASS


def test_unwritable_dir(tmp_path):
    assert cli.check_writable(tmp_path).status == cli.PASS
    assert cli.check_writable(tmp_path / "nope" / "deeper").status == cli.FAIL


def test_table_and_exit_code():
    checks = [cli.Check("a", cli.PASS, "ok"), cli.Check("exiftool", cli.FAIL, "missing", "brew install exiftool")]
    out = cli.format_table(checks)
    assert "FAIL" in out and "fix: brew install exiftool" in out
    assert cli.exit_code(checks) == 1
    assert cli.exit_code(checks[:1] + [cli.Check("b", cli.WARN, "w")]) == 0


def test_preflight_reports_missing_hard_dependency():
    def importer(name):
        if name == "rawpy":
            raise ImportError("libraw missing")
        return types.SimpleNamespace()
    bad = cli.preflight(importer)
    assert [c.name for c in bad] == ["rawpy / LibRaw"]


def test_launch_exits_nonzero_without_traceback(monkeypatch, capsys):
    monkeypatch.setattr(cli, "preflight", lambda: [cli.Check("rawpy / LibRaw", cli.FAIL, "gone", "uv sync")])
    assert cli.launch_editor([]) == 1
    err = capsys.readouterr().err
    assert "uv sync" in err and "Traceback" not in err


def test_banner_injected_and_dismissible():
    from flask import Flask
    app = Flask(__name__)

    @app.route("/")
    def i():
        return "<html><body>hi</body></html>"

    cli.install_exiftool_banner(app, "brew install exiftool")
    body = app.test_client().get("/").get_data(as_text=True)
    assert "brew install exiftool" in body and "Dismiss" in body


def test_ffmpeg_optional(monkeypatch):
    assert cli.check_ffmpeg(which=lambda n: "/x/ffmpeg").status == cli.PASS
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    c = cli.check_ffmpeg(which=lambda n: None)
    assert c.status == cli.WARN and "brew" in c.fix


def test_disk_thresholds(tmp_path):
    usage = lambda gb: (lambda p: types.SimpleNamespace(free=int(gb * 1e9)))
    assert cli.check_disk(tmp_path, usage(500)).status == cli.PASS
    assert cli.check_disk(tmp_path, usage(5)).status == cli.WARN
    assert cli.check_disk(tmp_path / "new" / "dir", usage(0.2)).status == cli.FAIL
