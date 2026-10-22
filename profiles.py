"""
Camera profiles: a base look applied right after the camera-matrix conversion, before any edit.

Lightroom's Profile browser (Adobe Standard, Landscape, Portrait ...) decides what a raw looks like
*before* the sliders; this module is that layer. A profile is a small, fixed colour transform:

    HueSatMap -> LookTable -> tone curve        (the order the DNG specification gives)

plus, for the built-in profiles, a 3x3 colour matrix (saturation) ahead of them. It runs once per
photo/profile on the geometry output (``editor.apply_adjustments``) and is cached under a key that
includes the profile, so a slider drag never recomputes it (``stagecache.stage_keys``).

Sources of profiles
-------------------
* Built-ins (``builtin-standard`` ...): an S-curve in sRGB-coded values, a saturation matrix and a
  synthesised hue/saturation table that favours some hue bands. These are photo-room's own looks
  in the spirit of the camera-maker styles; they are NOT Adobe's profiles.
* ``.dcp`` (DNG Camera Profile): a TIFF-like container. ProfileHueSatMap, ProfileLookTable and
  ProfileToneCurve are applied. ColorMatrix1/2 and ForwardMatrix1/2 are parsed and kept for
  display, but not applied: they map *camera-native* RGB, and by the time this layer runs LibRaw has
  already converted to the output space with the camera's own matrix.
* ``.icc`` / ``.icm`` input profiles of the matrix/TRC kind: the photo's colours are re-interpreted
  as being in that profile's RGB space (its TRC and primaries) and converted to the working space.
  LUT-based (A2B) profiles are rejected with a message.

Storage: ``~/.photo-room/profiles`` (``PHOTO_ROOM_PROFILE_DIR`` redirects it), one file per profile
named by the first 12 hex digits of its MD5, plus ``index.json``. XMP: ``crs:CameraProfile`` (the
name) and ``crs:CameraProfileDigest`` (the file's MD5, as Adobe does) so a photo finds its profile
again on open.

Nothing here imports Flask or the editor.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

import colour

F32 = np.float32
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TABLE_ENTRIES = 1_000_000
NAME_MAX = 80
BUILTIN_REV = "b1"          # bump when a built-in's maths changes (part of every cache key)
XMP_BUILTIN_PREFIX = "photo-room "


class ProfileError(ValueError):
    """The file is not a usable camera profile; the message says why."""


class ProfileNotFound(KeyError):
    """A photo refers to a profile this machine does not have."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""


@dataclass
class HsvTable:
    dims: tuple[int, int, int]          # hue, sat, val divisions
    data: np.ndarray                    # (val, hue, sat, 3) float32: hue shift deg, sat scale, val scale
    encoding: int = 0                   # 0 linear, 1 sRGB-coded (ProfileHueSatMapEncoding), 2 working-space coded


@dataclass
class IccMatrix:
    to_xyz: np.ndarray                  # 3x3, device linear -> XYZ (D50)
    trc: list[np.ndarray]               # three 1024-entry tables: device-coded -> linear


@dataclass
class Profile:
    id: str
    name: str
    kind: str                           # builtin | dcp | icc
    matrix: np.ndarray | None = None    # 3x3 on linear working RGB
    hue_sat: HsvTable | None = None
    look: HsvTable | None = None
    curve: tuple[np.ndarray, np.ndarray] | None = None
    curve_encoded: bool = False         # curve x/y are the working space's coded values (built-ins), not linear (DCP)
    matrix_coded: bool = False          # matrix acts on coded values (built-ins), not linear light
    icc: IccMatrix | None = None
    info: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Applying a profile
# ---------------------------------------------------------------------------

_XYZ_D50 = np.array([0.9642, 1.0, 0.8249])


def _bradford_to_d50(white_xy) -> np.ndarray:
    import lut as lutmod
    b = lutmod._BRADFORD
    src = b @ lutmod._xyz(white_xy)
    return np.linalg.inv(b) @ np.diag((b @ _XYZ_D50) / src) @ b


def _xyz50_from_working(space: str) -> np.ndarray:
    """3x3 taking linear RGB of ``space`` to XYZ adapted to D50."""
    import lut as lutmod
    space = colour.normalise_space(space)
    return _bradford_to_d50(lutmod._PRIMARIES[space][3]) @ lutmod._rgb_to_xyz(space)


def _matmul(img: np.ndarray, m: np.ndarray) -> np.ndarray:
    return (img.reshape(-1, 3) @ np.asarray(m, dtype=np.float32).T).reshape(img.shape)


def _rgb_to_hsv(rgb: np.ndarray):
    r, g, b = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    v = rgb.max(axis=1)
    c = v - rgb.min(axis=1)
    s = np.where(v > 1e-9, c / np.maximum(v, 1e-9), 0.0).astype(F32)
    cs = np.maximum(c, 1e-9)
    h = np.where(v == r, ((g - b) / cs) % 6.0,
                 np.where(v == g, (b - r) / cs + 2.0, (r - g) / cs + 4.0))
    h = np.where(c > 1e-9, h * 60.0, 0.0).astype(F32)
    return h, s, v.astype(F32)


def _hsv_to_rgb(h, s, v) -> np.ndarray:
    h6 = (h % 360.0) / 60.0
    i = np.floor(h6).astype(np.int32) % 6
    f = (h6 - np.floor(h6)).astype(F32)
    p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
    r = np.choose(i, [v, q, p, p, t, v])
    g = np.choose(i, [t, v, v, q, p, p])
    b = np.choose(i, [p, p, t, v, v, q])
    return np.stack([r, g, b], axis=1).astype(F32)


_FINE_H, _FINE_S = 720, 128


def _fine_map(t: HsvTable) -> np.ndarray:
    """For a table with one value division: its (hue, sat) response baked to a fine grid, so a pixel costs one gather."""
    if getattr(t, "_fine", None) is None:
        hh = (np.arange(_FINE_H, dtype=np.float32) + 0.5) * (360.0 / _FINE_H)
        ss = np.linspace(0.0, 1.0, _FINE_S, dtype=np.float32)
        h, s = np.meshgrid(hh, ss, indexing="ij")
        t._fine = _sample_table(t, h.reshape(-1), s.reshape(-1), np.zeros(h.size, np.float32)
                                ).reshape(_FINE_H, _FINE_S, 3).astype(F32)
    return t._fine


def _sample_table(t: HsvTable, h, s, v) -> np.ndarray:
    hd, sd, vd = t.dims
    hp = (h / 360.0 * hd) % hd
    h0 = np.floor(hp).astype(np.int32)
    hf = (hp - h0).astype(F32)[:, None]
    h0 %= hd
    h1 = (h0 + 1) % hd
    sp = np.clip(s, 0.0, 1.0) * (sd - 1)
    s0 = np.minimum(np.floor(sp).astype(np.int32), max(sd - 2, 0))
    s1 = np.minimum(s0 + 1, sd - 1)
    sf = (sp - s0).astype(F32)[:, None] if sd > 1 else np.zeros((len(h), 1), F32)
    if vd > 1:
        vp = np.clip(v, 0.0, 1.0) * (vd - 1)
        v0 = np.minimum(np.floor(vp).astype(np.int32), vd - 2)
        v1 = v0 + 1
        vf = (vp - v0).astype(F32)[:, None]
    else:
        v0 = v1 = np.zeros(len(h), np.int32)
        vf = np.zeros((len(h), 1), F32)
    d = t.data
    out = 0.0
    for vi, vw in ((v0, 1 - vf), (v1, vf)):
        for hi, hw in ((h0, 1 - hf), (h1, hf)):
            for si, sw in ((s0, 1 - sf), (s1, sf)):
                out = out + d[vi, hi, si] * (vw * hw * sw)
    return out


def _apply_table(rgb: np.ndarray, t: HsvTable) -> np.ndarray:
    """``rgb`` is already in the table's own encoding (see :func:`apply`)."""
    flat = np.clip(rgb.reshape(-1, 3), 0.0, 1.0)
    h, s, v = _rgb_to_hsv(flat)
    if t.dims[2] == 1:
        fine = _fine_map(t)
        hi = np.minimum((h * (_FINE_H / 360.0)).astype(np.int32), _FINE_H - 1)
        si = np.minimum((s * (_FINE_S - 1) + 0.5).astype(np.int32), _FINE_S - 1)
        mod = fine[hi, si]
    else:
        mod = _sample_table(t, h, s, v)
    out = _hsv_to_rgb(h + mod[:, 0], np.clip(s * mod[:, 1], 0, 1), np.clip(v * mod[:, 2], 0, 1))
    return out.reshape(rgb.shape)


_CURVE_N = 16385


def _apply_curve(x: np.ndarray, profile: Profile) -> np.ndarray:
    """One-lookup tone curve (16 385 entries: finer than 14 bits)."""
    xs, ys = profile.curve
    table = np.interp(np.linspace(0.0, 1.0, _CURVE_N), xs, ys).astype(F32)
    idx = (np.clip(x, 0.0, 1.0) * (_CURVE_N - 1) + 0.5).astype(np.int32)
    return table[idx]


def _apply_icc(lin: np.ndarray, icc: IccMatrix, space: str) -> np.ndarray:
    enc = np.clip(colour.to_encoded(np.clip(lin, 0, 1), space), 0, 1)
    grid = np.linspace(0.0, 1.0, 1024)
    dev = np.stack([np.interp(enc[..., c], grid, icc.trc[c]) for c in range(3)], axis=-1).astype(F32)
    m = np.linalg.inv(_xyz50_from_working(space)) @ icc.to_xyz
    return _matmul(dev, m)


class _Img:
    """The image in whichever domain the current step wants, converting only when that changes."""

    def __init__(self, arr: np.ndarray, linear: bool, space: str):
        self.arr, self.lin, self.space = np.asarray(arr, dtype=F32), linear, space

    def get(self, linear: bool) -> np.ndarray:
        if linear != self.lin:
            self.arr = (colour.to_linear if linear else colour.to_encoded)(np.clip(self.arr, 0, 1), self.space)
            self.lin = linear
        return self.arr

    def set(self, arr: np.ndarray, linear: bool) -> None:
        self.arr, self.lin = arr, linear


def apply(img: np.ndarray, profile: Profile, space: str = "srgb", linear: bool = False) -> np.ndarray:
    """``img`` (float RGB 0..1, encoded unless ``linear``) through ``profile``; a new float32 array.

    Built-in profiles work on the working space's coded values (as the Saturation slider does) so the
    common case needs no transfer-function conversion; DCP data is defined in linear light and keeps to it.
    """
    space = colour.normalise_space(space)
    im = _Img(img, linear, space)
    if profile.icc is not None:
        im.set(_apply_icc(im.get(True), profile.icc, space), True)
    if profile.matrix is not None:
        coded = profile.matrix_coded
        im.set(np.clip(_matmul(im.get(not coded), profile.matrix), 0.0, 1.0), not coded)
    for table in (profile.hue_sat, profile.look):
        if table is None:
            continue
        if table.encoding == 0:                                 # linear light
            im.set(_apply_table(im.get(True), table), True)
        elif table.encoding == 2 or space == "srgb":            # the working space's coded values
            im.set(_apply_table(im.get(False), table), False)
        else:                                                   # sRGB-coded, in another working space
            srgb = colour.to_encoded(np.clip(im.get(True), 0, 1), "srgb")
            im.set(colour.to_linear(_apply_table(srgb, table), "srgb"), True)
    if profile.curve is not None:
        coded = profile.curve_encoded
        im.set(_apply_curve(im.get(not coded), profile), not coded)
    return np.clip(im.get(linear), 0.0, 1.0).astype(F32, copy=False)


# ---------------------------------------------------------------------------
# Built-in profiles
# ---------------------------------------------------------------------------

# name -> (saturation, contrast 0..1 (negative flattens), {hue centre deg: extra saturation})
_BUILTIN_SPECS = {
    "Standard":   (1.00, 0.25, {}),
    "Neutral":    (0.92, 0.10, {}),
    "Landscape":  (1.12, 0.35, {120: 0.25, 230: 0.20, 30: 0.08}),
    "Portrait":   (0.96, 0.12, {0: -0.08, 30: -0.10}),
    "Vivid":      (1.30, 0.45, {}),
    "Flat":       (0.85, -0.25, {}),
    "Monochrome": (0.00, 0.30, {}),
}
_HUE_DIVS = 36


def builtin_id(name: str) -> str:
    return "builtin-" + name.lower()


def _band_table(bands: dict) -> HsvTable | None:
    if not bands:
        return None
    centres = np.arange(_HUE_DIVS) * (360.0 / _HUE_DIVS)
    boost = np.zeros(_HUE_DIVS)
    for centre, amount in bands.items():
        d = np.abs((centres - centre + 180.0) % 360.0 - 180.0)
        boost += amount * np.exp(-0.5 * (d / 25.0) ** 2)
    data = np.zeros((1, _HUE_DIVS, 2, 3), np.float32)
    data[..., 1] = 1.0
    data[..., 2] = 1.0
    data[0, :, 1, 1] = 1.0 + boost          # full saturation entry; sat 0 stays 1.0 (greys untouched)
    return HsvTable((_HUE_DIVS, 2, 1), data, encoding=2)


def _saturation_matrix(s: float) -> np.ndarray:
    return s * np.eye(3) + (1.0 - s) * np.ones((3, 1)) @ colour.LUMA[None, :]


_builtin_cache: dict[str, Profile] = {}


def builtin(profile_id: str) -> Profile | None:
    for name, (sat, contrast, bands) in _BUILTIN_SPECS.items():
        if builtin_id(name) == profile_id:
            if profile_id not in _builtin_cache:
                x = np.linspace(0.0, 1.0, 65)
                y = np.clip(x - contrast * np.sin(2 * math.pi * x) / (2 * math.pi), 0, 1)
                _builtin_cache[profile_id] = Profile(
                    id=profile_id, name=name, kind="builtin",
                    matrix=None if abs(sat - 1) < 1e-9 else _saturation_matrix(sat), matrix_coded=True,
                    hue_sat=_band_table(bands),
                    curve=None if abs(contrast) < 1e-9 else (x, y), curve_encoded=True,
                    info={"saturation": sat, "contrast": contrast})
            return _builtin_cache[profile_id]
    return None


def builtin_ids() -> dict[str, str]:
    return {builtin_id(n): n for n in _BUILTIN_SPECS}


# ---------------------------------------------------------------------------
# DCP parsing
# ---------------------------------------------------------------------------

_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}
T_UNIQUE_MODEL, T_COLOR1, T_COLOR2 = 50708, 50721, 50722
T_ILLUM1, T_ILLUM2 = 50778, 50779
T_FORWARD1, T_FORWARD2 = 50964, 50965
T_NAME, T_COPYRIGHT, T_CURVE = 50936, 50942, 50940
T_HSM_DIMS, T_HSM1, T_HSM2 = 50937, 50938, 50939
T_LOOK_DIMS, T_LOOK_DATA = 50981, 50982
T_HSM_ENC, T_LOOK_ENC = 51107, 51108


def _tiff_entries(raw: bytes) -> dict[int, list | str]:
    if len(raw) < 8:
        raise ProfileError("This file is too small to be a camera profile")
    order = {b"II": "<", b"MM": ">"}.get(raw[:2])
    if order is None:
        raise ProfileError("Not a DNG camera profile (.dcp): the TIFF byte order mark is missing")
    magic, ifd = struct.unpack(order + "HI", raw[2:8])
    if magic not in (0x4352, 0x5243, 42):
        raise ProfileError("Not a DNG camera profile (.dcp): wrong magic number")
    if ifd + 2 > len(raw):
        raise ProfileError("Damaged camera profile: the tag directory is outside the file")
    (count,) = struct.unpack(order + "H", raw[ifd:ifd + 2])
    if count == 0 or count > 2000 or ifd + 2 + count * 12 > len(raw):
        raise ProfileError("Damaged camera profile: bad tag directory")
    out: dict[int, list | str] = {}
    for i in range(count):
        tag, typ, n, val = struct.unpack(order + "HHI4s", raw[ifd + 2 + i * 12: ifd + 14 + i * 12])
        size = _TYPE_SIZE.get(typ)
        if size is None or n > MAX_TABLE_ENTRIES * 3:
            continue
        nbytes = size * n
        if nbytes <= 4:
            blob = val[:nbytes]
        else:
            (off,) = struct.unpack(order + "I", val)
            if off + nbytes > len(raw):
                raise ProfileError(f"Damaged camera profile: tag {tag} runs past the end of the file")
            blob = raw[off:off + nbytes]
        out[tag] = _decode(order, typ, n, blob)
    return out


def _decode(order: str, typ: int, n: int, blob: bytes):
    if typ in (2, 1, 7, 6):
        return blob.rstrip(b"\0").decode("utf-8", "replace") if typ == 2 else list(blob)
    fmt = {3: "H", 4: "I", 8: "h", 9: "i", 11: "f", 12: "d"}.get(typ)
    if fmt:
        return list(struct.unpack(order + fmt * n, blob))
    pairs = struct.unpack(order + ("II" if typ == 5 else "ii") * n, blob)       # (s)rational
    return [a / b if b else 0.0 for a, b in zip(pairs[0::2], pairs[1::2])]


def _matrix(vals) -> list | None:
    if isinstance(vals, list) and len(vals) == 9 and all(math.isfinite(v) for v in vals):
        return [list(map(float, vals[i:i + 3])) for i in (0, 3, 6)]
    return None


def _table(entries, dims_tag, data_tags, enc_tag) -> HsvTable | None:
    dims = entries.get(dims_tag)
    data = next((entries[t] for t in data_tags if t in entries), None)
    if dims is None or data is None:
        return None
    if len(dims) != 3 or any(not isinstance(d, int) or d < 1 for d in dims):
        raise ProfileError("Camera profile has an invalid hue/saturation table size")
    hd, sd, vd = dims
    if hd * sd * vd > MAX_TABLE_ENTRIES or hd * sd * vd * 3 != len(data):
        raise ProfileError("Camera profile hue/saturation table does not match its declared size")
    arr = np.asarray(data, dtype=np.float32).reshape(vd, hd, sd, 3)       # DNG order: value, hue, sat
    if not np.isfinite(arr).all():
        raise ProfileError("Camera profile hue/saturation table contains invalid numbers")
    enc = entries.get(enc_tag, [0])
    return HsvTable((hd, sd, vd), arr, encoding=1 if enc and enc[0] == 1 else 0)


def parse_dcp(raw: bytes, filename: str = "") -> Profile:
    """A :class:`Profile` from the bytes of a ``.dcp``; raises :class:`ProfileError` if unusable."""
    if len(raw) > MAX_FILE_BYTES:
        raise ProfileError("Camera profile is too large")
    e = _tiff_entries(raw)
    name = e.get(T_NAME) if isinstance(e.get(T_NAME), str) else ""
    name = _clean_name(name or Path(filename).stem, "Imported profile")
    curve = None
    pts = e.get(T_CURVE)
    if pts is not None:
        if len(pts) < 4 or len(pts) % 2 or not all(math.isfinite(p) for p in pts):
            raise ProfileError("Camera profile has an invalid tone curve")
        xs, ys = np.asarray(pts[0::2], np.float64), np.asarray(pts[1::2], np.float64)
        if (np.diff(xs) <= 0).any() or xs[0] < 0 or xs[-1] > 1.0001:
            raise ProfileError("Camera profile tone curve is not increasing")
        curve = (xs, np.clip(ys, 0, 1))
    hue_sat = _table(e, T_HSM_DIMS, (T_HSM2, T_HSM1), T_HSM_ENC)
    look = _table(e, T_LOOK_DIMS, (T_LOOK_DATA,), T_LOOK_ENC)
    if curve is None and hue_sat is None and look is None:
        raise ProfileError("This camera profile has no tone curve or hue/saturation table to apply "
                           "(only its colour matrices, which photo-room cannot use on an already-converted image)")
    info = {"camera": e.get(T_UNIQUE_MODEL), "copyright": e.get(T_COPYRIGHT),
            "illuminants": [e.get(T_ILLUM1), e.get(T_ILLUM2)],
            "color_matrix1": _matrix(e.get(T_COLOR1)), "color_matrix2": _matrix(e.get(T_COLOR2)),
            "forward_matrix1": _matrix(e.get(T_FORWARD1)), "forward_matrix2": _matrix(e.get(T_FORWARD2)),
            "matrices_applied": False}
    digest = hashlib.md5(raw).hexdigest()
    return Profile(id=digest[:12], name=name, kind="dcp", hue_sat=hue_sat, look=look,
                   curve=curve, curve_encoded=False, info=info)


# ---------------------------------------------------------------------------
# ICC matrix/TRC input profiles
# ---------------------------------------------------------------------------

def _s15(b: bytes) -> float:
    return struct.unpack(">i", b)[0] / 65536.0


def _trc(tag: bytes) -> np.ndarray:
    typ = tag[:4]
    x = np.linspace(0.0, 1.0, 1024)
    if typ == b"curv":
        (n,) = struct.unpack(">I", tag[8:12])
        if n == 0:
            return x
        if n == 1:
            return x ** (struct.unpack(">H", tag[12:14])[0] / 256.0)
        if n > 65536 or len(tag) < 12 + 2 * n:
            raise ProfileError("ICC profile has a damaged tone curve")
        table = np.frombuffer(tag[12:12 + 2 * n], dtype=">u2").astype(np.float64) / 65535.0
        return np.interp(x, np.linspace(0, 1, n), table)
    if typ == b"para":
        (ft,) = struct.unpack(">H", tag[8:10])
        counts = {0: 1, 1: 3, 2: 4, 3: 5, 4: 7}
        if ft not in counts or len(tag) < 12 + 4 * counts[ft]:
            raise ProfileError("ICC profile has an unsupported parametric curve")
        p = [_s15(tag[12 + 4 * i:16 + 4 * i]) for i in range(counts[ft])]
        g = p[0]
        if ft == 0:
            return x ** g
        a, b = p[1], p[2]
        with np.errstate(all="ignore"):
            core = np.maximum(a * x + b, 0.0) ** g
            if ft == 1:
                y = np.where(x >= -b / a, core, 0.0) if a else core
            elif ft == 2:
                c = p[3]
                y = np.where(x >= -b / a, core + c, c) if a else core + c
            elif ft == 3:
                c, d = p[3], p[4]
                y = np.where(x >= d, core, c * x)
            else:
                c, d, e_, f = p[3], p[4], p[5], p[6]
                y = np.where(x >= d, core + e_, c * x + f)
        return np.clip(np.nan_to_num(y), 0.0, 1.0)
    raise ProfileError("ICC profile has an unsupported tone curve type")


def parse_icc(raw: bytes, filename: str = "") -> Profile:
    if len(raw) > MAX_FILE_BYTES:
        raise ProfileError("ICC profile is too large")
    if len(raw) < 132 or raw[36:40] != b"acsp":
        raise ProfileError("Not an ICC profile")
    if raw[16:20] != b"RGB ":
        raise ProfileError("Only RGB ICC input profiles can be used")
    (count,) = struct.unpack(">I", raw[128:132])
    if count > 200 or 132 + 12 * count > len(raw):
        raise ProfileError("Damaged ICC profile: bad tag table")
    tags = {}
    for i in range(count):
        sig, off, size = struct.unpack(">4sII", raw[132 + 12 * i: 144 + 12 * i])
        if off + size > len(raw):
            raise ProfileError("Damaged ICC profile: a tag runs past the end of the file")
        tags[sig] = raw[off:off + size]
    need = (b"rXYZ", b"gXYZ", b"bXYZ", b"rTRC", b"gTRC", b"bTRC")
    if any(t not in tags for t in need):
        raise ProfileError("Only matrix/curve ICC profiles are supported; this one uses lookup tables (A2B)")
    cols = []
    for t in need[:3]:
        if tags[t][:4] != b"XYZ " or len(tags[t]) < 20:
            raise ProfileError("ICC profile has a damaged colorant tag")
        cols.append([_s15(tags[t][8 + 4 * i:12 + 4 * i]) for i in range(3)])
    name = ""
    d = tags.get(b"desc", b"")
    if d[:4] == b"desc" and len(d) > 12:
        (n,) = struct.unpack(">I", d[8:12])
        name = d[12:12 + max(n - 1, 0)].decode("latin-1", "replace")
    elif d[:4] == b"mluc" and len(d) >= 28:
        (ln, off) = struct.unpack(">II", d[20:28])
        name = d[off:off + ln].decode("utf-16-be", "replace")
    name = _clean_name(name or Path(filename).stem, "Imported ICC profile")
    return Profile(id=hashlib.md5(raw).hexdigest()[:12], name=name, kind="icc",
                   icc=IccMatrix(np.array(cols).T, [_trc(tags[t]) for t in need[3:]]),
                   info={"matrices_applied": True})


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------

def library_dir() -> Path:
    override = os.environ.get("PHOTO_ROOM_PROFILE_DIR")
    return Path(override) if override else Path.home() / ".photo-room" / "profiles"


_lock = threading.RLock()
_loaded: dict[tuple, Profile] = {}
MAX_LOADED = 6
_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_EXT = {"dcp": ".dcp", "icc": ".icc"}


def _clean_name(name: str | None, fallback: str) -> str:
    name = re.sub(r"[\x00-\x1f]", "", re.sub(r"\s+", " ", (name or "").strip()))[:NAME_MAX]
    return name or fallback


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


def parse(raw: bytes, filename: str = "") -> Profile:
    """Parse by content: a ``.dcp`` or an ICC profile."""
    if raw[:2] in (b"II", b"MM") and raw[2:4] in (b"RC", b"CR", b"*\0", b"\0*"):
        return parse_dcp(raw, filename)
    if len(raw) >= 40 and raw[36:40] == b"acsp":
        return parse_icc(raw, filename)
    raise ProfileError("Unrecognised file: expected a DNG camera profile (.dcp) or an ICC profile")


def describe(profile_id: str, meta: dict | None = None) -> dict:
    b = builtin(profile_id)
    if b is not None:
        return {"id": profile_id, "name": b.name, "kind": "builtin", "builtin": True,
                "group": "photo-room", "digest": ""}
    meta = meta or _read_index(library_dir()).get(profile_id) or {}
    return {"id": profile_id, "name": meta.get("name", profile_id), "kind": meta.get("kind", "dcp"),
            "builtin": False, "group": "Imported", "digest": meta.get("digest", ""),
            "camera": meta.get("camera"), "matrices_applied": meta.get("matrices_applied", False)}


def list_profiles() -> list[dict]:
    out = [describe(i) for i in builtin_ids()]
    index = _read_index(library_dir())
    out += sorted((describe(i, m) for i, m in index.items()), key=lambda d: d["name"].lower())
    return out


def import_bytes(raw: bytes, filename: str = "", name: str | None = None) -> dict:
    """Validate and add a profile to the library. Importing the same file again returns it."""
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise ProfileError("The file is empty")
    raw = bytes(raw)
    prof = parse(raw, filename)
    root = library_dir()
    with _lock:
        index = _read_index(root)
        path = root / f"{prof.id}{_EXT.get(prof.kind, '.bin')}"
        if prof.id in index and path.exists():
            return describe(prof.id, index[prof.id])
        root.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        index[prof.id] = {"name": _clean_name(name, prof.name), "kind": prof.kind,
                          "digest": hashlib.md5(raw).hexdigest(), "added": time.time(),
                          "camera": prof.info.get("camera"),
                          "matrices_applied": bool(prof.info.get("matrices_applied"))}
        _write_index(root, index)
        return describe(prof.id, index[prof.id])


def _path_of(profile_id: str) -> Path | None:
    for ext in _EXT.values():
        p = library_dir() / f"{profile_id}{ext}"
        if p.exists():
            return p
    return None


def get(profile_id: str) -> Profile:
    """The profile with this id; raises :class:`ProfileNotFound` if this machine does not have it."""
    b = builtin(profile_id)
    if b is not None:
        return b
    if not _ID_RE.match(str(profile_id or "")):
        raise ProfileNotFound(f"No camera profile called {profile_id!r}")
    path = _path_of(profile_id)
    if path is None:
        raise ProfileNotFound(f"Camera profile {profile_id} is not in the profile library on this computer. "
                              "Import the .dcp file again.")
    key = (str(path), path.stat().st_mtime)
    with _lock:
        hit = _loaded.get(key)
        if hit is None:
            hit = parse(path.read_bytes(), path.name)
            hit.name = describe(profile_id)["name"]
            if len(_loaded) >= MAX_LOADED:
                _loaded.pop(next(iter(_loaded)))
            _loaded[key] = hit
        return hit


def exists(profile_id: str) -> bool:
    try:
        get(profile_id)
        return True
    except (ProfileNotFound, ProfileError, OSError):
        return False


def delete(profile_id: str) -> bool:
    if builtin(profile_id) is not None or not _ID_RE.match(str(profile_id or "")):
        return False
    with _lock:
        index = _read_index(library_dir())
        path = _path_of(profile_id)
        found = index.pop(profile_id, None) is not None or path is not None
        if path is not None:
            path.unlink(missing_ok=True)
        if found:
            _write_index(library_dir(), index)
        _loaded.clear()
    return found


def revision(profile_id) -> str:
    """Cache-key component: changes whenever what this id renders as could change."""
    if not profile_id or not isinstance(profile_id, str):
        return ""
    if builtin(profile_id) is not None:
        return BUILTIN_REV
    path = _path_of(profile_id) if _ID_RE.match(profile_id) else None
    return "missing" if path is None else f"{profile_id}@{int(path.stat().st_mtime_ns)}"


# ---------------------------------------------------------------------------
# XMP: crs:CameraProfile (+ CameraProfileDigest)
# ---------------------------------------------------------------------------

READ_TAGS = ["XMP-crs:CameraProfile", "XMP-crs:CameraProfileDigest"]


def xmp_arguments(adj: dict) -> list[str]:
    """exiftool assignments. A profile this tool did not choose (e.g. Lightroom's) is written back unchanged."""
    pid = adj.get("camera_profile") or ""
    if pid:
        d = describe(pid)
        if d["builtin"]:
            return [f"-XMP-crs:CameraProfile={XMP_BUILTIN_PREFIX}{d['name']}", "-XMP-crs:CameraProfileDigest="]
        return [f"-XMP-crs:CameraProfile={d['name']}", f"-XMP-crs:CameraProfileDigest={d['digest']}"]
    foreign = adj.get("camera_profile_foreign")
    if foreign:
        return [f"-XMP-crs:CameraProfile={foreign}"]
    return ["-XMP-crs:CameraProfile=", "-XMP-crs:CameraProfileDigest="]


def parse_xmp(name: str | None, digest: str | None) -> dict:
    """``camera_profile`` (our id) or ``camera_profile_foreign`` (a name we cannot resolve) from XMP values."""
    name = (name or "").strip()
    digest = (digest or "").strip().lower()
    name = "" if name == "-" else name
    digest = "" if digest == "-" else digest
    if not name:
        return {}
    if name.startswith(XMP_BUILTIN_PREFIX):
        for pid, n in builtin_ids().items():
            if n == name[len(XMP_BUILTIN_PREFIX):]:
                return {"camera_profile": pid}
    index = _read_index(library_dir())
    if re.fullmatch(r"[0-9a-f]{32}", digest):
        for pid, meta in index.items():
            if meta.get("digest") == digest:
                return {"camera_profile": pid}
    for pid, meta in index.items():
        if meta.get("name") == name:
            return {"camera_profile": pid}
    return {"camera_profile_foreign": name[:120]}
