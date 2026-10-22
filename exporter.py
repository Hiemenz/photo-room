"""
Export edited photos as PNG or JPEG, keeping the source's metadata.

The raw (or DNG) is never modified. Pixels are rendered from the raw with the
develop settings stored in its sidecar (or inside the DNG), then every tag
from the source -- EXIF, GPS, IPTC, XMP and so on -- is copied onto the output
with exiftool.

Colour: for raw sources the sensor data is decoded straight into the chosen
output space (sRGB, Adobe RGB or ProPhoto RGB) and a matching ICC profile is
embedded. For ordinary images (JPEG/TIFF/PNG) the source's own profile is kept
unless a colour space is requested.
"""

from __future__ import annotations

import io
import os
import re
import string
import shutil
import struct
import subprocess
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, ImageOps

import colour
import decode_cache
import fastops
import imgload
import exifpool
import export_finish
import process
import render_kernels
from process import EXIFTOOL_TIMEOUT, RAW_SUFFIXES, run_tool

FORMATS = {"png": ".png", "jpeg": ".jpg", "jpg": ".jpg",
           "tiff": ".tif", "tif": ".tif", "webp": ".webp"}
SIXTEEN_BIT_FORMATS = ("png", "tiff")
# Lightroom-style metadata choices (see metadata_args).
METADATA_MODES = ("all", "copyright", "no_location", "no_develop", "none")
COLOR_SPACES = ("srgb", "adobergb", "prophoto")

# Tags that describe the *file* or the rendering rather than the photograph.
# Copying them would be wrong (dimensions, orientation already applied by the
# decoder, thumbnails of the unedited frame) or would double-apply edits
# (Camera Raw develop settings are baked into the pixels by now).
_NOT_COPIED = [
    "--ICC_Profile:all",     # the output carries its own profile
    "--IFD1:all",            # thumbnail of the unedited frame
    "--ExifImageWidth", "--ExifImageHeight",
    "--ImageWidth", "--ImageHeight",
    "--XMP-crs:all",         # edits are baked in; do not apply them twice
    "--XMP-tiff:Orientation",
]


@dataclass
class ExportOptions:
    format: str = "jpeg"
    quality: int = 92
    long_edge: int | None = None
    colorspace: str | None = None  # None: sRGB (raws and images alike)
    template: str = "{name}"
    keep_metadata: bool = True      # False is the same as metadata="none"
    metadata: str = "all"           # one of METADATA_MODES
    bit_depth: int = 8  # 16 is PNG or TIFF only
    sharpen: dict | None = None     # {"for": screen|matte|glossy, "amount": low|standard|high}
    watermark: dict | None = None   # see export_finish.DEFAULT_WATERMARK
    # Encoder speed knobs; they change file size and time, never a pixel.
    png_compression: int = 1        # zlib level 0-9 (Pillow's old default, 6, is ~4x slower)
    full_sensor: bool = False       # raw: the whole decoded sensor area instead of the camera's default crop
    optimize_jpeg: bool = False     # optimal Huffman tables: ~5% smaller, ~2x slower to encode

    def validate(self) -> "ExportOptions":
        fmt = self.format.lower()
        if fmt not in FORMATS:
            raise ValueError(f"format must be jpeg, png, tiff or webp, not {self.format!r}")
        self.format = {"jpg": "jpeg", "tif": "tiff"}.get(fmt, fmt)
        mode = str(self.metadata or "all").lower().replace(" ", "_").replace("-", "_")
        if mode not in METADATA_MODES:
            raise ValueError(f"metadata must be one of {', '.join(METADATA_MODES)}")
        if not self.keep_metadata:
            mode = "none"
        self.metadata = mode
        self.keep_metadata = mode != "none"
        if not 1 <= int(self.quality) <= 100:
            raise ValueError("quality must be between 1 and 100")
        if int(self.bit_depth) not in (8, 16):
            raise ValueError("bit depth must be 8 or 16")
        self.bit_depth = int(self.bit_depth)
        if not 0 <= int(self.png_compression) <= 9:
            raise ValueError("PNG compression must be between 0 and 9")
        self.png_compression = int(self.png_compression)
        if self.bit_depth == 16 and self.format not in SIXTEEN_BIT_FORMATS:
            raise ValueError("16-bit output is only available for PNG and TIFF")
        if self.long_edge is not None and int(self.long_edge) < 16:
            raise ValueError("long edge must be at least 16 pixels")
        if self.colorspace is not None:
            cs = self.colorspace.lower().replace(" ", "").replace("-", "")
            if cs not in COLOR_SPACES:
                raise ValueError(f"colour space must be one of {', '.join(COLOR_SPACES)}")
            self.colorspace = cs
        self.sharpen = export_finish.normalize_sharpen(self.sharpen)
        self.watermark = export_finish.normalize_watermark(self.watermark)
        return self


# ---------------------------------------------------------------------------
# ICC profiles
# ---------------------------------------------------------------------------

# D50-adapted RGB->XYZ colorants and gamma for the wide-gamut spaces.
_MATRIX_SPACES = {
    "adobergb": ("Adobe RGB (1998)",
                 [(0.6097559, 0.3111242, 0.0194811),
                  (0.2052401, 0.6256560, 0.0608902),
                  (0.1492240, 0.0632197, 0.7448387)], 2.19921875),
    "prophoto": ("ProPhoto RGB",
                 [(0.7977, 0.2880, 0.0000),
                  (0.1352, 0.7119, 0.0000),
                  (0.0313, 0.0001, 0.8249)], 1.8),
}


def _s15(v: float) -> bytes:
    return struct.pack(">i", int(round(v * 65536)))


def _matrix_profile(name: str, colorants, gamma: float) -> bytes:
    """A minimal ICC v2 matrix/TRC display profile."""
    def xyz(v):
        return b"XYZ \x00\x00\x00\x00" + b"".join(_s15(c) for c in v)

    curve = b"curv\x00\x00\x00\x00" + struct.pack(">I", 1) + struct.pack(">H", int(round(gamma * 256))) + b"\x00\x00"
    desc_text = name.encode("ascii") + b"\x00"
    desc = (b"desc\x00\x00\x00\x00" + struct.pack(">I", len(desc_text)) + desc_text
            + b"\x00" * 4 + b"\x00" * 4 + struct.pack(">H", 0) + b"\x00" + b"\x00" * 67)
    cprt_text = b"Public domain\x00"
    tags = [
        (b"desc", desc),
        (b"cprt", b"text\x00\x00\x00\x00" + cprt_text),
        (b"wtpt", xyz((0.9642, 1.0, 0.8249))),
        (b"rXYZ", xyz(colorants[0])), (b"gXYZ", xyz(colorants[1])), (b"bXYZ", xyz(colorants[2])),
        (b"rTRC", curve), (b"gTRC", curve), (b"bTRC", curve),
    ]
    table = struct.pack(">I", len(tags))
    offset = 128 + 4 + 12 * len(tags)
    body = b""
    for sig, data in tags:
        pad = (-len(data)) % 4
        table += sig + struct.pack(">II", offset + len(body), len(data))
        body += data + b"\x00" * pad
    size = 128 + len(table) + len(body)
    header = (struct.pack(">I", size) + b"\x00\x00\x00\x00" + b"\x02\x10\x00\x00"
              + b"mntr" + b"RGB " + b"XYZ " + b"\x00" * 12 + b"acsp" + b"\x00" * 28
              + _s15(0.9642) + _s15(1.0) + _s15(0.8249))
    header = header.ljust(128, b"\x00")
    return header + table + body


def icc_profile_for(space: str) -> bytes:
    space = space.lower()
    if space == "srgb":
        return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    name, colorants, gamma = _MATRIX_SPACES[space]
    return _matrix_profile(name, colorants, gamma)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _scaled(img: Image.Image, long_edge: int | None) -> Image.Image:
    if not long_edge or max(img.size) <= long_edge:
        return img  # never upscale
    scale = long_edge / max(img.size)
    size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    return img.resize(size, Image.LANCZOS)


def _saved_adjustments(path: Path, source_size: tuple[int, int] | None = None) -> dict:
    """Saved edits in canonical coordinates (crop relative to the straightened
    preview, see geometry.py); ``source_size`` is the untransformed frame."""
    import editor  # deferred: imports Flask and the preview caches

    saved = editor.read_saved_adjustments(path, source_size=source_size)
    return dict(saved) if saved else {}


def _scaled_float(arr: np.ndarray, long_edge: int | None) -> np.ndarray:
    """Resize a float RGB array (never upscaling), channel by channel in mode F."""
    h, w = arr.shape[:2]
    if not long_edge or max(h, w) <= long_edge:
        return arr
    scale = long_edge / max(h, w)
    size = (max(1, round(w * scale)), max(1, round(h * scale)))
    planes = [np.asarray(Image.fromarray(np.ascontiguousarray(arr[:, :, c]), "F")
                         .resize(size, Image.LANCZOS)) for c in range(3)]
    return np.clip(np.stack(planes, axis=-1), 0.0, 1.0)


def _render_array(path: Path, opts: ExportOptions,
                  adjustments: dict | None = None) -> tuple[np.ndarray, bytes | None]:
    """
    Render ``path`` with its saved edits. Returns (float RGB 0..1, ICC bytes).

    ``adjustments`` overrides the saved edits; a virtual copy exports with its
    own settings this way.
    """
    import editor

    adj = dict(adjustments) if adjustments is not None else None
    space = colour.normalise_space(opts.colorspace)

    if path.suffix.lower() in RAW_SUFFIXES:
        import rawpy

        with rawpy.imread(str(path)) as raw:
            from process import baseline_temp_from_wb
            baseline = baseline_temp_from_wb(raw.camera_whitebalance, raw)[0]

            crop = None if opts.full_sensor else process.camera_crop(raw)

            def decode():
                # Linear light, 16 bit, in the output space's primaries: every
                # edit and the transfer function start from here (no 8-bit step).
                out = process.decode_linear(raw, space=space)
                if crop:
                    top, left, h, w = crop
                    out = np.ascontiguousarray(out[top:top + h, left:left + w])
                return out

            # The demosaic is the same every time for the same file and
            # parameters, so re-exports read it back from the decode cache.
            lin16, _ = decode_cache.get_or_decode(
                path, {"wb": "camera", "auto_bright": False, "bps": 16, "tone": "linear",
                       "space": space, "half": False, "crop": crop}, decode)
        src_size = (lin16.shape[1], lin16.shape[0])
        icc = icc_profile_for(space)
        # Stays linear light: the geometry and tone stages work on it directly.
        base = render_kernels.u16_to_float(np.asarray(lin16))
        linear_input = True
        del lin16
    else:
        # JPEG/TIFF/PNG/HEIC: decoded to linear light in the output space
        # (embedded profile converted, untagged = sRGB) and then edited by the
        # same stages as a raw. See imgload.py.
        if not opts.colorspace:
            space = imgload.native_space(path)     # no choice made: keep an Adobe RGB / ProPhoto source as is
        lin16 = imgload.decode_linear16(path, space)
        src_size = (lin16.shape[1], lin16.shape[0])
        baseline = imgload.BASELINE_TEMP
        icc = icc_profile_for(space)
        base = render_kernels.u16_to_float(lin16)
        linear_input = True
        del lin16

    # Resizing first keeps the heavy float passes proportional to the output.
    # Geometry (crop/straighten) is fractional, so it is unaffected. Everything
    # stays float32 until the single rounding in render()/write_png16().
    base = _scaled_float(base, opts.long_edge)

    if adj is None:
        adj = _saved_adjustments(path, src_size)
    adj.setdefault("color_temp", baseline)
    adj["_baseline_temp"] = baseline
    adj = editor.resolve_edit_version(adj, baseline)
    adj = editor.effective_adjustments(adj)
    adj["_space"] = space
    adj["_linear_input"] = linear_input
    adj = editor.lens.attach_meta(path, adj)    # lens profile needs the EXIF
    out = editor.apply_adjustments(base, baseline, adj)
    return np.clip(out, 0.0, 1.0), icc


def render(path: Path, opts: ExportOptions,
           adjustments: dict | None = None) -> tuple[Image.Image, bytes | None]:
    """Render ``path`` with its saved edits. Returns (8-bit RGB image, ICC bytes)."""
    out, icc = _render_array(path, opts, adjustments=adjustments)
    return Image.fromarray(render_kernels.float_to_u8(out)), icc


def write_png16(path: Path, arr: np.ndarray, icc: bytes | None, level: int = 1) -> None:
    """
    Write a float RGB array (0..1) as a 16-bit-per-channel PNG.

    Pillow cannot save 16-bit RGB, so the file is assembled directly:
    signature, IHDR, optional iCCP, one IDAT, IEND. Rows are quantised, byte
    swapped and filter-prefixed in strips on all cores, and zlib runs at
    ``level`` (the default 1 is several times faster than the old 6).
    """
    h, w = arr.shape[:2]
    stride = w * 6
    raw = np.empty((h, stride + 1), dtype=np.uint8)
    raw[:, 0] = 0

    def rows(a, b):
        t = np.clip(arr[a:b], 0.0, 1.0) * np.float32(65535.0)
        t += np.float32(0.5)
        samples = t.astype(">u2")
        raw[a:b, 1:] = samples.reshape(b - a, w * 3).view(np.uint8)

    fastops.for_rows(rows, h, w, rows=64)

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 16, 2, 0, 0, 0))
    if icc:
        out += chunk(b"iCCP", b"ICC Profile\x00\x00" + zlib.compress(icc))
    out += chunk(b"IDAT", zlib.compress(raw, level)) + chunk(b"IEND", b"")
    path.write_bytes(out)


def write_tiff16(path: Path, arr: np.ndarray, icc: bytes | None) -> None:
    """Uncompressed little-endian 16-bit RGB TIFF from a float 0..1 array (Pillow cannot write one)."""
    h, w = arr.shape[:2]
    data = np.clip(np.rint(arr[..., :3] * 65535.0), 0, 65535).astype("<u2").tobytes()
    icc = icc or b""
    entries = [  # tag, type, count, value (type 3 SHORT, 4 LONG, 7 UNDEFINED)
        (256, 4, 1, w), (257, 4, 1, h), (258, 3, 3, None), (259, 3, 1, 1),
        (262, 3, 1, 2), (273, 4, 1, None), (277, 3, 1, 3), (278, 4, 1, h),
        (279, 4, 1, len(data)), (284, 3, 1, 1),
    ]
    if icc:
        entries.append((34675, 7, len(icc), None))
    n = len(entries)
    ifd_at = 8
    extra_at = ifd_at + 2 + 12 * n + 4
    bps_at = extra_at
    icc_at = bps_at + 6
    data_at = icc_at + len(icc) + (len(icc) & 1)
    out = bytearray(b"II*\x00" + struct.pack("<I", ifd_at) + struct.pack("<H", n))
    for tag, typ, count, val in sorted(entries):
        if tag == 258:
            field = struct.pack("<I", bps_at)
        elif tag == 273:
            field = struct.pack("<I", data_at)
        elif tag == 34675:
            field = struct.pack("<I", icc_at)
        elif typ == 3:
            field = struct.pack("<HH", val, 0)
        else:
            field = struct.pack("<I", val)
        out += struct.pack("<HHI", tag, typ, count) + field
    out += struct.pack("<I", 0) + struct.pack("<HHH", 16, 16, 16) + icc
    out += b"\x00" * (data_at - len(out))
    path.write_bytes(bytes(out) + data)


def exiftool_available() -> bool:
    return shutil.which("exiftool") is not None


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _run_exiftool(args: list[str], what: str) -> None:
    """Write through the shared stay-open exiftool (exifpool); a plain subprocess when that is off."""
    try:
        result = exifpool.run(args, timeout=EXIFTOOL_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"exiftool did not finish within {EXIFTOOL_TIMEOUT}s and was stopped. "
                           "It may be waiting on a dialog -- try running it once by hand.") from None
    if result.returncode != 0:
        raise RuntimeError(f"exiftool failed {what}: {result.stderr.strip()}")


# Location in every container: GPS, IPTC/Photoshop place names, IPTC Extension locations.
_LOCATION_TAGS = [
    "--GPS*",   # every GPS tag in EXIF and XMP (a "--GPS:all" group exclusion does not work)
    "--IPTC:City", "--IPTC:Sub-location", "--IPTC:Province-State",
    "--IPTC:Country-PrimaryLocationCode", "--IPTC:Country-PrimaryLocationName",
    "--XMP-photoshop:City", "--XMP-photoshop:State", "--XMP-photoshop:Country",
    "--XMP-iptcCore:Location", "--XMP-iptcCore:CountryCode",
    "--XMP-iptcExt:LocationShown", "--XMP-iptcExt:LocationCreated",
]

# Authorship and rights only.
_COPYRIGHT_TAGS = [
    "-EXIF:Copyright", "-EXIF:Artist", "-IPTC:CopyrightNotice", "-IPTC:By-line",
    "-IPTC:Contact", "-XMP-dc:Rights", "-XMP-dc:Creator", "-XMP-xmpRights:all",
    "-XMP-photoshop:Credit", "-XMP-iptcCore:CreatorContactInfo",
]

# Camera raw develop settings. XMP-crs is never copied (the edits are baked in);
# this mode also drops the camera's MakerNotes, which hold its raw-processing state.
_DEVELOP_TAGS = ["--MakerNotes:all", "--XMP-aux:all", "--XMP-photomechanic:all"]


def metadata_args(mode: str) -> list[str]:
    """exiftool arguments (after -TagsFromFile SRC) that select what to copy for a metadata mode."""
    if mode == "copyright":
        return list(_COPYRIGHT_TAGS)
    args = ["-all:all", *_NOT_COPIED]
    if mode == "no_location":
        args += _LOCATION_TAGS
    elif mode == "no_develop":
        args += _DEVELOP_TAGS
    return args


def copy_metadata(source: Path, dest: Path, size: tuple[int, int] | None = None,
                  extra: list[str] | None = None, mode: str = "all") -> None:
    """
    Copy every tag from ``source`` (and its sidecar, if any) onto ``dest``.

    Group-qualified, so EXIF (including GPS and MakerNotes), IPTC and XMP all
    travel; the output keeps the ICC profile it was written with. ``size`` is
    the output's pixel size (read from the file when omitted); ``extra`` are
    further assignments applied after the copy in the same exiftool call.
    """
    cmd = ["-TagsFromFile", str(source), *metadata_args(mode)]
    sidecar = source.with_suffix(".xmp")
    if mode == "copyright" and source.suffix.lower() != ".xmp" and sidecar.exists():
        # Rights typed in the metadata panel live in the sidecar; they win over the file's own.
        cmd += ["-TagsFromFile", str(sidecar), *_COPYRIGHT_TAGS]
    elif source.suffix.lower() != ".xmp" and sidecar.exists():
        # Rating, keywords and anything else kept in the sidecar. Develop
        # settings are excluded above.
        cmd += ["-TagsFromFile", str(sidecar), "-XMP:all", "--XMP-crs:all"]
        if mode == "no_location":
            cmd += [t for t in _LOCATION_TAGS if "XMP" in t]
        elif mode == "no_develop":
            cmd += [t for t in _DEVELOP_TAGS if "XMP" in t]
    # Those two are deliberately not copied (the source's size is wrong for the
    # output), but a JPEG's Exif block must declare its own pixel size: exiftool
    # warns "Missing required JPEG ExifIFD tag" and strict readers reject it.
    if size is None:
        try:
            with Image.open(dest) as out:
                size = out.size
        except OSError:
            size = None
    if size is not None:
        cmd += [f"-ExifIFD:ExifImageWidth={size[0]}", f"-ExifIFD:ExifImageHeight={size[1]}"]
    cmd += ["-Orientation#=1", *(extra or []), "-overwrite_original", str(dest)]
    _run_exiftool(cmd, "copying metadata")


_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_PNG_OWN = (b"IHDR", b"IDAT", b"IEND", b"iCCP", b"sRGB", b"gAMA", b"cHRM")   # the real file keeps its own


def _png_chunks(data: bytes):
    pos = len(_PNG_SIG)
    while pos + 8 <= len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        yield kind, data[pos:pos + 12 + n]
        pos += 12 + n


def splice_png_metadata(dest: Path, donor: Path) -> None:
    """
    Move the metadata chunks of ``donor`` (a tiny PNG exiftool wrote the tags to)
    into ``dest`` just before its pixel data.

    exiftool rewrites a whole PNG to add tags -- seconds of Perl for a 10 MP
    file -- but what it adds does not depend on the pixels, so it is done on a
    1x1 stand-in and the chunks are copied across.
    """
    extra = b"".join(chunk for kind, chunk in _png_chunks(donor.read_bytes()) if kind not in _PNG_OWN)
    data = dest.read_bytes()
    at = next(offset for offset, kind in _chunk_offsets(data) if kind == b"IDAT")
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(data[:at] + extra + data[at:])
    os.replace(tmp, dest)


def _chunk_offsets(data: bytes):
    pos = len(_PNG_SIG)
    while pos + 8 <= len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        yield pos, kind
        pos += 12 + n


def copy_metadata_png(source: Path, dest: Path, size: tuple[int, int], icc: bytes | None,
                      extra: list[str] | None = None, mode: str = "all") -> None:
    """``copy_metadata`` for a PNG without making exiftool rewrite the full-size file."""
    donor = dest.with_name(dest.name + ".meta.png")
    try:
        Image.new("RGB", (1, 1)).save(donor, "PNG", icc_profile=icc)
        copy_metadata(source, donor, size=size, extra=extra, mode=mode)
        splice_png_metadata(dest, donor)
    finally:
        donor.unlink(missing_ok=True)


def _virtual_args(virtual: dict) -> list[str]:
    """The copy's own rating, colour label and keywords, as exiftool assignments."""
    import library

    args = [f"-XMP-xmp:Rating={max(0, int(virtual.get('rating', 0)))}"]
    return args + library.xmp_args(label=virtual.get("label", ""),
                                   keywords=virtual.get("keywords", []))


def _apply_virtual_metadata(dest: Path, virtual: dict) -> None:
    """Put the copy's own rating, colour label and keywords on the exported file."""
    _run_exiftool([*_virtual_args(virtual), "-overwrite_original", str(dest)],
                  "writing copy metadata")


# ---------------------------------------------------------------------------
# Filenames and the export itself
# ---------------------------------------------------------------------------

def _shoot_date(path: Path) -> str:
    raw = process.get_shoot_date(path)
    return raw.replace("/", "") if raw else "undated"


_NUM_SPEC = re.compile(r"^0?\d{1,2}d?$")
_STR_SPEC = re.compile(r"^[<>^]?\d{0,2}s?$")
_TEMPLATE_FIELDS = {"name": _STR_SPEC, "date": _STR_SPEC,
                    "seq": _NUM_SPEC, "width": _NUM_SPEC, "height": _NUM_SPEC}


def _check_template(template: str) -> None:
    """
    Allow only the documented fields with small width specs. str.format would
    otherwise build a string of any requested width (``{seq:150000000d}``)
    before our length check ever runs, and allows attribute/index access.
    """
    if len(template) > 200:
        raise ValueError("filename template is too long")
    try:
        parts = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise ValueError(f"bad filename template {template!r}: {exc}") from None
    for _lit, field, spec, conv in parts:
        if field is None:
            continue
        if field not in _TEMPLATE_FIELDS or conv:
            raise ValueError(
                f"bad filename template {template!r}: unknown field {{{field}}} "
                "(use name, date, seq, width, height)")
        if spec and not _TEMPLATE_FIELDS[field].match(spec):
            raise ValueError(f"bad filename template {template!r}: unsupported format {spec!r}")


def output_name(source: Path, template: str, ext: str, seq: int,
                size: tuple[int, int], stem: str | None = None) -> str:
    """
    Expand a filename template. Tokens: {name} {date} {seq} {width} {height}
    ({seq:04d} pads). Path separators are rejected so a template cannot
    write outside the output folder. ``stem`` replaces the source's file stem
    for {name} (a virtual copy passes ``IMG_0001-copy2``).
    """
    _check_template(template)
    needs_date = "{date" in template
    try:
        stem = template.format(
            name=stem or source.stem, seq=seq, width=size[0], height=size[1],
            date=_shoot_date(source) if needs_date else "",
        )
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(f"bad filename template {template!r}: {exc}") from None
    if not stem or re.search(r"[/\\\x00]", stem) or stem in (".", ".."):
        raise ValueError(f"filename template {template!r} produced an unusable name")
    return stem + ext


def virtual_stem(source: Path, virtual: dict) -> str:
    """Export name for a virtual copy: IMG_0001-copy2, so it never clashes with the original."""
    return f"{source.stem}-copy{virtual['copy_no']}"


def claim_output(out_dir: Path, name: str, overwrite: bool = False) -> Path:
    """
    Reserve an output file name atomically (O_EXCL), so an existing file, a
    planted symlink or a racing export can never be written through.
    Without ``overwrite`` a taken name gets -1, -2, ... With it, an existing
    regular file is replaced; a symlink never is.
    """
    stem, ext = Path(name).stem, Path(name).suffix
    n = 0
    while True:
        cand = out_dir / (name if n == 0 else f"{stem}-{n}{ext}")
        if overwrite and cand.is_file() and not cand.is_symlink():
            return cand
        try:
            fd = os.open(cand, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
        except FileExistsError:
            n += 1
            continue
        os.close(fd)
        return cand


def export_photo(source: Path, out_dir: Path, opts: ExportOptions, seq: int = 1,
                 virtual: dict | None = None, overwrite: bool = False) -> Path:
    """
    Export one photo. Returns the written path; never overwrites an existing file.

    With ``virtual`` (a catalog entry) the raw is rendered with the copy's own
    adjustments and the output is named ``<stem>-copy<N>``. Metadata still comes
    from the raw, with the copy's rating, colour label and keywords on top.
    """
    opts = opts.validate()
    adjustments = virtual["adjustments"] if virtual else None
    arr, icc = _render_array(source, opts, adjustments=adjustments)
    if opts.sharpen or opts.watermark:
        # After the resize, at export only: sharpen for the medium, then mark.
        arr = export_finish.finish(arr, opts.sharpen, opts.watermark)
    size = (arr.shape[1], arr.shape[0])
    img = None if opts.bit_depth == 16 else Image.fromarray(render_kernels.float_to_u8(arr))

    out_dir.mkdir(parents=True, exist_ok=True)
    ext = FORMATS[opts.format]
    name = output_name(source, opts.template, ext, seq, size,
                       stem=virtual_stem(source, virtual) if virtual else None)
    dest = claim_output(out_dir, name, overwrite)

    try:
        if img is None and opts.format == "tiff":
            write_tiff16(dest, arr, icc)
        elif img is None:
            write_png16(dest, arr, icc, level=int(opts.png_compression))
        elif opts.format == "tiff":
            img.save(dest, "TIFF", compression="tiff_lzw", icc_profile=icc)
        elif opts.format == "webp":
            img.save(dest, "WEBP", quality=int(opts.quality), method=4, icc_profile=icc)
        elif opts.format == "jpeg":
            img.save(dest, "JPEG", quality=int(opts.quality), subsampling=0 if opts.quality >= 90 else 2,
                     optimize=bool(opts.optimize_jpeg), icc_profile=icc)
        else:
            img.save(dest, "PNG", icc_profile=icc, compress_level=int(opts.png_compression))
    except BaseException:
        dest.unlink(missing_ok=True)
        raise

    if opts.keep_metadata:
        try:
            extra = _virtual_args(virtual) if virtual and opts.metadata != "copyright" else None
            if opts.format == "png" and os.environ.get("PHOTO_ROOM_PNG_SPLICE", "1") != "0":
                copy_metadata_png(source, dest, size, icc, extra, mode=opts.metadata)
            else:
                copy_metadata(source, dest, size=size, extra=extra, mode=opts.metadata)
        except Exception:
            dest.unlink(missing_ok=True)  # never leave a stripped file behind
            raise
    return dest
