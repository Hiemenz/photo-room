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
import re
import shutil
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, ImageOps

import export_finish
import process
from process import EXIFTOOL_TIMEOUT, RAW_SUFFIXES, run_tool

FORMATS = {"png": ".png", "jpeg": ".jpg", "jpg": ".jpg"}
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
    colorspace: str | None = None  # None: raw -> srgb, other images -> keep source
    template: str = "{name}"
    keep_metadata: bool = True
    bit_depth: int = 8  # 16 is PNG only
    sharpen: dict | None = None     # {"for": screen|matte|glossy, "amount": low|standard|high}
    watermark: dict | None = None   # see export_finish.DEFAULT_WATERMARK

    def validate(self) -> "ExportOptions":
        fmt = self.format.lower()
        if fmt not in FORMATS:
            raise ValueError(f"format must be png or jpeg, not {self.format!r}")
        self.format = "jpeg" if fmt == "jpg" else fmt
        if not 1 <= int(self.quality) <= 100:
            raise ValueError("quality must be between 1 and 100")
        if int(self.bit_depth) not in (8, 16):
            raise ValueError("bit depth must be 8 or 16")
        self.bit_depth = int(self.bit_depth)
        if self.bit_depth == 16 and self.format != "png":
            raise ValueError("16-bit output is only available for PNG")
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


def _saved_adjustments(path: Path) -> dict:
    import editor  # deferred: imports Flask and the preview caches

    saved = editor.read_saved_adjustments(path)
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

    adj = dict(adjustments) if adjustments is not None else _saved_adjustments(path)
    deep = opts.bit_depth == 16

    if path.suffix.lower() in RAW_SUFFIXES:
        import rawpy

        space = opts.colorspace or "srgb"
        color = {"srgb": rawpy.ColorSpace.sRGB, "adobergb": rawpy.ColorSpace.Adobe,
                 "prophoto": rawpy.ColorSpace.ProPhoto}[space]
        with rawpy.imread(str(path)) as raw:
            cam_mul = np.array(raw.camera_whitebalance[:4], dtype=float)
            cam_mul /= cam_mul[1]
            rb = cam_mul[0] / cam_mul[2] if cam_mul[2] > 0 else 1.0
            baseline = int(np.clip(5500 / rb, 2500, 10000))
            rgb = raw.postprocess(use_camera_wb=True, no_auto_bright=True,
                                  output_bps=16 if deep else 8, output_color=color)
        icc = icc_profile_for(space)
        if deep:
            base = np.asarray(rgb, dtype=np.float32) / 65535.0
        else:
            img = Image.fromarray(rgb)
    else:
        with Image.open(path) as src:
            src = ImageOps.exif_transpose(src)
            source_icc = src.info.get("icc_profile")
            img = src.convert("RGB")
        baseline = 5500
        if opts.colorspace and source_icc is None and opts.colorspace != "srgb":
            raise ValueError("source has no colour profile to convert from")
        if opts.colorspace:
            icc = icc_profile_for(opts.colorspace)
            if source_icc:
                img = ImageCms.profileToProfile(
                    img, ImageCms.ImageCmsProfile(io.BytesIO(source_icc)),
                    ImageCms.ImageCmsProfile(io.BytesIO(icc)), outputMode="RGB")
        else:
            icc = source_icc or icc_profile_for("srgb")
        if deep:
            base = np.asarray(img, dtype=np.float32) / 255.0

    # Resizing first keeps the heavy float passes proportional to the output.
    # Geometry (crop/straighten) is fractional, so it is unaffected.
    if deep:
        base = _scaled_float(base, opts.long_edge)
    else:
        base = np.asarray(_scaled(img, opts.long_edge), dtype=np.float32) / 255.0

    adj.setdefault("color_temp", baseline)
    adj["_baseline_temp"] = baseline
    adj = editor.effective_adjustments(adj)
    adj = editor.lens.attach_meta(path, adj)    # lens profile needs the EXIF
    out = editor.apply_adjustments(base, baseline, adj)
    return np.clip(out, 0.0, 1.0), icc


def render(path: Path, opts: ExportOptions,
           adjustments: dict | None = None) -> tuple[Image.Image, bytes | None]:
    """Render ``path`` with its saved edits. Returns (8-bit RGB image, ICC bytes)."""
    out, icc = _render_array(path, opts, adjustments=adjustments)
    return Image.fromarray((out * 255.0 + 0.5).astype(np.uint8)), icc


def write_png16(path: Path, arr: np.ndarray, icc: bytes | None) -> None:
    """
    Write a float RGB array (0..1) as a 16-bit-per-channel PNG.

    Pillow cannot save 16-bit RGB, so the file is assembled directly:
    signature, IHDR, optional iCCP, one IDAT, IEND.
    """
    h, w = arr.shape[:2]
    samples = (np.clip(arr, 0.0, 1.0) * 65535.0 + 0.5).astype(">u2")
    rows = samples.reshape(h, w * 3).tobytes()
    stride = w * 6
    raw = b"".join(b"\x00" + rows[i * stride:(i + 1) * stride] for i in range(h))

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 16, 2, 0, 0, 0))
    if icc:
        out += chunk(b"iCCP", b"ICC Profile\x00\x00" + zlib.compress(icc))
    out += chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
    path.write_bytes(out)


def exiftool_available() -> bool:
    return shutil.which("exiftool") is not None


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def copy_metadata(source: Path, dest: Path) -> None:
    """
    Copy every tag from ``source`` (and its sidecar, if any) onto ``dest``.

    Group-qualified, so EXIF (including GPS and MakerNotes), IPTC and XMP all
    travel; the output keeps the ICC profile it was written with.
    """
    cmd = ["exiftool", "-TagsFromFile", str(source), "-all:all", *_NOT_COPIED]
    sidecar = source.with_suffix(".xmp")
    if source.suffix.lower() != ".xmp" and sidecar.exists():
        # Rating, keywords and anything else kept in the sidecar. Develop
        # settings are excluded above.
        cmd += ["-TagsFromFile", str(sidecar), "-XMP:all", "--XMP-crs:all"]
    # Those two are deliberately not copied (the source's size is wrong for the
    # output), but a JPEG's Exif block must declare its own pixel size: exiftool
    # warns "Missing required JPEG ExifIFD tag" and strict readers reject it.
    try:
        with Image.open(dest) as out:
            width, height = out.size
        cmd += [f"-ExifIFD:ExifImageWidth={width}", f"-ExifIFD:ExifImageHeight={height}"]
    except OSError:
        pass
    cmd += ["-Orientation#=1", "-overwrite_original", str(dest)]
    result = run_tool(cmd, timeout=EXIFTOOL_TIMEOUT, what="exiftool",
                      capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"exiftool failed copying metadata: {result.stderr.strip()}")


def _apply_virtual_metadata(dest: Path, virtual: dict) -> None:
    """Put the copy's own rating, colour label and keywords on the exported file."""
    import library

    args = [f"-XMP-xmp:Rating={max(0, int(virtual.get('rating', 0)))}"]
    args += library.xmp_args(label=virtual.get("label", ""),
                             keywords=virtual.get("keywords", []))
    result = run_tool(["exiftool", *args, "-overwrite_original", str(dest)],
                      timeout=EXIFTOOL_TIMEOUT, what="exiftool",
                      capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"exiftool failed writing copy metadata: {result.stderr.strip()}")


# ---------------------------------------------------------------------------
# Filenames and the export itself
# ---------------------------------------------------------------------------

def _shoot_date(path: Path) -> str:
    raw = process.get_shoot_date(path)
    return raw.replace("/", "") if raw else "undated"


def output_name(source: Path, template: str, ext: str, seq: int,
                size: tuple[int, int], stem: str | None = None) -> str:
    """
    Expand a filename template. Tokens: {name} {date} {seq} {width} {height}
    ({seq:04d} pads). Path separators are rejected so a template cannot
    write outside the output folder. ``stem`` replaces the source's file stem
    for {name} (a virtual copy passes ``IMG_0001-copy2``).
    """
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


def export_photo(source: Path, out_dir: Path, opts: ExportOptions, seq: int = 1,
                 virtual: dict | None = None) -> Path:
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
    img = None if opts.bit_depth == 16 else Image.fromarray((arr * 255.0 + 0.5).astype(np.uint8))

    out_dir.mkdir(parents=True, exist_ok=True)
    ext = FORMATS[opts.format]
    name = output_name(source, opts.template, ext, seq, size,
                       stem=virtual_stem(source, virtual) if virtual else None)
    dest = out_dir / name
    n = 1
    while dest.exists():
        dest = out_dir / f"{Path(name).stem}-{n}{ext}"
        n += 1

    if img is None:
        write_png16(dest, arr, icc)
    elif opts.format == "jpeg":
        img.save(dest, "JPEG", quality=int(opts.quality), subsampling=0 if opts.quality >= 90 else 2,
                 optimize=True, icc_profile=icc)
    else:
        img.save(dest, "PNG", icc_profile=icc)

    if opts.keep_metadata:
        try:
            copy_metadata(source, dest)
            if virtual:
                _apply_virtual_metadata(dest, virtual)
        except Exception:
            dest.unlink(missing_ok=True)  # never leave a stripped file behind
            raise
    return dest
