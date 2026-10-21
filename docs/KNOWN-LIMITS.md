# Known limits and manual QA

What has not been verified, what is only verified with fakes, and a checklist
you can run on your Mac with a real card. Companion to
[LIGHTROOM-PARITY.md](LIGHTROOM-PARITY.md), which lists missing features.

## What the automated tests do and do not prove

| Claim | Evidence | Gap |
|---|---|---|
| Suite is green on Linux and macOS | CI matrix (ubuntu + macos) runs `pytest`, `pytest -m exiftool`, a Chromium UI job, a fresh-install `doctor` job; `uv run pytest` also green on a Raspberry Pi 5 (aarch64) | Most tests fake rawpy and the DNG Converter. Pixel maths tests assert direction and invariants, not Lightroom's numbers |
| A real CR2 decodes, edits and exports with every tag kept | `PHOTO_ROOM_REAL_RAW=1 uv run pytest -m realraw` (CC0 Canon EOS 1000D sample, 10 MP) passes on the Pi in about 32 s | One camera body, one sample. CR3 and other brands decode through LibRaw but have no real-file test |
| Export keeps EXIF, GPS, IPTC, XMP, ICC, MakerNotes | `tests/test_real_exiftool_export.py`, `tests/test_real_raw_e2e.py` | Compares tags with exiftool; does not prove another program (Lightroom, Photos, a print lab) displays them |
| Card import copies safely and ejects | `tests/test_cardwatch.py`, `tests/test_ingest.py` with fake cards and fake eject commands | Never run against a real card reader, a real unmount, `diskutil eject` or `udisksctl power-off` |

## Unverified

- **Real-camera timings.** Measured on the Pi 5 with the 10 MP 1000D CR2:
  full-size JPEG export 2.6 s, 16-bit PNG 7.9 s (wall, includes Python start),
  synthetic 1400 px preview drag renders about 100 ms median
  (`scripts/bench_preview.py`), first sharp render about 120-320 ms. No numbers exist for a
  26-50 MP body, a 5D-class CR2, a card reader at USB-3 speed, or a Mac. A
  45 MP export will take roughly four to five times longer than the 10 MP one.
  The README's "47 seconds for 431 thumbnails" and "665-photograph library"
  figures are from an earlier machine and are not reproduced here.
- **macOS in manual use.** CI runs the unit and fresh-install jobs on macOS, but
  the browser UI job runs on Ubuntu only. Nothing has exercised on a Mac:
  Safari or Firefox (only Chromium is tested), Retina zoom (the 100% view is
  "device-pixel aware" by design), `Cmd` shortcuts, the card watcher on
  `/Volumes` with `diskutil`, a launchd job, or port 5000 (AirPlay Receiver
  uses it; run with `-p 5001`).
- **Large libraries.** `scripts/bench_library.py` shows the query engine at
  27-60 ms for a synthetic 20 000-photo index, but that benchmark starts from a
  built index. The first scan of a real 20 000-raw library runs one exiftool
  pass per batch; nobody has timed it, nor SSE behaviour with thousands of
  cells, nor a library on a network drive. Thumbnails take roughly 28 ms each
  cold on the Pi (embedded JPEG), so a first open of 20 000 files is on the
  order of ten minutes of background work.
- **Lightroom opening our XMP.** Sidecars are valid Camera Raw XMP according to
  the real exiftool, and the tone curve matches Lightroom's format, but nobody
  has opened one in Lightroom or Camera Raw. In particular unverified: exact
  appearance of exposure, shadows and highlights (the maths is an
  approximation, see [COLOUR-PIPELINE.md](COLOUR-PIPELINE.md)), mask feather
  and brush dab encoding, spot removal circles, `crs:Look` for LUTs, and
  whether Lightroom keeps the sidecar when it syncs metadata. The reverse
  (Lightroom edits into photo-room) is covered for basics, curve, HSL, grading,
  crop, spots and geometric masks, but not for Lightroom's AI or range masks.
- **Lens profiles.** `lensfunpy` is an optional extra and is not installed on
  the Pi used for this review (`doctor` WARN). The lens-profile path was not run
  here; only the manual and generic CA controls are covered by tests.
- **Virtual copies, history, snapshots and cull data live only beside your
  photos** (`*.photoroom.json`, `.photo-room-catalog.json`,
  `.photo-room-cull.sqlite`). Lightroom cannot see them and there is no backup
  tool. Copy the library folder to back them up.
- **Orientation.** Portrait frames rely on LibRaw applying the camera flip
  (rawpy default). Covered by tests with fakes; check one portrait frame by eye.
- **Security stance.** Local-only, single user. Do not expose the port.

## Known defects (not unverified; confirmed in code)

- JPEG, HEIC and video files copied from a card by `watch-cards` are not listed
  in the Library.
- Export is JPEG or PNG only; "Keep metadata" is all or nothing.

## Manual QA checklist (Mac with a real card)

Set up once:

```bash
brew install exiftool
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/Hiemenz/photo-room && cd photo-room
uv sync
uv run photo-room doctor            # expect 0 failed; note any WARN
uv run photo-room ~/Pictures/pr-test -p 5001   # use 5001: AirPlay holds 5000
```

Use a card you can afford to lose nothing from (the app never deletes from the
card unless you pass `--delete-after-import`, but test with a duplicate set
first). Tick each item and write down anything odd, with the time it took.

### A. Card import

- [ ] Insert the card. `uv run photo-room watch-cards ~/Pictures/pr-test --no-eject --once`
      lists the card, copies every raw into `YYYY/MM/DD` folders, and prints
      a clean summary (copied, skipped, failed = 0).
- [ ] Time it: files, total GB, seconds. Record the card model and reader.
- [ ] Re-run the same command: zero copied, everything skipped as duplicate.
- [ ] Compare a few raws with the card (`shasum -a 256`): identical.
- [ ] RAW+JPEG card: note that JPEGs land in the folders but do not appear in the
      editor (known).
- [ ] Without `--no-eject`: the card ejects only after verification, a macOS
      notification appears, Finder no longer shows the volume. Pull the card
      mid-copy on a second run: the import fails loudly, nothing is ejected.
- [ ] `--delete-after-import --delete-dry-run`: log lists only verified photos,
      deletes nothing.
- [ ] `photo-room ingest cards` shows the card (macOS path); `ingest run
      /Volumes/<card> -o ~/Pictures/pr-test` also works.

### B. Library and culling

- [ ] Open the editor. The grid fills; the footer thumbnail counter reaches
      100%. Time the first open of the whole folder.
- [ ] `g`, arrow keys, `1`-`5`, `x`, `p`, `6`-`9` all act on the selection;
      stars and labels appear in the cell without reload.
- [ ] Quit and relaunch: ratings, labels, keywords persist.
- [ ] `K` Smart cull: bursts grouped sensibly, suggestions explained, nothing
      changes until Apply, Undo batch works.
- [ ] Filter by rating, flag, label, camera, date; sort by capture date.
- [ ] Space opens Quick look; the image is sharp after a moment.
- [ ] Make a collection and a smart collection; make a virtual copy (`v`) and
      edit it differently.

### C. Develop

- [ ] Open a landscape and a portrait frame: orientation is correct.
- [ ] Drag Exposure, Highlights, Shadows, Temp, Contrast: the preview follows
      in well under a second on your Mac. Record the lag at 100% zoom.
- [ ] Auto tone, Auto WB, WB eyedropper, Clipping (`j`).
- [ ] Tone curve, HSL, Colour grading, a preset, a film style (`.cube`).
- [ ] Crop (`r`) + Straighten + Transform, Upright Auto.
- [ ] Masks: graduated, radial, brush; toggle overlay `o`.
- [ ] Spot removal on a dust speck.
- [ ] Noise reduction and Colour noise reduction on a high-ISO frame: grain and colour speckle
      shrink at 100% zoom and in an export, edges stay sharp. Record the drag lag.
- [ ] Trackpad pinch and Cmd shortcuts (`Cmd+Z`, `Cmd+S`, `Cmd+Shift+E`) work.
- [ ] Safari and Firefox open the page and the sliders respond (only Chromium
      is tested).
- [ ] If you installed `uv sync --extra lens`: "Use lens profile" finds your
      lens and visibly changes distortion or vignetting.

### D. Export and metadata

- [ ] Export one photo as JPEG q92 sRGB, full size. Open it in Preview: colours
      match the Develop preview.
- [ ] Same photo as 16-bit PNG: opens, file is large, no banding in a sky.
- [ ] `exiftool -a -G1 -s source.CR2 > a.txt; exiftool -a -G1 -s out.jpg > b.txt;
      diff a.txt b.txt`: only dimensions, orientation, thumbnail, Develop
      settings and container fields differ.
- [ ] GPS, lens model, serial number, copyright (if set in-camera) still present.
- [ ] Open the exported JPEG in Lightroom Classic or Photos: capture time,
      camera, lens, GPS show correctly.
- [ ] Export 50+ photos: the batch queue shows progress; cancel one halfway
      and confirm the files written so far are intact. Record seconds per photo.
- [ ] Output sharpening and a watermark do not change the metadata.

### E. Lightroom round trip

- [ ] In photo-room set Exposure +0.7, a curve, one HSL move, a crop. Save. Copy
      the raw and its `.xmp` to a folder, import into Lightroom Classic with
      "Read metadata from file" or `Metadata > Read Metadata from Files`.
      Record what Lightroom shows for each setting and whether the picture
      looks the same.
- [ ] Do the same with a brush mask and a spot removal.
- [ ] Edit a photo in Lightroom, write its sidecar (`Cmd+S` in Lightroom, or
      auto-write XMP on), open the raw in photo-room: basics, curve, crop load.
- [ ] Rating and label set here show up in Lightroom after a metadata read.

### F. Robustness

- [ ] Quit the editor mid-export (Ctrl+C): on restart the batch is listed as
      interrupted and Resume finishes it.
- [ ] Open the editor with the library on an external drive, unplug the drive,
      replug: no corrupted `.xmp`, `.photoroom.json` or catalog (`.bad` files
      appear only if a write was cut mid-way).
- [ ] Original raws are byte-identical after the whole session (`shasum -a 256`
      before and after).
