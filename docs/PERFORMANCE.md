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
