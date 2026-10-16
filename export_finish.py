"""
Output finishing for export: sharpening for the output medium, and watermarks.

Both run on the float RGB array (0..1) after the resize and are applied at
export only -- nothing here touches the editor preview, the saved edits or the
raw. Order is resize -> sharpen -> watermark, so the watermark is never
sharpened.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

MEDIA = ("screen", "matte", "glossy")
AMOUNTS = ("low", "standard", "high")

# (radius in pixels for a 3000 px long edge, strength multiplier) per medium.
# Matte paper absorbs ink and needs a wider, stronger halo; glossy keeps
# contrast and needs less; screen needs only a touch after downsizing.
_MEDIUM = {"screen": (0.6, 1.0), "matte": (1.4, 1.2), "glossy": (1.0, 0.9)}
_AMOUNT = {"low": 0.35, "standard": 0.7, "high": 1.1}

POSITIONS = ("tl", "tc", "tr", "ml", "c", "mr", "bl", "bc", "br")
DEFAULT_WATERMARK = {
    "type": "text", "text": "", "font": "", "size": 3.0, "opacity": 50,
    "position": "br", "margin": 2.0, "color": "#ffffff",
    "image": "", "scale": 15.0,
}


# ---------------------------------------------------------------------------
# Sharpening
# ---------------------------------------------------------------------------

def normalize_sharpen(spec) -> dict | None:
    """Validate {"for": medium, "amount": level}; None or an empty value means off."""
    if not spec:
        return None
    if not isinstance(spec, dict):
        raise ValueError("sharpening must be an object")
    medium = str(spec.get("for") or "").lower()
    if not medium or medium == "none":
        return None
    amount = str(spec.get("amount") or "standard").lower()
    if medium not in MEDIA:
        raise ValueError(f"sharpening medium must be one of {', '.join(MEDIA)}")
    if amount not in AMOUNTS:
        raise ValueError(f"sharpening amount must be one of {', '.join(AMOUNTS)}")
    return {"for": medium, "amount": amount}


def _gaussian_kernel(sigma: float) -> np.ndarray:
    r = max(1, int(np.ceil(sigma * 3)))
    x = np.arange(-r, r + 1, dtype=np.float32)
    k = np.exp(-(x * x) / (2 * sigma * sigma))
    return k / k.sum()


def _blur(arr: np.ndarray, sigma: float) -> np.ndarray:
    k = _gaussian_kernel(sigma)
    r = len(k) // 2
    out = arr
    for axis in (0, 1):
        pad = [(0, 0)] * out.ndim
        pad[axis] = (r, r)
        p = np.pad(out, pad, mode="edge")
        acc = np.zeros_like(out)
        n = out.shape[axis]
        for i, w in enumerate(k):
            sl = [slice(None)] * out.ndim
            sl[axis] = slice(i, i + n)
            acc += w * p[tuple(sl)]
        out = acc
    return out


def sharpen(arr: np.ndarray, spec: dict | None) -> np.ndarray:
    """Unsharp mask on luminance only (no colour fringes), scaled to the image size."""
    spec = normalize_sharpen(spec)
    if spec is None:
        return arr
    radius, mult = _MEDIUM[spec["for"]]
    sigma = max(0.4, radius * max(arr.shape[:2]) / 3000.0)
    amount = _AMOUNT[spec["amount"]] * mult
    a = np.asarray(arr, dtype=np.float32)
    luma = a @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    detail = luma - _blur(luma, sigma)
    return np.clip(a + amount * detail[..., None], 0.0, 1.0)


# ---------------------------------------------------------------------------
# Watermark
# ---------------------------------------------------------------------------

def normalize_watermark(spec) -> dict | None:
    """Fill defaults and validate; None or an empty watermark means off."""
    if not spec:
        return None
    if not isinstance(spec, dict):
        raise ValueError("watermark must be an object")
    wm = {**DEFAULT_WATERMARK, **{k: v for k, v in spec.items() if k in DEFAULT_WATERMARK}}
    kind = str(wm["type"]).lower()
    if kind in ("", "none"):
        return None
    if kind not in ("text", "image"):
        raise ValueError("watermark type must be text or image")
    wm["type"] = kind
    try:
        for key, lo, hi in (("size", 0.5, 50), ("opacity", 0, 100), ("margin", 0, 40), ("scale", 1, 100)):
            wm[key] = float(wm[key])
            if not lo <= wm[key] <= hi:
                raise ValueError(f"watermark {key} must be between {lo:g} and {hi:g}")
    except (TypeError, ValueError) as exc:
        raise ValueError(str(exc) if "watermark" in str(exc) else "watermark numbers are invalid") from None
    if wm["position"] not in POSITIONS:
        raise ValueError(f"watermark position must be one of {', '.join(POSITIONS)}")
    wm["text"], wm["font"], wm["image"] = str(wm["text"]), str(wm["font"] or ""), str(wm["image"] or "")
    try:
        wm["color"] = _parse_color(wm["color"])
    except ValueError:
        raise ValueError("watermark colour must be like #rrggbb") from None
    wm["color"] = "#%02x%02x%02x" % wm["color"]
    if kind == "text":
        if not wm["text"].strip():
            raise ValueError("watermark text is empty")
        if len(wm["text"]) > 200:
            raise ValueError("watermark text is too long")
    else:
        if not wm["image"]:
            raise ValueError("choose a watermark image")
        p = Path(wm["image"]).expanduser()
        if not p.is_file():
            raise ValueError(f"watermark image not found: {wm['image']}")
        try:
            with Image.open(p) as im:
                im.verify()
        except Exception:
            raise ValueError(f"watermark image is not a readable image: {wm['image']}") from None
    return wm


def _parse_color(value) -> tuple[int, int, int]:
    s = str(value).strip().lstrip("#")
    if len(s) != 6:
        raise ValueError(value)
    try:
        return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        raise ValueError(value) from None


_FONT_DIRS = ("/usr/share/fonts", "/usr/local/share/fonts", "/Library/Fonts",
              "/System/Library/Fonts", str(Path.home() / ".fonts"),
              str(Path.home() / ".local/share/fonts"))


def load_font(name: str, px: int):
    """A TrueType font by path or family/file name, falling back to the bundled default."""
    px = max(8, int(px))
    candidates = [name] if name else []
    if name and not Path(name).is_absolute():
        stem = name.lower().replace(" ", "")
        for d in _FONT_DIRS:
            base = Path(d)
            if not base.is_dir():
                continue
            for f in base.rglob("*"):
                if f.suffix.lower() in (".ttf", ".otf") and stem in f.stem.lower().replace(" ", "").replace("-", ""):
                    candidates.insert(0, str(f))
                    break
    for c in candidates + ["DejaVuSans.ttf"]:
        try:
            return ImageFont.truetype(c, px)
        except OSError:
            continue
    return ImageFont.load_default(size=px)


def list_fonts() -> list[str]:
    """Font family file stems found on this machine, for the dialog's font list."""
    seen = set()
    for d in _FONT_DIRS:
        base = Path(d)
        if base.is_dir():
            for f in base.rglob("*"):
                if f.suffix.lower() in (".ttf", ".otf"):
                    seen.add(f.stem)
    return sorted(seen)[:300]


def _place(pos: str, canvas: tuple[int, int], size: tuple[int, int], margin: int) -> tuple[int, int]:
    cw, ch = canvas
    w, h = size
    row, col = ("m", "c") if pos == "c" else (pos[0], pos[1] if pos[1] != "c" else "c")
    if pos == "c":
        row, col = "m", "c"
    col = {"l": 0, "c": 1, "r": 2}[col]
    row = {"t": 0, "m": 1, "b": 2}[row]
    xs = (margin, (cw - w) // 2, cw - w - margin)
    ys = (margin, (ch - h) // 2, ch - h - margin)
    return xs[col], ys[row]


def watermark_layer(size: tuple[int, int], wm: dict) -> Image.Image:
    """The watermark as an RGBA layer the size of the picture, already positioned."""
    w, h = size
    long_edge = max(w, h)
    margin = int(round(long_edge * wm["margin"] / 100.0))
    if wm["type"] == "text":
        font = load_font(wm["font"], long_edge * wm["size"] / 100.0)
        probe = ImageDraw.Draw(Image.new("L", (1, 1)))
        box = probe.multiline_textbbox((0, 0), wm["text"], font=font)
        tw, th = box[2] - box[0], box[3] - box[1]
        mark = Image.new("RGBA", (tw + 2, th + 2), (0, 0, 0, 0))
        ImageDraw.Draw(mark).multiline_text((1 - box[0], 1 - box[1]), wm["text"], font=font,
                                            fill=_parse_color(wm["color"]) + (255,))
    else:
        with Image.open(Path(wm["image"]).expanduser()) as im:
            mark = im.convert("RGBA")
        target = max(1, int(round(long_edge * wm["scale"] / 100.0)))
        k = target / max(mark.size)
        mark = mark.resize((max(1, round(mark.width * k)), max(1, round(mark.height * k))), Image.LANCZOS)
    alpha = mark.getchannel("A").point(lambda v: int(v * wm["opacity"] / 100.0))
    mark.putalpha(alpha)
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    x, y = _place(wm["position"], size, mark.size, margin)
    if x < 0 or y < 0:  # larger than the picture: keep the part that fits
        mark = mark.crop((max(0, -x), max(0, -y), mark.width, mark.height))
        x, y = max(0, x), max(0, y)
    layer.alpha_composite(mark.crop((0, 0, w - x, h - y)), (x, y))
    return layer


def apply_watermark(arr: np.ndarray, spec: dict | None) -> np.ndarray:
    wm = normalize_watermark(spec)
    if wm is None:
        return arr
    h, w = arr.shape[:2]
    layer = np.asarray(watermark_layer((w, h), wm), dtype=np.float32) / 255.0
    a = layer[..., 3:4]
    return np.clip(arr * (1.0 - a) + layer[..., :3] * a, 0.0, 1.0)


def finish(arr: np.ndarray, sharpen_spec, watermark_spec) -> np.ndarray:
    return apply_watermark(sharpen(arr, sharpen_spec), watermark_spec)
