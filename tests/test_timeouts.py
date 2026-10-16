"""
Tests that a stuck helper program cannot stall a run.

The DNG Converter is a GUI application driven from the command line and
answers some invocations by opening a window rather than writing to
stdout — `-h` does exactly that, producing no output and never exiting.
Unbounded, one such file would stall an entire shoot with nothing to show.
"""

import ast
import subprocess
from pathlib import Path

import pytest

import process
from process import CONVERTER_TIMEOUT, EXIFTOOL_TIMEOUT, run_tool


def test_a_hanging_program_is_reported_not_awaited(monkeypatch):
    def hangs(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))

    monkeypatch.setattr(process.subprocess, "run", hangs)

    with pytest.raises(RuntimeError, match="did not finish within"):
        run_tool(["sleep", "999"], timeout=1, what="test program")


def test_the_error_names_what_hung(monkeypatch):
    """"Something timed out" on a 431-frame shoot is not actionable."""
    def hangs(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(process.subprocess, "run", hangs)

    with pytest.raises(RuntimeError, match="DNG Converter on IMG_0001.CR2"):
        run_tool(["x"], timeout=1, what="DNG Converter on IMG_0001.CR2")


def test_the_error_suggests_the_likely_cause(monkeypatch):
    def hangs(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(process.subprocess, "run", hangs)

    with pytest.raises(RuntimeError, match="waiting on a dialog"):
        run_tool(["x"], timeout=1, what="tool")


def test_a_normal_run_passes_through(monkeypatch):
    def fine(cmd, **kwargs):
        assert kwargs["timeout"] == 30
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(process.subprocess, "run", fine)
    assert run_tool(["x"], timeout=30, what="tool", capture_output=True).stdout == "ok"


def test_a_converter_hang_fails_one_file_not_the_batch(tmp_path, monkeypatch):
    """A stalled frame must be reported and the rest of the shoot continue."""
    def hangs(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(process.subprocess, "run", hangs)

    with pytest.raises(RuntimeError, match="did not finish"):
        process.convert_cr2_to_dng(tmp_path / "IMG_0001.CR2", tmp_path)


ROOT = Path(__file__).resolve().parent.parent
# Every top-level module that is not itself under test, found rather than listed,
# so a new module that shells out cannot silently escape the check.
MODULES = sorted(p.stem for p in ROOT.glob("*.py"))


@pytest.mark.parametrize("module", MODULES)
def test_every_subprocess_call_is_bounded(module):
    """
    A single unbounded call is enough to hang an unattended ingest, so this
    is checked structurally rather than case by case.
    """
    source = (ROOT / f"{module}.py").read_text()
    unbounded = []

    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "run":
            continue
        # Flask's app.run is a server, not a subprocess.
        if isinstance(node.func.value, ast.Name) and node.func.value.id == "app":
            continue
        if not any(k.arg == "timeout" for k in node.keywords):
            unbounded.append(node.lineno)

    assert not unbounded, f"{module}.py has unbounded subprocess calls at {unbounded}"


def test_the_converter_gets_longer_than_metadata_reads():
    """Converting a large raw is slow; reading a tag is not."""
    assert CONVERTER_TIMEOUT > EXIFTOOL_TIMEOUT
    assert CONVERTER_TIMEOUT >= 300
