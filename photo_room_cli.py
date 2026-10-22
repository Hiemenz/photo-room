"""`photo-room` command line: launch the editor, run the doctor, ingest.

    photo-room [folder] [-p PORT]     open the editor on a folder
    photo-room doctor [folder]        check the install and say how to fix it
    photo-room ingest ...             same as ingest.py (cards / run / watch)
    photo-room import-lightroom CATALOG --library DIR   import a Lightroom .lrcat (see lrimport.py)
    photo-room backup <library> -o <dest> | restore <backup> <library> [--force]   snapshot / restore library state
    photo-room watch-cards [library]  import + verify + safely eject cards as they are inserted

The doctor is plain standard library on purpose: it has to keep working when
the thing it is diagnosing (numpy, rawpy, ...) is the part that is broken.
"""

from __future__ import annotations

import importlib
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
MIN_PYTHON = (3, 12)
MIN_EXIFTOOL = 12.0
GOOD_EXIFTOOL = 13.0
DEFAULT_PORT = 5000


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""


def _platform_key() -> str:
    return "mac" if sys.platform == "darwin" else "linux"


def _exiftool_fix() -> str:
    return ("brew install exiftool" if _platform_key() == "mac"
            else "sudo apt install libimage-exiftool-perl")


def _py_fix(name: str) -> str:
    return f"uv sync   (then run via `uv run photo-room`; {name} is a required dependency)"


# --- individual checks (each takes its environment as arguments so tests can fake it)

def check_python(version=None) -> Check:
    v = tuple(version or sys.version_info[:3])
    text = ".".join(str(p) for p in v[:3])
    if v[:2] >= MIN_PYTHON:
        return Check("Python", PASS, text)
    return Check("Python", FAIL, f"{text}; Photo Room needs 3.12 or newer",
                 "uv python install 3.12 && uv sync --python 3.12")


def check_import(label: str, module: str, importer=importlib.import_module,
                 required: bool = True, version_attr: str = "__version__") -> Check:
    try:
        mod = importer(module)
    except Exception as exc:  # ImportError, or a broken native library
        status = FAIL if required else WARN
        return Check(label, status, f"cannot import {module}: {exc}", _py_fix(label))
    ver = getattr(mod, version_attr, "")
    return Check(label, PASS, str(ver) if ver else "ok")


def parse_exiftool_version(text: str):
    m = re.search(r"(\d+)\.(\d+)", text or "")
    return float(f"{m.group(1)}.{m.group(2)}") if m else None


def check_exiftool(which=shutil.which, run=subprocess.run) -> Check:
    path = which("exiftool")
    if not path:
        return Check("exiftool", FAIL, "not found on PATH", _exiftool_fix())
    try:
        out = run([path, "-ver"], capture_output=True, text=True, timeout=15).stdout
    except Exception as exc:
        return Check("exiftool", FAIL, f"{path} did not run: {exc}", _exiftool_fix())
    ver = parse_exiftool_version(out)
    if ver is None:
        return Check("exiftool", FAIL, f"{path}: unreadable version {out.strip()!r}", _exiftool_fix())
    if ver < MIN_EXIFTOOL:
        return Check("exiftool", FAIL, f"{ver} is too old (need >= 12)", _exiftool_fix())
    if ver < GOOD_EXIFTOOL:
        return Check("exiftool", WARN, f"{ver}; 13 or newer is recommended", _exiftool_fix())
    return Check("exiftool", PASS, f"{ver}")


def _ffmpeg_fix() -> str:
    return ("brew install ffmpeg" if _platform_key() == "mac"
            else "sudo apt install ffmpeg")


def check_ffmpeg(which=shutil.which) -> Check:
    if which("ffmpeg"):
        return Check("ffmpeg", PASS, "found")
    return Check("ffmpeg", WARN, "not found (optional: video files)", _ffmpeg_fix())


MIN_FREE_GB = 1.0
WARN_FREE_GB = 10.0


def check_disk(directory: Path, usage=shutil.disk_usage) -> Check:
    directory = Path(directory).expanduser()
    probe = directory
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = usage(probe).free / 1e9
    except Exception as exc:
        return Check("free disk", WARN, f"cannot read free space for {directory}: {exc}")
    fix = f"free up space on the drive holding {directory} (a card of raws is often 10+ GB)"
    if free < MIN_FREE_GB:
        return Check("free disk", FAIL, f"{free:.1f} GB free", fix)
    if free < WARN_FREE_GB:
        return Check("free disk", WARN, f"{free:.1f} GB free; a full card needs more", fix)
    return Check("free disk", PASS, f"{free:.0f} GB free")


def check_dng_converter(exists=os.path.exists) -> Check:
    candidates = ["/Applications/Adobe DNG Converter.app"]
    for c in candidates:
        if exists(c):
            return Check("DNG Converter", PASS, "found (optional)")
    return Check("DNG Converter", PASS, "not installed (optional; raws are decoded with rawpy)")


def check_lensfun(importer=importlib.import_module) -> Check:
    try:
        importer("lensfunpy")
    except Exception:
        return Check("lensfun", WARN, "lensfunpy not installed (optional: lens profiles)",
                     "uv sync --extra lens")
    return Check("lensfun", PASS, "ok")


def check_writable(directory: Path) -> Check:
    try:
        directory = Path(directory)
        probe_dir = directory if directory.is_dir() else directory.parent
        with tempfile.TemporaryFile(dir=probe_dir):
            pass
    except Exception as exc:
        return Check("cache dir", FAIL, f"{directory} is not writable: {exc}",
                     f"chmod u+w {directory}   (thumbnails are cached in <folder>/.photo-room-thumbs)")
    return Check("cache dir", PASS, f"{directory} is writable")


def check_port(port: int, host: str = "127.0.0.1", sock_factory=socket.socket) -> Check:
    s = sock_factory(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
    except OSError as exc:
        return Check("port", WARN, f"{port} is in use ({exc.strerror or exc})",
                     f"photo-room -p {port + 1}   (or stop the other program using {port})")
    finally:
        s.close()
    return Check("port", PASS, f"{port} is free")


def run_checks(folder: Path | None = None, port: int = DEFAULT_PORT) -> list[Check]:
    cache = Path(folder).expanduser() if folder else Path.cwd()
    return [
        check_python(),
        check_import("rawpy / LibRaw", "rawpy"),
        check_import("Pillow", "PIL"),
        check_import("OpenCV", "cv2"),
        check_import("numpy", "numpy"),
        check_exiftool(),
        check_ffmpeg(),
        check_dng_converter(),
        check_lensfun(),
        check_writable(cache),
        check_disk(cache),
        check_port(port),
    ]


def format_table(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines = [f"Photo Room doctor ({platform.system()}, Python {platform.python_version()})", ""]
    for c in checks:
        lines.append(f"  {c.status:<4}  {c.name:<{width}}  {c.detail}")
        if c.fix and c.status != PASS:
            lines.append(f"        {'':<{width}}  fix: {c.fix}")
    fails = sum(c.status == FAIL for c in checks)
    warns = sum(c.status == WARN for c in checks)
    lines += ["", f"{fails} failed, {warns} warnings."
              + ("" if fails else " Ready to go: photo-room ~/Pictures/shoot")]
    return "\n".join(lines)


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.status == FAIL for c in checks) else 0


def doctor_main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="photo-room doctor", description="Check the installation.")
    p.add_argument("folder", nargs="?", default=None, help="Photo folder (checked for write access)")
    p.add_argument("-p", "--port", type=int, default=DEFAULT_PORT)
    args = p.parse_args(argv)
    checks = run_checks(args.folder, args.port)
    print(format_table(checks))
    code = exit_code(checks)
    if argv is None and sys.argv[0].endswith("photo-room-doctor"):
        sys.exit(code)
    return code


# --- launching the editor

HARD_MODULES = [("rawpy", "rawpy / LibRaw"), ("PIL", "Pillow"), ("numpy", "numpy"),
                ("cv2", "OpenCV"), ("flask", "Flask")]

BANNER = """
<div id="pr-exiftool-banner" style="position:fixed;top:0;left:0;right:0;z-index:99999;
background:#7a4b00;color:#fff;padding:8px 14px;font:14px system-ui,sans-serif;display:none">
exiftool was not found, so saving edits and exporting with metadata will fail.
Fix: <code>%s</code>, then restart Photo Room.
<button onclick="document.getElementById('pr-exiftool-banner').style.display='none';
try{sessionStorage.setItem('pr-exiftool-dismissed','1')}catch(e){}"
style="float:right;margin-left:12px">Dismiss</button></div>
<script>try{if(!sessionStorage.getItem('pr-exiftool-dismissed'))
document.getElementById('pr-exiftool-banner').style.display='block'}catch(e){
document.getElementById('pr-exiftool-banner').style.display='block'}</script>
"""


def install_exiftool_banner(app, fix: str) -> None:
    """Inject a dismissible banner into the editor page (kept out of editor.html)."""
    snippet = (BANNER % fix).encode()

    @app.after_request
    def _banner(resp):
        if resp.mimetype == "text/html" and not resp.direct_passthrough:
            body = resp.get_data()
            if b"</body>" in body and b"pr-exiftool-banner" not in body:
                resp.set_data(body.replace(b"</body>", snippet + b"</body>", 1))
        return resp


def preflight(importer=importlib.import_module) -> list[Check]:
    """Hard-dependency failures that must stop the editor from starting."""
    bad = []
    for module, label in HARD_MODULES:
        c = check_import(label, module, importer)
        if c.status == FAIL:
            bad.append(c)
    if sys.version_info[:2] < MIN_PYTHON:
        bad.insert(0, check_python())
    return bad


def launch_editor(argv: list[str]) -> int:
    bad = preflight()
    if bad:
        print("Photo Room cannot start because a required dependency is missing:\n", file=sys.stderr)
        for c in bad:
            print(f"  - {c.name}: {c.detail}\n    fix: {c.fix}", file=sys.stderr)
        print("\nRun `photo-room doctor` for the full report.", file=sys.stderr)
        return 1
    try:
        import editor
    except Exception as exc:
        print(f"Photo Room could not load the editor: {exc}\nRun `photo-room doctor`.", file=sys.stderr)
        return 1
    ex = check_exiftool()
    if ex.status == FAIL:
        print(f"WARNING: exiftool problem: {ex.detail}\n  fix: {ex.fix}", file=sys.stderr)
        install_exiftool_banner(editor.app, ex.fix)
    sys.argv = ["photo-room"] + argv
    try:
        editor.main()
    except OSError as exc:
        if "in use" in str(exc) or getattr(exc, "errno", None) in (48, 98):
            print(f"Port is already in use. Try: photo-room -p 5001 ...  ({exc})", file=sys.stderr)
            return 1
        raise
    return 0


def main(argv=None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "doctor":
        sys.exit(doctor_main(argv[1:]))
    if argv and argv[0] in ("backup", "restore"):
        import backup
        sys.exit((backup.main if argv[0] == "backup" else backup.restore_main)(argv[1:]))
    if argv and argv[0] == "ingest":
        if preflight():
            sys.exit(launch_editor([]))  # prints the friendly message and exits 1
        import ingest
        sys.argv = ["photo-room ingest"] + argv[1:]
        ingest.main()
        return
    if argv and argv[0] == "import-lightroom":
        import lrimport
        sys.exit(lrimport.main(argv[1:]))
    if argv and argv[0] == "watch-cards":
        if preflight():
            sys.exit(launch_editor([]))
        import cardwatch
        sys.exit(cardwatch.main(argv[1:]))
    sys.exit(launch_editor(argv))


if __name__ == "__main__":
    main()
