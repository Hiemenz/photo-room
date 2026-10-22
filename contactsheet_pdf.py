"""
Contact-sheet PDF: a grid of thumbnails with file names, one or more pages.

Built from the cached grid thumbnails, so a sheet of a whole shoot takes
seconds and never decodes a raw. Pillow writes the PDF; the pages are
rasterised at 150 dpi, which prints sharply for thumbnail-sized cells.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw

import export_finish

DPI = 150
# Portrait page sizes in millimetres.
PAGES = {"a4": (210, 297), "letter": (215.9, 279.4)}
MAX_PHOTOS = 400
MAX_COLUMNS = 10


def _px(mm: float) -> int:
    return int(round(mm / 25.4 * DPI))


def _check(columns: int, page: str, landscape: bool) -> tuple[int, int, int]:
    if page not in PAGES:
        raise ValueError(f"page must be one of {', '.join(PAGES)}")
    if not 1 <= int(columns) <= MAX_COLUMNS:
        raise ValueError(f"columns must be between 1 and {MAX_COLUMNS}")
    w, h = (_px(v) for v in PAGES[page])
    if landscape:
        w, h = h, w
    return w, h, int(columns)


def build_pages(paths: list[Path], thumb_for: Callable[[Path], bytes], *, columns: int = 4,
                page: str = "a4", landscape: bool = False, title: str = "",
                labels: bool = True) -> list[Image.Image]:
    """One RGB image per page; ``thumb_for(path)`` returns encoded thumbnail bytes."""
    if not paths:
        raise ValueError("No photos for the contact sheet")
    if len(paths) > MAX_PHOTOS:
        raise ValueError(f"A contact sheet holds at most {MAX_PHOTOS} photos")
    pw, ph, cols = _check(columns, page, landscape)
    margin = _px(12)
    gap = _px(4)
    label_h = _px(6) if labels else 0
    head_h = _px(12) if title else 0
    cell_w = (pw - 2 * margin - gap * (cols - 1)) // cols
    cell_h = cell_w * 2 // 3 + label_h + gap          # a 3:2 frame plus its caption
    rows = max(1, (ph - 2 * margin - head_h + gap) // (cell_h + gap))
    per_page = cols * rows
    font = export_finish.load_font("", max(10, label_h * 2 // 3)) if labels else None
    head_font = export_finish.load_font("", max(14, head_h * 2 // 3)) if title else None
    total = (len(paths) + per_page - 1) // per_page

    pages = []
    for p in range(total):
        sheet = Image.new("RGB", (pw, ph), "white")
        draw = ImageDraw.Draw(sheet)
        if title:
            draw.text((margin, margin), title[:120] + (f"  ({p + 1}/{total})" if total > 1 else ""),
                      fill="black", font=head_font)
        for i, path in enumerate(paths[p * per_page:(p + 1) * per_page]):
            r, c = divmod(i, cols)
            x = margin + c * (cell_w + gap)
            y = margin + head_h + r * (cell_h + gap)
            try:
                with Image.open(io.BytesIO(thumb_for(path))) as im:
                    im = im.convert("RGB")
                    im.thumbnail((cell_w, cell_h - label_h - gap))
                    sheet.paste(im, (x + (cell_w - im.width) // 2, y + (cell_h - label_h - gap - im.height) // 2))
            except Exception:
                draw.rectangle((x, y, x + cell_w - 1, y + cell_h - label_h - gap - 1), outline="#999999")
            if labels:
                name = path.name
                while len(name) > 4 and draw.textlength(name, font=font) > cell_w:
                    name = name[:-2]
                draw.text((x, y + cell_h - label_h), name, fill="#333333", font=font)
        pages.append(sheet)
    return pages


def write_pdf(out: Path, paths: list[Path], thumb_for: Callable[[Path], bytes], **options) -> Path:
    """Write the sheet to ``out`` (a ``.pdf``; an existing file is replaced). Returns the path."""
    pages = build_pages(paths, thumb_for, **options)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(out, "PDF", resolution=float(DPI), save_all=True, append_images=pages[1:])
    return out
