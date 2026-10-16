# Photo Room

Imports Canon CR2 (and other raw) files, applies automatic tone and white
balance adjustments, lets you refine them in a local web editor, and exports
PNG or JPEG with all the original metadata intact. Runs on Linux and macOS.

Every edit is stored as Camera Raw XMP develop settings. Image pixels are
never modified. Raws are decoded directly with rawpy/LibRaw, and edits live in
an `.xmp` sidecar beside each raw, so the original is never touched. If Adobe
DNG Converter is installed (macOS/Windows) it is used as an optional extra:
photos are converted to DNG with the raw embedded, and edits go inside the DNG.

## Requirements

- `exiftool` — `brew install exiftool` (macOS) or `sudo apt install libimage-exiftool-perl` (Debian/Ubuntu/Raspberry Pi OS)
- Python 3.12+ with [uv](https://docs.astral.sh/uv/)
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
uv run python editor.py ~/Pictures/dng/
# open http://127.0.0.1:5000
```

Pick a photo from the library on the left; the preview updates live as you
move the sliders.

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

A histogram sits above the controls with per-channel curves and a clipping
readout — the shadow and highlight percentages turn red past 1%.

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
`GrayMixer*`, `ColorGrade{Shadow,Midtone,Highlight,Global}{Hue,Sat,Lum}`,
`ColorGradeBlending` and `ColorGradeBalance`, and read back on open. Edit
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

After conversion a folder holds both the CR2 and its DNG. They are one
photograph, so the library lists the DNG and hides the source raw — the
DNG carries the edits and embeds the original anyway.

**Grid** turns the main pane into a contact sheet. Click a frame to open
it; `cmd`-click still selects for a batch apply.

Thumbnails come from the JPEG preview the camera already embedded in each
raw rather than from demosaicing the sensor — about five times faster, and
cached on disk keyed by file size and modification time. A 431-frame shoot
costs roughly 47 seconds the first time and is instant afterwards. Cells
load only as they scroll into view.

### Rating and culling

Press `0`–`5` to rate the open photo, `x` to reject it, or click the stars.
The dropdown above the library filters to unrated, rated, your best, or
rejects — and the arrow keys then move within whatever the filter shows, so
culling down to your picks and arrowing through does not jump back to
hidden frames.

Ratings are written as `XMP-xmp:Rating` with `-1` for rejected, which is
what Lightroom and Bridge read, so marks made here mean the same thing
wherever the photos are opened next. The whole library's ratings are read
in a single exiftool call.

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

| Key | Action |
|---|---|
| `←` `→` | Previous / next photo |
| `\` (hold or tap) | Show the as-imported "Before"; tap to latch |
| `b` | Toggle black and white |
| `cmd`+`s` | Save adjustments |
| `cmd`+`c` / `cmd`+`v` | Copy / paste settings |

### Reverting

- **Revert to original** clears every stored adjustment.
- **Recover original raw file** writes the embedded CR2 back out, byte for byte.

## Card ingest

```bash
python ingest.py cards                          # what's mounted, and what looks like a card
python ingest.py watch -o ~/Pictures/library/   # wait for a card, then ingest it
python ingest.py run /Volumes/EOS_DIGITAL -o ~/Pictures/library/

# Develop a shoot again after the learned style has improved
python ingest.py run /Volumes/EOS_DIGITAL -o ~/Pictures/library/ --no-skip
```

Insert a card and it copies every raw into the library, sorts by shoot
date, converts to DNG, and applies your learned adjustments.

**The card is read-only throughout.** Nothing is deleted or modified on
it — erase it in-camera once you are satisfied the copy is good. Each
copy is checksummed and written to a temporary name first, so an
interrupted import can never leave a truncated file that later looks
like a successful one.

**It does not upload.** Ingest stages the shoot; you review it in the
editor and upload from there. A shoot generally contains frames you
would not want published.

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

Conversion produces mosaic DNGs rather than linear ones. The sensor data
is identical; a linear DNG stores it demosaiced at roughly twice the size
(130MB against 60MB for a 5D Mark IV frame, of which 33MB is the embedded
original either way).

Three fields are rewritten by Adobe DNG Converter itself, not by this tool:
`ShutterSpeedValue` and `ApertureValue` are re-derived as rounded APEX
values (the exact `ExposureTime` and `FNumber` are preserved), and
`FocalPlaneXResolution` changes because the output is a linear DNG.

## Tests

```bash
uv run pytest
```

Runs on every push and pull request via GitHub Actions.

Most of the suite fakes rawpy, exiftool, and the DNG Converter, so it needs
no camera files. The export tests (`tests/test_export.py`) use the real
`exiftool` and are skipped if it is not installed; CI installs it on both
ubuntu and macOS. The image-math tests assert
direction and invariants rather than exact pixel values, since the preview
approximates Camera Raw rather than reproducing it.

### Browser tests

`tests/ui/` drives the editor in a real Chromium through Playwright: page
load with no console errors, every slider, the tone curve canvas, the HSL
tabs and colour wheels, revert, and the crop overlay. They are marked `ui`
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
paint in when they arrive, rather than the list waiting on them.

## Memory

The editor keeps the eight most recently opened photographs in memory —
their previews and the decoded sensor data behind them, roughly 38MB per
photograph. That keeps arrow-key browsing and before/after comparison
instant while capping the cost near 300MB. Without the bound, browsing a
431-frame shoot would have held about 16GB.

## Damaged files

A card read error or an interrupted copy produces truncated or empty
files. Convert, cull and ingest each handle them per file and carry on —
a run reports what it could not read rather than stopping. The editor
returns a readable error and stays up, and the rest of the shoot is
unaffected.

A truncated file often still shows a thumbnail, because the camera's
embedded preview sits near the start of the file and may have survived
the copy.

## Photos edited in Lightroom

A DNG exported from Lightroom or Camera Raw carries its develop settings
inside the file, in the same place this tool writes to. Overwriting them
destroys editing work that may exist nowhere else outside a catalogue.

Files carrying settings from other software are detected by `crs:Version`,
which Lightroom stamps and this tool never writes, and by the creator tags.
When one is found:

- **`revert` refuses** and explains, unless you pass `--force`.
  `revert --extract-original` is always safe — it only writes the embedded
  raw back out and changes nothing.
- **Batch apply skips it** and says how many were skipped.
- **The editor warns** when you open it, before you save over anything.

Lightroom Classic may also keep `.xmp` sidecars beside your photos. Where
one exists, this tool **reads** it in preference to the copy inside the
DNG, so opening a photo shows the edit you actually made rather than a
differing embedded copy. On a real shoot the two disagreed — exposure
+1.12 against +1.11, highlights -84 against -81, on files written in the
same minute.

By default it **writes** only into the DNG, so on a sidecar-managed photo
Lightroom will not see edits made here — and the editor says so when you
save.

Start it with `--write-sidecars` to mirror each save into the `.xmp` as
well, keeping both stores in step:

```bash
uv run python editor.py ~/Pictures/dng/ --write-sidecars
```

This is off by default deliberately. A sidecar is what Lightroom reads
back, so writing one decides which application wins — worth choosing
rather than inheriting. An existing sidecar is updated in place, so the
tone curve, lens profile and everything else Lightroom keeps there
survives untouched.

## Amazon Photos uploads do not work

`--upload-amazon` refuses and explains why. This is not a bug that can be
fixed here.

Amazon closed the Drive API to third parties in August 2019 and shut the
Drive service down entirely in December 2023. The endpoint this code
targets, `content-na.drive.amazonaws.com/cdproxy/nodes`, answers 404, and
the `clouddrive:write` scope can no longer be granted. Amazon Photos has
no public API that replaces it.

The implementation is kept as `_upload_to_amazon_photos_unreachable` so
that if Amazon ever publishes one, this becomes a change of endpoint
rather than a rewrite. Nothing calls it.

## Process version

Edits are written declaring Camera Raw process version `11.0` — Process
Version 5, what Camera Raw 11 writes for new images and the oldest that
supports every slider here, Texture and Dehaze included.

A file already declaring something newer keeps it. Lightroom Classic
currently writes `15.4` (Process Version 6), and process version selects
the rendering engine, so overwriting that with `11.0` would change how the
photograph looks rather than merely editing its metadata.

## Hung helper programs

Every call to the DNG Converter and to exiftool has a deadline — 300
seconds for a conversion, 120 for metadata. A stalled program is reported
as a failure of that one file and the rest of the run continues.

This matters because the DNG Converter is a GUI application driven from
the command line, and answers some invocations by opening a window rather
than writing to stdout. `-h` does exactly that: no output, never exits.
Unbounded, one such file would stall a 431-frame ingest with nothing on
screen to say why.

## Running as a local server

The editor listens on `127.0.0.1` only, so nothing on the network can
reach it. It has no password: anyone with an account on the same machine
can use it while it is running.

Requests that change anything are refused when they carry an `Origin`
header from another site, and every route requires a JSON content type.
Both matter because a page open in the same browser can post to a local
server: a cross-origin form POST with `Content-Type: text/plain` skips the
CORS preflight, and without these checks any site visited while the editor
was running could have rejected photographs, stripped develop settings, or
overwritten a whole shoot.

Requests carrying no `Origin` at all are allowed, so `curl` and scripts
still work — browsers attach that header to every cross-origin request.

## Known issues

exiftool has been observed returning success while silently dropping
`XMP-crs:ColorTemperature` and `WhiteBalance`. It was reproducible three
times in a row and then stopped; the root cause is not established. Both
write paths now read the tag back and raise if it did not land, rather
than trusting the exit code.

The preview is an approximation of Camera Raw, useful for judging an edit
but not byte-exact to what Lightroom will render.

The automatic adjustments produce confident-looking values for frames that
are entirely blown out, where no correction can recover anything. Nothing
flags those yet.

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
