# UI structure

The editor UI is plain static files: no bundler, no npm. `templates/editor.html`
is a thin Jinja shell (markup only); styles live in `static/css/`, behaviour in
`static/js/`. Flask serves `static/` from the folder next to `editor.py`, so
the editor can be launched from any working directory. Every asset URL goes
through `asset('css/x.css')`, which appends `?v=<mtime>`; versioned URLs are
cached for a year, so edits show up on the next load with no manual busting.

## Load order matters

The JS files are **ordered classic scripts** sharing one global scope (top-level
`const`/`let`/`function` are visible to later files). Order is the list of
`<script src>` tags at the bottom of `editor.html`; a file may only run
top-level code that uses things defined in files above it (functions called
from event handlers later are fine). The CSS cascade follows the `<link>` order.

The server-provided constants (`CONTROLS`, `MASK_CONTROLS`, `CURVE_PRESETS`, ...)
are emitted by a small inline `<script>` in `editor.html` before the static
scripts. A tiny inline script in `<head>` applies the saved theme to avoid a flash.

## Who owns what

CSS (`static/css/`, in cascade order)

| File | Owns |
| --- | --- |
| `base.css` | theme tokens (dark/light), reset, body |
| `layout.css` | left file list / page layout |
| `develop.css` | masks overlay, crop, preview, slider controls |
| `panels.css` | tone-curve panel, buttons |
| `library.css` | module picker, Library grid, Compare/Survey, virtual copies |
| `history.css` | History / snapshots, before-after |
| `dialogs.css` | Export dialog |

JS (`static/js/`, in load order)

| File | Owns |
| --- | --- |
| `core.js` | `$`, shared state (`current`, `values`, ...), `ALL_CONTROLS` |
| `develop-basic.js` | slider building (basic/presence/detail panels) |
| `develop-hsl-grading.js` | colour mixer and grading |
| `develop-transform-lens-spots.js` | transform, lens corrections, spot removal |
| `preview.js` | histogram, preview render, opening a file |
| `actions.js` | save/reset/auto, copy-paste settings |
| `ratings-filter.js` | ratings, labels, filtering |
| `contact-sheet.js` | contact sheet |
| `presets.js` | presets |
| `develop-crop.js` | crop overlay |
| `develop-tone-curve.js` | tone curve (+ copy/paste settings) |
| `develop-masks.js` | graduated/radial/brush masks, canvas, panel wiring |
| `selection.js` | multi-select |
| `before-after.js`, `before-after-views.js` | compare key, split/side views |
| `history.js` | history and snapshots |
| `shortcuts.js` | shortcut table and `?` help |
| `theme.js` | theme toggle |
| `keyboard.js` | global key handlers |
| `library-module.js` | Library grid, paging, thumbnails |
| `collections.js` | collections, virtual copies |
| `compare-survey.js` | Compare and Survey |
| `library-files.js` | file list loading |
| `init.js` | startup calls (keep last of the app scripts) |
| `export-dialog.js` | Export dialog (self-contained IIFE) |

## Adding a new panel

1. Markup: add the panel to `templates/editor.html` (or a `templates/partials/`
   include); keep ids unique and prefixed by the panel.
2. Styles: add a new `static/css/<panel>.css` and a `<link>` after the existing
   ones. Do not edit other panels' files.
3. Script: add `static/js/develop-<panel>.js` (or `library-...`) and a
   `<script src="{{ asset('js/...') }}">` tag before `init.js`. Use only shared
   state from `core.js`; prefer wrapping private code in an IIFE.
4. Server-provided data goes in the inline config block in `editor.html`.
5. Update the tables above, and check `uv run pytest` and `pytest -m ui`.
