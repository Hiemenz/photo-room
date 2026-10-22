"""
Shared import logic: destination layout, rename templates, sidecars, metadata
on import and the optional second (backup) copy.

One module so that the Import dialog (``import_api``), ``photo-room watch-cards``
(``cardwatch``) and ``ingest`` all name, place and carry files the same way.

Layout templates build the folder under the destination:
    {yyyy} {mm} {dd} {date} {camera}        e.g. "{yyyy}/{date}"
named layouts: date = {yyyy}/{mm}/{dd}, month = {yyyy}/{mm}, flat = "".

Rename templates build the file name (the original extension is kept):
    {date} {yyyy} {mm} {dd} {time} {seq} {seq:N} {camera} {original}
    e.g. "{date}_{camera}_{seq:4}" -> "2026-09-14_EOS-R5_0001.CR2"
An empty template keeps the camera's name.

Nothing here overwrites a file: every copy goes through ``ingest``'s
temp-name, fsync, SHA-256, no-overwrite claim.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import ingest

LAYOUTS = {"date": "{yyyy}/{mm}/{dd}", "month": "{yyyy}/{mm}", "flat": ""}
LAYOUT_TOKENS = frozenset({"yyyy", "mm", "dd", "date", "camera"})
NAME_TOKENS = frozenset({"yyyy", "mm", "dd", "date", "time", "seq", "camera", "original"})
MAX_NAME = 120

_TOKEN = re.compile(r"\{([^{}]*)\}")
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class TemplateError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def _check_tokens(tpl: str, allowed: frozenset[str], what: str) -> None:
    for raw in _TOKEN.findall(tpl):
        name, _, width = raw.partition(":")
        if name not in allowed or (width and (name != "seq" or not width.isdigit() or not 1 <= int(width) <= 8)):
            raise TemplateError(f"unknown {what} field {{{raw}}}; use "
                                + ", ".join("{" + t + "}" for t in sorted(allowed)))
    if "{" in _TOKEN.sub("", tpl) or "}" in _TOKEN.sub("", tpl):
        raise TemplateError(f"{what} {tpl!r} has an unbalanced brace")


def resolve_layout(layout: str) -> str:
    """A named layout or a template; raises TemplateError if it could leave the destination."""
    tpl = LAYOUTS.get(layout, layout) or ""
    _check_tokens(tpl, LAYOUT_TOKENS, "layout")
    rest = _TOKEN.sub("x", tpl)
    if rest.startswith("/") or ".." in rest.split("/") or "\\" in rest:
        raise TemplateError(f"layout {layout!r} must be a relative folder path")
    return tpl


def resolve_rename(template: str) -> str:
    tpl = (template or "").strip()
    _check_tokens(tpl, NAME_TOKENS, "rename")
    if "/" in tpl or "\\" in tpl:
        raise TemplateError("a rename template is a file name; it cannot contain / or \\")
    return tpl


def uses(tpl: str, fields: set[str]) -> bool:
    return any(raw.partition(":")[0] in fields for raw in _TOKEN.findall(tpl or ""))


def _clean(text: str) -> str:
    text = _BAD_CHARS.sub("_", str(text)).strip().strip(".")
    return re.sub(r"\s+", " ", text)


def frame_info(path: Path, want_camera: bool = False, want_time: bool = False,
               date_fn: Callable[[Path], tuple[str, str, str]] | None = None) -> dict:
    """{yyyy, mm, dd, time, camera} for one file; mtime and 'camera' when EXIF is unreadable."""
    info = {"time": None, "camera": None}
    exif: dict = {}
    if want_camera or want_time or date_fn is None:
        try:
            import process
            exif = process._exiftool_get(path, "DateTimeOriginal", "Model")
        except Exception:
            exif = {}
    dt = None
    raw = (exif.get("DateTimeOriginal") or "").strip()
    if raw and raw != "-":
        try:
            dt = datetime.strptime(raw[:19], "%Y:%m:%d %H:%M:%S")
        except ValueError:
            dt = None
    if date_fn is not None:
        y, m, d = date_fn(path)
    elif dt is not None:
        y, m, d = f"{dt.year:04d}", f"{dt.month:02d}", f"{dt.day:02d}"
    else:
        t = datetime.fromtimestamp(path.stat().st_mtime)
        y, m, d = f"{t.year:04d}", f"{t.month:02d}", f"{t.day:02d}"
    if dt is None:
        try:
            dt = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            dt = None
    cam = (exif.get("Model") or "").strip()
    info.update(yyyy=y, mm=m, dd=d,
                time=dt.strftime("%H%M%S") if dt else "000000",
                camera=_clean(cam.replace(" ", "-")) if cam and cam != "-" else "camera")
    return info


def _fill(tpl: str, values: dict, seq: int | None) -> str:
    def sub(m):
        name, _, width = m.group(1).partition(":")
        if name == "seq":
            return str(seq if seq is not None else 1).zfill(int(width) if width else 4)
        if name == "date":
            return f"{values['yyyy']}-{values['mm']}-{values['dd']}"
        return _clean(values.get(name, "")) if name != "original" else values["original"]
    return _TOKEN.sub(sub, tpl)


def render_layout(tpl: str, info: dict) -> str:
    """Relative folder path ('' for flat)."""
    parts = [_clean(p) for p in _fill(tpl, dict(info, original=""), None).split("/")]
    return "/".join(p for p in parts if p and p != "..")


def render_name(tpl: str, info: dict, seq: int, original: str) -> str:
    """Final file name with the original extension; the original name if the template yields nothing usable."""
    p = Path(original)
    if not tpl:
        return original
    stem = _clean(_fill(tpl, dict(info, original=p.stem), seq))[:MAX_NAME]
    return f"{stem}{p.suffix}" if stem else original


# ---------------------------------------------------------------------------
# What to do with each file
# ---------------------------------------------------------------------------

@dataclass
class ImportSpec:
    layout: str = "date"
    rename: str = ""
    seq_start: int = 1
    sidecars: bool = True
    backup: Path | None = None
    rating: int | None = None
    keywords: list[str] = field(default_factory=list)
    copyright: str = ""
    post_copy: Callable[[Path], None] | None = None     # e.g. apply a preset (editor only)
    date_fn: Callable[[Path], tuple[str, str, str]] | None = None

    def validate(self) -> "ImportSpec":
        self.layout = resolve_layout(self.layout)
        self.rename = resolve_rename(self.rename)
        if self.rating is not None and not -1 <= int(self.rating) <= 5:
            raise TemplateError("rating must be 0 to 5 stars (or -1 for rejected)")
        self.keywords = _keywords(self.keywords)
        self.copyright = " ".join(str(self.copyright or "").split())
        return self

    @property
    def writes_metadata(self) -> bool:
        return self.rating is not None or bool(self.keywords) or bool(self.copyright)


def _keywords(kws) -> list[str]:
    import library
    return library.normalize_keywords(kws)


@dataclass
class OneResult:
    dest: Path | None = None
    status: str = ""                 # copied | renamed | identical
    sidecar: str = "none"            # none | copied | exists | identical
    backup: Path | None = None
    backup_status: str = ""
    warnings: list[str] = field(default_factory=list)


def write_metadata(dest: Path, spec: ImportSpec, run=None) -> None:
    """Rating, keywords (added to any already in a carried sidecar) and copyright into dest's .xmp."""
    import fsutil
    import library

    args: list[str] = []
    if spec.rating is not None:
        args.append(f"-XMP-xmp:Rating={int(spec.rating)}")
    args += [f"-XMP-dc:Subject+={kw}" for kw in spec.keywords]
    if spec.copyright:
        args += [f"-XMP-dc:Rights={spec.copyright}", "-XMP-xmpRights:Marked=True"]
    if not args:
        return
    target = dest.with_suffix(".xmp")
    run = run or library._run_exiftool
    with fsutil.lock_for(target):
        if target.exists():
            result = run(args + ["-overwrite_original", str(target)])
            ok = result.returncode == 0
        else:
            result = run(args + ["-o", str(target), os.devnull])
            ok = target.exists()           # seeding from /dev/null warns yet succeeds
    if not ok:
        raise RuntimeError(f"exiftool failed: {(result.stderr or '').strip() or 'no output'}")


def dest_for(src: Path, library_root: Path, spec: ImportSpec, seq: int) -> tuple[Path, str]:
    """(folder, file name) a source file will be written to under ``library_root``."""
    want_cam = uses(spec.layout, {"camera"}) or uses(spec.rename, {"camera"})
    want_time = uses(spec.rename, {"time"})
    info = frame_info(src, want_cam, want_time, spec.date_fn)
    rel = render_layout(spec.layout, info)
    return (library_root / rel if rel else library_root), render_name(spec.rename, info, seq, src.name)


def import_one(src: Path, library_root: Path, spec: ImportSpec, seq: int = 1,
               dup: Path | None = None) -> OneResult:
    """
    Copy one file (verified, never overwriting), carry its sidecar, apply the
    on-import metadata, then make the optional backup copy.

    ``dup`` is the library file that already has these bytes: nothing is
    written to the library, but the backup is still made if one is wanted, so a
    backup that failed last time is completed by the next run. Raises on a
    failed primary copy; trouble after the copy is reported in ``warnings`` or,
    for the backup, raised as ``BackupError`` carrying the result.
    """
    out = OneResult()
    if dup is not None:
        out.dest, out.status = dup, "identical"
    else:
        folder, name = dest_for(src, library_root, spec, seq)
        out.dest, out.status = ingest.copy_from_card_ex(
            src, library_root, date_folders=False, verify=True, dest_name=name, dest_dir=folder)
        if out.status != "identical":
            if spec.sidecars:
                try:
                    _, out.sidecar = ingest.copy_sidecar(src, out.dest)
                except (OSError, RuntimeError) as exc:
                    out.warnings.append(f"sidecar for {src.name} not carried over: {exc}")
            for step, label in ((lambda: write_metadata(out.dest, spec) if spec.writes_metadata else None,
                                 "rating/keywords/copyright"),
                                (lambda: spec.post_copy(out.dest) if spec.post_copy else None, "preset")):
                try:
                    step()
                except Exception as exc:
                    out.warnings.append(f"{label} not applied to {out.dest.name}: {exc}")
    if spec.backup is not None:
        rel = out.dest.parent.relative_to(library_root) if library_root in out.dest.parents else Path()
        bdir = Path(spec.backup) / rel
        out.backup, out.backup_status = ingest.copy_from_card_ex(
            src, library_root, date_folders=False, verify=True, dest_name=out.dest.name, dest_dir=bdir)
        side = out.dest.with_suffix(".xmp")
        if spec.sidecars and side.is_file():
            try:
                ingest.copy_companion(side, out.backup.with_suffix(".xmp"))
            except (OSError, RuntimeError) as exc:
                out.warnings.append(f"sidecar of {out.dest.name} not copied to the backup: {exc}")
    return out


def check_backup_dir(backup: Path, library_root: Path, source: Path | None = None) -> Path:
    """The backup folder, resolved; refuses one inside the library, the source, or that holds them."""
    backup = Path(backup).expanduser()
    if not backup.is_absolute():
        raise TemplateError("the backup folder must be a full path")
    b = backup.resolve()
    lib = Path(library_root).resolve()
    for other, what in ((lib, "library"), (Path(source).resolve() if source else None, "source")):
        if other is None:
            continue
        if b == other or other in b.parents or b in other.parents:
            raise TemplateError(f"the backup folder cannot be the {what} folder or inside/around it; "
                                "a backup on the same disk path is not a second copy")
    return b
