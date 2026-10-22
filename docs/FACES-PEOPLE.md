# Faces / People (optional, on-device, opt-in)

Find the faces in your library, group them into people, name them, and click a
person in the Library sidebar to filter the grid to their photos. Everything
runs on this machine; nothing is uploaded and nothing is downloaded. Nothing
runs until you ask: the scan starts only from **Find faces** (Library >
People, with a confirm) or `photo-room faces scan`.

## Use it

- Library sidebar > **People** > **Find faces**. It scans the 2048 px smart
  previews in a resumable background job (cancel any time; scanned photos are
  kept, a re-scan only does new or changed files).
- Each person shows a face thumbnail and the number of photos. Click to
  filter the grid (click again to clear; **Clear filters** also clears it).
  Other filters, sort and collections still apply (`/api/library?person=<id>`).
- Pencil (or double-click): name a person. Arrow: merge a duplicate person
  into another (the name you gave is kept). Cross: hide a person (false
  faces, strangers); hidden faces are not brought back by later scans.
- Named and merged people are never re-clustered away: new faces join the
  nearest existing person if close enough, otherwise start a new unnamed one.

CLI (same store, same code):

    photo-room faces scan   ~/Pictures/shoot
    photo-room faces list   ~/Pictures/shoot
    photo-room faces name   ~/Pictures/shoot 3 Ann Lee
    photo-room faces merge  ~/Pictures/shoot 5 3      # fold person 5 into 3
    photo-room faces remove ~/Pictures/shoot 7
    photo-room faces status ~/Pictures/shoot

## Honest limits

- **Default grouping is not face recognition.** Without a model, each face is
  described by local-binary-pattern histograms (texture and rough lighting).
  It groups similar-looking crops, so the same person in a different pose or
  light is often split into several people and different people can be
  merged. Treat the groups as suggestions: name and merge to correct them.
- **Detection is the OpenCV Haar frontal-face cascade** (the same as cull
  scoring): it misses profiles, tilted, small, masked or sunglassed faces and
  gives false positives on busy textures. Use **hide** for those.
- Groups are built per embedder; switching embedder (for example after adding a
  model) re-scans and starts new groups. Names given under the old embedder
  stay in the store but are not carried across.
- Group quality was checked on synthetic textures only. The Haar/LBP path has
  not been measured on real portrait libraries, the optional model paths have
  not been run against a real model, and the People panel is not
  browser-verified.
- Person tags are kept only in `.photo-room-faces.sqlite`; they are not
  written to XMP, not exported and not synced with Lightroom. Only the
  originals' reads are involved, they are never modified.
- Only whole photos: no per-face boxes in the viewer, no "unnamed faces to
  confirm" queue, no splitting one person's group by hand (merge and hide
  exist), virtual copies are not scanned separately.

## Better results (optional)

Put either or both of these in the model folder
(`$PHOTO_ROOM_FACES_MODEL`, else `$PHOTO_ROOM_CACHE_DIR/faces-model`, else
`~/.cache/photo-room/faces-model`). The app never fetches them.

- `face_detection_yunet*.onnx`: the OpenCV YuNet detector, run by
  `cv2.FaceDetectorYN` (no extra Python package). Falls back to Haar if the
  OpenCV build cannot load it.
- `face_embedding*.onnx` plus `uv sync --extra faces` (onnxruntime): an
  ArcFace/SFace-style recognition model taking 112x112 RGB scaled to -1..1
  and returning one embedding. Faces are grouped at cosine similarity >= 0.40.
  The preprocessing assumes that convention and no face alignment is done, so
  a model that needs aligned landmarks will group worse.

The People status line says whether the approximate grouping is in use.

## Where things live

`faces.py` (detectors, embedders, store, clustering, background builder, CLI),
`faces_api.py` (`/api/faces/status|build|cancel|people|people/<id>/name|merge|remove|face/<id>.jpg`),
`static/js/people.js` (sidebar panel), `.photo-room-faces.sqlite` beside the
library (faces keyed by file content hash like semantic search, so renames keep
them). Tests: `tests/test_faces.py` (synthetic textures and a fake detector;
no model, no network).
