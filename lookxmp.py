"""
Film style (LUT) and preset provenance in the XMP sidecar.

Camera Raw has one place for "the look this photo uses": the ``crs:Look`` block
(Name, Amount, UUID, Group). photo-room fills it so the sidecar says, in terms any
XMP reader can show, which film style or preset is on the photo and how strongly:

* ``LookName``   -- the LUT's name (or, with no LUT, the preset's name)
* ``LookAmount`` -- its intensity, 0..1
* ``LookGroup``  -- ``photo-room``
* ``LookUUID``   -- ``photo-room:v1?lut=<id>&lutamt=<0-100>&preset=<name>&presetamt=<0-100>``,
  the machine-readable record that is read back on open

The slider values the preset set are ordinary develop settings and round-trip like any
others; the LUT itself is *not* embedded (a sidecar stays small and Lightroom does not
read ``.cube`` files), so the sidecar names it by content id and a machine without that
LUT in its library shows a warning and imports it again from the original ``.cube``.
Exports bake the LUT into the pixels, and develop settings are deliberately not copied
into exported files (``exporter.copy_metadata``), so nothing is applied twice.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, quote, urlencode

import lut as lutmod

PREFIX = "photo-room:v1?"
GROUP = "photo-room"
DEFAULTS = {"lut": "", "lut_amount": 100, "preset_name": "", "preset_amount": 100}
READ_TAG = "LookUUID"
_LUT_ID = re.compile(r"builtin-[a-z0-9-]{1,60}|[0-9a-f]{12}")


def _amount(v, default=100.0) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return min(100.0, max(0.0, v)) if v == v else default


def complete(adj: dict) -> dict:
    """The look keys of ``adj`` with defaults filled in and values made safe."""
    lut_id = adj.get("lut") or ""
    return {
        "lut": lut_id if isinstance(lut_id, str) else "",
        "lut_amount": _amount(adj.get("lut_amount", 100)),
        "preset_name": str(adj.get("preset_name") or "")[:60],
        "preset_amount": _amount(adj.get("preset_amount", 100)),
    }


def encode(adj: dict) -> str | None:
    c = complete(adj)
    if not c["lut"] and not c["preset_name"]:
        return None
    parts = []
    if c["lut"]:
        parts += [("lut", c["lut"]), ("lutamt", f"{c['lut_amount']:g}")]
    if c["preset_name"]:
        parts += [("preset", c["preset_name"]), ("presetamt", f"{c['preset_amount']:g}")]
    return PREFIX + urlencode(parts, quote_via=quote)


def xmp_arguments(adj: dict) -> list[str]:
    """exiftool assignments; with no look they clear the block so reverting removes it."""
    c = complete(adj)
    enc = encode(adj)
    if enc is None:
        return ["-XMP-crs:LookName=", "-XMP-crs:LookAmount=", "-XMP-crs:LookGroup=", "-XMP-crs:LookUUID="]
    if c["lut"]:
        try:
            name = lutmod.describe(c["lut"])["name"]
        except Exception:
            name = c["lut"]
        amount = c["lut_amount"]
    else:
        name, amount = c["preset_name"], c["preset_amount"]
    return [f"-XMP-crs:LookName={name}", f"-XMP-crs:LookAmount={amount / 100.0:.3f}",
            f"-XMP-crs:LookGroup={GROUP}", f"-XMP-crs:LookUUID={enc}"]


def read_tags() -> list[str]:
    return [f"XMP-crs:{READ_TAG}"]


def parse(uuid: str | None) -> dict:
    """Look keys recorded by ``encode`` (empty for anything else, e.g. a Lightroom look)."""
    uuid = (uuid or "").strip()
    if not uuid.startswith(PREFIX):
        return {}
    fields = dict(parse_qsl(uuid[len(PREFIX):], keep_blank_values=False))
    out: dict = {}
    if fields.get("lut") and _LUT_ID.fullmatch(fields["lut"]):
        out["lut"] = fields["lut"]
        out["lut_amount"] = _amount(fields.get("lutamt"))
    if fields.get("preset"):
        out["preset_name"] = fields["preset"][:60]
        out["preset_amount"] = _amount(fields.get("presetamt"))
    return out
