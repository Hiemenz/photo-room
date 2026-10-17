# Presets, film styles (LUTs) and match reference look

Everything here is built from the editor's own controls. A preset is a set of slider values,
a tone curve, grading wheels or a LUT reference, so whatever you apply stays editable, is
one undoable History step, and is saved in the XMP sidecar like any other edit.

Code: `presetlib.py` (presets), `lut.py` (LUTs), `matchlook.py` (match reference),
`lookxmp.py` (sidecar record), `looks_api.py` (HTTP), `static/js/looks.js` (panel).

## Presets

* **Built-in library**: 30 looks in seven categories (B&W, Warm / Cool, Faded, Contrast,
  Vivid, Portrait, Film), all made from existing parameters. The three "(LUT)" presets use the
  generated film-style LUTs.
* **Applying**: click a tile. A preset moves every setting it names from the current value to its
  own; settings it does not name are left alone (so a preset never undoes your exposure). The
  **Amount** slider (0-100%) re-applies it from the state before the preset: 0 changes nothing,
  100 is the preset. Hover a tile to preview it on the open photo without changing anything.
  Black and white is on or off, so below 100% a B&W preset drains saturation instead of flipping
  the switch. Hue angles take the short way round the wheel.
* **Thumbnails** are rendered by the server from the photo's cached 160 px thumbnail through the
  real colour stages, one small immutable image per tile.
* **Save current edit as preset**: choose groups. Tone, Colour (incl. B&W and the colour mixer),
  Curves, Grading, Effects (clarity, texture, dehaze, sharpness, noise reduction) and LUT are on
  by default. **White balance** (kept as a ratio to the photo's own camera setting) and **Crop**
  are off: they belong to one photo and one light.
* **Apply to selected**: tick photos in the filmstrip, pick a preset and press *Apply to
  selected*. Each photo keeps its own edit with the preset merged in and gets one History step
  ("Preset: Vivid (70%)"). Photos edited in other software and photos on the old colour maths are
  skipped and reported, as with Paste settings.
* **Files**: *Export* writes your presets as photo-room `.json`; *Import* reads that, and
  Lightroom / Camera Raw `.xmp` presets. From an `.xmp` it maps: Basic tone, vibrance,
  saturation, clarity, texture, dehaze, sharpness, noise reduction, tint, B&W, HSL, B&W mixer,
  colour grading / split toning, tone curves (point and parametric). It reports, and does not
  guess, everything else: absolute Temperature, profiles, embedded LUT tables, lens, calibration,
  local adjustments. Presets saved by older versions (in the style database) are imported once.
* Stored as one JSON file per preset in `~/.photo-room/presets` (`PHOTO_ROOM_PRESET_DIR`).

## Film styles (LUTs)

* **Import** `.cube` 1D or 3D LUTs (Adobe / DaVinci). The file is validated first: exactly one of
  `LUT_1D_SIZE` / `LUT_3D_SIZE` (2-65536 / 2-129), the right number of finite rows, a sane
  `DOMAIN_MIN/MAX` or `LUT_*_INPUT_RANGE`, at most 64 MB. Anything else is refused with the reason.
  Same table imported twice is one LUT (content hash).
* **Library**: `~/.photo-room/luts` (`PHOTO_ROOM_LUT_DIR`), with rename, delete, export and a
  thumbnail tile per LUT. Six small generated 33-point built-ins (Warm Negative, Cool Chrome,
  Teal and Orange, Faded Matte, Silver Mono, Sepia Print) are always there.
* **Interpolation**: tetrahedral for 3D (exact along the grey axis, no hue skew), trilinear is
  available in `lut.py`; 1D LUTs are linear per channel. `tests/test_lut.py` checks identity
  LUTs, grid points, affine LUTs, a brute-force tetrahedral reference, error versus LUT size and
  grey-axis neutrality.
* **Where it sits in the pipeline** (see [COLOUR-PIPELINE.md](COLOUR-PIPELINE.md)): after tone,
  saturation, colour mixer and grading, before clarity / texture / sharpening, on the display-
  encoded image, and before local masks and edit strength. With black & white on it runs after
  the grey conversion, so a toning LUT keeps its tint.
* **Colour spaces**: a LUT is display-referred sRGB by default. For Adobe RGB or ProPhoto
  output the pixels are converted to linear sRGB primaries (Bradford-adapted), run through the
  sRGB curve, the LUT, and back, so a LUT looks the same in every space; colours outside sRGB keep
  their overshoot, so a neutral LUT returns them untouched. A LUT can declare
  `# photo-room: space=linear` (or be imported as *Linear light*) to be sampled on linear light,
  which is what 1D shaper / linearisation curves want. Log-encoded camera LUTs expect a log
  input this app does not produce; they will look wrong.
* **Intensity** blends the look with its input in the output space (0-100). Edit strength
  scales it on save like every other control.
* **Speed**: one lookup of an 800 px drag preview takes ~0.15 s on a Pi 5. The LUT result is
  kept in the stage cache under the key of the stages before it, so dragging Intensity only
  re-blends, and a LUT change re-runs only the LUT and the detail stage.

## Match reference look

Pick a reference image (or another photo ticked in the filmstrip) and photo-room compares its
statistics with the unedited open photo. Nothing is hidden; the difference becomes controls:

1. an **RGB tone curve** from luminance-histogram matching, reduced to 8 points;
2. **red/green/blue curves**: only the colour-cast part of per-channel histogram matching, at
   70% strength and capped;
3. **Saturation** and **colour mixer saturation** per hue band, from the ratio of mean
   saturation (bands with under 1.5% coverage in either picture are left alone);
4. **grading wheels** for shadows, midtones and highlights, from the average colour left over.

The report lists each step and the distance to the reference (tone, colour cast, saturation)
before and after, measured by rendering the values through the real curve, saturation, mixer and
grading code. *Apply* writes the values into the edit (with an Amount); *Save as preset* keeps
them. Limits: it matches statistics, not content, so a reference with a very different subject
or a heavily clipped histogram can only be matched as far as the statistics allow; it does not
change exposure/white balance sliders or hue and luminance of bands, and it works on the
unedited render (apply it before fine-tuning).

## Sidecar and export

* The photo's film style and last preset are recorded in the XMP `crs:Look` block:
  `LookName` (the LUT's name, else the preset's), `LookAmount` (0-1), `LookGroup`
  (`photo-room`) and `LookUUID`, a machine-readable
  `photo-room:v1?lut=<id>&lutamt=<n>&preset=<name>&presetamt=<n>` that is read back on open.
  The preset's values are ordinary develop settings and round-trip as usual. Saving without a
  look clears the block.
* The LUT is **not embedded** in the sidecar (keeps it small; Lightroom does not read `.cube`).
  On a machine without it the preview shows a warning and renders without it; **export refuses**
  rather than silently producing an unfiltered file. Import the `.cube` again (same table = same id).
* **Export** bakes the LUT into the pixels (same code as the preview, in the chosen output
  space). Develop settings, including the Look block, are deliberately not copied to exported
  files, so the look is never applied twice; every other tag (EXIF, GPS, IPTC, XMP, ICC) is copied
  as before (`tests/test_looks_xmp.py` compares them with the real exiftool).

## API

| Route | Purpose |
|---|---|
| `GET /api/looks/presets` | presets (id, name, category, groups, rev), save groups, LUTs |
| `POST /api/looks/preset/save` `delete` `rename` | user presets (save takes `adjustments` + `groups`, or an inline `preset`) |
| `POST /api/looks/preset/apply` | merged adjustments + history label for `id` or inline `preset`, `amount` 0-100 |
| `POST /api/looks/preset/preview` | one render of the open photo with a preset merged in (hover, amount) |
| `POST /api/looks/preset/apply_batch` | merge into each of `paths`, one History step each |
| `GET /api/looks/preset/thumb`, `/api/looks/lut/thumb` | tile JPEG for `path`, `id` |
| `GET /api/looks/preset/export?ids=`, `POST .../import` | preset files (JSON, Lightroom XMP) |
| `GET /api/looks/luts`, `POST /api/looks/lut/import` `rename` `delete`, `GET .../lut/export?id=` | LUT library |
| `POST /api/looks/match` | derive a preset from `reference_data` (base64 image) or `reference_path` |

## Not done

* No 3D LUT *creation* from edits, no `.3dl` / `.look` / log-input handling, no LUT tables inside
  the sidecar or in Lightroom-compatible `crs:RGBTable` form.
* Presets are not written back as Lightroom `.xmp` files (read only).
* Match reference works on global statistics only (no masks, no skin-tone protection).
