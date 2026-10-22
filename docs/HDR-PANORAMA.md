# HDR merge and panorama stitching

Code: `merge.py` (the work), `merge_api.py` (Library jobs), `static/js/merge.js` (buttons),
`photo-room merge` (command line). Tests: `tests/test_merge.py`.

## Use

    photo-room merge hdr  a.CR2 b.CR2 c.CR2 -o merged.tif [--half] [--no-align] [--float]
    photo-room merge pano a.CR2 b.CR2 c.CR2 -o pano.tif   [--half] [--scans] [--preview]

In the editor, select 2 or more photos in the Library (Ctrl/Cmd-click or Shift-click) and press
**Merge to HDR** or **Merge to Panorama** under the grid. Progress shows in the status line; **Cancel merge**
stops it. There is no stacks feature in the codebase yet, so selection is the only way to choose frames.

Rules that always hold:

- Sources are only read. The output is a new `.tif`/`.tiff` (`-o` must say so; DNG output is not supported).
  Without `-o` it is `<first photo>-HDR.tif` / `-Pano.tif` beside the first photo.
- An existing file is never overwritten; a numbered name (`-1`, `-2`) is used instead. An output equal to a source is refused.
- Written to a hidden temp file and linked into place, so a half-written file never appears. A failed or
  cancelled merge leaves nothing behind.
- Metadata of the base frame is copied with exiftool (every call has a `timeout=`): everything except saved Camera Raw
  edits (`XMP-crs`), embedded previews and thumbnails; Orientation is set to 1 because the decode is already upright.
  Base frame: HDR = the middle exposure; panorama = the middle photo of the list.

## HDR

1. Each frame is decoded to linear light (raws via LibRaw with camera white balance, as in Develop; JPEG/PNG/TIFF by
   undoing the sRGB curve). Frames must be the same size.
2. Relative exposure is measured from the pixels (median ratio over pixels well exposed in both of two neighbouring
   exposures); EXIF `ExposureTime x ISO / f-number^2` is the fallback. Frames are ordered by brightness, base = the middle one.
3. `cv2.createAlignMTB` aligns each frame to the base by translation (computed on a 1536 px proxy, applied at full size).
4. Weighted average of radiance: zero weight for clipped pixels, ramped weight out of the noise floor, more weight for
   frames with more light. Pixels no frame can see are black.
5. Output is a **linear-light, sRGB-primaries** TIFF. Default 16-bit: radiance is divided by its peak (never below 1) so
   highlights above the base frame's white are kept; the divisor is written in the TIFF ImageDescription. `--float`
   writes 32-bit float TIFF with true radiance (base frame white = 1.0), which many viewers cannot open.

Memory: about 52 bytes per pixel (about 1.2 GB for 24 MP); above `PHOTO_ROOM_MERGE_MB` (default 3000) the merge refuses
and suggests `--half`. Frames are read twice (proxy pass, then merge pass), one at a time.

## Panorama

`cv2.Stitcher` (feature matching, bundle adjustment, seam finding, multi-band blending), `--scans` for flat
subjects. A preview-size pass (1200 px, half-size raw decode) runs first so an unmatchable set fails in seconds with a
plain message; `--preview` stops there. The full pass stitches at full size, then crops to the largest axis-aligned
rectangle without empty corners. If all inputs together exceed `PHOTO_ROOM_PANO_MP` (default 300) megapixels they are scaled down
together.

Because OpenCV's Stitcher works on 8-bit data, the panorama is an **8-bit sRGB-encoded TIFF**, not linear and not 16-bit. There is
no deghosting for HDR and no projection choice for panoramas.

Cancel: checked between frames and stages. The single full-size stitch call cannot be interrupted; a cancel during it takes effect
when it returns and the result is discarded.

## HTTP

`POST /api/merge {kind: "hdr"|"pano", paths: [...], options: {...}}` returns a job (202);
`GET /api/merge/status/<id>` gives `state` (queued, running, done, error, cancelled), `progress` 0-1, `message`, `result`;
`POST /api/merge/cancel/<id>`. One merge runs at a time. Paths are checked to be inside the library.
After a merge the library index is rescanned so the change feed (SSE) reaches open Library grids.

## Not verified

Only synthetic frames (generated clipped brackets; shifted and overlapping textures) and a faked rawpy have been tested.
Not tried on real CR2/CR3 brackets or real handheld panoramas: ghosting from moving subjects, white-balance variation between
frames, vignetting seams, parallax, lens distortion, very large (60 MP) inputs and Pi memory limits are all unknown.
The Library lists the output TIFF and Develop opens it through the non-raw image loader (`imgload.py`).
