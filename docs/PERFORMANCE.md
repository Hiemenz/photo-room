# Performance

## Library backend

Measured with `scripts/bench_library.py` on a Raspberry Pi 5 (4 cores, SD card): a synthetic
2,000-photo library (100 hard-linked copies of a real 11 MB Canon CR2 plus 1,900 tiny DNGs with
EXIF/XMP written by the real exiftool) and a synthetic 20,000-photo index for the query engine.
Medians, `--quick`, other processes were running on the machine.

| Operation | Before | After |
|---|---|---|
| Rate / label / keywords (response time) | 500-700 ms | 0.6-1.1 ms |
| Flag pick | 1.3 ms | 1.1 ms |
| Rate 50 photos in a burst | 35 s | 45 ms (sidecars follow in ~1.3 s, one exiftool command per group) |
| `/api/library` page, warm | 410 ms | 1.3 ms |
| `/api/library` filter + sort | 416 ms | 1.2 ms |
| First `/api/library` on a never-seen library | 17 s | 10.6 s (one scan; later starts load the index from SQLite) |
| Thumbnail, cold, from CR2 | 382 ms | 33 ms |
| Thumbnail, warm | 1.0 ms | 0.5 ms |
| 20k index: filter + sort, first request | 20-85 ms | 27-81 ms |
| 20k index: next page / repeated query | 20-85 ms | < 0.01 ms |
| 20k index: query after a write | 20-85 ms | 29-55 ms |

(`first request` and `after a write` still run the reference filter/sort once; sorted orders are
cached per field and survive writes to other fields.)

### How it works

* `libindex.py` keeps every photo's rating, flag, label, keywords, camera, lens, ISO and date in
  memory and in `.photo-room-index.sqlite` (WAL). Queries never touch exiftool or the disk.
* Writes update memory and the database in one transaction, then return. The `pending` table is
  the write-ahead log: a background writer debounces (0.35 s quiet, 2 s max), coalesces per photo,
  groups photos with identical changes into one exiftool command, writes atomically (exiftool's
  temp-file-and-rename for existing files; a temp `.xmp` and `os.replace` for new sidecars), then
  deletes the row. After a crash the next start replays the rows. Failed writes stay queued and are
  retried with backoff. `POST /api/library/flush` and process exit write everything now.
  SQLite runs `synchronous=NORMAL`: safe against a process crash, a power cut can lose the last
  few milliseconds of acknowledged changes (`libindex.SQLITE_SYNCHRONOUS = "FULL"` closes that).
* `exifpool.py` keeps `exiftool -stay_open` processes running; reads are batched (400 files per
  command) and a rescan only asks about files whose size/mtime (or sidecar's) changed.
* Change feed: `GET /api/library/changes?since=N&timeout=25` (long-poll) and
  `GET /api/library/events` (SSE). The `/api/library` response carries `seq`. `reset: true` means
  reload the page. UI wiring is not done yet.
* Query API: `/api/library` accepts rating, flag, label, keyword, camera, lens, `iso_min`/`iso_max`,
  date range and text, sort by name/date/rating/label/flag/camera/lens/iso, `offset`/`limit`.
* Thumbnails (`thumbs.py`): embedded JPEG from `rawpy.extract_thumb()` (exiftool fallback, then a
  half-size decode), PIL DCT-scaled decode, orientation applied, three cached sizes
  (160/320/1024, `?size=` on `/api/thumb`), keyed by path+size+mtime. Workers pull from a priority
  queue; `POST /api/thumbs/want {paths}` moves on-screen tiles to the front without blocking.
* Smart previews (`smartpreview.py`): `smartpreview.get(path, root)` returns what
  `process.decode_for_analysis` returns, at <= 2048 px from a cached JPEG (LRU-pruned, 2 GB). The
  render code can call it to open Develop without a demosaic; it is not wired in yet.

Run the benchmark: `uv run python scripts/bench_library.py` (needs exiftool on PATH).

---

## Render and export pipeline

Measured on the Pi 5 (4 cores, 16 GB) with a real 10 MP raw (Canon EOS 1000D CR2, 3888x2592, CC0, the
sample `tests/test_real_raw_e2e.py` downloads). The machine was shared with other jobs (load average
2-3 on 4 cores) while measuring, so every figure is the **best of N runs**; where wall and CPU time
differ a lot, trust CPU time. Reproduce with `scripts/bench_export.py`, `scripts/bench_render.py` and
`scripts/bench_preview.py`; run them from an older checkout for the "before" column. All "before"
numbers are commit `bf866d8`.

## Full-resolution export (raw -> file, including the exiftool metadata copy)

"Heavy" edit = exposure, contrast, shadows/highlights, saturation + vibrance, clarity, texture, dehaze,
sharpening, two HSL bands, a colour-grading wheel and a tone curve. "Light" = exposure, contrast, saturation, white balance.
"Cold" = first export of the file; "warm" = a re-export that hits the decode cache.

| Export (wall s)       | before | after cold | after warm | CPU s before -> after (cold) |
|-----------------------|-------:|-----------:|-----------:|-----------------------------:|
| JPEG, heavy edit      | 12.7   | 6.4        | 5.5        | 10.5 -> 6.1                  |
| PNG, heavy edit       | 21.0   | 8.5        | 7.8        | 14.3 -> 7.8                  |
| JPEG, light edit      | 4.3    | 2.7        | 1.7        | 3.4 -> 2.5                   |
| PNG, light edit       | 14.5   | 4.5        | 3.5        | 8.6 -> 4.2                   |

The UX review's ~35 s PNG was the same code on a busier machine (the PNG encoder alone was 4.4 s CPU and
the exiftool rewrite of the 17 MB file several more).

Targets: JPEG < 6 s (reached warm, 6.4 s cold with a heavy edit; 2.7 s with a light one), PNG < 12 s (reached: 8.5 s heavy, 4.5 s light).

## Per-stage, best of 3, wall / CPU ms

| Stage | 1400 px before | 1400 px after | full-res before | full-res after |
|---|---:|---:|---:|---:|
| decode, full 8-bit (rawpy)         |  -        | -         | 1398 / 1635 | 1462 / 1833 (skipped by the decode cache on re-export) |
| decode, half-size (preview)        | 90 / 121  | 98 / 127  | - | - |
| resize to 1400 px                  | 173 / 168 | 202 / 179 | - | - |
| 8-bit -> float                     | -         | -         | 82 / 76 | 87 / 81 |
| WB + exposure + tone + curve       | 316 / 306 | 65 / 62   | 2860 / 2630 | 709 / 679 |
| saturation / vibrance              | 108 / 102 | 35 / 31   | 1015 / 963 | 420 / 377 |
| HSL                                | 123 / 125 | 87 / 85   | 1159 / 1158 | 922 / 984 |
| colour grading                     | 87 / 77   | 36 / 36   | 886 / 849 | 418 / 400 |
| clarity / texture / dehaze / sharpen | 293 / 283 | 225 / 204 | 2774 / 2596 | 1850 / 1685 |
| one graduated mask                 | 158 / 150 | 140 / 138 | 1690 / 1473 | 1648 / 1476 (not optimised) |
| float -> 8-bit                     | 11 / 11   | 11 / 11   | 140 / 138 | 166 / 159 |
| JPEG encode q92                    | 20 / 20   | 18 / 18   | 160 / 156 | 138 / 138 (Huffman optimisation off: it cost 2x) |
| PNG encode, zlib 6 -> 1            | 561 / 523 | 229 / 223 | 4623 / 4391 | 1753 / 1658 |
| exiftool copy (JPEG)               | -         | -         | 972 / ~0 (separate process) | 939 / ~0 |
| exiftool copy (PNG)                | -         | -         | ~6000 (rewrites the 17 MB file) | ~1000 (1x1 stand-in, chunks spliced in) |

## Slider drag (`scripts/bench_preview.py`, median ms; "drag" is the 800 px render sent while a slider moves)

| Control | sharp before | sharp after | drag before | drag after |
|---|---:|---:|---:|---:|
| exposure         | 535 | 378 | 180 | 118 |
| contrast         | 554 | 352 | 191 | 137 |
| saturation       | 470 | 312 | 151 | 110 |
| HSL hue (red)    | 421 | 290 | 126 | 105 |
| HSL sat (blue)   | 421 | 295 | 129 | 86 |
| grading          | 311 | 227 | 119 | 70 |
| clarity          | 197 | 171 | 69  | 47 |
| sharpness        | 198 | 173 | 62  | 51 |

Drag renders are under the 150 ms target at the median (worst single tick 155 ms, exposure).

## What changed

* **Fast kernels** (`render_kernels.py`, `colormix.py`, `tonecurve.py`): float32 throughout; white balance, tint
  and exposure fold into one per-channel multiply; the four tonal-range masks fold into one added plane;
  luminance, saturation, contrast and gains run as OpenCV 3x3/3x4 `cv2.transform`s (NumPy's strided per-channel
  passes and `max(axis=2)` were the slowest parts); the tone curve is a 4097-entry float32 table gathered with
  `np.take` instead of `np.interp` (which promotes to float64; at most 0.12 of an 8-bit level from the old curve);
  detail runs per row strip and keeps the old quantised Pillow blurs, so clarity/texture/dehaze/sharpen are
  numerically the old ones.
* **Decode cache** (`decode_cache.py`): the demosaiced image is stored as an uncompressed `.npy` under
  `~/.cache/photo-room/decode` keyed by BLAKE2b of the raw file + wb/auto-bright/bit-depth/colour-space/half-size +
  the LibRaw version; capped at 3 GB (`PHOTO_ROOM_DECODE_CACHE_MB`), LRU; `PHOTO_ROOM_DECODE_CACHE=0` disables.
* **Encoders**: PNG zlib level 1 by default (`png_compression` option; 6 was 2.6x slower for 2% smaller files),
  16-bit PNG assembled in strips; JPEG Huffman optimisation is now an option (`optimize_jpeg`, default off).
* **Metadata**: one exiftool call per export (the virtual-copy tags ride in the same call), through a persistent
  `-stay_open` process (the library work's `exifpool.py`);
  for PNG the tags are written on a 1x1 stand-in and its chunks are spliced into the real file.
* **Row-strip engine** (`fastops.py`): per-pixel stages can run on a thread pool. **Off by default**: on this Pi, while
  other jobs were running, 2-4 threads were 1.5-2.5x *slower* for these memory-bound passes (and separate processes
  scaled the same way, so it is the machine, not the GIL). `PHOTO_ROOM_THREADS=N` turns it on; re-measure on a quiet machine.

## Equivalence

`tests/legacy_pipeline.py` is a frozen copy of the old stages. `tests/test_render_equivalence.py` pins every
stage and the whole chain against it on synthetic images (1 and 4 threads) and on a full-resolution crop of the
real raw (`-m realraw`): max abs difference <= 1/255 in float (observed <= 0.25/255) and <= 1 level after
8-bit quantisation. The blur is bit-identical to the old one. `tests/test_decode_cache.py` guards the cache key
(every decode parameter, file content, no cross-file sharing) and `tests/test_export_speed_paths.py` checks the
PNG writer, exiftool session and that spliced PNG metadata equals a direct exiftool rewrite.

## Not done / deferred

* Batch export still runs one photo at a time (`export_api._run_job`); a process pool with a memory guard
  needs a quiet machine to tune and touches the job queue other work is changing.
* Masks (`masks.apply_local`) are unchanged.
* `rawpy` demosaic is untouched (`half_size`/other algorithms would change pixels).

### Default camera crop

Raw decodes now use the camera's default crop (`rawpy` `crop_*`; the Canon 1000D sample is 3888x2591 instead of
the 3906x2602 sensor area) for both the preview and the export, so they frame identically. `ExportOptions.full_sensor`
(`"full_sensor": true` in the export payload) keeps the whole decoded area. Colour math is untouched.

## Linear-light colour pipeline (edit version 2)

Edits now run in linear light (docs/COLOUR-PIPELINE.md): the tone stage decodes to linear, applies gains,
re-encodes with the exact output curve, then applies Contrast/curve through one 65 537-entry lookup. That costs
two power functions per full frame where the old stage cost none. Measured on the Pi 5 with the same scripts,
`main` and this branch back to back (load average 0.7-1.6; best/median of runs; the earlier tables in this file
were taken on a busier machine, so compare within this table):

| Slider drag (`bench_preview.py`, median ms) | main sharp | now sharp | main drag | now drag |
|---|---:|---:|---:|---:|
| exposure   | 253 | 323 | 80 | 102 |
| contrast   | 249 | 322 | 78 | 102 |
| saturation | 222 | 220 | 71 | 71 |
| HSL, grading, clarity, sharpness | unchanged | unchanged | | |

Drag renders stay under the 150 ms target (the figures above are below the 118/137 ms recorded earlier). The
decode to linear is cached per photo in the stage cache, so a drag costs one re-encode, not a decode and an encode.

| Export wall s (`bench_export.py`) | main cold | now cold | main warm | now warm |
|---|---:|---:|---:|---:|
| JPEG, heavy | 5.03 | 5.27 | 4.27 | 4.53 |
| PNG, heavy  | 6.81 | 6.97 | 6.00 | 6.16 |
| JPEG, light | 1.99 | 2.49 | 1.28 | 1.75 |
| PNG, light  | 3.73 | 4.32 | 2.96 | 3.34 |

Targets (JPEG < 6 s, PNG < 12 s) still hold. The exporter passes the 16-bit linear decode straight in
(`_linear_input`), so only the final encode is added. `bench_render.py`: white balance + exposure + tone + curve,
1400 px 52 -> 142 ms, full resolution 581 -> 1179 ms when run stand-alone on an encoded image (it pays a decode
and an encode; the preview caches the decode and the export skips it). Decode cache entries are now 16-bit linear
(`FORMAT_VERSION` v2), so old entries are never hit.

## Noise reduction

`render_kernels.denoise_stage` runs at the start of the detail stage (before sharpening). Luminance is an
OpenCV bilateral filter on the Y plane (one or two passes); colour is a Gaussian blur of the two chroma planes,
guarded so chroma is kept near luminance edges. Its radii scale with the long edge (reference 3000 px), so the
preview, the 100% tiles and the export look alike. It is part of the DETAIL stage cache key, so dragging
Noise reduction re-runs only the detail stage. Measured on the Pi 5 standalone: luminance bilateral 13 ms at
1400 px and about 150 ms at 10 MP; chroma blur about 25 ms and 225 ms. Check the drag cost with
`scripts/bench_preview.py` (rows `noise reduction` and `colour noise reduction`).

`bench_preview.py` slider drag with the new rows (median ms): noise reduction 260 sharp / 82 drag, colour noise
reduction 278 / 86, against 144 / 53 for sharpness in the same run; both under the 150 ms drag target.
