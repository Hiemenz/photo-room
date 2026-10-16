# Roadmap: a Lightroom Classic-style editor

Goal: a local, non-destructive raw workflow with Lightroom Classic's layout
(Library + Develop) and as much of its editing feature set as can be built and
tested honestly. It will not match Lightroom feature-for-feature (Adobe colour
profiles, AI masking and denoise, Print/Book/Web modules, tethering and cloud
sync are out of scope).

## Ground rules
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
- [ ] M1 Tone curve (parametric + point curve, per-channel) with XMP read/write
- [ ] M2 HSL / colour mixer (8 hue bands) and colour grading (shadows/mids/highlights wheels)
- [ ] M3 History panel, snapshots, before/after (side by side and split), reset-to-import
- [ ] M4 Library module: grid view, flags and colour labels, keywords, metadata filter bar, search
- [ ] M5 Collections and virtual copies; compare and survey views
- [ ] M6 Local adjustments: graduated filter, radial filter, adjustment brush with feather and mask overlay
- [ ] M7 Spot removal (clone/heal), transform (vertical/horizontal/rotate/aspect), lens profile correction via lensfun, chromatic aberration
- [ ] M8 Export module: format, size, quality, sharpening for output, filename templates, watermark, export presets
- [ ] M9 Performance and polish: preview caching, background thumbnails for large libraries, keyboard shortcuts matching Lightroom (G, D, E, R, 1-5, X, P), dark theme

## Not planned
AI masking, AI denoise, generative remove, Print/Book/Slideshow/Web modules, tethered capture, cloud sync, mobile apps.
