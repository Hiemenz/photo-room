"""Shared fixtures.

No camera raw files are checked into the repo, so the tests stand in a fake
rawpy decoder and a fake exiftool. That keeps the suite hermetic and fast
while still exercising our own logic end to end.
"""

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class FakeRaw:
    """Stands in for the object rawpy.imread() yields."""

    def __init__(self, whitebalance, rgb):
        self.camera_whitebalance = whitebalance
        self._rgb = rgb

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def postprocess(self, **kwargs):
        self.postprocess_kwargs = kwargs
        # rawpy honours output_bps; mirror that so callers get the dtype they ask for.
        if kwargs.get("output_bps") == 16:
            return (self._rgb.astype(np.float32) / 255.0 * 65535).astype(np.uint16)
        return self._rgb.astype(np.uint8)


@pytest.fixture
def gradient_rgb():
    """A 64x64 RGB image ramping dark to bright, with a distinct channel tilt."""
    ramp = np.linspace(0, 255, 64, dtype=np.float32)
    img = np.zeros((64, 64, 3), dtype=np.float32)
    img[:, :, 0] = ramp[None, :]
    img[:, :, 1] = ramp[:, None]
    img[:, :, 2] = 128.0
    return img.astype(np.uint8)


@pytest.fixture
def fake_rawpy(monkeypatch, gradient_rgb):
    """Patch rawpy.imread in both modules to yield a deterministic fake raw."""
    state = {"whitebalance": [2.0, 1.0, 1.5, 1.0], "rgb": gradient_rgb}

    def _imread(path):
        return FakeRaw(state["whitebalance"], state["rgb"])

    import editor
    import process

    monkeypatch.setattr(process.rawpy, "imread", _imread)
    monkeypatch.setattr(editor.rawpy, "imread", _imread)
    return state


@pytest.fixture
def fake_exiftool(monkeypatch):
    """
    Replace subprocess.run with a scriptable exiftool double.

    Set ``tool.stdout_lines`` for what the next -s3 read should return, and
    inspect ``tool.calls`` to assert on the arguments we passed.
    """

    class FakeTool:
        """
        Keeps written tags in memory and serves them back on reads, so a
        write-then-verify sequence behaves like the real tool. Set
        ``stdout_lines`` to override reads for a specific scenario.
        """

        def __init__(self):
            self.calls: list[list[str]] = []
            self.stdout_lines: list[str] | None = None
            self.stdout_bytes: bytes = b""
            self.returncode = 0
            self.stderr = ""
            self.tags: dict[str, str] = {}

        def __call__(self, cmd, **kwargs):
            self.calls.append(list(cmd))
            args = [a for a in cmd[1:] if a.startswith("-")]

            # Real exiftool creates the file named by -o; callers check for
            # it, because exiftool warns while still succeeding.
            if "-o" in cmd:
                out = Path(cmd[cmd.index("-o") + 1])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text("<x:xmpmeta/>")

            # A write: "-GROUP:Tag=value". Record it, unless it clears a group.
            wrote = False
            for arg in args:
                if "=" not in arg:
                    continue
                name, _, value = arg.partition("=")
                tag = name.split(":")[-1]
                if tag.lower() == "all":
                    self.tags.clear()
                else:
                    self.tags[tag] = value
                wrote = True

            if not kwargs.get("text"):
                return subprocess.CompletedProcess(
                    cmd, self.returncode, stdout=self.stdout_bytes, stderr=self.stderr
                )

            if wrote:
                out = "    1 image files updated"
            elif self.stdout_lines is not None:
                out = "\n".join(self.stdout_lines)
            else:
                requested = [a.split(":")[-1] for a in args if "-s3" not in a and ":" in a]
                out = "\n".join(self.tags.get(t, "-") for t in requested)

            return subprocess.CompletedProcess(
                cmd, self.returncode, stdout=out, stderr=self.stderr
            )

        def last_call(self) -> list[str]:
            assert self.calls, "no subprocess call was made"
            return self.calls[-1]

        def write_call(self) -> list[str]:
            """The most recent call that set tags, skipping verification reads."""
            for cmd in reversed(self.calls):
                if any("=" in a for a in cmd if a.startswith("-")):
                    return cmd
            raise AssertionError("no exiftool write was made")

    tool = FakeTool()
    import editor
    import process

    monkeypatch.setattr(process.subprocess, "run", tool)
    monkeypatch.setattr(editor.subprocess, "run", tool)
    return tool


@pytest.fixture
def fake_converter(monkeypatch, fake_exiftool):
    """
    Stand in for Adobe DNG Converter, which writes its output to a directory
    rather than to stdout. Set ``produces`` to the bytes it should write, or
    None to simulate it silently producing nothing.
    """

    class FakeConverter:
        def __init__(self):
            self.calls: list[list[str]] = []
            self.produces: bytes | None = b"II*\x00fake-raw"
            self.returncode = 0
            self.stderr = ""

        def handles(self, cmd) -> bool:
            return "DNG Converter" in str(cmd[0])

        def run(self, cmd, **kwargs):
            self.calls.append(list(cmd))
            if self.returncode == 0 and self.produces is not None:
                out_dir = Path(cmd[cmd.index("-d") + 1])
                out_dir.mkdir(parents=True, exist_ok=True)
                # Name the output after whatever exiftool reported as embedded.
                name = (fake_exiftool.stdout_lines or ["IMG_1.CR2"])[0]
                (out_dir / name).write_bytes(self.produces)
            return subprocess.CompletedProcess(
                cmd, self.returncode, stdout="", stderr=self.stderr
            )

        def last_call(self) -> list[str]:
            assert self.calls, "the DNG Converter was never invoked"
            return self.calls[-1]

    converter = FakeConverter()
    exiftool_run = fake_exiftool

    def _dispatch(cmd, **kwargs):
        if converter.handles(cmd):
            return converter.run(cmd, **kwargs)
        return exiftool_run(cmd, **kwargs)

    import process

    monkeypatch.setattr(process.subprocess, "run", _dispatch)
    return converter


@pytest.fixture(autouse=True)
def isolate_style_store(tmp_path, monkeypatch):
    """
    Point the learned-style database at a throwaway file.

    Without this the suite wrote into the user's real ~/.photo-room store:
    every pytest run recorded training data from synthetic gradient images
    against temp-file paths, quietly corrupting the style the tool had
    actually learned. It also made the editor's model appear trained, so a
    test expecting an unstyled result saw "styled".
    """
    import editor
    import style

    monkeypatch.setattr(style, "DB_PATH", tmp_path / "style-test.duckdb")
    # The editor caches the fitted model globally; drop it so it refits
    # against the empty store rather than whatever was loaded before.
    monkeypatch.setattr(editor, "_style_model", None)
    yield
    monkeypatch.setattr(editor, "_style_model", None)


@pytest.fixture(autouse=True)
def clear_preview_cache():
    """
    Both render caches are module-global; keep them from leaking between
    tests. Clearing only the preview cache left decodes behind, which made
    a test asserting the two stay in step fail against perfectly good code.
    """
    import editor

    editor._base_cache.clear()
    editor._decode_cache.clear()
    yield
    editor._base_cache.clear()
    editor._decode_cache.clear()


@pytest.fixture(autouse=True)
def converter_present(request, monkeypatch):
    """
    The suite predates the portable path and assumes Adobe's converter is
    installed (it is faked). Tests of the portable path opt out with
    ``@pytest.mark.no_converter``.
    """
    import ingest
    import process

    present = "no_converter" not in request.keywords
    monkeypatch.setattr(process, "dng_converter_available", lambda: present)
    monkeypatch.setattr(ingest, "dng_converter_available", lambda: present)
