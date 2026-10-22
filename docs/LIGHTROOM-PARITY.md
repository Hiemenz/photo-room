# Lightroom Classic parity matrix and gap list

How photo-room compares with Adobe Lightroom Classic today, for one user: a
photographer on Linux or macOS who pulls CR2 files off a card, culls and edits
them, and exports JPEG, PNG, TIFF or WebP with all the original metadata.

Everything under "status" was checked against the code at commit `d0163ba`
(main after PR #2). "Evidence" names the module, route or test that shows it.
Where the README and the code disagree, the code wins and the disagreement is
listed in [Findings](#findings-where-the-docs-and-the-code-disagree).
The Lightroom side is described from general knowledge of Lightroom Classic
(through the 14.x/15.x releases); Adobe's help pages were not re-fetched, so
treat the Lightroom column as "believed current", not "re-verified today".

How the checks were done: read the modules, README, ROADMAP and `static/js`;
grepped for every "Missing" claim (a feature is Missing only if no code
mentions it); ran the full suite (`uv run pytest`, green on the Pi), the
real-CR2 end-to-end test (`PHOTO_ROOM_REAL_RAW=1 uv run pytest -m realraw`,
passes, 32 s), and a few probes quoted in the findings. Nothing was run on
macOS and nothing was opened in Lightroom; see [KNOWN-LIMITS.md](KNOWN-LIMITS.md).

## Legend

- **Status**: Done = works end to end and has tests; Partial = exists but
  narrower than Lightroom, or with a stated defect; Missing = no code;
  Won't do = deliberately out of scope (ROADMAP "Not planned") or not
  buildable honestly.
- **Effort**: S = under a day for one agent, M = a few days, L = a week or more.
- **Value** for this user (card, cull, edit, export with metadata): H = changes
  the daily workflow or the quality of the output, M = noticeable, L = rarely
  missed. A dash means the row is Done.

## Findings where the docs and the code disagree

1. ~~The Noise reduction slider changes nothing.~~ **Fixed.** Luminance and colour noise
   reduction now run in the render pipeline (`render_kernels.denoise_stage`) for preview, tiles
   and export; `tests/test_noise_reduction_pixels.py` checks pixel effect, cache keys and XMP.
2. **`photo-room ingest cards` and `ingest watch` scanned only `/Volumes`.** Fixed: they now
   scan `/Volumes` on macOS and `/media/$USER`, `/run/media/$USER` and `/mnt` on Linux
   (`ingest.volume_roots()`), like `watch-cards`.
3. **Card import brings in files the Library cannot show.** `cardwatch.py`
   imports raws, JPEG/HEIC and video by default (`PHOTO_SUFFIXES`,
   `VIDEO_SUFFIXES`); `libindex.RAW_SUFFIXES` lists raws and DNGs only. A
   RAW+JPEG card therefore leaves JPEGs and MP4s in the date folders that no
   grid, filter or export will ever list.
4. **(Resolved) The Library filter backend was ahead of its UI.** `library.filter_items`
   and `SORT_KEYS` support lens and ISO filters and sorting; `#lib-bar` in
   `templates/editor.html` has no lens or ISO control. Smart-collection rules
   (`catalog.RULE_OPS`) cover rating, flag, label, keyword, camera and date only.
5. **Stray tracked files:** `x.xmp` (an empty `<x:xmpmeta/>` stub) and two
   `.photo-room-thumbs/*.jpg` files are committed in the repo root. They look
   like test leftovers. Not removed here (this change is docs only).
6. **README text that is out of date** (small fixes included in this PR): the
   quick-start list under "Then in the browser" started at step 4, and the
   Known issues paragraph saying nothing flags blown-out frames is stale
   because `process.py cull` and Smart cull both do.
7. **ROADMAP "Not wired yet":** navigator mini-view and smart previews feeding
   the Develop render (they are 8-bit 2048 px decodes; Develop decodes linear
   16-bit). Sidecars, the Import dialog and importing a card from the editor
   are now done. None of the five already-queued items (timelapse,
   Lightroom .xmp/.lrcat migration, semantic search, HDR/panorama, LAN
   culling) has any code or doc mention yet (`grep -i timelapse|semantic|
   panorama|lrcat` finds nothing).

Verified and fine (checked because it looked risky): the white-balance slider
is written by exiftool as `crs:Temperature` (the tag `ColorTemperature` maps
to that XMP property), and a hand-written Lightroom-style sidecar using
`crs:Temperature="4300"` reads back as `color_temp 4300`. Full-size export of
the CC0 Canon 1000D CR2 (10 MP) on the Pi 5: JPEG 2.6 s wall, 16-bit PNG 7.9 s,
with Make, Model, LensModel, ISO and ExposureTime intact.

## Matrix

### 1. Library

| Lightroom feature | Status | Evidence | Effort | Value |
|---|---|---|---|---|
| Import from card (copy, verify, no overwrite) | Done | `cardwatch.import_card`, `ingest.copy_from_card`, `import_api.py`; SHA-256 verify, temp name then no-overwrite rename, content dedupe. `tests/test_ingest.py`, `tests/test_cardwatch.py`, `tests/test_import_api.py`. Fake cards only | - | - |
| Auto-detect card, safe eject, optional delete-after-verify | Done (unproven on hardware) | `photo-room watch-cards` (`cardwatch.py`: `/proc/mounts`, udisksctl/umount/diskutil). Fake cards and fake eject commands only | - | - |
| Card detection in `ingest cards/watch` | Partial | macOS only, `ingest.list_volumes` (finding 2) | S | H |
| Import dialog (choose frames, destination, options) | Done | `import_api.py` (`/api/import/*`), `static/js/import-dialog.js`: card or folder source, thumbnail grid with checkboxes, select all/none, hide already-imported, one background verified job. `tests/test_import_dialog.py` | - | - |
| Import: rename templates | Done | `importkit.render_name`: `{date} {yyyy} {mm} {dd} {time} {seq[:N]} {camera} {original}`; dialog, `watch-cards --rename`, `ingest --rename` | - | - |
| Import: destination organisation | Done | folder under the library + `date`/`month`/`flat`/template (`{yyyy} {mm} {dd} {date} {camera}`) in the dialog, `watch-cards`, `ingest` | - | - |
| Import: apply preset, keywords, copyright on import | Done | Rating, keywords, copyright to the sidecar (`importkit.write_metadata`) and a preset (dialog only, `import_api._preset_hook`); `watch-cards --rating --keywords --copyright`. Learned-style auto edit remains `ingest run` only | - | - |
| Import: second backup copy | Done | `--backup` / dialog: verified at copy and in the verify pass, mirrors sub-folders, blocks eject on failure. Not tried on a real second disk | - | - |
| Import: skip duplicates | Done | content hash across the whole library (`cardwatch.LibraryIndex`, `import_api`) | - | - |
| Import: XMP sidecars come along with the raw | Done | `ingest.copy_sidecar`: renamed with the raw, never overwrites; to the backup as well | - | - |
| Import: JPEG, TIFF, PNG, HEIC, video in the Library | Partial | Listed; JPEG/TIFF/PNG (HEIC with `pillow-heif`) are editable via `imgload.py`, sidecar + export work; video is a placeholder. 8-bit source, relative WB, no lens profile | S | M |
| Tethered capture | Won't do | ROADMAP "Not planned" (gphoto2 would be the route) | L | L |
| Watched-folder auto import | Partial | `process.py watch` converts as files arrive; `watch-cards` for cards | - | - |
| Grid view with thumbnails | Done | `libindex.py` paged API (200/page), embedded-JPEG thumbs (`thumbs.py`), background builder, live updates over SSE. `tests/test_libindex.py`, `tests/ui/test_library_live_ui.py` | - | - |
| Grid cell size / density / badge options | Missing | fixed cell size; badges for stars, flag, label, virtual copy | S | L |
| Loupe / Quick look | Partial | `Space` shows a 2048 px smart preview (`smartpreview.py`); Develop has Fit/Fill/100/200% zoom (`tile_server.py`). No info overlay (the Navigator is in the Develop panel) | S | M |
| Compare and Survey | Done | `compare-survey.js`; `c`, `n`. `tests/test_collections.py` | - | - |
| Star ratings, pick/reject, colour labels | Done | `XMP-xmp:Rating` (-1 reject), `XMP-xmp:Label`; pick is catalog-only (not a Lightroom field). `tests/test_ratings.py`, `tests/test_library.py` | - | - |
| Stacks | Done | `catalog.py`, `stacks_api.py`, `static/js/stacks.js`; manual `Ctrl+G` / `Ctrl+Shift+G` / `S`, auto-stack from Smart cull burst groups, collapse with count badge. Catalog-only: Lightroom keeps stacks in its own catalog, not XMP, so they do not round-trip. `tests/test_stacks.py` | - | - |
| Virtual copies | Done | `catalog.py`, `/api/virtual/*`; own edits/rating/keywords, exported as `<stem>-copyN`. Catalog-only: Lightroom cannot see them | - | - |
| Collections | Done | `catalog.py`, `/api/collections/*` | - | - |
| Smart collections | Partial | rating, flag, label, keyword, camera, date, lens, ISO, focal length, edit state, file type (`catalog.RULE_OPS`; rule builder in the smart-collection dialog); no GPS or exposure-program rules | S | M |
| Collection sets, Quick/Target collection | Missing | flat list of collections | S | L |
| Keywording (add/remove, autocomplete) | Done | comma list on the selection, `XMP-dc:Subject`, datalist (`/api/keywords`); Keyword List panel in the Library sidebar with per-keyword counts, click to filter, `+`/double-click/drag to tag the selection, rename (merge on collision) and delete across photos and sidecars (`kwapi.py`, `keywordlib.py`, `static/js/keyword-panel.js`). Sidecars rewritten in the background, not browser-verified in Lightroom | - | - |
| Keyword hierarchy, synonyms, export flags | Partial | `XMP-lr:HierarchicalSubject` written next to `dc:Subject` and read back; parent > child tree with subtree counts; synonyms and "include on export" stored in `.photo-room-keywords.json` and honoured by the export job (hidden keywords and their paths are stripped from the output). Names are unique library-wide (a keyword cannot sit under two parents); synonyms are not written to XMP nor searched; the flag does not apply to the CLI `export` or to Lightroom itself | S | L |
| Metadata panel (read EXIF/IPTC) | Done | read-only readout of camera, lens, ISO, shutter, aperture, focal length, capture time, GPS (text, no map) and dimensions via `exiftool -j -n`; Develop side panel and right of the Library grid. `metadata.py`, `metadata_api.py`, `static/js/metadata.js`; `tests/test_metadata.py`, `tests/test_real_exiftool_metadata.py`, `tests/ui/test_metadata_ui.py`. IPTC location fields are not shown | - | - |
| Metadata editing (title, caption, creator, copyright, location) | Partial | title, caption, creator, copyright, copyright status and usage terms for the selection, written to the `.xmp` sidecar (`XMP-dc:Title/Description/Creator/Rights`, `XMP-xmpRights:Marked/UsageTerms`), raw never opened, undo via the previous sidecar values, all-or-nothing batches; copyright-only export carries them. Not written: IPTC location, contact, rights URL, and nothing goes into IPTC/EXIF blocks. Same tests as above | M | M |
| Metadata presets | Done | named sets of the fields above, saved, applied (only the filled fields are written) and deleted from the panel; `.photo-room-metadata-presets.json` in the library root. Not applied automatically on import or export | - | - |
| Library filter bar | Partial | rating, flag, label, keyword, camera, lens, ISO range, focal length range, file type, edited/unedited, GPS present, date range, free text, sort by 8 keys (`library.filter_items`, `#lib-bar`); "edited" means the photo or its sidecar carries Camera Raw develop settings | S | M |
| Folders panel (tree, move, rename, delete) | Partial | Library sidebar: tree with counts, click to filter (+ subfolders toggle), new folder, drag photos onto a folder to move, rename folders. A photo moves with its `.xmp`, same-stem raw/JPEG, `*.photoroom.json`, `*.masks.json` and virtual-copy files; catalog (copies, collections), picks and cull DB follow; never overwrites; one-step undo log `.photo-room-moves.json`. `folders.py`, `folders_api.py`, `tests/test_folders.py`. No delete (by design), no move-folder UI (API only), no reveal | S | M |
| Quick Develop | Done | Library strip with relative step buttons (small and large steps for exposure, contrast, highlights, shadows, whites, blacks, temp, tint, vibrance, saturation, clarity, texture, dehaze) and Undo: `static/js/quick-develop.js`, `/api/sync_settings` and `/api/sync_undo` (`quick_api.py`). One History step per photo; framing, spots and masks untouched; absolute copy still via `/api/batch_apply`. `tests/test_quick_sync.py`. Not browser-verified | - | - |
| Sync / copy-paste settings | Done | `Mod+C/V`, WB re-anchored per camera; spots and crop never copied. `tests/test_batch.py` | - | - |
| Auto Sync (edit many at once) | Done | Develop switch: with other photos selected, each slider release is sent to them via `/api/sync_settings` (WB re-anchored per camera, crop/straighten/spots/masks excluded, one History step per photo, undoable via `/api/sync_undo`). Slider release only (not curve, masks, HSL wheels). `tests/test_quick_sync.py`; UI not browser-verified | - | - |
| Painter tool | Missing | no code | S | L |
| Catalog and backup | Partial | no catalogue: state is JSON/SQLite beside the photos (`.photo-room-library.json`, `.photo-room-catalog.json`, `*.photoroom.json`, `.photo-room-cull.sqlite`). Atomic writes, `.bad` quarantine; `photo-room backup`/`restore` (timestamped folder or zip, keep last N, optional hash-verified raw mirror, reminder/auto on editor start) | S | M |
| Smart previews (offline) | Partial | 2048 px 8-bit decodes for Quick look only; Develop needs the raw | M | L |
| Smart cull / Assisted Culling | Done | `cull.py`, `cull_api.py`, `docs/AUTO-CULL.md`; burst groups, scores, learned taste, undoable apply. `tests/test_cull*.py` | - | - |
| Map / GPS | Missing | GPS is preserved on export, never shown | M | L |
| Faces / People | Missing | Haar faces are used for cull scoring only | L | L |
| Publish services | Won't do | `uploader.py` (Google/Amazon) is CLI-only with no UI, and the Amazon path is a stub | L | L |
| Lightroom catalog (.lrcat) import | Partial (unverified) | `lrimport.py`, `photo-room import-lightroom`, Import > Lightroom catalog (dry-run report first): rating, pick/reject, colour label, keywords (+ `lr:hierarchicalSubject`), manual collections (sets flattened), virtual copies, basic develop settings to `.xmp`; smart collections, stacks, curves, masks reported not converted. Synthetic-catalog tests only, never run on a real `.lrcat` | S to verify | H |

### 2. Develop: basic, tone, presence

| Lightroom feature | Status | Evidence | Effort | Value |
|---|---|---|---|---|
| Exposure, Contrast, Highlights, Shadows, Whites, Blacks | Done (approximation) | linear-light kernels, `render_kernels.py`; differences from PV2012 listed in `docs/COLOUR-PIPELINE.md`. `tests/test_linear_light.py` | - | - |
| Temp / Tint, As Shot, presets, eyedropper, Auto WB | Done | `develop_api.py` `/wb_presets`, `/wb_pick`, `/auto_wb`; as-shot Kelvin from the camera matrix | - | - |
| Auto tone | Done | `/api/develop/auto_tone`, one History step. `tests/test_develop_api.py` | - | - |
| Texture, Clarity, Dehaze | Done | `editor._apply_detail`; `tests/test_detail.py` | - | - |
| Vibrance, Saturation | Done | `editor._stage_saturation` | - | - |
| Histogram, clipping overlay (J), RGB readout | Done | `/api/develop/histogram`, `/clipping`, `/wb_pick` | - | - |
| Zoom/pan, Fit/Fill/100/200% | Done | `/api/tile`, `tests/test_tiles.py`, `tests/ui/test_zoom_ui.py` | - | - |
| Navigator panel | Done | Top of the Develop side panel (`navigator.js`): whole-photo preview with the visible-region rectangle; click or drag pans; first click while at Fit zooms to 100%; hover shows Fit / Fill / 100% / 200% presets | S | M |
| Profile browser (Adobe Color, Landscape, Portrait...) | Partial | Develop > Profile: 7 built-in photo-room profiles (Standard, Neutral, Landscape, Portrait, Vivid, Flat, Monochrome) as a curve + saturation + hue-band layer after the camera-matrix conversion; written to `crs:CameraProfile`. They are not Adobe's profiles (no Adobe Color/Vivid/Standard look). `profiles.py`, `tests/test_profiles.py` | M (Adobe look parity) | H |
| DCP / ICC camera profile import | Partial | `.dcp` HueSatMap, LookTable and ProfileToneCurve applied; ColorMatrix/ForwardMatrix parsed but not applied (image is already camera-matrix converted); matrix/TRC ICC input profiles re-interpret the colours; A2B ICC refused. Tested on synthetic profiles only, not verified against real Adobe DCPs. `profiles.py` | M | M |
| Creative profiles / LUT looks | Done | `.cube` import + 30 built-in presets (`lut.py`, `presetlib.py`, `looks_api.py`). `tests/test_lut.py`, `tests/test_looks_*.py` | - | - |
| Match reference look | Done (beyond Lightroom) | `matchlook.py` | - | - |
| Black and white, B&W mixer | Done | `colormix.py`, `tests/test_colormix.py` | - | - |

### 3. Develop: tone curve, colour, grading

| Lightroom feature | Status | Evidence | Effort | Value |
|---|---|---|---|---|
| Parametric curve | Done | `tonecurve.py`, `crs:Parametric*`; `tests/test_tone_curve.py` | - | - |
| Point curve RGB + per channel | Done | monotone cubic, `crs:ToneCurvePV2012*`; checked with real exiftool in `tests/test_real_exiftool_xmp.py` | - | - |
| Targeted adjustment tool (on-image drag) | Missing | no on-image tool for curve or HSL | S | L |
| HSL: 8 bands (hue, sat, luminance) | Done | `colormix.py` | - | - |
| Point Color | Missing | no code | M | L |
| Colour grading (3 wheels + global, blending, balance) | Done | `colormix.py`, `crs:ColorGrade*` | - | - |

### 4. Develop: detail

| Lightroom feature | Status | Evidence | Effort | Value |
|---|---|---|---|---|
| Sharpening amount | Partial | one `sharpness` 0-150, luminance-only unsharp; no Radius, Detail, Masking | S | H |
| Luminance noise reduction | Partial | `render_kernels.denoise_stage` (bilateral on luma), preview, tiles and export; no Detail/Contrast | S | M |
| Colour noise reduction | Done | `color_nr` slider, edge-guarded chroma blur, `crs:ColorNoiseReduction` | - | - |
| Output sharpening | Done | `export_finish.sharpen`, screen/matte/glossy x low/standard/high | - | - |

### 5. Develop: lens, geometry, crop

| Lightroom feature | Status | Evidence | Effort | Value |
|---|---|---|---|---|
| Lens profile correction | Partial (optional extra, unverified here) | `lens.py` via `lensfunpy` (`uv sync --extra lens`); not installed on this Pi, so the profile path was not run. `doctor` reports it as WARN. `tests/test_m8.py` | S to verify | M |
| Manual distortion, vignette fix, CA removal, defringe | Done | `lens.py` | - | - |
| Transform (vertical, horizontal, rotate, aspect, scale, offset) | Done | `transform.py`, one homography, auto-crop to clean rectangle | - | - |
| Upright (Auto, Level, Vertical, Full) | Partial | OpenCV Canny + Hough, `/api/upright`; no Guided Upright | M | L |
| Crop: free + 1:1, 4:3, 3:2, 16:9, thirds, straighten | Partial | `#crop-bar`; no portrait/landscape flip, no original or custom ratio, no 90-degree rotate or flip, no other overlays | S | M |
| Spot removal: circle clone/heal | Done | `spots.py`, `retouch.py`, `crs:RetouchAreas` | - | - |
| Spot removal: brush shape, visualise spots, dust finder | Missing | circles only | M | M |
| Red eye / pet eye | Missing | no code | S | L |

### 6. Develop: local adjustments and masks

| Lightroom feature | Status | Evidence | Effort | Value |
|---|---|---|---|---|
| Graduated, radial, brush masks | Done | `masks.py`; max 16 per photo; `tests/test_masks.py`, `tests/ui/test_masks_ui.py` | - | - |
| Local sliders | Partial | 11: exposure, contrast, highlights, shadows, temp, tint, saturation, clarity, texture, dehaze, sharpness (`masks._LOCAL`). Missing whites, blacks, hue, noise, moire, defringe, curve, colour tint | M | M |
| Range masks (luminance, colour, depth) | Missing | no code | M | M |
| Combine masks (add / subtract / intersect) | Missing | masks are independent; only brush Erase subtracts | M | M |
| Mask XMP readable by Lightroom | Partial (unverified) | Camera Raw structure, round-trips through exiftool; never opened in Lightroom (README says so). Lightroom-made AI/range masks are not imported | M to verify | H |
| AI masks: subject, sky, background, people | Won't do (for now) | ROADMAP "Not planned"; open models via onnxruntime would be the route | L | H |
| Adaptive presets | Won't do | need AI masks | L | L |

### 7. Develop: effects, calibration

| Lightroom feature | Status | Evidence | Effort | Value |
|---|---|---|---|---|
| Post-crop vignette | Missing | only the lens-profile vignette correction exists | S | M |
| Grain | Missing | no code | S | M |
| Calibration (primaries, shadow tint) | Missing | no code | M | L |
| Soft proofing | Missing | no code (`PIL.ImageCms` could do it) | M | L |

### 8. Develop: history, snapshots, undo

| Lightroom feature | Status | Evidence | Effort | Value |
|---|---|---|---|---|
| History, undo/redo | Done | newest 100 steps per photo, `history.py`; stored in `*.photoroom.json`, not XMP. `tests/test_history.py` | - | - |
| Snapshots | Done | up to 50 per photo | - | - |
| Before/after: split, side by side, `\` | Done | `before-after*.js` | - | - |
| Reset to import, revert | Done | `/api/history/reset_to_import`, `/api/reset` | - | - |
| Reference view | Missing | no code | S | L |
| Edit strength (scale the whole edit) | Done (extra) | `GLOBAL_CONTROLS` | - | - |
| Learned personal style, auto-edit | Done (extra, not in Lightroom) | `style.py` (DuckDB), `docs/AUTO-CULL.md` | - | - |

### 9. Export, print, web, book, slideshow

| Lightroom feature | Status | Evidence | Effort | Value |
|---|---|---|---|---|
| Export JPEG / PNG / TIFF (8/16-bit) / WebP, full-res render from the raw | Done | `exporter.py`, `export_api.py`; `tests/test_export*.py` | - | - |
| Keep all metadata (EXIF, GPS, IPTC, XMP, ICC, MakerNotes) | Done | exiftool `-TagsFromFile -all:all`; `tests/test_real_exiftool_export.py`, `tests/test_real_raw_e2e.py` (real CR2 incl. Canon MakerNotes). Real 1000D CR2 on the Pi: Make, Model, LensModel, ISO, ExposureTime survive | - | - |
| Metadata options (copyright only, strip location, drop develop settings) | Done | `ExportOptions.metadata`, `exporter.metadata_args`, `#ex-meta`; `tests/test_export_formats_metadata.py`. Person/face tags are not stripped | - | - |
| Format: TIFF, PSD, DNG, WebP, AVIF, JPEG XL | Partial | TIFF and WebP done (`--format`, `exporter.FORMATS`); AVIF and JPEG XL work when the optional `pillow-avif-plugin` / `pillow-jxl-plugin` are installed (`uv sync --extra avif --extra jxl`; the dialog lists only installed formats, 8-bit, metadata copy is exiftool best effort); PSD, DNG missing. `tests/test_export_group_d.py` | S | L |
| 16-bit output | Done | 16-bit PNG and TIFF (`write_png16`, `write_tiff16`) | - | - |
| Colour space: sRGB, Adobe RGB, ProPhoto with embedded ICC | Done | `exporter.py`; custom ICC absent | - | - |
| Resize | Partial | long edge only, never upscales | S | M |
| Naming template | Done | `{name} {date} {seq} {width} {height} {rating} {camera} {keyword} {text}` (`--template`, `--text`, literal text option); `{keyword}` is the first keyword; values are made filename-safe; no sequence-start or custom-token editor | - | - |
| Destination | Done | chosen folder (outside the library), sub-folder of it, same folder as each original (never overwrites; `-1`, `-2`), or ask each time (`dest_mode`, `--subfolder`, `--same-as-original`) | - | - |
| Output sharpening, watermark | Done | `export_finish.py` | - | - |
| Export presets (4 built-in + user) | Done | `export_presets.py` | - | - |
| Queue, progress, cancel, resume after restart | Done | `export_queue.json`, `/api/export/jobs\|cancel\|resume` | - | - |
| Timelapse video (selection / collection / folder; deflicker, stabilise, ffmpeg) | Done | `timelapse.py`, `timelapse_api.py`, `static/js/timelapse.js`; `tests/test_timelapse.py` | - | - |
| Export with Previous | Done | last settings kept in `export_last.json`; button and Ctrl/Cmd+Alt+Shift+E in the dialog, `/api/export/previous`, CLI `export --previous` | - | - |
| Post-processing (open in app, run action) | Done | `postexport.py`: open in an app / default viewer, or run a command as an argument list (no shell, 30 s timeout, `{files}`/`{folder}`); CLI `--post-open`, `--post-command`. Results shown in the dialog | - | - |
| Contact sheet PDF | Done | `contactsheet_pdf.py`, dialog section, `/api/export/contact-sheet`, CLI `export --contact-sheet`; A4/Letter, columns, heading, file names; no print module layouts or templates | - | - |
| Print module | Won't do | ROADMAP "Not planned" | L | L |
| Web module, Book, Slideshow | Won't do | ROADMAP "Not planned" | L | L |
| Contact-sheet PDF | Missing | not the same as the grid; no code | S | L |

### 10. Sync, cloud, interop

| Lightroom feature | Status | Evidence | Effort | Value |
|---|---|---|---|---|
| Lightroom cloud / mobile sync | Won't do | local-only by design (listens on 127.0.0.1) | - | - |
| Read Lightroom / Camera Raw sidecar edits | Partial | basics, curve, HSL, grading, crop, spots, masks from a CR2 sidecar (`editor._read_saved_values`); Lightroom AI/range masks ignored. `tests/test_foreign_edits.py`, `tests/test_real_exiftool_xmp.py` | M | H |
| Write XMP that Lightroom renders the same | Partial (unverified) | tags exist and validate with real exiftool; Lightroom never opened them (finding in KNOWN-LIMITS) | M to verify | H |
| Import Lightroom presets (.xmp) | Done | `presetlib.py`, `tests/test_presetlib.py` | - | - |
| Import `.lrcat` | Partial (unverified) | `lrimport.py`, `photo-room import-lightroom`, Import > Lightroom catalog (dry-run report first): rating, pick/reject, colour label, keywords (+ `lr:hierarchicalSubject`), manual collections (sets flattened), virtual copies, basic develop settings to `.xmp`; smart collections, stacks, curves, masks reported not converted. Synthetic-catalog tests only, never run on a real `.lrcat` | S to verify | H |
| Raw formats | Partial | CR2, CR3, NEF, ARW, RAF, ORF, RW2, DNG via LibRaw; only the 1000D CR2 is exercised with a real file | S | M |
| Lightroom opens our exported JPEG with metadata | Unverified | exiftool-level checks only | S | H |

### 11. AI and merge features

| Lightroom feature | Status | Evidence | Effort | Value |
|---|---|---|---|---|
| AI Denoise | Won't do (for now) | ROADMAP "Not planned"; classical NR is the gap to close first | L | H |
| Super Resolution | Won't do | not planned | L | L |
| Generative Remove, Lens Blur | Won't do | cloud / depth AI | L | L |
| HDR merge, Panorama merge | Partial | `merge.py`, `merge_api.py`, `photo-room merge`; synthetic tests only, see [HDR-PANORAMA.md](HDR-PANORAMA.md) | M | M |
| Semantic / natural-language search | Partial (optional extra, fake-embedder tests only) | `semantic.py`, `semantic_api.py`, docs/SEMANTIC-SEARCH.md; real model unverified | L | M |

## Prioritised gap list (top 25, not yet queued)

Ordered by value over effort. Each package is sized for one agent in one PR
with tests; "Touches" names the files to start from. The status of the
Lightroom features is in the matrix above.

| # | Work package | Why (value for this user) | Effort | Touches |
|---|---|---|---|---|
| 1 | ~~**Real noise reduction.** Luma + chroma denoise from the Noise reduction slider (OpenCV, applied at full size on export and on the 100% tile; keep the half-size preview as is) and a colour-NR slider. Until then relabel the slider so it does not promise a pixel effect~~ (done) | A shipped slider that does nothing; high-ISO CR2s are the common case | M | `render_kernels.py`, `editor.py`, `tile_server.py`, `stagecache.py`, `tests/test_noise_reduction.py` |
| 2 | **Linux-aware `ingest cards` / `ingest watch`**: make `list_volumes` reuse `cardwatch.find_cards`, fix the README Card ingest section | The headline Linux flow fails at its first command | S | `ingest.py`, `cardwatch.py`, README |
| 3 | **Export metadata options**: All / all except location / copyright and contact only / none | The user's stated goal is metadata; privacy-safe sharing needs the middle options | S | `exporter.py`, `export_api.py`, `export_presets.py`, `static/js/export-dialog.js` |
| 4 | ~~**Metadata panel (read-only)**: EXIF/IPTC/GPS in a Library/Develop side panel via cached `exiftool -j`~~ (done, with editing and presets) | Nothing in the UI shows camera, lens, ISO or GPS today | S | `libapi.py`, new `static/js/metadata.js`, `templates/editor.html` |
| 5 | **Copyright / IPTC preset applied on export and import**: Creator, Copyright, Credit, caption; writes XMP/IPTC on the output (and optionally the sidecar) | Photographers stamp copyright on every file; no write path exists | M | `exporter.py`, `library.py`, export dialog |
| 6 | **Sharpening Radius / Detail / Masking** | One slider is the main Detail-panel gap | S | `render_kernels.py`, `editor.py`, `CONTROLS`, XMP map |
| 7 | **Export formats: 16-bit TIFF and WebP** (AVIF later), same metadata copy | 16-bit hand-off to other editors and print labs; JPEG/PNG only today | S | `exporter.py`, `process.py`, `test_real_exiftool_export.py` |
| 8 | **Library shows JPEG/HEIC (and hides RAW+JPEG twins)**, no longer orphans what `watch-cards` imports; videos listed but not editable | Finding 3 | M | `libindex.py`, `process.py`, `thumbs.py`, `cardwatch.py` |
| 9 | **Post-crop vignette and grain** | Standard look tools, tiny maths, seeded so preview equals export | S | `render_kernels.py`, `editor.py`, XMP map |
| 10 | **Crop completeness**: flip orientation, 90-degree rotate, original/custom ratio, ratio lock, more overlays | Common daily operations; portrait crops are awkward now | S | `static/js/develop-crop.js`, `geometry.py` |
| 11 | (Done) **Filters in the UI for lens, ISO, focal length, edit status**, plus matching smart-collection rules | Backend already filters lens/ISO (finding 4) | S | `templates/editor.html`, `static/js/ratings-filter.js`, `catalog.py` |
| 12 | **Import dialog**: choose frames, destination, rename template, keywords/copyright/preset on import, Move vs Copy | The Lightroom Import experience; ROADMAP "Not wired yet" | M | `import_api.py`, `cardwatch.py`, `static/js/import.js` |
| 13 | **Carry XMP sidecars with imported raws** (copy `IMG.xmp` beside the raw when present) | Prerequisite for any Lightroom round trip | S | `ingest.py`, `cardwatch.py`, `import_api.py` |
| 14 | (Partly done: subfolder, same-as-original, Export with Previous) **Export sizing and destination**: short edge, megapixels, percent | Daily export ergonomics | S | `exporter.py`, `export_api.py`, export dialog |
| 15 | **More local sliders**: whites, blacks, hue, noise, colour tint (reuse global kernels) | Closes most of the local-adjustment slider gap | M | `masks.py`, `editor.py` |
| 16 | **Range masks (luminance, colour) and mask combine** | The first non-geometric masking; makes brush masks reusable | M | `masks.py`, `static/js/develop-masks.js` |
| 17 | ~~**Keyword list panel with counts, hierarchy (`lr:HierarchicalSubject`)**~~ done | Keyword workflow beyond a text box | M | `keywordlib.py`, `kwapi.py`, `static/js/keyword-panel.js` |
| 18 | **DCP / camera-profile import** (Adobe-style colour and base curve): first version shipped (look tables and curve; camera matrices and two-illuminant blending still to do) | The largest reason an edit will not look like Lightroom's | L | `colour.py`, `render_kernels.py`, new `dcp.py` |
| 19 | **Folders panel with move/rename** that rewrites sidecars, `*.photoroom.json`, masks JSON and catalog paths together | Library management on disk | M | `libapi.py`, `catalog.py`, `library.py` |
| 20 | **Stacks** (auto from Smart cull burst groups) (done) | Reduces grid noise after a burst-heavy shoot | M | `catalog.py`, `cull.py`, `static/js/library-module.js` |
| 21 | **Spot removal: brush shape, visualise spots, dust finder** | Sensor dust is the common retouch job | M | `spots.py`, `retouch.py`, `develop-transform-lens-spots.js` |
| 22 | **Navigator mini-view and loupe info overlay** | Orientation while zoomed; ROADMAP "Not wired yet" | S | `zoom.js`, `library-live.js` |
| 23 | **Backup / export of library state** (catalog, flags, history, cull DB) to a zip, with restore | The "catalogue" is files beside the photos with no backup | S | new `backup.py`, `photo_room_cli.py` |
| 24 | **AI denoise (optional extra, onnxruntime)** after package 1 lands | Best-in-class NR for ISO 6400+ | L | new `denoise_ai.py`, `pyproject.toml` extra |
| 25 | **AI subject / sky masks (optional extra)** | Fastest local-edit workflow in Lightroom today | L | new `aimask.py`, `masks.py` |

Smaller items not in the top 25: Calibration panel (M), targeted adjustment
tool (S), Point Color (M), soft proofing (M), Guided Upright (M), red eye (S),
reference view (S), grid density options (S), Painter (S), collection sets (S),
Map module (M).

## Already queued (not repeated above)

| Queued item | Matrix rows it covers | Notes from this review |
|---|---|---|
| Timelapse | none (Lightroom builds time-lapse in Slideshow) | Done: `photo-room timelapse` and the Timelapse dialog. Not verified against a real raw sequence or in a browser; virtual copies in a collection render as their raw; no stack grouping yet (select the frames or use a collection) |
| Lightroom `.xmp` / `.lrcat` migration | "Read Lightroom sidecar edits", ".lrcat import", "Import Lightroom presets" | Sidecar reading and `.xmp` preset import already exist, `.lrcat` import is now built (`lrimport.py`, synthetic-catalog tests); what remains is running it on real catalogs and the unverified-in-Lightroom checks. Package 13 is a prerequisite for sidecars |
| Semantic search | "Find / filter" | Nothing exists; thumbnails (`thumbs.py`) and the SQLite pattern in `cull_store.py` are the natural base; model choice must be CPU and Pi friendly |
| HDR / panorama merge | "HDR merge", "Panorama merge" | Built (`merge.py`), writes 16-bit linear / float TIFF. Listing the TIFF in the Library depends on the non-raw image loader landing; see [HDR-PANORAMA.md](HDR-PANORAMA.md) |
| LAN culling | "Smart cull" | Server binds 127.0.0.1 with a Host/Origin guard and a per-process token (`security.py`, README "Running as a local server"); any LAN mode needs a deliberate, authenticated opt-in. Do not weaken the default |
