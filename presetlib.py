"""
Preset filters: built-in looks, user presets, applying them at an amount, and files.

A preset is a *sparse* set of the editor's own adjustment keys (``values``), grouped
for saving and display. Nothing else is involved: applying one just writes slider
values, a tone curve, grading wheels or a LUT reference into the edit, which is why a
preset is always editable afterwards, exports like any other edit and round-trips in
the XMP sidecar with no extra machinery.

* ``apply_preset(base, preset, amount, baseline_temp)`` moves every key the preset
  names from its current value toward the preset's by ``amount`` (0 = unchanged,
  1 = the preset's value). It is a pure function, so a thumbnail, a hover preview and
  a batch apply all produce the same numbers.
* ``from_adjustments`` builds a user preset from an edit and the chosen groups. Crop
  and white balance are off by default: they belong to one photo and one light.
* White balance travels as a ratio to the photo's own camera setting
  (``temp_ratio``); built-ins use the relative ``temp_shift`` ("warmer by 10%").
* Files: ``export_json`` / ``import_file`` for our own format and a reader for
  Lightroom/Camera Raw ``.xmp`` presets (the settings that map onto our controls;
  everything else is reported, never guessed).

Storage is one small JSON file per user preset in ``~/.photo-room/presets``
(``PHOTO_ROOM_PRESET_DIR`` redirects it).
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

import colormix
import tonecurve

FORMAT = "photo-room-preset"
FORMAT_VERSION = 1
MAX_FILE_BYTES = 2 * 1024 * 1024
NAME_MAX = 60

# ---------------------------------------------------------------------------
# Groups and key metadata
# ---------------------------------------------------------------------------

TONE = ("exposure", "contrast", "highlights", "shadows", "whites", "blacks")
EFFECTS = ("clarity", "texture", "dehaze", "sharpness", "luminance_nr")
_COLOUR_MIX = tuple(k for k in colormix.COLOR_DEFAULTS if not k.startswith("grade_"))
GROUPS: dict[str, tuple[str, ...]] = {
    "tone": TONE,
    "colour": ("vibrance", "saturation", "monochrome") + _COLOUR_MIX,
    "curves": ("tone_curve",),
    "grading": tuple(k for k in colormix.COLOR_DEFAULTS if k.startswith("grade_")),
    "effects": EFFECTS,
    "lut": ("lut", "lut_amount"),
    "wb": ("temp_ratio", "temp_shift", "tint"),
    "crop": ("crop", "straighten"),
}
GROUP_LABELS = {"tone": "Tone", "colour": "Colour", "curves": "Curves", "grading": "Grading",
                "effects": "Effects", "lut": "LUT", "wb": "White balance", "crop": "Crop"}
DEFAULT_GROUPS = ("tone", "colour", "curves", "grading", "effects", "lut")
KEY_GROUP = {k: g for g, keys in GROUPS.items() for k in keys}

DEFAULTS: dict[str, object] = {
    **{k: 0 for k in TONE}, "vibrance": 0, "saturation": 0, "monochrome": False,
    **colormix.COLOR_DEFAULTS, "clarity": 0, "texture": 0, "dehaze": 0, "sharpness": 0,
    "luminance_nr": 0, "tint": 0, "lut": "", "lut_amount": 100, "straighten": 0.0, "crop": None,
}
_RANGES = {"exposure": (-5.0, 5.0), "tint": (-150, 150), "sharpness": (0, 150), "luminance_nr": (0, 100),
           "lut_amount": (0, 100), "temp_ratio": (0.4, 2.5), "temp_shift": (0.4, 2.5),
           "straighten": (-15.0, 15.0)}
for _c in colormix.COLOR_CONTROLS:
    _RANGES[_c["key"]] = (_c["min"], _c["max"])
_NUMERIC = set(_RANGES) | set(TONE) | {"vibrance", "saturation", "clarity", "texture", "dehaze"}
HUE_KEYS = {k for k in colormix.COLOR_DEFAULTS if k.startswith("grade_") and k.endswith("_hue")}
LUT_ID = re.compile(r"^(builtin-[a-z0-9-]{1,60}|[0-9a-f]{12})$")


def key_range(key: str) -> tuple[float, float]:
    return _RANGES.get(key, (-100, 100))


class PresetError(ValueError):
    """A preset or preset file that cannot be used; the message says why."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def clean_values(values) -> tuple[dict, list[str]]:
    """``values`` restricted to known keys, clamped to the controls' ranges. Returns (clean, dropped keys)."""
    if not isinstance(values, dict):
        raise PresetError("a preset's values must be an object")
    out, dropped = {}, []
    for key, raw in values.items():
        if key not in KEY_GROUP:
            dropped.append(str(key))
            continue
        try:
            if key == "monochrome":
                out[key] = bool(raw)
            elif key == "lut":
                if raw not in ("", None) and not (isinstance(raw, str) and LUT_ID.match(raw)):
                    raise ValueError
                out[key] = raw or ""
            elif key == "tone_curve":
                if raw is not None and not isinstance(raw, dict):
                    raise ValueError
                out[key] = tonecurve.normalise(raw) if raw else tonecurve.default_curve()
            elif key == "crop":
                if raw is None:
                    out[key] = None
                else:
                    crop = {s: float(raw[s]) for s in ("left", "top", "right", "bottom")}
                    if not (0 <= crop["left"] < crop["right"] <= 1 and 0 <= crop["top"] < crop["bottom"] <= 1):
                        raise ValueError
                    out[key] = crop
            else:
                v = float(raw)
                if v != v or v in (float("inf"), float("-inf")):
                    raise ValueError
                lo, hi = key_range(key)
                out[key] = min(hi, max(lo, v))
        except (TypeError, ValueError, KeyError):
            raise PresetError(f"'{key}' has a value that is not valid") from None
    return out, dropped


def groups_of(values: dict) -> list[str]:
    present = {KEY_GROUP[k] for k in values if k in KEY_GROUP}
    return [g for g in GROUPS if g in present]


def _name(name) -> str:
    name = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", str(name or ""))).strip()
    if not name:
        raise PresetError("A preset needs a name")
    return name[:NAME_MAX]


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
    return s or "preset-" + hashlib.sha1(name.encode()).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Built-in presets (built purely from existing adjustment parameters)
# ---------------------------------------------------------------------------

def _curve(**channels) -> dict:
    tc = tonecurve.default_curve()
    for ch, pts in channels.items():
        tc["points"][ch] = [list(map(float, p)) for p in pts]
    return tonecurve.normalise(tc)


def _grade(**kw) -> dict:
    return {f"grade_{k}": v for k, v in kw.items()}


_BUILTINS: list[tuple[str, str, dict]] = [
    # --- Black and white
    ("B&W Soft", "B&W", dict(monochrome=True, contrast=10, highlights=-10, shadows=15, clarity=-5)),
    ("B&W Classic", "B&W", dict(monochrome=True, contrast=25, blacks=-10, whites=10, clarity=10,
                                gray_red=15, gray_yellow=10, gray_blue=-15)),
    ("B&W High Contrast", "B&W", dict(monochrome=True, contrast=55, blacks=-30, whites=20, clarity=25,
                                      gray_red=25, gray_orange=15, gray_blue=-30,
                                      tone_curve=_curve(rgb=[[0, 0], [64, 46], [192, 214], [255, 255]]))),
    ("B&W Low Key", "B&W", dict(monochrome=True, exposure=-0.5, contrast=30, shadows=-15,
                                highlights=-20, blacks=-25)),
    ("B&W Infrared", "B&W", dict(monochrome=True, contrast=20, dehaze=10, gray_green=60, gray_yellow=35,
                                 gray_aqua=-30, gray_blue=-55)),
    ("B&W Split Tone", "B&W", dict(monochrome=True, contrast=20,
                                   **_grade(shadow_hue=215, shadow_sat=25, highlight_hue=40, highlight_sat=20))),
    # --- Warm and cool
    ("Warm", "Warm / Cool", dict(temp_shift=1.10, **_grade(highlight_hue=40, highlight_sat=10))),
    ("Golden Hour", "Warm / Cool", dict(temp_shift=1.18, exposure=0.1, contrast=12, highlights=-15, vibrance=15,
                                        **_grade(highlight_hue=35, highlight_sat=25, shadow_hue=20, shadow_sat=8))),
    ("Cool", "Warm / Cool", dict(temp_shift=0.90, **_grade(shadow_hue=215, shadow_sat=12))),
    ("Blue Hour", "Warm / Cool", dict(temp_shift=0.82, contrast=15, saturation=-5, blacks=-10,
                                      **_grade(shadow_hue=225, shadow_sat=25))),
    # --- Faded
    ("Faded Film", "Faded", dict(saturation=-18, contrast=-5,
                                 tone_curve=_curve(rgb=[[0, 26], [64, 70], [192, 200], [255, 240]]),
                                 **_grade(shadow_hue=190, shadow_sat=8))),
    ("Matte", "Faded", dict(saturation=-10, vibrance=-5, clarity=-5,
                            tone_curve=_curve(rgb=[[0, 32], [255, 236]]))),
    ("Washed Out", "Faded", dict(saturation=-35, contrast=-15, tone_curve=_curve(rgb=[[0, 40], [255, 225]]))),
    # --- Contrast
    ("High Contrast", "Contrast", dict(contrast=45, blacks=-20, whites=15, clarity=15)),
    ("Soft Contrast", "Contrast", dict(contrast=-20, highlights=-20, shadows=25)),
    ("Dramatic", "Contrast", dict(contrast=35, highlights=-35, shadows=20, clarity=30, dehaze=15,
                                  vibrance=10, saturation=-8)),
    # --- Vivid
    ("Vivid", "Vivid", dict(vibrance=40, saturation=12, contrast=15, clarity=10)),
    ("Punchy Landscape", "Vivid", dict(vibrance=35, saturation=8, contrast=20, clarity=20, dehaze=12,
                                       sat_green=15, sat_blue=12, lum_blue=-15)),
    ("Muted", "Vivid", dict(vibrance=-15, saturation=-20, contrast=5)),
    # --- Portrait
    ("Portrait Soft", "Portrait", dict(clarity=-20, texture=-20, highlights=-15, shadows=15, contrast=-5,
                                       vibrance=5)),
    ("Portrait Glow", "Portrait", dict(clarity=-30, texture=-25, exposure=0.15, highlights=-20, shadows=20,
                                       sat_red=-8, lum_orange=10, **_grade(highlight_hue=35, highlight_sat=10))),
    ("Natural Skin", "Portrait", dict(sat_red=-6, sat_orange=-4, lum_orange=8, vibrance=8, texture=-8)),
    # --- Film-like
    ("Film Warm", "Film", dict(contrast=10, saturation=-8, temp_shift=1.06,
                               tone_curve=_curve(rgb=[[0, 14], [64, 58], [192, 204], [255, 248]]),
                               **_grade(shadow_hue=170, shadow_sat=10, highlight_hue=40, highlight_sat=16))),
    ("Film Teal", "Film", dict(contrast=18, blacks=8, sat_green=-10,
                               **_grade(shadow_hue=195, shadow_sat=22, highlight_hue=45, highlight_sat=14))),
    ("Cinematic", "Film", dict(contrast=22, highlights=-15, saturation=-6, vibrance=10, clarity=8,
                               **_grade(shadow_hue=200, shadow_sat=28, highlight_hue=38, highlight_sat=22,
                                        mid_hue=30, mid_sat=6))),
    ("Vintage Fade", "Film", dict(contrast=-5, saturation=-15, temp_shift=1.08,
                                  tone_curve=_curve(rgb=[[0, 30], [255, 235]]),
                                  **_grade(shadow_hue=200, shadow_sat=12, highlight_hue=50, highlight_sat=20))),
    ("Cross Process", "Film", dict(saturation=15, contrast=20,
                                   tone_curve=_curve(red=[[0, 0], [128, 150], [255, 255]],
                                                     blue=[[0, 30], [128, 120], [255, 220]]))),
    # --- Film styles that use a generated LUT
    ("Warm Negative (LUT)", "Film", dict(lut="builtin-warm-negative", lut_amount=100)),
    ("Cool Chrome (LUT)", "Film", dict(lut="builtin-cool-chrome", lut_amount=100)),
    ("Teal and Orange (LUT)", "Film", dict(lut="builtin-teal-and-orange", lut_amount=100)),
]


def _builtin_records() -> list[dict]:
    out = []
    for name, category, values in _BUILTINS:
        vals, dropped = clean_values(values)
        assert not dropped, (name, dropped)
        out.append({"id": "builtin:" + slug(name), "name": name, "category": category, "builtin": True,
                    "groups": groups_of(vals), "values": vals, "format": FORMAT, "version": FORMAT_VERSION})
    return out


BUILTINS = _builtin_records()
_BUILTIN_BY_ID = {p["id"]: p for p in BUILTINS}


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------

def blend_curves(cur, target, a: float):
    """Curve ``a`` of the way from ``cur`` to ``target`` (point curves are sampled at both curves' knots)."""
    if a <= 0:
        return cur
    if a >= 1:
        return tonecurve.normalise(target)
    c, t = tonecurve.normalise(cur), tonecurve.normalise(target)
    out = tonecurve.default_curve()
    for key in ("highlights", "lights", "darks", "shadows"):
        out["parametric"][key] = c["parametric"][key] + a * (t["parametric"][key] - c["parametric"][key])
    for ch in tonecurve.CHANNELS:
        xs_pts = sorted({p[0] for p in c["points"][ch]} | {p[0] for p in t["points"][ch]})
        xs = np.array(xs_pts) / 255.0
        yc = tonecurve.interpolate(c["points"][ch], xs)
        yt = tonecurve.interpolate(t["points"][ch], xs)
        out["points"][ch] = [[x, float(255.0 * (u + a * (v - u)))] for x, u, v in zip(xs_pts, yc, yt)]
    return tonecurve.normalise(out)


def _round(key: str, v: float):
    return round(v, 2) if key == "exposure" else round(v, 1) if key not in ("temp_ratio", "temp_shift") else v


def apply_preset(base: dict, preset: dict, amount: float = 1.0, baseline_temp: float | None = None) -> dict:
    """
    ``base`` (an edit) with the keys named by ``preset`` moved ``amount`` (0..1) of the way
    to the preset's values. Returns a new dict; ``base`` is not touched.
    """
    a = min(1.0, max(0.0, float(amount)))
    out = dict(base)
    if a <= 0:
        return out
    vals = preset["values"]
    baseline = float(baseline_temp or base.get("_baseline_temp") or 5500)

    def cur(key):
        return base.get(key, DEFAULTS.get(key))

    mono_pending = None
    for key, v in vals.items():
        if key == "monochrome":
            if v:
                out["monochrome"] = True if a >= 1 else bool(cur(key))
                mono_pending = a < 1
            else:
                out["monochrome"] = False if a >= 0.5 else bool(cur(key))
        elif key == "tone_curve":
            out["tone_curve"] = blend_curves(cur(key), v, a)
        elif key == "lut":
            if v:
                out["lut"] = v
                if "lut_amount" not in vals:
                    out["lut_amount"] = 100 * a
            elif a >= 1:
                out["lut"] = ""
            else:
                out["lut_amount"] = float(cur("lut_amount") or 0) * (1 - a)
        elif key == "lut_amount":
            if vals.get("lut"):
                out["lut_amount"] = _round(key, float(v) * a)
            else:
                out["lut_amount"] = _round(key, float(cur(key)) + a * (float(v) - float(cur(key))))
        elif key == "temp_ratio":
            now = float(base.get("color_temp") or baseline)
            out["color_temp"] = int(round(now + a * (baseline * float(v) - now)))
        elif key == "temp_shift":
            now = float(out.get("color_temp") or base.get("color_temp") or baseline)
            out["color_temp"] = int(round(now * (1 + a * (float(v) - 1))))
        elif key in ("crop", "straighten"):
            if a >= 0.5:
                out[key] = copy.deepcopy(v)
        elif key in HUE_KEYS:
            sat_key = key[:-3] + "sat"
            now_sat = float(cur(sat_key) or 0)
            if a >= 1 or now_sat <= 0:
                out[key] = v
            else:
                now = float(cur(key) or 0)
                d = ((float(v) - now + 180) % 360) - 180          # shortest way round the wheel
                out[key] = round((now + a * d) % 360, 1)
        else:
            now = float(cur(key) if cur(key) is not None else 0)
            out[key] = float(v) if a >= 1 else _round(key, now + a * (float(v) - now))
    if mono_pending:
        # Black and white is on or off; part-way, drain the colour instead of flipping the switch.
        now = float(cur("saturation") or 0)
        out["saturation"] = round(now + a * (-100 - now), 1)
    out["edit_version"] = max(int(base.get("edit_version") or 0), 2)
    return out


def neutral_adjustments(baseline_temp: float = 5500) -> dict:
    """The untouched edit a thumbnail is rendered from before a preset is applied."""
    out = dict(DEFAULTS)
    out.update(color_temp=baseline_temp, edit_version=2, tone_curve=tonecurve.default_curve(), masks=[])
    return out


# ---------------------------------------------------------------------------
# Building a preset from the current edit
# ---------------------------------------------------------------------------

def from_adjustments(name: str, adj: dict, groups, baseline_temp: float | None = None,
                     category: str = "User") -> dict:
    groups = list(dict.fromkeys(DEFAULT_GROUPS if groups is None else groups))
    bad = [g for g in groups if g not in GROUPS]
    if bad:
        raise PresetError(f"unknown preset group: {', '.join(bad)}")
    if not groups:
        raise PresetError("Choose at least one group to save")
    baseline = float(baseline_temp or adj.get("_baseline_temp") or 5500)
    values: dict = {}
    for g in groups:
        for key in GROUPS[g]:
            if key == "temp_ratio":
                values[key] = float(adj.get("color_temp", baseline)) / baseline
            elif key == "temp_shift":
                continue
            elif key == "tone_curve":
                values[key] = tonecurve.normalise(adj.get("tone_curve"))
            elif key in adj:
                values[key] = copy.deepcopy(adj[key])
            elif key in DEFAULTS:
                values[key] = copy.deepcopy(DEFAULTS[key])
    if "lut" in groups and not values.get("lut"):
        values["lut_amount"] = 100
    values, _ = clean_values(values)
    pretty = _name(name)
    return {"id": "user:" + slug(pretty), "name": pretty, "category": category, "builtin": False,
            "groups": list(groups), "values": values, "format": FORMAT, "version": FORMAT_VERSION}


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def preset_dir() -> Path:
    override = os.environ.get("PHOTO_ROOM_PRESET_DIR")
    return Path(override) if override else Path.home() / ".photo-room" / "presets"


def _path_for(preset_id: str) -> Path:
    kind, _, rest = preset_id.partition(":")
    if kind != "user" or not re.fullmatch(r"[a-z0-9-]{1,60}", rest):
        raise PresetError(f"No such preset: {preset_id!r}")
    return preset_dir() / f"{rest}.json"


def save(preset: dict) -> dict:
    path = _path_for(preset["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(preset, indent=1, sort_keys=True))
    os.replace(tmp, path)
    return preset


def _load_file(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
        vals, _ = clean_values(data["values"])
        pretty = _name(data["name"])
        if not vals:
            return None
    except (OSError, ValueError, KeyError, TypeError, PresetError):
        return None
    return {"id": "user:" + path.stem, "name": pretty, "category": str(data.get("category") or "User")[:40],
            "builtin": False, "groups": groups_of(vals), "values": vals,
            "format": FORMAT, "version": FORMAT_VERSION}


def list_user() -> list[dict]:
    root = preset_dir()
    if not root.is_dir():
        return []
    found = [p for p in (_load_file(f) for f in sorted(root.glob("*.json"))) if p]
    return sorted(found, key=lambda p: p["name"].lower())


def list_all() -> list[dict]:
    return BUILTINS + list_user()


def get(preset_id: str) -> dict:
    if preset_id in _BUILTIN_BY_ID:
        return _BUILTIN_BY_ID[preset_id]
    found = _load_file(_path_for(preset_id))
    if found is None:
        raise PresetError(f"No such preset: {preset_id!r}")
    return found


def delete(preset_id: str) -> bool:
    if preset_id in _BUILTIN_BY_ID:
        raise PresetError("Built-in presets cannot be deleted")
    path = _path_for(preset_id)
    if not path.exists():
        return False
    path.unlink()
    return True


def rename(preset_id: str, new_name: str) -> dict:
    old = get(preset_id)
    if old["builtin"]:
        raise PresetError("Built-in presets cannot be renamed")
    pretty = _name(new_name)
    new = {**old, "name": pretty, "id": "user:" + slug(pretty)}
    if new["id"] != old["id"] and _path_for(new["id"]).exists():
        raise PresetError(f"A preset called '{pretty}' already exists")
    save(new)
    if new["id"] != old["id"]:
        delete(old["id"])
    return new


def import_legacy(db_path=None) -> int:
    """Copy presets saved by older versions (the style database) into the preset folder, once each."""
    import editversion
    import style

    count = 0
    for name in style.list_presets(db_path):
        got = style.get_preset(name, baseline_temp=5500, db_path=db_path)
        if not got:
            continue
        if not got.get("edit_version"):
            got = editversion.map_parameters(got, 5500)
        got = dict(got)
        if "color_temp" in got:
            got["temp_ratio"] = float(got.pop("color_temp")) / 5500.0
        values, _ = clean_values({k: v for k, v in got.items() if k in KEY_GROUP})
        try:
            preset = {"id": "user:" + slug(name), "name": _name(name), "category": "User", "builtin": False,
                      "groups": groups_of(values), "values": values, "format": FORMAT, "version": FORMAT_VERSION}
        except PresetError:
            continue
        if not _path_for(preset["id"]).exists():
            save(preset)
            count += 1
    return count


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def export_json(preset_ids) -> str:
    items = [get(i) for i in preset_ids]
    if not items:
        raise PresetError("Nothing to export")
    body = [{"name": p["name"], "category": p["category"], "groups": p["groups"], "values": p["values"]}
            for p in items]
    return json.dumps({"format": FORMAT, "version": FORMAT_VERSION, "presets": body}, indent=1, sort_keys=True)


def _from_json(data) -> list[dict]:
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise PresetError("This is not a photo-room preset file")
    if int(data.get("version", 0)) > FORMAT_VERSION:
        raise PresetError("This preset file is from a newer version of photo-room")
    items = data["presets"] if isinstance(data.get("presets"), list) else [data]
    if not items or len(items) > 500:
        raise PresetError("The file holds no presets (or more than 500)")
    out = []
    for item in items:
        if not isinstance(item, dict):
            raise PresetError("A preset in the file is not an object")
        values, dropped = clean_values(item.get("values"))
        if not values:
            raise PresetError(f"Preset '{item.get('name', '?')}' has no usable settings")
        pretty = _name(item.get("name"))
        out.append({"id": "user:" + slug(pretty), "name": pretty,
                    "category": str(item.get("category") or "Imported")[:40], "builtin": False,
                    "groups": groups_of(values), "values": values, "dropped": dropped,
                    "format": FORMAT, "version": FORMAT_VERSION})
    return out


_XMP_SIMPLE = {
    "Exposure2012": "exposure", "Contrast2012": "contrast", "Highlights2012": "highlights",
    "Shadows2012": "shadows", "Whites2012": "whites", "Blacks2012": "blacks", "Vibrance": "vibrance",
    "Saturation": "saturation", "Clarity2012": "clarity", "Texture": "texture", "Dehaze": "dehaze",
    "Sharpness": "sharpness", "LuminanceSmoothing": "luminance_nr", "Tint": "tint",
}
_XMP_COLOUR = {tag: key for key, tag in colormix.XMP_TAGS.items()}
_XMP_PARAMETRIC = {tag: key for key, (tag, _) in tonecurve.PARAMETRIC_TAGS.items()}
_XMP_CURVES = {tag: ch for ch, tag in tonecurve.POINT_TAGS.items()}
_CRS = "{http://ns.adobe.com/camera-raw-settings/1.0/}"
_RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
_DANGEROUS = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.I)


def parse_lightroom_xmp(data: bytes) -> dict:
    """
    Read the settings of a Lightroom / Camera Raw preset (``.xmp``) that map onto our controls.

    Returns a preset record with an extra ``skipped`` list naming what could not be mapped
    (absolute white balance, local adjustments, embedded profiles and LUTs, lens and
    camera calibration, ...). Nothing unmapped is guessed.
    """
    if _DANGEROUS.search(data):
        raise PresetError("This XMP uses a DOCTYPE or entities, which are not accepted")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise PresetError(f"The XMP file could not be read: {exc}") from None
    descs = [d for d in root.iter(_RDF + "Description")
             if any(k.startswith(_CRS) for k in d.attrib) or any(c.tag.startswith(_CRS) for c in d)]
    if not descs:
        raise PresetError("No Camera Raw settings found in this XMP file")

    raw: dict[str, object] = {}
    for d in descs:
        for k, v in d.attrib.items():
            if k.startswith(_CRS):
                raw[k[len(_CRS):]] = v
        for child in d:
            if child.tag.startswith(_CRS):
                items = [li.text.strip() for li in child.iter(_RDF + "li") if li.text]
                raw[child.tag[len(_CRS):]] = items if items else (child.text or "").strip()

    def number(v):
        return float(str(v).strip().replace("+", ""))

    values, skipped = {}, []
    name = None
    for tag, v in raw.items():
        try:
            if tag in ("Name", "Group", "UUID", "SupportsAmount", "SupportsMonochrome", "SupportsOutputReferred",
                       "Version", "ProcessVersion", "HasSettings", "CompatibleVersion", "PresetType",
                       "Cluster", "SortName", "ToneCurveName2012", "WhiteBalance", "ConvertToGrayscale"):
                if tag == "ConvertToGrayscale":
                    values["monochrome"] = str(v).lower() == "true"
                continue
            if tag in _XMP_SIMPLE:
                values[_XMP_SIMPLE[tag]] = number(v)
            elif tag in _XMP_COLOUR:
                values[_XMP_COLOUR[tag]] = number(v)
            elif tag in _XMP_PARAMETRIC:
                values.setdefault("_para", {})[_XMP_PARAMETRIC[tag]] = number(v)
            elif tag in _XMP_CURVES:
                pts = []
                for item in (v if isinstance(v, list) else [v]):
                    x, y = (float(s) for s in str(item).replace(",", " ").split())
                    pts.append([x, y])
                values.setdefault("_points", {})[_XMP_CURVES[tag]] = pts
            else:
                skipped.append(tag)
        except (ValueError, TypeError):
            skipped.append(tag)
    name_el = next((e for e in root.iter(_CRS + "Name")), None)
    if name_el is not None:
        li = next(name_el.iter(_RDF + "li"), None)
        name = (li.text if li is not None else name_el.text) or None
    if not name and isinstance(raw.get("Name"), str):
        name = raw["Name"]

    para, points = values.pop("_para", None), values.pop("_points", None)
    if para or points:
        tc = tonecurve.default_curve()
        tc["parametric"].update(para or {})
        for ch, pts in (points or {}).items():
            tc["points"][ch] = pts
        values["tone_curve"] = tonecurve.normalise(tc)
    clean, dropped = clean_values(values)
    skipped = sorted(set(skipped) | set(dropped))
    if not clean:
        raise PresetError("None of the settings in this XMP map onto photo-room's controls")
    return {"name": _name(name or "Imported preset"), "values": clean, "skipped": skipped}


def import_file(data: bytes, filename: str = "") -> tuple[list[dict], list[str]]:
    """Parse an uploaded preset file (our JSON or a Lightroom ``.xmp``). Returns (presets, skipped settings). Not saved."""
    if len(data) > MAX_FILE_BYTES:
        raise PresetError("Preset file is larger than 2 MB")
    head = data.lstrip()[:1]
    if head == b"<":
        lr = parse_lightroom_xmp(data)
        pretty = lr["name"]
        preset = {"id": "user:" + slug(pretty), "name": pretty, "category": "Imported", "builtin": False,
                  "groups": groups_of(lr["values"]), "values": lr["values"],
                  "format": FORMAT, "version": FORMAT_VERSION}
        return [preset], lr["skipped"]
    try:
        parsed = json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        raise PresetError("This is not a preset file (expected photo-room .json or a Lightroom .xmp preset)") from None
    presets = _from_json(parsed)
    return presets, sorted({k for p in presets for k in p.pop("dropped", [])})
