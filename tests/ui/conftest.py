"""
Fixtures for the real-browser tests.

The Flask editor runs in a background thread on a free port, against a tiny
synthetic library. rawpy and exiftool are faked in-process (the same trick as
the rest of the suite), so the only real pieces are Flask, the template and
Chromium. Run with ``uv run pytest -m ui``.
"""

import subprocess
import threading
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

SCREENSHOT_DIR = Path(__file__).resolve().parent / "artifacts"


def pytest_collection_modifyitems(items):
    for item in items:
        if "tests/ui" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.ui)


def _colourful_rgb() -> np.ndarray:
    """A 240x360 frame with every hue band, a grey ramp and some shadow/highlight range."""
    h, w = 240, 360
    img = np.zeros((h, w, 3), dtype=np.float32)
    ys, xs = np.mgrid[0:h, 0:w]
    hue = xs / w * 6.0
    k = lambda n: (n + hue) % 6  # noqa: E731
    f = lambda n: np.clip(np.minimum(k(n), 4 - k(n)), 0, 1)  # noqa: E731
    sat = np.where(ys < h * 0.7, 0.8, 0.0)       # coloured bands over a grey strip
    val = 0.15 + 0.8 * (ys % (h * 0.7)) / (h * 0.7)
    for c, n in enumerate((5, 3, 1)):
        img[:, :, c] = val * (1 - sat * f(n))
    return (img * 255).astype(np.uint8)


class _FakeRaw:
    def __init__(self, rgb):
        self.camera_whitebalance = [2.0, 1.0, 1.5, 1.0]
        self._rgb = rgb

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def postprocess(self, **kwargs):
        if tuple(kwargs.get("gamma") or ()) == (1, 1):          # linear request: the sRGB-decoded picture
            from colour import srgb_decode_exact
            return np.rint(srgb_decode_exact(self._rgb / 255.0) * 65535.0).astype(np.uint16)
        if kwargs.get("output_bps") == 16:
            return (self._rgb.astype(np.float32) / 255.0 * 65535).astype(np.uint16)
        return self._rgb.astype(np.uint8)


class _FakeExiftool:
    """In-memory exiftool: remembers written tags and serves them back."""

    def __init__(self):
        self.tags: dict[str, str] = {}

    def __call__(self, cmd, **kwargs):
        args = [a for a in cmd[1:] if a.startswith("-")]
        if "-j" in cmd:
            out = "[]"
        else:
            wrote = False
            for arg in args:
                if "=" in arg:
                    name, _, value = arg.partition("=")
                    tag = name.split(":")[-1]
                    if tag.lower() == "all":
                        self.tags.clear()
                    else:
                        self.tags[tag] = value
                    wrote = True
            if wrote:
                out = "    1 image files updated"
            else:
                wanted = [a.split(":")[-1] for a in args if ":" in a and "-s3" not in a]
                out = "\n".join(self.tags.get(t, "-") for t in wanted) or "-"
        if not kwargs.get("text"):
            return subprocess.CompletedProcess(cmd, 0, stdout=out.encode(), stderr=b"")
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")


class _Url(str):
    tool = None


# Per test, not shared: saves, history and ratings persist per photo, and a
# test that saved an edit must not change what the next one opens.
@pytest.fixture
def editor_url(tmp_path):
    import editor
    import process
    import style

    root = tmp_path / "library"
    root.mkdir()
    (root / "IMG_0001.dng").write_bytes(b"fake-dng")
    (root / "IMG_0002.dng").write_bytes(b"fake-dng")

    rgb = _colourful_rgb()
    mp = pytest.MonkeyPatch()
    mp.setattr(process.rawpy, "imread", lambda path: _FakeRaw(rgb))
    mp.setattr(editor.rawpy, "imread", lambda path: _FakeRaw(rgb))
    tool = _FakeExiftool()
    mp.setattr(process.subprocess, "run", tool)
    mp.setattr(editor.subprocess, "run", tool)
    mp.setattr(editor, "LIBRARY_ROOT", root)
    mp.setenv("PHOTO_ROOM_EMBED_DNG", "1")  # the fake exiftool models in-place DNG writes
    mp.setattr(style, "DB_PATH", root / "style.duckdb")
    mp.setattr(editor, "_style_model", None)

    server = make_server("127.0.0.1", 0, editor.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = _Url(f"http://127.0.0.1:{server.server_port}")
    url.tool = tool                       # tests may pre-seed tags, as if a sidecar already existed
    try:
        yield url
    finally:
        server.shutdown()
        thread.join(timeout=5)
        mp.undo()


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except PlaywrightError as exc:
            pytest.skip(f"Chromium is not available (run `playwright install chromium`): {exc}")
        yield b
        b.close()


@pytest.fixture
def page(browser, editor_url):
    ctx = browser.new_context(viewport={"width": 1500, "height": 1000})
    pg = ctx.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(f"pageerror: {e}"))
    pg.on("console", lambda m: pg.errors.append(f"console.{m.type}: {m.text}")
          if m.type == "error" else None)
    pg.goto(editor_url)
    yield pg
    ctx.close()


@pytest.fixture
def opened(page):
    """The editor with the first photo open and its preview rendered."""
    page.locator("#file-list .file").first.click()
    page.wait_for_function(
        "document.getElementById('preview').complete && "
        "document.getElementById('preview').naturalWidth > 0 && !document.getElementById('save').disabled"
    )
    return page
