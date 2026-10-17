# Roadmap: a Lightroom Classic-style editor

Ultimate goal (from the owner): run it on **Linux or macOS**, pull CR2 files
off a card, convert them to PNG, edit them like Lightroom, then export PNG or
JPEG with **all of the original photo metadata kept** (EXIF, GPS, IPTC, XMP,
colour profile).

Goal: a local, non-destructive raw workflow with Lightroom Classic's layout
(Library + Develop) and as much of its editing feature set as can be built and
tested honestly. It will not match Lightroom feature-for-feature (Adobe colour
profiles, AI masking and denoise, Print/Book/Web modules, tethering and cloud
sync are out of scope).

## Ground rules
- Must run on Linux and macOS with no Adobe software: CR2 is decoded with
  rawpy/LibRaw. Adobe DNG Converter (macOS/Windows only) becomes optional.
  CI runs on both ubuntu and macos.
- Metadata is never dropped: exports copy every tag from the source raw with
  exiftool (EXIF, GPS, IPTC, XMP, ICC), and a test compares tags before and after.
- Edits stay non-destructive and stored as Camera Raw XMP, so Lightroom and
  Adobe Camera Raw can still open the same files.
- Every milestone ships as its own PR with tests; CI (uv + pytest) must be green
  before merge. Nothing is checked off until its acceptance test passes.
- UI stays the existing Flask + `templates/editor.html` app. Layout follows
  Lightroom: filmstrip bottom, panels right, histogram top right, module picker top.
- Previews stay interactive: a slider drag must re-render a half-size preview
  in well under a second on the Pi 5.

## Definition of "fully working"
All milestones below checked, `uv run pytest` green, and the README documents
every control. A manual smoke test passes: import a card, cull, edit with
every panel, export JPEGs, and re-open the files in Lightroom with the edits intact.

## Milestones
- [x] M0 Basic develop (exposure, tone, WB, vibrance, clarity, texture, dehaze, sharpen, straighten, crop), ratings, presets, batch apply, ingest, style learning, upload
- [x] M1 Portable pipeline: import CR2 from a card/folder, decode with rawpy (no DNG Converter), XMP sidecar edits, export PNG/JPEG with full metadata preserved; Linux + macOS CI
- [x] M1b Export dialog basics: format (PNG/JPEG), quality, size, colour space, filename template, "keep metadata" check (CLI `process.py export` and the editor Export dialog, Ctrl/Cmd+Shift+E, with scope, progress and 16-bit PNG)
- [x] M2 Tone curve (parametric + point curve, per-channel) with XMP read/write
- [x] M3 HSL / colour mixer (8 hue bands) and colour grading (shadows/mids/highlights wheels)
- [x] M4 History panel, snapshots, before/after (side by side and split), reset-to-import
- [x] M5 Library module: grid view, flags and colour labels, keywords, metadata filter bar, search
- [x] M6 Collections and virtual copies; compare and survey views
- [x] M7 Local adjustments: graduated filter, radial filter, adjustment brush with feather and mask overlay
- [x] M8 Spot removal (clone/heal), transform (vertical/horizontal/rotate/aspect), lens profile correction via lensfun, chromatic aberration
- [x] M9 Export presets, output sharpening, watermark, batch export queue (builds on M1b)
- [x] M10 Performance and polish: preview caching, background thumbnails for large libraries, keyboard shortcuts matching Lightroom (G, D, E, R, 1-5, X, P), dark theme

## Post-1.0 enhancements
Develop panel helpers from the UX review (proposals 3, 4 and 7). The backend is done; the buttons that call it come with the UI work.
- [x] Auto tone (Exposure, Contrast, Highlights, Shadows, Whites, Blacks, Vibrance) as one undoable step: `POST /api/develop/auto_tone`
- [x] Auto white balance from grey-world plus highlights, skin-safe: `POST /api/develop/auto_wb`
- [x] As-shot Temp reads daylight as ~5000-6500 K (camera matrix to Kelvin) instead of ~3100 K
- [x] White balance presets (As Shot, Auto, Daylight, Cloudy, Shade, Tungsten, Fluorescent, Flash): `/api/develop/wb_presets`
- [x] White balance eyedropper with RGB readout: `POST /api/develop/wb_pick`
- [x] Clipping overlays (shadows blue, highlights red) with per-channel percentages: `POST /api/develop/clipping`
- [x] Tile server for zoom and 1:1 (fit, 50, 100, 200 %) with halo and LRU cache: `/api/tile`, `/api/tile/meta`
- [x] 256-bin histogram in a single cv2.calcHist pass: `POST /api/develop/histogram`
- [x] Smart cull and Auto edit, all on-device: burst grouping (perceptual hash + EXIF time), per-photo scores with reasons (focus-region sharpness, exposure/clipping, Haar faces and eyes), suggested pick/reject per burst, keyboard review with side-by-side and synced 100% compare, explicit undoable apply, one-click Auto edit as one history step, and a personal model learned from accept/override decisions and post-Auto corrections (SQLite in the library folder, with accuracy chart, export, reset and clear). See [AUTO-CULL.md](AUTO-CULL.md)
- [ ] Develop panel buttons: Auto, WB picker and preset menu, clipping toggles (J), zoom/1:1 viewer using the tile server
- [ ] Use the 256-bin histogram and tile viewer in place of the 64-bin histogram and the 1400 px preview

## Not planned
AI masking, AI denoise, generative remove, Print/Book/Slideshow/Web modules, tethered capture, cloud sync, mobile apps.
