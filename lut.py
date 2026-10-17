"""
Film styles: .cube LUT import, interpolation, built-in looks and the LUT library.

Where a LUT sits in the pipeline
--------------------------------
A creative LUT is a *display-referred* look: it maps display-encoded RGB to
display-encoded RGB (that is what nearly every ``.cube`` film emulation on the
market is). So it runs on the encoded image, after the tone, colour mixer and
grading stages and before clarity/texture/sharpening (``editor.apply_adjustments``),
and the intensity slider blends the result with its input. For a photo whose
output space is not sRGB the pixels are converted to sRGB primaries and the sRGB
curve before the LUT and back afterwards, so a LUT looks the same in every
output space. A LUT that declares itself *linear* (set at import, or by the
``# photo-room: space=linear`` comment) is sampled on linear-light sRGB instead,
which is what 1D linearisation/shaper curves want.

Interpolation is tetrahedral by default (what DaVinci Resolve and most hardware
use: exact along the grey axis, no hue skew) with trilinear available. 1D LUTs
are interpolated linearly per channel.

Nothing here imports Flask or the editor, so the maths is testable on its own.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

import colour

F32 = np.float32

MAX_BYTES = 64 * 1024 * 1024         # a 129^3 LUT is ~40 MB of text
MAX_3D = 129
MAX_1D = 65536
MIN_SIZE = 2
LUT_SPACES = ("srgb", "linear")      # what the LUT's input/output are encoded as
METHODS = ("tetrahedral", "trilinear")
CHUNK = 1 << 18                      # pixels per interpolation chunk (bounds temporary memory)


class LutError(ValueError):
    """The file is not a usable .cube LUT; the message says why."""


class LutNotFound(KeyError):
    """A photo refers to a LUT that is not in the library on this machine."""

    def __str__(self) -> str:         # KeyError would repr() the message
        return str(self.args[0]) if self.args else ""


@dataclass
class Lut:
    kind: str                         # "1d" or "3d"
    size: int
    table: np.ndarray                 # 1d: (N, 3); 3d: (N, N, N, 3) indexed [blue][green][red]
    domain_min: np.ndarray = field(default_factory=lambda: np.zeros(3, np.float32))
    domain_max: np.ndarray = field(default_factory=lambda: np.ones(3, np.float32))
    title: str = ""
    space: str = "srgb"

    def sample(self, rgb: np.ndarray, method: str = "tetrahedral") -> np.ndarray:
        """LUT output for ``rgb`` (any shape ending in 3; values in the LUT's input encoding)."""
        shape = rgb.shape
        flat = np.ascontiguousarray(rgb, dtype=np.float32).reshape(-1, 3)
        span = np.maximum(self.domain_max - self.domain_min, F32(1e-12))
        out = np.empty_like(flat)
        for a in range(0, len(flat), CHUNK):
            x = np.clip((flat[a:a + CHUNK] - self.domain_min) / span, 0.0, 1.0)
            if self.kind == "1d":
                out[a:a + CHUNK] = interp_1d(self.table, x)
            elif method == "trilinear":
                out[a:a + CHUNK] = interp_trilinear(self.table, x)
            else:
                out[a:a + CHUNK] = interp_tetrahedral(self.table, x)
        return out.reshape(shape)

    def is_identity(self, tol: float = 1e-4) -> bool:
        grid = np.linspace(0.0, 1.0, self.size, dtype=np.float32)
        span = self.domain_max - self.domain_min
        if self.kind == "1d":
            want = np.stack([grid * span[c] + self.domain_min[c] for c in range(3)], axis=1)
        else:
            b, g, r = np.meshgrid(grid, grid, grid, indexing="ij")
            want = np.stack([r, g, b], axis=-1) * span + self.domain_min
        return bool(np.max(np.abs(self.table - want)) <= tol)


# ---------------------------------------------------------------------------
# Parsing and writing .cube
# ---------------------------------------------------------------------------

_KEYWORDS = ("TITLE", "DOMAIN_MIN", "DOMAIN_MAX", "LUT_1D_SIZE", "LUT_3D_SIZE",
             "LUT_1D_INPUT_RANGE", "LUT_3D_INPUT_RANGE")
_SPACE_HINT = re.compile(r"photo-room:\s*space\s*=\s*(srgb|linear)", re.I)


def _floats(parts: list[str], n: int, what: str, line_no: int) -> list[float]:
    if len(parts) != n:
        raise LutError(f"line {line_no}: {what} needs {n} number(s), found {len(parts)}")
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        raise LutError(f"line {line_no}: {what} has a value that is not a number") from None
    if not all(math.isfinite(v) for v in vals):
        raise LutError(f"line {line_no}: {what} has a value that is not finite")
    return vals


def parse_cube(text: str | bytes, space: str | None = None) -> Lut:
    """
    Parse and validate Adobe/Resolve ``.cube`` text.

    Raises :class:`LutError` for anything malformed: a missing or doubled size,
    the wrong number of rows, non-numeric or non-finite values, an empty domain,
    or a 3D+1D combination (not supported). ``space`` overrides the encoding the
    LUT declares for itself ("srgb" if it declares nothing).
    """
    if isinstance(text, (bytes, bytearray)):
        if len(text) > MAX_BYTES:
            raise LutError("LUT file is larger than 64 MB")
        try:
            text = bytes(text).decode("utf-8-sig")
        except UnicodeDecodeError:
            raise LutError("LUT file is not text (expected a UTF-8 .cube file)") from None
    if len(text) > MAX_BYTES:
        raise LutError("LUT file is larger than 64 MB")

    title, size1, size3 = "", None, None
    dmin, dmax = [0.0] * 3, [1.0] * 3
    declared_space = None
    data: list[str] = []
    for line_no, raw in enumerate(text.splitlines(), 1):
        hint = _SPACE_HINT.search(raw)
        if hint:
            declared_space = hint.group(1).lower()
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        head = line.split(None, 1)[0]
        if head in _KEYWORDS:
            rest = line[len(head):].strip()
            if head == "TITLE":
                title = rest.strip().strip('"')[:120]
            elif head == "DOMAIN_MIN":
                dmin = _floats(rest.split(), 3, "DOMAIN_MIN", line_no)
            elif head == "DOMAIN_MAX":
                dmax = _floats(rest.split(), 3, "DOMAIN_MAX", line_no)
            elif head in ("LUT_1D_INPUT_RANGE", "LUT_3D_INPUT_RANGE"):
                lo, hi = _floats(rest.split(), 2, head, line_no)
                dmin, dmax = [lo] * 3, [hi] * 3
            else:
                (n,) = _floats(rest.split(), 1, head, line_no)
                if n != int(n):
                    raise LutError(f"line {line_no}: {head} must be a whole number")
                if head == "LUT_1D_SIZE":
                    if size1 is not None:
                        raise LutError(f"line {line_no}: LUT_1D_SIZE appears twice")
                    size1 = int(n)
                else:
                    if size3 is not None:
                        raise LutError(f"line {line_no}: LUT_3D_SIZE appears twice")
                    size3 = int(n)
            continue
        if head[0].isalpha():
            raise LutError(f"line {line_no}: unknown keyword {head!r}")
        data.append(line)

    if size1 is None and size3 is None:
        raise LutError("no LUT_1D_SIZE or LUT_3D_SIZE line")
    if size1 is not None and size3 is not None:
        raise LutError("a file with both LUT_1D_SIZE and LUT_3D_SIZE is not supported")
    kind, n = ("1d", size1) if size1 is not None else ("3d", size3)
    limit = MAX_1D if kind == "1d" else MAX_3D
    if not MIN_SIZE <= n <= limit:
        raise LutError(f"LUT_{kind.upper()}_SIZE {n} is outside {MIN_SIZE}..{limit}")
    rows = n if kind == "1d" else n ** 3
    if len(data) != rows:
        raise LutError(f"expected {rows} data rows for a {kind.upper()} LUT of size {n}, found {len(data)}")
    if any(hi <= lo for lo, hi in zip(dmin, dmax)):
        raise LutError("DOMAIN_MAX must be greater than DOMAIN_MIN on every channel")

    try:
        arr = np.array(" ".join(data).split(), dtype=np.float64)
    except ValueError:
        raise LutError("a data row contains something that is not a number") from None
    if arr.size != rows * 3:
        raise LutError("every data row needs exactly three numbers")
    if not np.all(np.isfinite(arr)):
        raise LutError("the table contains NaN or infinite values")
    arr = arr.astype(np.float32).reshape(rows, 3)
    table = arr if kind == "1d" else arr.reshape(n, n, n, 3)    # red varies fastest -> [b][g][r]

    chosen = space or declared_space or "srgb"
    if chosen not in LUT_SPACES:
        raise LutError(f"space must be one of {', '.join(LUT_SPACES)}")
    return Lut(kind=kind, size=n, table=np.ascontiguousarray(table),
               domain_min=np.array(dmin, np.float32), domain_max=np.array(dmax, np.float32),
               title=title, space=chosen)


def to_cube(lut: Lut, title: str | None = None) -> str:
    """Serialise (what the library stores); ``parse_cube`` reads it back."""
    lines = [f'TITLE "{(title if title is not None else lut.title) or "LUT"}"']
    if lut.space != "srgb":
        lines.append(f"# photo-room: space={lut.space}")
    lines.append(f"LUT_{lut.kind.upper()}_SIZE {lut.size}")
    if not (np.allclose(lut.domain_min, 0) and np.allclose(lut.domain_max, 1)):
        lines.append("DOMAIN_MIN " + " ".join(f"{v:.6f}" for v in lut.domain_min))
        lines.append("DOMAIN_MAX " + " ".join(f"{v:.6f}" for v in lut.domain_max))
    rows = lut.table.reshape(-1, 3)
    lines.extend(f"{r:.6f} {g:.6f} {b:.6f}" for r, g, b in rows)
    return "\n".join(lines) + "\n"


def identity(kind: str = "3d", size: int = 17) -> Lut:
    grid = np.linspace(0.0, 1.0, size, dtype=np.float32)
    if kind == "1d":
        return Lut("1d", size, np.stack([grid] * 3, axis=1))
    b, g, r = np.meshgrid(grid, grid, grid, indexing="ij")
    return Lut("3d", size, np.stack([r, g, b], axis=-1).astype(np.float32))


# ---------------------------------------------------------------------------
# Interpolation (x is (n, 3) in 0..1)
# ---------------------------------------------------------------------------

def interp_1d(table: np.ndarray, x: np.ndarray) -> np.ndarray:
    grid = np.linspace(0.0, 1.0, table.shape[0])
    out = np.empty(x.shape, np.float32)
    for c in range(3):
        out[:, c] = np.interp(x[:, c], grid, table[:, c])
    return out


def _corner_setup(table: np.ndarray, x: np.ndarray):
    n = table.shape[0]
    s = x * F32(n - 1)
    i0 = np.minimum(s.astype(np.int64), n - 2)
    f = s - i0
    base = i0[:, 0] + n * i0[:, 1] + n * n * i0[:, 2]
    return n, f, base, table.reshape(-1, 3)


def interp_trilinear(table: np.ndarray, x: np.ndarray) -> np.ndarray:
    n, f, base, flat = _corner_setup(table, x)
    dr, dg, db = 1, n, n * n
    fr, fg, fb = f[:, 0:1], f[:, 1:2], f[:, 2:3]
    c00 = flat[base] * (1 - fr) + flat[base + dr] * fr
    c10 = flat[base + dg] * (1 - fr) + flat[base + dg + dr] * fr
    c01 = flat[base + db] * (1 - fr) + flat[base + db + dr] * fr
    c11 = flat[base + db + dg] * (1 - fr) + flat[base + db + dg + dr] * fr
    c0 = c00 * (1 - fg) + c10 * fg
    c1 = c01 * (1 - fg) + c11 * fg
    return (c0 * (1 - fb) + c1 * fb).astype(np.float32)


def interp_tetrahedral(table: np.ndarray, x: np.ndarray) -> np.ndarray:
    """
    Walk from the cell's origin to its far corner along the axes in descending
    order of fractional offset; the four vertices visited and the gaps between
    the sorted fractions are the tetrahedron's corners and barycentric weights.
    """
    n, f, base, flat = _corner_setup(table, x)
    steps = np.array([1, n, n * n], dtype=np.int64)
    order = np.argsort(-f, axis=1, kind="stable")                 # axes, largest fraction first
    fs = np.take_along_axis(f, order, axis=1)
    v1 = base + steps[order[:, 0]]
    v2 = v1 + steps[order[:, 1]]
    v3 = v2 + steps[order[:, 2]]
    w0 = 1.0 - fs[:, 0:1]
    w1 = fs[:, 0:1] - fs[:, 1:2]
    w2 = fs[:, 1:2] - fs[:, 2:3]
    w3 = fs[:, 2:3]
    return (flat[base] * w0 + flat[v1] * w1 + flat[v2] * w2 + flat[v3] * w3).astype(np.float32)


# ---------------------------------------------------------------------------
# Colour space handling
# ---------------------------------------------------------------------------

_PRIMARIES = {
    "srgb": ((0.64, 0.33), (0.30, 0.60), (0.15, 0.06), (0.3127, 0.3290)),
    "adobergb": ((0.64, 0.33), (0.21, 0.71), (0.15, 0.06), (0.3127, 0.3290)),
    "prophoto": ((0.7347, 0.2653), (0.1596, 0.8404), (0.0366, 0.0001), (0.3457, 0.3585)),
}
_BRADFORD = np.array([[0.8951, 0.2664, -0.1614], [-0.7502, 1.7135, 0.0367], [0.0389, -0.0685, 1.0296]])


def _xyz(xy) -> np.ndarray:
    x, y = xy
    return np.array([x / y, 1.0, (1 - x - y) / y])


def _rgb_to_xyz(space: str) -> np.ndarray:
    r, g, b, w = _PRIMARIES[space]
    prim = np.stack([_xyz(r), _xyz(g), _xyz(b)], axis=1)
    return prim * np.linalg.solve(prim, _xyz(w))


def to_srgb_matrix(space: str) -> np.ndarray:
    """3x3 taking linear RGB in ``space`` primaries to linear sRGB (Bradford-adapted)."""
    space = colour.normalise_space(space)
    if space == "srgb":
        return np.eye(3)
    cone_src = _BRADFORD @ _xyz(_PRIMARIES[space][3])
    cone_dst = _BRADFORD @ _xyz(_PRIMARIES["srgb"][3])
    adapt = np.linalg.inv(_BRADFORD) @ np.diag(cone_dst / cone_src) @ _BRADFORD
    return np.linalg.inv(_rgb_to_xyz("srgb")) @ adapt @ _rgb_to_xyz(space)


def _matmul(img: np.ndarray, m: np.ndarray) -> np.ndarray:
    return (img.reshape(-1, 3) @ m.T.astype(np.float32)).reshape(img.shape)


def apply(img: np.ndarray, lut: Lut, space: str = "srgb", amount: float = 1.0,
          method: str = "tetrahedral") -> np.ndarray:
    """
    Apply ``lut`` to an encoded float image (output ``space``) and blend by ``amount`` (0..1).

    Returns a new float32 array, clipped to 0..1. Non-sRGB output spaces are
    converted to sRGB primaries (linear) for the LUT and back.
    """
    space = colour.normalise_space(space)
    amount = float(min(1.0, max(0.0, amount)))
    x = np.clip(np.asarray(img, dtype=np.float32), 0.0, 1.0)
    if amount <= 0.0:
        return x

    if space == "srgb" and lut.space == "srgb":
        look = lut.sample(x, method)
    else:
        lin = colour.to_linear(x, space)
        if space != "srgb":
            lin = _matmul(lin, to_srgb_matrix(space))
        # The LUT only knows sRGB's gamut. Colours outside it (saturated Adobe RGB / ProPhoto
        # values) are looked up at their clipped position and keep their overshoot, so a
        # neutral LUT returns them untouched instead of flattening them.
        inside = np.clip(lin, 0.0, 1.0)
        pre = inside if lut.space == "linear" else colour.to_encoded(inside, "srgb")
        post = np.clip(lut.sample(pre, method), 0.0, 1.0)
        lin_out = (post if lut.space == "linear" else colour.to_linear(post, "srgb")) + (lin - inside)
        if space != "srgb":
            lin_out = _matmul(lin_out, np.linalg.inv(to_srgb_matrix(space)))
        look = colour.to_encoded(lin_out, space)
    look = np.clip(look, 0.0, 1.0)
    if amount < 1.0:
        look = x + (look - x) * F32(amount)
    return look.astype(np.float32, copy=False)


# ---------------------------------------------------------------------------
# Built-in film styles (generated, not shipped as files)
# ---------------------------------------------------------------------------

def _grade(c, *, contrast=0.0, lift=0.0, ceiling=1.0, sat=1.0, shadow=(0, 0, 0), high=(0, 0, 0), mono=0.0):
    if contrast:                                         # blend toward smoothstep: an S that never clips
        c = c + contrast * (c * c * (3 - 2 * c) - c)
    y = (c[..., 0] * 0.2126 + c[..., 1] * 0.7152 + c[..., 2] * 0.0722)[..., None]
    c = y + (c - y) * sat
    if mono:
        c = c * (1 - mono) + y * mono
    c = c + (1 - y) ** 2 * np.array(shadow) + y ** 2 * np.array(high)
    return np.clip(lift + c * (ceiling - lift), 0.0, 1.0)


BUILTIN_RECIPES = {
    "Warm Negative": dict(contrast=0.35, lift=0.025, ceiling=0.985, sat=0.92,
                          shadow=(-0.01, 0.012, 0.015), high=(0.045, 0.012, -0.05)),
    "Cool Chrome": dict(contrast=0.6, sat=1.12, shadow=(-0.02, 0.0, 0.05), high=(-0.02, 0.0, 0.03)),
    "Teal and Orange": dict(contrast=0.4, sat=1.1, shadow=(-0.05, 0.02, 0.05), high=(0.06, 0.02, -0.06)),
    "Faded Matte": dict(contrast=0.15, lift=0.07, ceiling=0.94, sat=0.82,
                        shadow=(0.0, 0.01, 0.02), high=(0.02, 0.01, 0.0)),
    "Silver Mono": dict(contrast=0.55, mono=1.0, shadow=(-0.005, -0.002, 0.008), high=(0.006, 0.003, -0.004)),
    "Sepia Print": dict(contrast=0.3, lift=0.03, ceiling=0.97, mono=1.0,
                        shadow=(0.03, 0.0, -0.03), high=(0.07, 0.035, -0.07)),
}
BUILTIN_SIZE = 33
_builtin_cache: dict[str, Lut] = {}


def builtin_id(name: str) -> str:
    return "builtin-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def builtin_ids() -> dict[str, str]:
    return {builtin_id(n): n for n in BUILTIN_RECIPES}


def builtin(lut_id: str) -> Lut | None:
    name = builtin_ids().get(lut_id)
    if name is None:
        return None
    if lut_id not in _builtin_cache:
        base = identity("3d", BUILTIN_SIZE)
        table = _grade(base.table.astype(np.float64), **BUILTIN_RECIPES[name]).astype(np.float32)
        _builtin_cache[lut_id] = Lut("3d", BUILTIN_SIZE, table, title=name)
    return _builtin_cache[lut_id]


# ---------------------------------------------------------------------------
# Library: ~/.photo-room/luts (PHOTO_ROOM_LUT_DIR redirects it)
# ---------------------------------------------------------------------------

def library_dir() -> Path:
    override = os.environ.get("PHOTO_ROOM_LUT_DIR")
    return Path(override) if override else Path.home() / ".photo-room" / "luts"


_lock = threading.RLock()
_loaded: dict[tuple[str, str, float], Lut] = {}
MAX_LOADED = 6
NAME_MAX = 80
_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def _index_path(root: Path) -> Path:
    return root / "index.json"


def _read_index(root: Path) -> dict:
    try:
        data = json.loads(_index_path(root).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_index(root: Path, index: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    tmp = _index_path(root).with_suffix(".tmp")
    tmp.write_text(json.dumps(index, indent=1, sort_keys=True))
    os.replace(tmp, _index_path(root))


def _clean_name(name: str | None, fallback: str) -> str:
    name = re.sub(r"\s+", " ", (name or "").strip())[:NAME_MAX]
    return name or fallback


def lut_id_of(lut: Lut) -> str:
    """Content hash: the same table is the same LUT, whatever it is called."""
    h = hashlib.sha1()
    h.update(f"{lut.kind}{lut.size}{lut.space}".encode())
    h.update(np.ascontiguousarray(lut.table).tobytes())
    h.update(np.ascontiguousarray(lut.domain_min).tobytes() + np.ascontiguousarray(lut.domain_max).tobytes())
    return h.hexdigest()[:12]


def import_text(text: str | bytes, name: str | None = None, space: str | None = None) -> dict:
    """Validate a .cube and add it to the library. Importing the same table again returns it."""
    lut = parse_cube(text, space=space)
    lut_id = lut_id_of(lut)
    root = library_dir()
    with _lock:
        index = _read_index(root)
        if lut_id in index and (root / f"{lut_id}.cube").exists():
            return describe(lut_id, index[lut_id])
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{lut_id}.cube").write_text(to_cube(lut))
        index[lut_id] = {"name": _clean_name(name or lut.title, "Imported LUT"), "kind": lut.kind,
                         "size": lut.size, "space": lut.space, "added": time.time()}
        _write_index(root, index)
        return describe(lut_id, index[lut_id])


def describe(lut_id: str, meta: dict | None = None) -> dict:
    if lut_id in builtin_ids():
        lut = builtin(lut_id)
        return {"id": lut_id, "name": lut.title, "kind": "3d", "size": lut.size,
                "space": "srgb", "builtin": True}
    meta = meta or _read_index(library_dir()).get(lut_id) or {}
    return {"id": lut_id, "name": meta.get("name", lut_id), "kind": meta.get("kind", "3d"),
            "size": meta.get("size", 0), "space": meta.get("space", "srgb"), "builtin": False}


def list_luts() -> list[dict]:
    out = [describe(i) for i in builtin_ids()]
    index = _read_index(library_dir())
    out += sorted((describe(i, m) for i, m in index.items()), key=lambda d: d["name"].lower())
    return out


def get(lut_id: str) -> Lut:
    """The LUT with this id; raises :class:`LutNotFound` if this machine does not have it."""
    lut = builtin(lut_id)
    if lut is not None:
        return lut
    if not _ID_RE.match(str(lut_id or "")):
        raise LutNotFound(f"No LUT called {lut_id!r}")
    path = library_dir() / f"{lut_id}.cube"
    try:
        stamp = path.stat().st_mtime
    except OSError:
        raise LutNotFound(f"LUT {lut_id} is not in the LUT library on this computer. "
                          "Import the .cube file again.") from None
    key = (str(path), lut_id, stamp)
    with _lock:
        hit = _loaded.get(key)
        if hit is None:
            hit = parse_cube(path.read_text())
            if len(_loaded) >= MAX_LOADED:
                _loaded.pop(next(iter(_loaded)))
            _loaded[key] = hit
        return hit


def exists(lut_id: str) -> bool:
    try:
        get(lut_id)
        return True
    except (LutNotFound, LutError):
        return False


def rename(lut_id: str, name: str) -> dict:
    root = library_dir()
    with _lock:
        index = _read_index(root)
        if lut_id not in index:
            raise LutNotFound(f"No LUT {lut_id!r} in the library (built-in LUTs cannot be renamed)")
        index[lut_id]["name"] = _clean_name(name, index[lut_id]["name"])
        _write_index(root, index)
        return describe(lut_id, index[lut_id])


def delete(lut_id: str) -> bool:
    root = library_dir()
    with _lock:
        index = _read_index(root)
        if lut_id not in index:
            return False
        del index[lut_id]
        _write_index(root, index)
        (root / f"{lut_id}.cube").unlink(missing_ok=True)
        _loaded.clear()
        return True


def export_text(lut_id: str) -> str:
    return to_cube(get(lut_id), title=describe(lut_id)["name"])
