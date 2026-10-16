# Photo Room

Converts Canon CR2 files to DNG with automatic tone and white balance
adjustments, then lets you refine those adjustments in a local web editor
and upload the results to Google Photos.

Every edit is stored as Camera Raw XMP develop settings. Image pixels are
never modified, and the source raw is embedded inside each DNG, so any
edit can be undone and the original recovered exactly.

## Requirements

- [Adobe DNG Converter](https://helpx.adobe.com/camera-raw/using/adobe-dng-converter.html) (free)
- `exiftool` — `brew install exiftool`
- Python 3.12+ with [uv](https://docs.astral.sh/uv/)

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

A histogram sits above the controls with per-channel curves and a clipping
readout — the shadow and highlight percentages turn red past 1%.

Clicking a slider's number resets that one slider. Opening a photo shows
its saved edits, or the auto-computed ones if it has none.

Noise reduction is written to the file but deliberately not shown in the
preview. The preview is a half-size render, and downsampling already hides
the noise this removes, so previewing it there would misrepresent the
full-resolution result in both directions.

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

### Presets

Save the current sliders under a name, then pick it from the dropdown to
apply it to any photo. Presets carry tone and colour only — crop and
straighten are framing decisions belonging to one photograph, so they are
never stored.

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

### Keyboard

| Key | Action |
|---|---|
| `←` `→` | Previous / next photo |
| `\` (hold) | Show the unedited original |
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

# Undo edits, or recover the embedded original
uv run python process.py revert ~/Pictures/dng/IMG_0001.dng
uv run python process.py revert ~/Pictures/dng/ --extract-original -o ~/Pictures/recovered/
```

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

The suite fakes rawpy, exiftool, and the DNG Converter, so it needs no
camera files and runs in well under a second. The image-math tests assert
direction and invariants rather than exact pixel values, since the preview
approximates Camera Raw rather than reproducing it.

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
