"""
Post-export action: open the written files in an app, or run a command on them.

The command is an argument list that is executed directly -- never through a
shell -- so file names with spaces, quotes or ``;`` are just data. The program
must be an existing executable (absolute path or found on PATH) and gets a
deadline; a command that outlives it is killed. An "open in app" action is a
GUI program that may keep running, so it is left running once it has started.

Action shapes (the dialog, presets and ``--post-command`` all produce these):

    {"type": "open", "app": "/usr/bin/gimp"}      app optional: xdg-open / open
    {"type": "command", "argv": ["prog", "--flag", "{files}"]}

In ``argv`` an argument that is exactly ``{files}`` expands to every written
file and ``{folder}`` inside an argument to their folder; with no ``{files}``
the files are appended.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

TIMEOUT = 30            # seconds a command may run
LAUNCH_WAIT = 3         # seconds an "open in app" is watched for an early failure
MAX_ARGS = 32
MAX_FILES = 200
TYPES = ("open", "command")


def normalize(action) -> dict | None:
    """Validated form of an action (None for no action). Raises ValueError."""
    if not action:
        return None
    if not isinstance(action, dict):
        raise ValueError("the post-export action must be an object")
    kind = str(action.get("type") or "")
    if kind not in TYPES:
        raise ValueError(f"post-export action must be one of {', '.join(TYPES)}")
    if kind == "open":
        app = str(action.get("app") or "").strip()
        if "\x00" in app:
            raise ValueError("the application path is not valid")
        return {"type": "open", "app": app}
    argv = action.get("argv")
    if isinstance(argv, str):
        try:
            argv = shlex.split(argv)       # split into words only; no shell ever runs
        except ValueError as exc:
            raise ValueError(f"cannot read the command: {exc}") from None
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError("the command needs a program name")
    if len(argv) > MAX_ARGS or any("\x00" in a for a in argv):
        raise ValueError("the command has too many or invalid arguments")
    return {"type": "command", "argv": argv}


def _resolve(program: str) -> str:
    found = program if os.path.isabs(program) and os.access(program, os.X_OK) else shutil.which(program)
    if not found or not Path(found).is_file():
        raise ValueError(f"{program!r} was not found or is not executable")
    return found


def build_argv(action: dict, files: list[str]) -> list[str]:
    """The exact argument list to execute for ``action`` and the written ``files``."""
    files = [str(f) for f in files][:MAX_FILES]
    if action["type"] == "open":
        app = action["app"]
        if app:
            return [_resolve(app), *files]
        return [_resolve("open" if sys.platform == "darwin" else "xdg-open"), *files[:1]]
    folder = str(Path(files[0]).parent) if files else ""
    out: list[str] = []
    expanded = False
    for arg in action["argv"][1:]:
        if arg == "{files}":
            out.extend(files)
            expanded = True
        else:
            out.append(arg.replace("{folder}", folder))
    if not expanded:
        out.extend(files)
    return [_resolve(action["argv"][0]), *out]


def run_action(action: dict | None, files: list[str], timeout: int = TIMEOUT) -> dict:
    """Run the action; always returns {"ok": bool, "message": str}, never raises."""
    if not action or not files:
        return {"ok": True, "message": ""}
    try:
        argv = build_argv(action, files)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    gui = action["type"] == "open"
    name = Path(argv[0]).name
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, shell=False, start_new_session=True)
    except OSError as exc:
        return {"ok": False, "message": f"could not start {name}: {exc.strerror or exc}"}
    try:
        code = proc.wait(timeout=LAUNCH_WAIT if gui else timeout)
    except subprocess.TimeoutExpired:
        if gui:
            return {"ok": True, "message": f"opened in {name}"}
        proc.kill()
        proc.wait()
        return {"ok": False, "message": f"{name} did not finish within {timeout}s and was stopped"}
    if code != 0:
        return {"ok": False, "message": f"{name} exited with status {code}"}
    return {"ok": True, "message": f"ran {name}"}
