# Semantic search (optional, on-device)

Type "sunset over water" or "dog" in the Library search box and get photos
ranked by meaning, or select a photo and click **Find similar**. It runs on
the CPU with a CLIP-style model; nothing leaves the machine.

It is an optional extra. Without it the app works exactly as before and the
same search box falls back to a keyword/EXIF search with a message saying how
to enable the real thing.

## Enable it

1. Install the extra: `uv sync --extra semantic` (adds `onnxruntime` and
   `tokenizers`; nothing else changes).
2. Get a model. This is explicit and opt-in; the app never downloads anything
   by itself:
   - `python semantic.py download --yes` fetches a quantised CLIP ViT-B/32
     ONNX export (about 90 MB) from huggingface.co into the model folder, or
   - copy your own CLIP-style export there: `vision_model.onnx` (or
     `visual.onnx`), `text_model.onnx` (or `textual.onnx`), `tokenizer.json`,
     and optionally `config.json` (`image_size`, `mean`, `std`,
     `context_length`, `name`). The text encoder takes `input_ids` (and
     `attention_mask` if it declares it); outputs are matched by the name
     `*embed*`, else the first output.
3. In Library, click **Semantic**, then **Build index**.

Model folder: `$PHOTO_ROOM_SEMANTIC_MODEL`, else
`$PHOTO_ROOM_CACHE_DIR/semantic-model`, else `~/.cache/photo-room/semantic-model`.
`python semantic.py status` says what is missing.

## How it works

- The builder embeds each photo's 2048 px smart preview (`smartpreview.py`,
  built on demand, read-only on the raw) in a background thread, a batch at
  a time. Cancel or quit at any point: finished photos are kept and the next
  build skips them.
- Vectors live in `.photo-room-semantic.sqlite` beside the library, keyed by
  a content hash (file size plus head/middle/tail samples) and model name.
  Renamed or moved files reuse their vector; an edited file is re-embedded;
  switching models keeps both sets. Originals are never written.
- Search embeds the query text, ranks by cosine similarity over the indexed
  photos (top 200) and keeps every other filter bar criterion (rating, flag,
  keyword, dates...). Photos not indexed yet are left out, and the status next
  to the box says how many are indexed.
- **Find similar** ranks by similarity to the active photo; click it again or
  **Clear filters** to leave.

## Fallback (no extra, no model, empty index)

Words are matched against file name, keywords, camera, lens, date and colour
label (keywords weigh most). **Find similar** then uses shared keywords, same
camera and same day. The response and the status line say which mode ran.

## API

`GET /api/semantic/status`, `POST /api/semantic/build`, `POST /api/semantic/cancel`,
`GET /api/semantic/search?q=...|similar=<path>`; the grid uses
`GET /api/library?sem=<text>` and `?similar=<path>` (response gains a
`semantic` object: `mode`, `message`, `indexed`, `library`).

## Testing

`semantic.py` takes any object with `name`, `dim`, `embed_images` and
`embed_text`; `tests/test_semantic.py` uses a fake colour embedder so the store,
resumable builder, ranking, fallback and HTTP paths run with no model.

## Limits

Verified only with the fake embedder in CI; see KNOWN-LIMITS. Ranking quality,
speed on the Pi and exact ONNX input/output names of other model exports are
not verified. Indexing a large library decodes every raw once (smart preview),
which is the slow part. Virtual copies are not indexed.
