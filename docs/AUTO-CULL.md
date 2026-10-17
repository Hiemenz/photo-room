# Smart cull and Auto edit

Group a shoot into bursts, get a suggested best frame per burst, review it
with the keyboard, and run Auto edit on the keepers. Everything runs on the
machine: numpy, Pillow and OpenCV only, no network, no cloud models. The
suggestions learn your taste from what you accept and override, and all of
that stays in one SQLite file in the library folder.

**Suggestions only.** Nothing is deleted, no rating or flag changes, and no
edit is written until you press **Apply** (cull) or **Apply** in the Auto edit
preview. Each is one batch with an **Undo batch** button.

## Using it

Library module, then **Smart cull** (or `K`). Choose the scope: the whole
library, the open collection, or the selected photos. The first run analyses
thumbnails in the background (progress bar; the UI stays usable) and caches the
result, so reopening is instant and only new or changed files are analysed.

| Key | Action |
| --- | --- |
| Left / Right | previous / next frame in the burst |
| Down / Up, `J` / `K` | next / previous burst |
| `P` / `X` / `U` | pick / reject / clear the frame |
| `A`, Enter, Space | accept the suggestion for this burst, go to the next |
| `C` | compare the burst side by side (up to 4 frames, best-scoring first) |
| `Z` | compare at actual size (100%), one pan shared by every frame |
| Tab (in compare) | move the highlight to the next frame; `P`/`X`/`U` act on it |
| `F` (in 100%) | re-centre on the burst's detected focus area |
| Ctrl/Cmd+Enter | Apply the reviewed bursts |
| Esc | leave compare, then leave cull |

A burst counts as **reviewed** when you accept it or change any frame in it.
Apply writes only reviewed bursts (**Accept rest** reviews the others in one
go). A photo you already flagged is never overridden by a "no suggestion"; only
an explicit choice changes it. Reject is the same flag as `X` elsewhere: it
sets the rating to -1 and remembers the stars so un-rejecting restores them.

The 100% compare uses the tile server (`/api/tile`, z=100), so it shows the
finished render at real pixels from the raw, not the thumbnail.

### Auto edit

**Auto edit picks** previews before/after for up to three of the photos
currently marked pick and lists the slider changes. Apply writes one history
step ("Auto edit") on every pick, via `history.py`, and saves the settings like
any other edit. It is auto tone (`analysis.auto_tone`) plus auto white balance
(`analysis.auto_white_balance`) plus your learned offsets, starting from the
photo's current settings (or the editor's own proposal when it has none).
Photos using the old colour maths (edit version 1) are skipped with a message;
use Upgrade edit first. **Undo batch** steps each photo back to the history
entry it was on before (or its import state if it had no history).

## How the scores work (`cull.py`)

Features come from the cached 1024 px thumbnail (the camera's embedded
preview), never a raw decode.

* **Burst grouping.** A 64-bit difference hash plus capture time (EXIF
  `DateTimeOriginal`, file mtime if missing). Frames are ordered by time. A
  frame joins the running burst when it is within 8 s of the previous frame and
  its hash is within 16 bits, or within 90 s and within 7 bits (near
  identical). With no time at all only the strict test applies.
* **Sharpness, focus-region aware.** Laplacian variance per tile of an 8x8
  grid; the focus region is the sharpest ~12% of tiles, so a sharp subject on
  smooth grass or sky is not marked down. When a face is found its crop
  (normalised to 160 px) is blended in. Within a burst each frame is compared to the
  burst's best (square root of the variance ratio). The number is a ranking
  inside one burst, never a verdict on its own (see `quality.py` for why).
* **Exposure and clipping** from the luminance histogram: share of pixels at
  white or black, plus a small penalty for a mean far from mid-grey.
* **Faces and eyes** with the OpenCV Haar cascades shipped in the wheel
  (`haarcascade_frontalface_default`, `haarcascade_eye`). Eyes open means two
  eyes found in the top 60% of the face; one eye is half. It is a tie-breaker
  only, and it is only ever used to reject a frame when another frame in the
  same burst has the eyes open.
* **Score** (0-100) = 50% sharpness, 25% exposure, 25% eyes (when a face is
  found). Every photo carries its list of reasons ("sharpest focus region in
  the burst", "4.2% of the frame is blown to white", "eyes look closed").
* **Suggestions.** The best-scoring frame of a burst of two or more is the
  pick. A reject is suggested only for clear failures: 45% or more blown or
  crushed, no tonal range, almost no detail anywhere, much softer (under 40% of
  the best) than the burst's best, or eyes closed where the best has them
  open. Everything else is left alone.

## Learning (`cull_store.py`)

All data is in `<library>/.photo-room-cull.sqlite`:

| Table | Holds |
| --- | --- |
| `features` | cached feature dicts, keyed by the thumbnail's path, size and mtime |
| `decisions` | per reviewed photo: suggested action, final action, feature vector, agreement |
| `batches` | what each applied batch replaced (for undo) |
| `autoedit`, `offsets` | the settings Auto edit wrote, and the corrections you made afterwards |

* **Pick and reject model.** A ridge logistic regression (numpy, Newton
  steps) over eight features (relative and absolute sharpness, exposure, blown,
  crushed, contrast, face, eyes). It has no say below 10 reviewed photos, then
  its weight is `n / (n + 40)`; the final score is the heuristic score pulled
  toward the learned pick probability by that weight. A learned reject is only
  suggested with at least 8 rejects and 8 keeps in the log and a probability of
  0.85 or more. Undoing a batch removes its decisions from the log.
* **Auto edit offsets.** After Auto edit, if the photo's current history step
  is no longer the Auto step, the difference per slider (exposure, contrast,
  highlights, shadows, whites, blacks, vibrance, saturation, clarity, tint,
  and temperature as a ratio) is one sample. Photos left untouched teach
  nothing, since "happy" cannot be told from "never looked". With 3 or more
  samples the typical offset, shrunk by `n / (n + 8)` and bounded per slider,
  is added to the next Auto edit; with 20 or more it also depends on the
  photo's median brightness and tonal range.
* **Accuracy over time.** The Learning dialog plots the share of reviewed
  photos where the suggestion equalled your final choice, in buckets of 5, 10
  or 20 photos, plus the overall and last-50 rates.
* **Controls.** *Export JSON* downloads the decisions, offsets, accuracy and
  model summaries. *Reset learning* deletes decisions and offsets (the analysis
  cache stays). *Clear all data* deletes everything in the file. Deleting the
  file by hand does the same.

The learned style here is separate from the older style model behind
`process.py style` (DuckDB at `~/.photo-room/style.duckdb`), which learns
from every saved edit. Auto edit starts from the photo's current settings,
which for an unedited photo is the editor's proposal including that older
model, and then applies the offsets above.

## API

All endpoints are JSON; the host guard and session token apply as everywhere
else. See `cull_api.py`.

| Endpoint | Purpose |
| --- | --- |
| `POST /api/cull/analyze` | `{scope, paths?, collection?}` returns a job that fills the feature cache |
| `GET /api/cull/job?id=` | progress and result of a job |
| `POST /api/cull/suggest` | groups, scores, suggested actions and reasons; writes nothing |
| `POST /api/cull/apply` | `{choices: [{path, action, suggested, group, gsize, vec}]}` flags and logs; returns a batch id |
| `POST /api/cull/undo` | `{batch}` restores the prior ratings and flags |
| `GET /api/cull/stats`, `GET /api/cull/export` | accuracy and models; full JSON export |
| `POST /api/cull/reset`, `POST /api/cull/clear` | forget learning; delete everything |
| `POST /api/cull/autoedit/preview` | before/after renders and slider changes for up to 6 photos |
| `POST /api/cull/autoedit/apply`, `/undo` | one history step per photo as a job; undo by batch |

## Performance

* Analysis reads one cached 1024 px thumbnail per photo (built by the existing
  thumbnail pool if missing), on 3 worker threads, and is cached by file
  identity, so a 5000-photo library is analysed once. Measured on a Pi 5, about 110 ms
  per 1024 px frame, of which about 85 ms is the Haar cascades (on a textured
  frame; faces make the eye pass a little slower), so 5000 photos take roughly
  3 minutes on 3 threads, once. If OpenCV cannot load the cascades the scores
  simply omit faces.
* Grouping and scoring are numpy over a few numbers per photo and run on
  every request in milliseconds. The burst list uses `content-visibility` and
  lazy thumbnails, so thousands of photos do not block the page.

## Limits

* Haar cascades find frontal faces only and mistake glasses, profiles and
  heavy shadow for closed eyes. Hence the tie-breaker role.
* Sharpness reads the 1024 px preview, so very fine focus misses show only in
  the 100% compare, which is why that view exists.
* Photos from different cameras or lenses in one burst are compared on the same
  scale, which is fine for a burst and meaningless across a library.
* Virtual copies are not culled; the raw they point to is.
