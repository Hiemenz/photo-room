# editor.py refactor plan

`editor.py` is ~2.5k lines: 40+ routes, the render pipeline, XMP read/write,
ratings and caches. Other work (security, the editor.html split, render and
export speed-ups) edits it concurrently, so the split is staged to avoid
churn. Done so far: the crop/straighten coordinate system and its conversions
live in `geometry.py` (pure, no Flask), with `editor.py` importing them.

1. `render.py`: move `apply_adjustments`, `_stage_*`, `_apply_detail`,
   `apply_geometry`, `effective_adjustments` (pure numpy). `exporter.py`,
   `export_api.py` and tests then stop importing Flask and the lazy
   `import editor` cycle-dodges go away. Do this right after the render-speed
   and colour-maths work lands, because they edit exactly these functions.
2. `xmp_store.py`: move `_xmp_arguments`, `_read_*`, `write_sidecar`,
   `save_adjustments`, `read_saved_adjustments`, `MetadataReadError`, ratings.
   Then collapse the 6 exiftool spawns per read into one `-json -struct` call
   (report L4), covered by the existing real-exiftool round-trip tests.
3. Blueprints for library / develop / history routes, as `export_api.py`
   already does. Move `_resolve_photo`/`_virtual` into a small `photos.py`.

Each step is a pure move with the old names re-exported from `editor.py` until
callers (tests) are updated, merged on its own with the full suite green.
