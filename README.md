# Photo Room

Imports Canon CR2 (and other raw) files, applies automatic tone and white
balance adjustments, lets you refine them in a local web editor, and exports
PNG or JPEG with all the original metadata intact. Runs on Linux and macOS.

Every edit is stored as Camera Raw XMP develop settings. Image pixels are
never modified. Raws are decoded directly with rawpy/LibRaw, and edits live in
an `.xmp` sidecar beside each raw, so the original is never touched. If Adobe
DNG Converter is installed (macOS/Windows) it is used as an optional extra:
photos are converted to DNG with the raw embedded. Edits, ratings, labels and
keywords for every file, DNGs included, go to the `.xmp` sidecar; a DNG is only
ever written to if you opt in (`editor.py --embed-dng`, or
`PHOTO_ROOM_EMBED_DNG=1`), because a DNG from a Leica, Pentax, DJI or phone may
be your only original.

## Quick start on Linux/macOS: nothing to first export

```bash
sudo apt install libimage-exiftool-perl              # macOS: brew install exiftool
curl -LsSf https://astral.sh/uv/install.sh | sh     # installs uv (needs Python 3.12+, uv fetches it)
git clone https://github.com/Hiemenz/photo-room && cd photo-room
uv sync                                              # fails loudly on Python < 3.12
uv run photo-room doctor                             # PASS/WARN/FAIL table with the exact fix for each problem
uv run photo-room ingest run /media/$USER/EOS_DIGITAL -o ~/Pictures/library/   # copy the card (macOS: /Volumes/EOS_DIGITAL)
uv run photo-room ~/Pictures/library/                # editor at http://127.0.0.1:5000, from any directory
```

Then in the browser:

1. **Cull** in the Library module: `g` for the grid, `1`-`5` / `x` / `p` to rate and flag.
2. **Edit** in Develop (`d`): sliders, tone curve, colour mixer, crop (`r`), masks. `?` lists every shortcut.
3. **Save** with `Ctrl/Cmd+S`; the edits go into an `.xmp` sidecar beside each raw.
4. **Export** with `Ctrl/Cmd+Shift+E`: pick PNG or JPEG, size, and leave *Keep metadata* ticked.
   Every EXIF, GPS, IPTC and XMP tag and the colour profile is copied from the raw.

No Adobe software is needed. CR2 is decoded with rawpy/LibRaw.

How this compares with Lightroom Classic, and what is missing, is in
[docs/LIGHTROOM-PARITY.md](docs/LIGHTROOM-PARITY.md). What has not been tested on real hardware, and a manual
QA checklist for a Mac with a real card, is in [docs/KNOWN-LIMITS.md](docs/KNOWN-LIMITS.md).

## Troubleshooting

Start with `uv run photo-room doctor`; it checks everything below and prints the fix.

- **exiftool missing or too old** (the editor shows a banner; saving and export fail):
  `sudo apt install libimage-exiftool-perl` (Linux) or `brew install exiftool` (macOS).
  Version 12 is the minimum, 13 or newer is recommended.
- **Wrong Python** (`uv sync` says the interpreter is incompatible with `>=3.12`):
  `uv python install 3.12 && uv sync --python 3.12`.
- **Port in use** (`Address already in use`): run `uv run photo-room -p 5001 <folder>`,
  or stop the program holding port 5000 (on macOS, AirPlay Receiver uses it).
- **Chromium for the browser tests**: `uv run playwright install chromium`
  (add `--with-deps` on a bare Linux box). Only `pytest -m ui` needs it.
- **`photo-room: command not found`**: run it through `uv run photo-room`, or activate `.venv`.

## Requirements

- `exiftool` — `brew install exiftool` (macOS) or `sudo apt install libimage-exiftool-perl` (Debian/Ubuntu/Raspberry Pi OS)
- Python 3.12+ with [uv](https://docs.astral.sh/uv/)
- Optional: `uv sync --extra semantic` enables on-device semantic search and Find similar (see docs/SEMANTIC-SEARCH.md); the model is downloaded only by `python semantic.py download --yes`.
- Optional: `uv sync --extra lens` installs `lensfunpy` for lens profiles
  (see "Lens corrections"); everything else works without it.
- Optional: [Adobe DNG Converter](https://helpx.adobe.com/camera-raw/using/adobe-dng-converter.html)
  (free, macOS/Windows only). Not needed on Linux; see below.

### Linux and macOS without DNG Converter

Nothing extra to install. `convert`, `watch` and card ingest detect that the
converter is missing and, instead of making a DNG, leave the raw alone and
write the automatic adjustments to `IMG_0001.xmp` next to it. The editor reads
and saves the same sidecar (Camera Raw develop settings, so Lightroom and
Camera Raw open it with the edits), and ratings go there too. Without a DNG,
"recover original" and `revert --extract-original` do not apply because
the raw was never replaced; `revert` clears the sidecar's develop settings.

```bash
uv sync
```

## Editor

```bash
uv run photo-room ~/Pictures/dng/
# open http://127.0.0.1:5000
```

Pick a photo from the library on the left; the preview updates live as you
move the sliders. The app starts in a neutral grey **dark theme** (the button
at the top right, or `Shift`+`T`, switches to a light one; the choice is
remembered in the browser). Press `?` at any time for the shortcut list.

| Control | Range | Notes |
|---|---|---|
| Exposure | ±5 EV | |
| Contrast, Highlights, Shadows, Whites, Blacks | ±100 | Each targets a different part of the tonal range |
| Temp | 2000–12000K | Starts at the camera's own white balance |
| Tint | ±150 | |
| Vibrance, Saturation | ±100 | Vibrance pushes muted colours harder than saturated ones |
| Clarity | ±100 | Midtone local contrast, masked away from highlights and shadows so it does not halo |
| Texture | ±100 | Fine detail on a much smaller scale than clarity; negative smooths |
| Dehaze | ±100 | Removes an atmospheric veil using a dark-channel estimate |
| Sharpness | 0–150 | Unsharp mask on luminance only |
| Straighten | ±15° | Rotates and auto-crops to the largest rectangle that fits, so there are never empty corners |
| Noise reduction | 0–100 | Seeded from the photograph's ISO on conversion. Not shown in the preview — see below |
| Edit strength | 0–150% | Scales the whole edit; 0% is the untouched photo, above 100% exaggerates it |
| Black & white | toggle | Applied after the colour sliders, so they act as a channel mixer |
| Color mixer, Color grading | see below | HSL by hue band, B&W mixer, shadow / midtone / highlight / global wheels |

Exposure, white balance and the tonal-range sliders edit **linear light**, as Camera
Raw does, so +1 EV is exactly twice the light, and the output is encoded with the exact
curve of its colour profile. Edits saved by earlier versions keep their old look and show
an **Upgrade edit** button. How every control is computed, and where it differs from
Lightroom, is in [docs/COLOUR-PIPELINE.md](docs/COLOUR-PIPELINE.md).

A 256-bin histogram sits above the controls with per-channel curves and a clipping
readout — the shadow and highlight percentages turn red past 1%.

#### Panel layout, accordion and zoom

The right panel is one scrolling column (histogram on top, the buttons at the bottom), so the
sliders keep their height on a laptop screen: 1440x900 and 1280x720 are both tested. The sliders
sit in folding sections (**Tone**, **White balance**, **Presence**, **Detail & geometry**, plus
**Overall**, Color mixer, Color grading, Masks and the rest). Click a heading to fold it;
**Collapse all**, **Expand all** and **Solo** (opening one folds the others) are at the top of the
column. Windows shorter than 800 px start with only Tone and White balance open. What is folded
and which panels are hidden is remembered in the browser.

- `]` hides or shows the adjustments panel (also the **Panel** button, top right); `[` hides or
  shows the photo list on the left. The photo grows into the space.
- **Zoom** bar under the photo: **Fit**, **Fill**, **100%** (one photo pixel per screen pixel,
  device-pixel aware) and **200%**. The scroll wheel zooms around the pointer (up to 400%), a
  trackpad or touch pinch zooms, dragging pans, double-click toggles 100% at that point, and `Z`
  toggles Fit and 100%. Zoomed views are drawn from the tile server (`/api/tile`) at full
  resolution with the sliders applied, so 100% is crisp, sharpening and noise are judged on real
  pixels, and the view follows the sliders after a short pause. Crop, spot removal, masks and
  Before/After need the whole photo, so zoom steps back to Fit when you open them.

#### Develop helpers

| Control | What it does |
|---|---|
| **Auto tone** (Tone section) | Sets Exposure, Contrast, Highlights, Shadows, Whites, Blacks and Vibrance. One History step, "Auto Tone", so one undo reverts it |
| **WB preset** menu | As Shot, Auto, Daylight, Cloudy, Shade, Tungsten, Fluorescent, Flash, each with its Temp and Tint; applying one is a History step |
| **Auto** (White balance) | Auto white balance (grey-world with a highlight check, skin-safe) |
| **Pick** (White balance) | Eyedropper: hover for a live `R G B` readout under the pointer, click something that should be neutral. Works in Fit and in the zoomed view; it asks the server for the Temp and Tint that neutralise the 7 px patch, and says so if the area is too dark or blown out. `Esc` cancels |
| **Clipping** button / `J` | Overlays clipped shadows in blue and clipped highlights in red on the photo, and lists the percentages per channel (R, G, B) under the histogram. The overlay follows the sliders and the zoom |

If a helper fails (server not running, old colour maths, too dark to pick) a toast says what
failed and what to do next, with a **Try again** button where retrying makes sense.

Clicking a slider's number resets that one slider. Opening a photo shows
its saved edits, or the auto-computed ones if it has none.

Noise reduction is written to the file but deliberately not shown in the
preview. The preview is a half-size render, and downsampling already hides
the noise this removes, so previewing it there would misrepresent the
full-resolution result in both directions.

### Colour mixer and colour grading

Both panels sit under the main sliders in the editor.

**Color mixer (HSL).** Eight hue bands -- red, orange, yellow, green, aqua,
blue, purple, magenta -- each with Hue, Saturation and Luminance sliders
(-100..100, shown as three tabs). A pixel is weighted by how close its hue
is to each band using raised-cosine overlaps, so the weights always sum to 1
and there are no hard edges between bands. Grey and near-grey pixels are
left alone (their hue is meaningless). Hue moves up to 30 degrees at +-100.
With **Black & white** on, the tabs are replaced by the **B&W mixer**: the
same eight bands, each brightening or darkening that colour's grey.

**Color grading.** Shadows, midtones, highlights and global wheels. Click or
drag the wheel (angle = hue, distance = saturation) or use the sliders, plus
a luminance slider per wheel. **Blending** (0..100, default 50) sets how far
the three tonal ranges overlap; **Balance** (-100..100) moves the shadow /
highlight pivot, positive favouring highlights. Tints are luma-neutral, so a
colour wheel changes colour without changing brightness; luminance is the
separate slider. Grading also applies to black & white photos (split toning).

All of these are plain keys in the adjustments (`hue_red`, `sat_red`,
`lum_red`, `gray_red`, `grade_shadow_hue`, ... `grade_blending`,
`grade_balance`), so presets, copy/paste and batch apply carry them, and
zero is an exact no-op. They are written to Camera Raw XMP as
`crs:HueAdjustmentRed`, `SaturationAdjustment*`, `LuminanceAdjustment*`,
`GrayMixer*`, `ColorGrade{Midtone,Global}{Hue,Sat}`,
`ColorGrade{Shadow,Midtone,Highlight,Global}Lum`,
`SplitToning{Shadow,Highlight}{Hue,Saturation}` (the names Camera Raw still uses for
those four), `ColorGradeBlending` and `SplitToningBalance`, and read back on open. Edit
strength scales saturation / luminance values but not hues, blending or
balance. The maths lives in `colormix.py`.

### Tone curve

Below the sliders, a Lightroom-style **Tone curve**:

- **Point curve** on a canvas, for **RGB** (master) and separately **R**, **G**,
  **B**. Click the curve to add a point, drag to move it, double-click an
  interior point to remove it. The curve is a smooth monotone cubic through
  the points (no overshoot, never inverts tones); the end points keep their
  position along the axis and can only be lifted or lowered. **Reset**
  returns all four curves and the sliders to linear.
- **Curve preset** menu: Linear, Medium Contrast, Strong Contrast (applied to
  the RGB curve).
- **Parametric** sliders: Highlights, Lights, Darks, Shadows (±100). Each moves
  its own tonal region of the curve; black and white points stay fixed.
- Stored as Camera Raw XMP (`crs:ToneCurvePV2012` and the `Red`/`Green`/`Blue`
  variants, `crs:ParametricHighlights/Lights/Darks/Shadows` and the split
  points), so Lightroom reads the same curve. A neutral curve clears those
  tags. It is part of presets, copy/paste settings and batch apply, and is
  scaled by Edit strength like the sliders.

### Local adjustments (masks)

Open the **Masks** panel below the colour grading. Pick a tool, then drag on
the photo:

- **Graduated** - drag from where the effect is full to where it fades out.
  Pins let you move the centre or either end; **Angle** and **Feather**
  sliders refine it.
- **Radial** - drag out from the centre. Pins move the centre and resize the
  width and height; sliders set angle and feather. The effect is inside the
  ellipse; **Invert** swaps it.
- **Brush** - paint. Each stroke has its own **Size**, **Feather**, **Flow**
  (how fast repeated passes build up) and **Density** (the most a stroke can
  reach); **Erase** (or hold Alt) subtracts paint. Strokes are kept as compact
  point lists and rasterised on demand, never as a bitmap.

Every mask has its own local sliders: Exposure, Contrast, Highlights, Shadows,
Temp, Tint, Saturation, Clarity, Texture, Dehaze and Sharpness. The local
adjustment is rendered over the whole picture and blended through the soft
mask, so outside the mask the pixels are exactly the unmasked render, and a
mask whose sliders are all zero changes nothing. A photo can have up to 16
masks; the list lets you select, enable/disable, **Duplicate**, **Invert** and
**Delete** (the Delete key) them. Press **O** (or tick *Show mask*) for a red
tint of the selected mask.

Mask positions are fractions of the cropped, straightened picture, so they are
the same in the preview and in the full-resolution export. Masks are part of
the settings: they go into presets, copy/paste, batch apply and the history
panel, and they are scaled by Edit strength like the sliders. Code:
`masks.py`.

**What is saved, and what Lightroom can read.** Graduated and radial masks are
written as `crs:MaskGroupBasedCorrections` (`Mask/Gradient`,
`Mask/CircularGradient`, with the `crs:Local*` sliders) and brush masks as
`crs:PaintBasedCorrections` (`Mask/Brush` with `Dabs`, `Radius`, `Flow`,
`CenterWeight`, `MaskValue`). The XMP cannot carry everything, so the full
set is also written to `IMG_0001.dng.masks.json` beside the photo (mask order,
this tool's feather model, brush density on erase strokes, disabled masks). On
open the JSON is used while the XMP still holds the same number of masks of
each kind; if Lightroom has since changed the XMP, the XMP wins and the masks
are rebuilt from it, approximately. Treat the XMP side as a best-effort
mapping: the structure is the one Camera Raw writes and exiftool round-trips
it, but it has not been opened in Lightroom, so gradient feather, radial
`Flipped`/`Feather` and the brush dab encoding may differ from what Lightroom
draws. Masks Lightroom itself makes (AI, range, colour, luminance) are not
imported, and saving from here replaces the mask lists in the file. A save
that sends no `masks` key leaves the file's masks alone.

### Crop

**Crop** opens a draggable overlay with thirds guides. Drag a corner to
resize, the box to reposition, or pick a ratio (1:1, 4:3, 3:2, 16:9) to
lock proportions.

Crops are stored as fractions of the frame, so the same crop means the
same framing regardless of preview size. When the photo is also
straightened, the crop you drag is relative to the rectangle the preview
shows — the largest one that fits inside the rotation — and is converted
to fractions of the whole straightened frame before being written, which
is how Camera Raw reads it. Straightening alone writes that rectangle as
the crop, so the empty corners are trimmed there too. Geometry is applied before the
tonal work, which means the histogram, the clipping readout and the
local-contrast masks all describe the picture you are keeping rather than
material you cropped away.

Edit strength does not scale the angle or the crop — framing is a
decision, not an intensity.

### Browsing a shoot

Where Adobe DNG Converter was used, a folder holds both the CR2 and its DNG.
They are one photograph, so the library lists the DNG and hides the source
raw — the DNG carries the edits and embeds the original anyway. Without the
converter (the Linux default) the raw itself is listed and its edits live in
the `.xmp` sidecar beside it.

**Grid** turns the main pane into a contact sheet. Click a frame to open
it; `cmd`-click still selects for a batch apply.

Thumbnails come from the JPEG preview the camera already embedded in each
raw rather than from demosaicing the sensor — about five times faster, and
cached on disk keyed by file size and modification time. A 431-frame shoot
costs roughly 47 seconds the first time and is instant afterwards.

For a big library the editor builds the missing thumbnails **in the
background**: when the Library opens, two worker threads walk the whole folder
while the grid stays usable, and a *Thumbnails 120/431* bar at the bottom
shows progress. Cells you scroll to are built first by a separate set of
workers, so they never queue behind the background job. The grid is paginated
(it fetches a page at a time) and cells load only as they scroll into view.

### Rating and culling

Press `1`–`5` to rate the open photo, `0` to clear the rating, `x` to reject it,
`p` to pick, `u` to unflag, `6`–`9` for the Red / Yellow / Green / Blue label,
or click the stars.
The dropdown above the library filters to unrated, rated, your best, or
rejects — and the arrow keys then move within whatever the filter shows, so
culling down to your picks and arrowing through does not jump back to
hidden frames.

Ratings are written as `XMP-xmp:Rating` with `-1` for rejected, which is
what Lightroom and Bridge read, so marks made here mean the same thing
wherever the photos are opened next. The whole library's ratings are read
in a single exiftool call.

### Smart cull and Auto edit

**Smart cull** (Library module, `K`) groups a shoot into bursts from capture
time and a perceptual hash, scores each frame (focus-region sharpness,
exposure and clipping, and OpenCV Haar face/eyes-open where found), and
suggests one pick per burst plus rejects for clearly bad frames, each with its
reasons. Review with the keyboard, compare side by side or at 100% with one
shared pan, then **Apply**: nothing changes until then, and the batch has an
Undo. **Auto edit picks** previews auto tone + auto white balance + your
learned style, then writes it as one undoable history step per photo.

The suggestions learn locally from what you accept or override (a small
numpy logistic regression, plus the typical corrections you make after Auto
edit), with accuracy-over-time, export, reset and clear controls. No network,
no cloud models; the data is `.photo-room-cull.sqlite` in the library folder.
Details: [docs/AUTO-CULL.md](docs/AUTO-CULL.md).

### Library module

The bar at the top switches between **Library** and **Develop**. Press `g`
anywhere to open the Library grid; `e`, `d`, `Enter` or a double-click opens
the selected photo in Develop.

In the grid, click selects, `cmd`/`ctrl`-click adds to the selection,
`shift`-click selects a range, and the arrow keys move the cursor. Every
action below applies to the whole selection.

| Key | Action |
|---|---|
| `p` / `u` / `x` | Pick / unflag / reject |
| `0`-`5` | Star rating |
| `6` `7` `8` `9` | Red / Yellow / Green / Blue label (swatches in the footer also set Purple and clear) |

Keywords are typed in the footer (comma separated, Enter to add, **Remove**
to take them off the selection).

**Live grid.** The grid listens to the server's library change feed
(`/api/library/events`, Server-Sent Events). A flag, rating, label or keyword set in another
window, by Smart cull, or from the command line appears on its cell without a refresh; photos
that arrive (an import or a card ingest) are added with a brief highlight and photos that
disappear are removed, keeping the filters and scroll position. A green **Live** dot in the footer
shows the feed is connected (red while it reconnects).

**Quick look.** `Space` opens a large view of the highlighted photo without leaving the grid;
`←` `→` step through the photos, `Esc`, `Space` or a click closes it, and the culling keys keep
working underneath. It shows the photo's **smart preview** (a cached 2048 px decode in
`.photo-room-previews`, built after an import or on first look) and the thumbnail while that is
being made.

**Import.** Press **Import...** (files) or **Import folder...**, or drag files or a whole folder
onto the grid. Only raw files are sent (`.cr2 .cr3 .nef .arw .raf .orf .rw2 .dng`); other files
and dot-files are ignored and counted. Each file is copied by the same code as the card ingest:
written to a temporary name, flushed, checked against the source with SHA-256, then given its final
name without ever replacing an existing file (a different file with the same name is kept as
`IMG_0001-1.CR2`). A file whose bytes are already anywhere in the library is skipped as a
duplicate, whatever it is called. Photos go into `YYYY/MM/DD` folders by capture date, as the
ingest does. A progress panel lists every file and why anything was skipped or failed, and
**Cancel** stops after the current file. Endpoints: `POST /api/import/upload?name=` (raw body),
`POST /api/import/finish`, `GET /api/smartpreview`, `GET /api/smartpreview/status`,
`POST /api/smartpreview/build`.

The filter bar narrows the grid by rating, flag, colour label, keyword,
camera and capture date range, plus a text search over file name, keywords
and camera; the sort menu orders by name, capture date, rating, label, flag
or camera, ascending or descending. Filters combine (all must match). Files
missing the sort field, such as an undated photo, always sort last.

Where the data lives, so Lightroom reads what you set here:

| Field | Stored as |
|---|---|
| Rating | `XMP-xmp:Rating` |
| Reject | rating `-1`, the same state the `x` key already set |
| Colour label | `XMP-xmp:Label` (`Red`, `Yellow`, `Green`, `Blue`, `Purple`) |
| Keywords | `XMP-dc:Subject` |
| Pick | `.photo-room-library.json` in the library root (Camera Raw has no standard field for it; Lightroom keeps flags in its catalogue) |

Picking a rejected photo un-rejects it, and rejecting a picked one drops the
pick. Writing label and keywords modifies the file's XMP in place, as ratings
already do, so that photo's thumbnail is rebuilt once.

Large libraries stay quick: the grid never decodes a raw. Metadata is read in
batched `exiftool -fast2` passes and cached in memory by file size and
modification time (changes made elsewhere are picked up automatically), the
API returns 200 photos per page, the next page loads as you scroll, and
thumbnails come from the camera's embedded preview and load only when
visible. Endpoints: `GET /api/library` (filters, `sort`, `dir`, `offset`,
`limit`), `POST /api/flag`, `/api/label`, `/api/keywords` (each accepts `path`
or `paths`).

### Spot removal

**Spot removal** (right panel) turns on a spot tool. Click a blemish and a
circle is placed with a source circle (dashed) chosen for you: the nearby
patch whose surroundings best match. Drag the dashed circle to pick the
source yourself, drag the solid one to move the spot, `Delete` removes it.
Per spot (or as the default for the next one): **Size**, **Feather**,
**Opacity** and **Heal** or **Clone**. Clone copies the source pixels;
Heal Poisson-blends them (OpenCV seamless clone) so the texture comes from
the source but the tone follows the area around the destination.

Spots are stored as fractions of the frame (radius as a fraction of the
width), so the preview and the full-resolution export repair the same
places. They are applied to the original frame before lens correction,
transform, straighten and crop, so changing the crop never moves a repair.
While the tool is on the preview shows the whole uncropped frame so clicks
land where they appear. They are written as `crs:RetouchAreas` (circle
mask, `SourceX`, `OffsetY`, opacity, feather) and read back from it.
Presets and **Paste settings** never carry spots, and batch apply drops
them; they belong to one photograph. Lightroom's own rendering of the same
areas is not guaranteed to match pixel for pixel, and its non-circular
retouch shapes are not read.

### Transform and Upright

**Transform** has **Vertical** and **Horizontal** (perspective, +-100 is a
30 degree tilt; positive widens the top / enlarges the right edge),
**Rotate** (degrees, on top of Straighten), **Aspect**, **Scale** and
**X / Y offset**. It is one homography; the result is automatically
cropped to the largest rectangle of the original proportions that has no
empty corners, so nothing needs filling. All neutral is an exact no-op.
Order is transform, then Straighten, then Crop, so a crop you drag is
relative to the transformed picture.

**Upright** buttons set those sliders from detected lines (OpenCV Canny and
Hough): **Level** rotates only, **Vertical** also fixes converging
verticals, **Full** also converging horizontals, **Auto** picks what the
lines support. With too few clear lines nothing changes. Detection runs on
the preview, and the sliders are ordinary values you can adjust afterwards.
They are written as the `crs:Perspective*` tags (Camera Raw has its own
auto-crop, so a transformed photo that is also cropped will frame slightly
differently there).

### Lens corrections and chromatic aberration

**Use lens profile** reads the camera, lens, focal length and aperture from
EXIF and applies the [lensfun](https://lensfun.github.io/) database's
distortion, vignetting and (where the profile has it) lateral CA
correction. That needs the optional package: `uv sync --extra lens`
(`lensfunpy` ships wheels for common Linux and macOS setups; elsewhere it
needs a C++ build). Without it, or when no profile matches, the panel says
so and the manual controls still work: **Distortion** (positive straightens
barrel distortion), **Vignetting** (positive lightens the corners).
**Remove chromatic aberration** registers the red and blue channels to green
with a generic radial-shift estimate when no lens profile supplies one, and
**Purple / Green fringe** desaturate fringes next to high-contrast edges.
Written as `LensProfileEnable`, `LensManualDistortionAmount`,
`VignetteAmount`, `AutoLateralCA` and `Defringe*Amount`. Every lens step is
wrapped so a failure leaves the picture uncorrected rather than raising.
Transform, lens and CA settings travel with presets, copy/paste, batch
apply and the history like any slider; they are not scaled by Edit strength.

### Collections, virtual copies, Compare and Survey

**Collections.** The Library has a left panel listing collections. `+` makes
a collection (holding the current selection, if any); `+ Smart` opens a rule
editor. Add photos by dragging grid cells onto a collection, or with the
**Add to collection** menu in the footer; **Remove from collection** takes
the selection back out. Click a collection to browse it (the filter bar still
applies inside it). The pencil renames a collection (or edits a smart one's
rules); the cross deletes the collection only, never the photos.

A **smart collection** holds rules joined by "all" or "any" and is
recalculated each time it is shown: rating (at least / at most / exactly),
flag, colour label (or none), keyword (has / does not have), camera (is /
contains) and capture date (on / after / before). Smart collections cannot be
filled by hand.

Collections and virtual copies are stored in `.photo-room-catalog.json` in the
library root (next to `.photo-room-library.json`). Names are unique,
ignoring case.

**Virtual copies.** Select a photo and press `v` (or **Virtual copy**) to
make a second version of it with its own edit stack, rating, colour label,
keywords and pick flag, and no second raw file. A new copy starts from the
photo's current edits and metadata, then the two are independent. Copies show
a folded page-corner badge, appear in the grid and in collections, open in
Develop with their own history and snapshots, and are named
`IMG_0001 (copy 1).dng`. Edits are saved in the catalog, not into the raw's
XMP, so Lightroom does not see them; ratings, labels and keywords on a copy
are likewise catalog-only. **Delete copy** removes the copy, its history file
and its collection entries; the raw is never touched. Deleting the raw's
file hides its copies.

Export honours copies: `process.py export <folder>` exports each virtual copy
of every photo in the folder as its own file rendered with the copy's
settings, named `<stem>-copy<N>` (for example `IMG_0001-copy2.jpg`) so it
never overwrites the original. Metadata comes from the raw, with the copy's
rating, colour label and keywords applied on top.

**Compare (`c`).** Opens the active photo (the "select") beside a candidate.
Left/Right step the candidate through the grid, Up promotes the candidate to
select, `0`-`5`/`p`/`x` rate and flag the candidate. The mouse wheel zooms
both panes together about the pointer, dragging pans both, double-click
toggles 100% (4x here) and fit. `c` or Esc closes.

**Survey (`n`).** Select two or more photos and press `n` to see them all at
once, sized to fit. Click a photo to drop it from the survey, double-click
to open it in Develop. `n` or Esc closes.

Endpoints: `GET /api/collections`, `POST /api/collections/create|rename|
delete|rules|add|remove`, `GET /api/library?collection=<id>`,
`POST /api/virtual/create|delete`, `POST /api/view_preview` (a read-only
preview used by Compare). Rate, flag, label, keyword, thumbnail, open,
preview, save, reset and history endpoints accept a virtual copy's id
(`<raw path>#vc-<hex>`) wherever they accept a path.

### Presets

Save the current sliders under a name, then pick it from the dropdown to
apply it to any photo. Presets carry tone, colour, transform and lens
settings — crop, straighten and spot removal are framing and retouching
decisions belonging to one photograph, so they are never stored.

Temperature is kept as a ratio of the source photo's white balance, so a
preset means "warmer than this camera saw it" rather than a fixed Kelvin
value that would force one photo's colour onto every other frame.

### Copying settings between photos

**Copy settings** (`cmd`+`c`) takes the current sliders; **Paste settings**
(`cmd`+`v`) applies them to the photo you are looking at. With something
copied, the batch button pastes those settings instead of the current ones.

Temperature travels as a ratio of each photo's own white balance rather
than an absolute Kelvin value, so pasting onto a frame shot in different
light keeps the intent rather than forcing the source photo's colour.

### Applying one edit to a whole shoot

`cmd`-click (or `ctrl`-click) files in the library to select them, or
`shift`-click for a range, then **Apply edit to N selected**. The current
slider values are written to every selected file.

Each file is attempted independently, so one unwritable photo does not
abandon the rest — the status line reports how many failed. Temperature is
re-anchored to each file's own camera white balance rather than copied as
an absolute Kelvin value, so a batch spanning different bodies does not
shift colour.

### History, snapshots and before/after

The left panel has a **History** list. Every committed step (a slider
release, a curve change, a preset, a crop, paste, revert) is recorded with a
label and time; click one to jump back to that state. Jumping is
non-destructive: later steps stay listed (dimmed) until you make a new edit,
which drops them, like undo/redo. Nothing is written to the photo until you
save. The newest 100 steps per photo are kept.

**Snapshots** (`+ New`) store the current settings under a name; click one to
apply it (recorded as a history step) and `x` to delete it. Re-using a name
replaces that snapshot.

**Reset to import** returns to the as-imported state, the auto (or learned
style) proposal captured the first time the photo is opened. It is a history
step, so it can be undone. *Revert to original* is different: it clears the
stored XMP from the file.

History and snapshots live in `IMG_0001.dng.photoroom.json` beside the photo,
not in the XMP, so the develop settings Lightroom reads are unchanged.

**Before/after.** The **After / Split / Side by side** buttons above the photo
switch views. Split has a draggable divider. Press `\` to flip to Before and
back (tap to latch, or hold to peek). *Before* is the as-imported state.

### Keyboard

Shortcuts follow Lightroom Classic. Press `?` in the editor for the same list.
`Mod` is `Ctrl` on Linux and `Cmd` on macOS. Shortcuts are ignored while you
type in a text box, number field or menu, and while a dialog is open.

| Key | Action |
|---|---|
| `g` | Library grid |
| `e` | Loupe: open the selected photo in Develop |
| `d` | Develop |
| `r` | Crop tool (Develop) |
| `c` | Compare view (from Develop it opens the Library first) |
| `n` | Survey view (likewise) |
| `1`–`5` | Star rating |
| `0` | Clear the rating |
| `x` | Reject |
| `p` | Pick flag |
| `u` | Unflag |
| `6` `7` `8` `9` | Label Red / Yellow / Green / Blue |
| `←` `→` | Previous / next photo (the cursor moves in the grid) |
| `\` (hold or tap) | Before: show the as-imported state; tap to latch |
| `o` | Mask overlay (red tint), with the Masks panel open |
| `b` | Toggle black and white |
| `Delete` | Delete the selected mask or spot |
| `Esc` | Leave the current tool, close overlays |
| `Mod`+`Z` | Undo (steps back through the History panel) |
| `Mod`+`Shift`+`Z` | Redo |
| `Mod`+`C` / `Mod`+`V` | Copy / paste settings |
| `Mod`+`S` | Save adjustments |
| `Mod`+`Shift`+`E` | Export dialog |
| `v` | Virtual copy (Library) |
| `?` | Shortcut list |
| `Shift`+`T` | Toggle dark / light theme |
| `j` | Clipping overlay (Develop) |
| `z` | Zoom: toggle Fit and 100% (Develop) |
| `]` / `[` | Hide / show the adjustments panel / the photo list |
| `Space` | Quick look at the highlighted photo (Library) |

### Reverting

- **Revert to original** clears every stored adjustment.
- **Recover original raw file** writes the embedded CR2 back out, byte for byte.

### Presets, film styles and match reference

The **Presets & film styles** panel (right column) has three tabs. Details and limits are in
[docs/PRESETS-AND-LUTS.md](docs/PRESETS-AND-LUTS.md).

- **Presets**: 30 built-in looks (B&W, warm/cool, faded, contrast, vivid, portrait, film) made from
  the normal controls, with live thumbnails of your photo, hover preview and an **Amount** slider
  (0-100%). Click applies as one History step. **Save...** keeps the current edit as a preset
  (choose Tone / Colour / Curves / Grading / Effects / LUT; crop and white balance are off by
  default). **Apply to selected** merges a preset into every ticked photo, one History step each.
  **Import / Export** read and write photo-room `.json` and read Lightroom `.xmp` presets.
- **Film styles**: import `.cube` 1D/3D LUTs (validated, tetrahedral interpolation), keep a LUT
  library, a few generated built-ins, an **Intensity** slider. The LUT runs after colour grading
  and before sharpening, with correct handling of Adobe RGB / ProPhoto output.
- **Match**: pick a reference image and get an editable tone curve, per-channel curves, colour
  mixer saturation and grading wheels that move the photo toward it, with a before/after
  distance report.

The LUT and the last preset used are recorded in the XMP sidecar (`crs:Look`); exports bake the
LUT into the pixels and keep all metadata.

## Card ingest

```bash
uv run photo-room ingest cards                          # what's mounted, and what looks like a card
uv run photo-room ingest watch -o ~/Pictures/library/   # wait for a card, then ingest it
uv run photo-room ingest run /Volumes/EOS_DIGITAL -o ~/Pictures/library/

# Develop a shoot again after the learned style has improved
uv run photo-room ingest run /Volumes/EOS_DIGITAL -o ~/Pictures/library/ --no-skip
```

Insert a card and it copies every raw into the library, sorts by shoot
date, and applies your learned adjustments. With Adobe DNG Converter installed
it also converts each raw to DNG; without it (Linux) the raws are kept as they
are and the adjustments go to `.xmp` sidecars.

**The card is read-only throughout.** Nothing is deleted or modified on
it — erase it in-camera once you are satisfied the copy is good. Each
copy is checksummed and written to a temporary name first, so an
interrupted import can never leave a truncated file that later looks
like a successful one.

**Ingest never loses or overwrites an original.** Duplicates are detected by
content (size, a head/middle/tail sample, then a full SHA-256), not by name.
If a different photo already uses the name (two cards, a reset counter,
`100CANON` and `101CANON`), the new one is kept as `IMG_0001-1.CR2`,
`-2` and so on, and is listed in the summary. A file is copied to a unique
temporary name, flushed to disk, verified and only then given its final name
by a no-overwrite rename. The summary reports copied, skipped as identical,
renamed and failed counts. Re-running an ingest copies nothing new.

**It does not upload.** Ingest stages the shoot; you review it in the
editor and upload from there. A shoot generally contains frames you
would not want published.

## Automatic card import

```bash
uv run photo-room watch-cards ~/Pictures/library      # import, verify and safely eject cards as they are inserted
```

Detects a mounted card (a volume with `DCIM`), copies new raws, JPEGs and videos into date folders with the
same checksum-verified, never-overwrite, content-deduplicated copy as `ingest`, re-checks every file
against the card, then unmounts and powers the card off (`udisksctl` / `umount` on Linux,
`diskutil eject` on macOS) and sends a desktop notification. It never ejects after a failure, an
interruption or a busy card. `--no-eject` leaves the card mounted; `--delete-after-import` (off by
default, with `--delete-dry-run`) removes only individually verified photos from the card. A progress
panel appears in the editor. Systemd, launchd and udev examples and the caveats are in
[docs/CARD-IMPORT.md](docs/CARD-IMPORT.md).

## Culling a shoot

```bash
python process.py cull ~/Pictures/library/2025/09/06/
python process.py cull ~/Pictures/library/ --sharpness --limit 20
```

Flags frames that no adjustment can rescue — entirely blown, entirely
black, or carrying no tonal range. On a 60-frame sample of a real polo
shoot this flagged exactly one frame, which was genuinely blown, with no
false positives.

`--sharpness` additionally ranks the usable frames by how much fine
detail they contain. **This is not a focus test and never produces a
verdict.** Measured across that shoot, scores spanned 24x, and the
lowest-scoring frame was a perfectly sharp photograph of a horse against
smooth grass — the metric reads scene content, not whether focus was
missed. Use it to decide what to look at, not what to delete.

The editor shows the same assessment when you open a photo, along with
the detail score and clipping percentages.

## Learning your style

Every edit you save is recorded: what the photo looked like, what the
automatic pass proposed, and what you actually kept. The model learns the
*correction* to the automatic values rather than the values themselves,
which is what makes it safe — with no history the correction is zero and
behaviour is identical to the plain heuristic.

```bash
python process.py style          # what it has learned
python process.py style --clear  # forget everything
```

The learned style applies wherever photographs are converted — card
ingest, `convert` and `watch` alike. A `[styled]` marker appears beside
each file in verbose output when it has been applied.

It stays deliberately cautious. Below three edits it predicts nothing at
all; after that the correction is scaled by `n / (n + 12)`, so early
edits nudge the defaults rather than replacing them. Confidence reaches
about 75% at 40 edits and never reaches 100%.

Temperature is learned as a ratio of the camera's own white balance, not
as absolute Kelvin, so a preference for warmer images transfers between
scenes shot under different light.

Black and white is never predicted. Converting a photo to monochrome is
an intent rather than a tonal preference, and guessing it wrong is far
more jarring than a slightly-off contrast value.

Training data lives in a single DuckDB file at `~/.photo-room/style.duckdb`
— one row per edit, a few megabytes even after tens of thousands. Set
`PHOTO_ROOM_STYLE_DB` to point it elsewhere; worth doing when running the
editor against test photographs, whose edits would otherwise be learned
from as though they were your own.

## Command line

```bash
# Every raw format the converter and LibRaw both handle is accepted:
# CR2, CR3, NEF, ARW, RAF, ORF, RW2. Two camera bodies at one event is
# ordinary, so convert, watch, cull and ingest all use one shared list.

# Convert a folder, organised into date subfolders
uv run python process.py convert ~/Pictures/shoot/ -o ~/Pictures/dng/ --date-folders

# Conversion runs 12 files at once by default; -j 1 makes it serial
uv run python process.py convert ~/Pictures/shoot/ -j 4

# Convert and upload
uv run python process.py convert IMG_0001.CR2 --upload-google -v

# Watch a folder and convert as files arrive
uv run python process.py watch ~/Pictures/import/ -o ~/Pictures/dng/

# Export edited photos as JPEG/PNG with all metadata kept (see "Export")
uv run python process.py export ~/Pictures/shoot/ -o ~/Pictures/out/ --format jpeg --quality 90

# Undo edits, or recover the embedded original
uv run python process.py revert ~/Pictures/dng/IMG_0001.dng
uv run python process.py revert ~/Pictures/dng/ --extract-original -o ~/Pictures/recovered/
```

## Export

```bash
uv run python process.py export IMG_0001.CR2 ~/Pictures/shoot2/ -o ~/Pictures/out/ \
    --format png --long-edge 2048 --colorspace adobergb --template "{date}_{seq:04d}_{name}"
```

Renders each photo from the raw at full resolution with its saved edits (from
the sidecar, or inside the DNG), then writes a PNG or JPEG. The raw is never
modified.

| Option | Meaning |
|---|---|
| `--format png\|jpeg` | Output format (default jpeg) |
| `--quality 1-100` | JPEG quality (default 92; 90 and up uses full-resolution chroma) |
| `--long-edge N` | Resize so the longer side is N pixels; never upscales |
| `--colorspace srgb\|adobergb\|prophoto` | Decode into this space and embed a matching ICC profile. Raw default is sRGB; ordinary images keep their own profile unless one is given |
| `--template` | Filename template with `{name}` `{date}` (shoot date) `{seq}` `{width}` `{height}`; `{seq:04d}` pads. Path separators are rejected. An existing file is never overwritten (`-1`, `-2` ... are appended) |
| `--bit-depth 8\|16` | PNG bits per channel (16 is PNG only; default 8) |
| `--no-metadata` | Skip the metadata copy (default is to keep it) |

**Metadata.** Everything is copied from the source with exiftool
(`-TagsFromFile source -all:all`): EXIF including GPS and maker notes, IPTC and
XMP, plus rating and keywords from the sidecar. In PNG these land in the
`eXIf` and XMP chunks. The exceptions are deliberate: dimensions and
orientation (already applied), the thumbnail of the unedited frame, and
Camera Raw develop settings, which are baked into the pixels and would be
applied twice if Lightroom saw them. The output carries its own ICC profile.
The test suite exports a synthetic photo with EXIF, GPS, IPTC and XMP and
fails if any tag differs before and after.

### Export dialog (in the editor)

Click **Export...** in the right-hand panel, or press <kbd>Ctrl</kbd>/<kbd>Cmd</kbd>+<kbd>Shift</kbd>+<kbd>E</kbd>.

| Setting | Meaning |
|---|---|
| Export | Current photo, all selected (Cmd/Ctrl-click or shift-click in the library), or the whole folder |
| Destination | Folder to write to (created if missing; default `~/Pictures/photo-room-export`). Kept outside the library so exports are not listed as photos |
| File name | Template with `{name}` `{date}` `{seq}` `{seq:04d}` `{width}` `{height}`; the resulting name is previewed live, including `-1` suffixes for files that already exist. `{width}`/`{height}` in the preview are estimates because a crop changes them |
| Format / Quality | PNG or JPEG; quality slider for JPEG |
| PNG bit depth | 8 or 16 bits per channel (see below) |
| Colour space | sRGB, Adobe RGB or ProPhoto RGB, with the ICC profile embedded |
| Resize | Original size, or long edge in pixels (never upscales) |
| Keep all metadata (EXIF, GPS, IPTC, XMP) | On by default; copied from the source with exiftool |
| Save the open photo's edits first | On by default; saves the sliders you are looking at so the export matches the screen |

The dialog calls the same `exporter.py` as `process.py export`, so it renders
the **saved** edit stack (basic, tone curve, HSL, colour grading, crop) from
the raw at **full resolution** (never the half-size preview). Batches run in
the background with a progress bar; one photo failing is reported at the end
without stopping the rest. Only one export runs at a time. If exiftool is not
installed the dialog says so and how to install it (it is needed to read the
saved edits as well as to copy metadata).

Endpoints: `GET /api/export/defaults`, `POST /api/export/preview`,
`POST /api/export` (returns 202 and a job id), `GET /api/export/status/<id>`,
plus the preset and queue endpoints below.

**Presets.** The dialog's Preset menu stores every setting (destination, file
name, format, quality, bit depth, colour space, resize, metadata, sharpening,
watermark) under a name. Four built-ins ship read-only: *Web 2048px JPEG q80
sRGB*, *Full-res JPEG q92*, *Full-res 16-bit PNG* and *Print AdobeRGB*. Your own
presets are saved in `~/.photo-room/export_presets.json` (set
`PHOTO_ROOM_CONFIG_DIR` to move it); names are case-insensitive and built-in
names are reserved. `GET /api/export/presets`, `POST /api/export/presets`
(`{"name", "settings"}`), `POST /api/export/presets/delete` (`{"name"}`).

**Output sharpening.** *Sharpen for* Screen, Matte paper or Glossy paper at
Low, Standard or High. It is an unsharp mask on luminance only (no colour
fringes) applied **after** the resize, with a radius scaled to the output size
(matte uses a wider, stronger halo, glossy less, screen a fine one). Request
field: `"sharpen": {"for": "screen|matte|glossy", "amount": "low|standard|high"}`.

**Watermark.** Text (font name or path, size as a percentage of the long edge,
colour, opacity, one of nine positions, margin as a percentage) or an image
file (PNG with transparency works; width as a percentage of the long edge).
It is drawn onto the exported pixels only, after sharpening, so the preview,
the saved edits and the original never change. Request field:
`"watermark": {"type": "text|image", "text", "font", "size", "color", "image",
"scale", "opacity", "position": "tl|tc|tr|ml|c|mr|bl|bc|br", "margin"}`.
Fonts are found in the system font folders; an unknown font falls back to
DejaVu Sans or Pillow's built-in font. Sharpening and watermarking never touch
metadata: the tests compare every EXIF/GPS/IPTC/XMP tag before and after for
presets, sharpening and both watermark types.

**Batch queue.** A second export started while one is running is queued (up
to 10 waiting) and runs when the first finishes. The dialog lists each batch
with a result row per photo (written, failed with the reason, or cancelled)
and a Cancel button; cancelling a running batch stops after the photo being
rendered and keeps what was written. The queue is saved to
`~/.photo-room/export_queue.json` after every photo; if the editor restarts
mid-batch, that batch shows as *interrupted* and **Resume** re-queues only the
photos not yet done (a photo that was mid-render may leave a partial file,
and the retry then gets a `-1` name). `GET /api/export/jobs`,
`POST /api/export/cancel/<id>`, `POST /api/export/resume/<id>`.

**16-bit PNG.** `--bit-depth 16` on the CLI, or the dialog's PNG bit depth.
Raw files are decoded by LibRaw at 16 bits and the edit stack runs in
floating point, so the output keeps the extra precision (smooth gradients
after heavy edits). Limits: PNG only (JPEG is 8-bit by definition); ordinary
8-bit sources (JPEG/TIFF) are written as 16-bit but gain no real detail; and
the file is larger. The metadata copy and embedded ICC profile work the same.

Virtual copies are exported too (see Collections, virtual copies, Compare and Survey).

## What the automatic adjustments do

| Setting | Basis |
|---|---|
| Exposure | Shifts median luminance toward 0.40, quantised to ¼ EV, clamped to ±2 EV |
| Shadows | Lifts when the 10th percentile is crushed |
| Highlights | Pulls back when the 90th percentile is near clipping |
| Temp / Tint | Derived from the camera's white balance multipliers |
| Noise reduction | Scales with ISO — none below 800, up to 60 at ISO 6400+ |

Once you have edited enough photos, a learned correction is applied on
top of these — see "Learning your style" above.

Conversion handles twelve files at once by default. Measured on a 14-core
machine, 30 frames read cold from an external drive took 54s serial and
23s parallel — about 2.4x. A warm-cache benchmark suggested 6x; disk and
DNG Converter contention account for the difference.

Tone statistics are taken from a strided sample of the half-size render
rather than every pixel; at stride 4 that is ~20x faster and moves the
resulting values by well under one unit.

On Canon bodies the green channels are usually equal, so the automatic
tint is normally 0.

## Metadata

All EXIF written by the camera survives conversion — make, model, lens,
serial numbers, date, ISO, exposure, aperture, focal length, GPS.

When Adobe DNG Converter is used (macOS/Windows), conversion produces mosaic
DNGs rather than linear ones. The sensor data
is identical; a linear DNG stores it demosaiced at roughly twice the size
(130MB against 60MB for a 5D Mark IV frame, of which 33MB is the embedded
original either way).

Three fields are rewritten by Adobe DNG Converter itself, not by this tool
(so only on that path):
`ShutterSpeedValue` and `ApertureValue` are re-derived as rounded APEX
values (the exact `ExposureTime` and `FNumber` are preserved), and
`FocalPlaneXResolution` changes because the output is a linear DNG.

## Develop analysis API

Backend for the Develop panel's one-click helpers (UX review proposals 3, 4 and 7). The maths is in
`analysis.py` and `tile_server.py`, the routes in `develop_api.py`; the Develop panel's
Auto tone, Auto, WB preset, Pick, Clipping and Zoom controls call them (see "Develop helpers"). All POST routes take a JSON body like `/api/preview` (`path` is a library path
or a virtual-copy id, `adjustments` the settings on screen; with no `adjustments` the photo's saved
settings are used) and need the usual session token. They work in the same linear-light maths as the
preview and export (`docs/COLOUR-PIPELINE.md`), so what they propose is what renders. Edits made under
the old colour maths (`edit_version` 1) are refused with 409 until upgraded.

| Route | Purpose |
|---|---|
| `POST /api/develop/auto_tone` | Lightroom's **Auto**: returns `settings` (Exposure, Contrast, Highlights, Shadows, Whites, Blacks, Vibrance), the full `adjustments` with them applied, `label: "Auto Tone"`, and before/after statistics. Post `adjustments` and `label` to `/api/history/record` for one undoable step. Deterministic, a few tens of ms on a preview. Aims for a median near mid-grey and at most 0.5 % of pixels clipped |
| `POST /api/develop/auto_wb` | Auto white balance: `color_temp`, `tint`, `confidence`. Weighted grey-world (clipped, near-black, strongly coloured and skin-tone pixels count less) blended with a highlight/specular estimate when the two agree |
| `GET/POST /api/develop/wb_presets` | As Shot, Auto, Daylight 5500/+10, Cloudy 6500/+10, Shade 7500/+10, Tungsten 2850/0, Fluorescent 3800/+21, Flash 5500/0 as Temp/Tint, plus `baseline_temp` |
| `POST /api/develop/wb_pick` | White-balance eyedropper. `x`, `y` are 0..1 fractions of the displayed (cropped) preview, `radius` the sample radius in preview pixels (default 3, max 50). Returns `color_temp`, `tint`, the sampled `rgb` as displayed now (0-255, for the loupe readout), `rgb_before_wb` and `rgb_after`. 422 when the area is too dark |
| `POST /api/develop/clipping` | Clipping overlay of the rendered preview: shadows blue, highlights red. `shadow` / `highlight` thresholds (0..1 or 0..255, default 2 and 253). `format`: `json` (PNG data URI plus `percent`: overall and per channel), `png` (raw RGBA overlay, percentages in `X-Clipping-*` headers) or `bitmask` (adds `np.packbits` rows) |
| `POST /api/develop/histogram` | 256-bin (or 128, 64, ...) R, G, B and luminance histograms of the preview with the clipping percentages; `stride` 2 by default |
| `GET /api/tile` | One 512 px tile of the fully rendered photo: `photo`, `z` (`fit`, `50`, `100`, `200`), `x`, `y`, and the settings as `settings` (JSON) or `sid`; `format` jpeg (default) or png. Edge tiles are cropped to the image. Cached in a byte-bounded LRU keyed on photo, settings hash, zoom and tile |
| `GET/POST /api/tile/meta` | Source and output size, per-zoom size and tile grid, and a `settings_id` to pass as `sid` (avoids long URLs) |

**Tiles.** Without straighten, transform, lens, spots or masks a tile is made by rendering the tile plus
a halo (three times the largest local filter radius) through the normal pipeline and cutting the tile
out; clarity, texture, dehaze and sharpening use the radii of the whole frame, so seams are invisible
(`tests/test_tiles.py` stitches tiles and compares with a one-piece render). Settings that work in
whole-frame coordinates render the frame once per zoom and slice it; that path is refused above 12 MP
so a 200 % view cannot exhaust a Pi. Sizes follow `geometry.py` (inscribed rectangle, then crop).

**As-shot temperature.** The reading was `5500 / (R/B)`, which runs the wrong way round and showed
about 3100 K on a daylight frame. It now takes the white the camera balanced for through the camera's
colour matrix to XYZ and reads its correlated colour temperature (McCamy), as Adobe does: the CC0 CR2
sample reads about 5900 K and LibRaw's own daylight multipliers read 6500 K. Edits saved with an absolute
Temp against the old baseline keep their number but now move against the new baseline.

## Tests

```bash
uv run pytest
```

Runs on every push and pull request via GitHub Actions.

Most of the suite fakes rawpy, exiftool, and the DNG Converter, so it needs
no camera files. The export tests (`tests/test_export.py`) and the
real-exiftool tests below use the real `exiftool` and are skipped if it is not
installed; CI installs it on both ubuntu and macOS. The image-math tests assert
direction and invariants rather than exact pixel values, since the preview
approximates Camera Raw rather than reproducing it.

### Real-exiftool tests

Tests marked `exiftool` run the real `exiftool` binary instead of the in-process
double (they skip if it is not installed). CI installs it (apt on ubuntu, brew on
macOS) and runs them explicitly as `uv run pytest -m exiftool`.

```bash
uv run pytest -m exiftool
```

- `tests/test_real_exiftool_xmp.py` pushes **every** develop setting the app
  writes (basic, tone curves, parametric, HSL, colour grading, masks, retouch
  spots, crop/straighten, transform, lens, rating/label/keywords) through the
  sidecar writer and reads it back with exiftool. It asserts the values, that
  the XML is valid with the `crs:` namespace, that every requested tag really
  exists in the file (exiftool only *warns* about unknown tags and drops them),
  and that exiftool reports no warning or error. The tone curve is compared to
  the Camera Raw format: `<crs:ToneCurvePV2012><rdf:Seq>` of `<rdf:li>x, y</rdf:li>`
  with whole numbers, as Lightroom writes it.
- `tests/test_real_exiftool_export.py` builds a synthetic source with rich EXIF
  (camera, lens, exposure, GPS), IPTC (keywords, caption, copyright, UTF-8
  text), an embedded ICC profile and a sidecar, exports it to JPEG and PNG
  (8 and 16 bit, built-in presets, sharpening, watermark, resize) and asserts
  that every EXIF/GPS/IPTC/XMP tag is still on the output, that PNGs carry
  `eXIf`, `iCCP` and XMP chunks, and that exiftool validates the result.
  Tags that legitimately change are listed with the reason in
  `EXPECTED_TO_CHANGE`: pixel dimensions, `Orientation` (applied to the pixels,
  then normalised to 1), the thumbnail/preview of the unedited frame, and
  container bookkeeping. Develop settings (`XMP-crs`) are deliberately not
  copied because the edits are baked into the pixels.
- `tests/test_real_raw_e2e.py` (marker `realraw`, **skipped by default**) runs a
  real Canon CR2 end to end: rawpy decode, edit, rating/label/keywords, export,
  then compares every tag including Canon MakerNotes. The sample is
  `IMG_9406.CR2` (Canon EOS 1000D, ~10 MB) from
  [raw.pixls.us](https://raw.pixls.us/), whose samples are all released by their
  uploaders under CC0 1.0. It is downloaded to `~/.cache/photo-room-test-raw`
  (never committed), checked against a pinned SHA-256, and the raw is asserted
  to be byte-identical afterwards.

  ```bash
  PHOTO_ROOM_REAL_RAW=1 uv run pytest -m realraw       # download once, then run
  PHOTO_ROOM_RAW_SAMPLE=/path/to/any.CR2 uv run pytest -m realraw
  ```

Not covered by any test: Adobe DNG Converter output (macOS/Windows only), and
whether Lightroom itself renders the sidecar identically (no Adobe software is
available on CI).

### Browser tests

`tests/ui/` drives the editor in a real Chromium through Playwright: page
load with no console errors, every slider, the tone curve canvas, the HSL
tabs and colour wheels, revert, and the crop overlay. The panel layout is
checked at 1440x900 and 1280x720 (and the Export dialog down to 1024x480),
and there are tests for every Develop helper, zoom and pan, the live Library
grid, Quick look and import. They are marked `ui`
and skipped by the default `pytest` run, which stays fast.

```bash
uv sync
uv run playwright install chromium     # once; add --with-deps on a bare Linux box
uv run pytest -m ui
```

The tests start the Flask editor on a free port against a tiny synthetic
library (rawpy and exiftool are faked in-process, as in the unit suite), so
no photos or exiftool are needed. If Chromium is not installed they skip
rather than fail. Screenshots of the editor land in `tests/ui/artifacts/`
(git-ignored); CI runs these in a separate `ui` job and uploads them as the
`ui-screenshots` artifact.

## Large libraries

Tested against a 665-photograph library on an external drive: the file
list returns in 24ms, and the ratings scan — one exiftool pass over every
file — takes about 4 seconds. The library appears immediately and ratings
paint in when they arrive, rather than the list waiting on them. Thumbnails
are built by background threads and cached on disk (see "Browsing a shoot").

## Performance and memory

Slider drags are meant to feel immediate on a Raspberry Pi 5. Four things
keep the preview quick:

- **Decoded base per photo.** The half-size linear render of each photograph
  is held in memory (the eight most recent photos, roughly 38MB each, so about
  300MB) and every slider move composites from it instead of re-reading the raw.
- **Stage cache.** The preview is built in stages (geometry, tone, saturation,
  colour mixer, grading, detail, then masks and strength). Each stage's output
  is cached under a key made from the settings of every stage up to it, so
  moving the Clarity slider reuses everything upstream and only redoes Detail,
  and dragging an HSL slider reuses the colour analysis of the pixels as well.
  The cache is capped at 160MB and evicts the least recently used stage.
- **Drag render.** While a slider is moving, the editor renders a smaller
  preview (800px on the long edge) and sends a sharp full-size one about
  220 ms after you stop. Photos with spot removal or lens corrections always
  render full-size, since those work at the true scale of the frame.
- **Stale requests are skipped.** Each request carries a sequence number; the
  browser cancels the one in flight and the server drops any request that a
  newer one has overtaken, even partway through the stages. Renders run one at
  a time.

`uv run python scripts/bench_preview.py` times each control's drag on a
synthetic 1400px photo (it is not part of the test run).

## Running as a local server

The editor listens on `127.0.0.1` only, so nothing on the network can
reach it. It has no password: anyone with an account on the same machine
can use it while it is running.

Every request, including `GET`s for thumbnails and previews, must carry a
`Host` of `127.0.0.1`, `localhost` or `[::1]` (on the port the editor was
started on), and any `Origin` must be the same. Anything else gets a 403.
This blocks DNS rebinding, where an attacker's hostname is re-resolved to
`127.0.0.1` so that their page is "same-origin" and could read your photos
and change anything. Requests that change anything also need a random
per-process session token, which the editor page carries and which other
sites cannot read; scripts must read it from the `photoroom-token` meta tag
of `/` and send it as `X-PhotoRoom-Token`. Every route also requires a JSON
content type (a cross-origin `text/plain` form POST skips the CORS
preflight), and request bodies are capped at 8 MB.

### Data-safety notes

- Originals (raws and DNGs) are never rewritten by default; see Metadata.
- Sidecars (`.xmp`), edit history, masks, catalog, flags, presets and the
  export queue are written to a temporary file, fsynced and renamed into
  place, and read-modify-write sequences hold a per-file lock, so concurrent
  requests (key repeat, two tabs, batch plus manual edit) cannot corrupt or
  wipe them. A history file that cannot be parsed is kept as `.bad`.
- Export destinations are normalised (`..` and symlinks resolved). Exporting
  into the library folder is refused unless the request sets `allow_library`;
  relative destinations go under `~/Pictures/photo-room-export`. Output
  files are created exclusively and never overwrite or follow a symlink; a
  taken name gets `-1`, `-2`. `overwrite: true` replaces regular files only.
- Filename templates accept only `{name} {date} {seq} {width} {height}`
  with small width specs like `{seq:04d}`.
- Uploader tokens are written 0600 in a 0700 folder via an atomic rename.
- exiftool, LibRaw and Pillow parse whatever is on a card. Keep them up to
  date (exiftool 12.24 or newer; older releases have a known RCE) and do not
  ingest cards from sources you do not trust.

## Known issues

exiftool has been observed returning success while silently dropping
`XMP-crs:ColorTemperature` and `WhiteBalance`. It was reproducible three
times in a row and then stopped; the root cause is not established. Both
write paths now read the tag back and raise if it did not land, rather
than trusting the exit code.

The preview is an approximation of Camera Raw, useful for judging an edit
but not byte-exact to what Lightroom will render.

The automatic adjustments produce confident-looking values for frames that
are entirely blown out, where no correction can recover anything. The
editor's quality readout and `process.py cull` flag those frames, but the
adjustments themselves still do not hold back.

A Google Photos upload that fails partway through the byte-upload stage
raises, discarding the tokens for files that already transferred in that
run. Uploads also read each file fully into memory.

Google's `mediaItems.batchCreate` answers `207 MULTI-STATUS` when only
some items were created. Those per-item rejections are now named with the
reason the API gave, rather than silently lowering the success count.

The `photoslibrary.appendonly` scope this uses survived the April 2025
scope removals; `photoslibrary.readonly`, `.sharing` and the full
`photoslibrary` scope did not, and none of them are used here.

Saving uses `exiftool -overwrite_original`, so no `.dng_original` backup
is kept. The embedded raw is the safety net.

## License

MIT. See [LICENSE](LICENSE).
