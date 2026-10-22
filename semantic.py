#!/usr/bin/env python3
"""
On-device semantic search ("sunset over water", "dog") and "find similar".

Everything is optional. The core here (store, background builder, ranking,
keyword/EXIF fallback) needs only numpy and sqlite and is driven by an
``Embedder``; tests use a fake one. The real embedder is a CLIP-style model run
by onnxruntime (extra ``semantic``: ``uv sync --extra semantic``).

Embedder contract (duck typed)::

    name: str                              identifies the model (rows are per model)
    dim: int
    embed_images(list[uint8 RGB array]) -> float array (n, dim), L2-normalised
    embed_text(str) -> float array (dim,), L2-normalised

Storage: ``.photo-room-semantic.sqlite`` beside the library (never touches the
originals; they are only read). Embeddings are keyed by a content hash of the
file (size plus head/middle/tail samples), so a rename or move reuses its
vector and an edited file gets a new one. ``files`` caches path -> hash so an
unchanged file is not re-hashed.

Model files: nothing is downloaded unless you run
``python semantic.py download --yes`` (explicit, opt-in). The model directory
is ``$PHOTO_ROOM_SEMANTIC_MODEL`` or ``$PHOTO_ROOM_CACHE_DIR/semantic-model``
(default ``~/.cache/photo-room/semantic-model``) and holds ``vision_model.onnx``,
``text_model.onnx`` and ``tokenizer.json`` (optionally ``config.json``).
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import threading
from pathlib import Path

import numpy as np

DB_NAME = ".photo-room-semantic.sqlite"
HASH_SAMPLE = 256 * 1024
BATCH = 8
DEFAULT_TOP = 200
DOWNLOAD_TIMEOUT = 60
# Where `download --yes` fetches from. A CLIP ViT-B/32 export in ONNX form.
MODEL_REPO = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main"
MODEL_FILES = {                # local name -> remote path
    "vision_model.onnx": "onnx/vision_model_quantized.onnx",
    "text_model.onnx": "onnx/text_model_quantized.onnx",
    "tokenizer.json": "tokenizer.json",
}
INSTALL_HINT = ("Semantic search is not installed. Install the optional extra "
                "(`uv sync --extra semantic`) and download a model explicitly with "
                "`python semantic.py download --yes`. Until then search uses names, "
                "keywords, camera and dates.")


class SemanticUnavailable(RuntimeError):
    """The optional dependency or model files are missing."""


# --------------------------------------------------------------- model location
def model_dir() -> Path:
    env = os.environ.get("PHOTO_ROOM_SEMANTIC_MODEL")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("PHOTO_ROOM_CACHE_DIR")
    root = Path(base) if base else Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "photo-room"
    return root / "semantic-model"


def _model_files(d: Path) -> dict[str, Path] | None:
    out = {}
    for key, names in (("vision", ("vision_model.onnx", "visual.onnx")),
                       ("text", ("text_model.onnx", "textual.onnx")),
                       ("tokenizer", ("tokenizer.json",))):
        found = next((d / n for n in names if (d / n).is_file()), None)
        if found is None:
            return None
        out[key] = found
    return out


def unavailable_reason() -> str | None:
    """Why the real embedder cannot load, or None when it can."""
    try:
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
    except ImportError:
        return INSTALL_HINT
    if _model_files(model_dir()) is None:
        return (f"No semantic model found in {model_dir()}. Run "
                "`python semantic.py download --yes` (downloads about 90 MB from "
                "huggingface.co) or point PHOTO_ROOM_SEMANTIC_MODEL at a folder with "
                "vision_model.onnx, text_model.onnx and tokenizer.json.")
    return None


class OnnxClipEmbedder:
    """CLIP-style image/text encoders run with onnxruntime on the CPU."""

    def __init__(self, directory: Path | None = None):
        reason = unavailable_reason() if directory is None else None
        if reason:
            raise SemanticUnavailable(reason)
        import json

        import onnxruntime as ort
        from tokenizers import Tokenizer
        d = Path(directory) if directory else model_dir()
        files = _model_files(d)
        if files is None:
            raise SemanticUnavailable(f"No semantic model found in {d}")
        cfg = {}
        if (d / "config.json").is_file():
            cfg = json.loads((d / "config.json").read_text())
        self.size = int(cfg.get("image_size", 224))
        self.mean = np.array(cfg.get("mean", [0.48145466, 0.4578275, 0.40821073]), dtype=np.float32)
        self.std = np.array(cfg.get("std", [0.26862954, 0.26130258, 0.27577711]), dtype=np.float32)
        self.ctx = int(cfg.get("context_length", 77))
        self.name = "onnx-clip:" + str(cfg.get("name", d.name))
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, (os.cpu_count() or 2) - 1)
        self._v = ort.InferenceSession(str(files["vision"]), opts, providers=["CPUExecutionProvider"])
        self._t = ort.InferenceSession(str(files["text"]), opts, providers=["CPUExecutionProvider"])
        self._tok = Tokenizer.from_file(str(files["tokenizer"]))
        self._tok.enable_truncation(self.ctx)
        self._tok.enable_padding(length=self.ctx)
        self._lock = threading.Lock()
        self.dim = int(self.embed_text("test").shape[0])

    @staticmethod
    def _pick(session, outs):
        names = [o.name for o in session.get_outputs()]
        for i, n in enumerate(names):
            if "embed" in n:
                return outs[i]
        return outs[0]

    def _prep(self, rgb: np.ndarray) -> np.ndarray:
        from PIL import Image
        img = Image.fromarray(np.asarray(rgb, dtype=np.uint8)).convert("RGB")
        w, h = img.size
        s = self.size / min(w, h)                           # CLIP: resize short side, centre crop
        img = img.resize((max(self.size, round(w * s)), max(self.size, round(h * s))), Image.BICUBIC)
        w, h = img.size
        l, t = (w - self.size) // 2, (h - self.size) // 2
        img = img.crop((l, t, l + self.size, t + self.size))
        a = (np.asarray(img, dtype=np.float32) / 255.0 - self.mean) / self.std
        return a.transpose(2, 0, 1)

    def embed_images(self, images):
        x = np.stack([self._prep(i) for i in images]).astype(np.float32)
        with self._lock:
            outs = _infer(self._v, {self._v.get_inputs()[0].name: x})
        return _normalise(np.asarray(self._pick(self._v, outs), dtype=np.float32))

    def embed_text(self, text: str):
        enc = self._tok.encode(text)
        feeds = {}
        for inp in self._t.get_inputs():
            if inp.name == "attention_mask":
                feeds[inp.name] = np.array([enc.attention_mask], dtype=np.int64)
            else:
                feeds[inp.name] = np.array([enc.ids], dtype=np.int64)
        with self._lock:
            outs = _infer(self._t, feeds)
        return _normalise(np.asarray(self._pick(self._t, outs), dtype=np.float32).reshape(1, -1))[0]


def _infer(session, feeds):
    """onnxruntime inference (in-process; not a subprocess, so no timeout applies)."""
    infer = session.run
    return infer(None, feeds)


def _normalise(m: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(m, axis=-1, keepdims=True)
    return m / np.maximum(n, 1e-12)


# ------------------------------------------------------------- embedder registry
_embedder = None
_embedder_lock = threading.Lock()
_embedder_error: str | None = None


def set_embedder(e) -> None:
    """Install an embedder (tests pass a fake); None resets to lazy auto-load."""
    global _embedder, _embedder_error
    with _embedder_lock:
        _embedder = e
        _embedder_error = None


def get_embedder():
    """The active embedder, loading the real one lazily; None when unavailable."""
    global _embedder, _embedder_error
    with _embedder_lock:
        if _embedder is not None:
            return _embedder
        if unavailable_reason() is not None:
            return None
        try:
            _embedder = OnnxClipEmbedder()
        except Exception as exc:               # a corrupt model must not break the library
            _embedder_error = f"Semantic model failed to load: {exc}"
            return None
        return _embedder


def unavailable_message() -> str:
    return _embedder_error or unavailable_reason() or "Semantic search is not ready."


# ------------------------------------------------------------------------ store
def content_hash(path: Path) -> str:
    """Size plus head, middle and tail samples: cheap on multi-GB libraries, stable across renames."""
    size = path.stat().st_size
    h = hashlib.sha256(str(size).encode())
    with open(path, "rb") as fh:
        for pos in sorted({0, max(0, size // 2 - HASH_SAMPLE // 2), max(0, size - HASH_SAMPLE)}):
            fh.seek(pos)
            h.update(fh.read(HASH_SAMPLE))
    return h.hexdigest()[:32]


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / DB_NAME
        self._lock = threading.RLock()
        self._cache: tuple | None = None
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, hash TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS embeddings(hash TEXT, model TEXT, dim INTEGER, vec BLOB, PRIMARY KEY(hash, model))")

    def _conn(self):
        c = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        return _Closing(c)

    def file_hash(self, rel: str, sig: tuple[int, int]) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT size, mtime_ns, hash FROM files WHERE path=?", (rel,)).fetchone()
        return row[2] if row and (row[0], row[1]) == tuple(sig) else None

    def set_file(self, rel: str, sig: tuple[int, int], h: str) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?)", (rel, sig[0], sig[1], h))

    def has(self, h: str, model: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM embeddings WHERE hash=? AND model=?", (h, model)).fetchone() is not None

    def put(self, rows: list[tuple[str, str, np.ndarray]]) -> None:
        with self._conn() as c:
            c.execute("BEGIN")
            c.executemany("INSERT OR REPLACE INTO embeddings VALUES(?,?,?,?)",
                          [(h, m, int(v.shape[0]), np.asarray(v, dtype=np.float32).tobytes()) for h, m, v in rows])
            c.execute("COMMIT")

    def count(self, model: str, rels=None) -> int:
        with self._conn() as c:
            if rels is None:
                return c.execute("SELECT COUNT(*) FROM embeddings WHERE model=?", (model,)).fetchone()[0]
            have = {p: h for p, h in c.execute("SELECT path, hash FROM files")}
            hashes = {h for (h,) in c.execute("SELECT hash FROM embeddings WHERE model=?", (model,))}
        return sum(1 for r in rels if have.get(r) in hashes)

    def matrix(self, model: str):
        """(path -> row index, float32 matrix) for every embedded file; cached until the table changes."""
        with self._conn() as c:
            stamp = c.execute("SELECT COUNT(*), COALESCE(MAX(rowid),0) FROM embeddings WHERE model=?", (model,)).fetchone()
            fstamp = c.execute("SELECT COUNT(*), COALESCE(MAX(rowid),0) FROM files").fetchone()
            key = (model, stamp, fstamp)
            if self._cache and self._cache[0] == key:
                return self._cache[1], self._cache[2]
            rows = {h: (d, v) for h, d, v in c.execute("SELECT hash, dim, vec FROM embeddings WHERE model=?", (model,))}
            files = list(c.execute("SELECT path, hash FROM files"))
        hashes = list(rows)
        pos = {h: i for i, h in enumerate(hashes)}
        mat = (np.stack([np.frombuffer(rows[h][1], dtype=np.float32) for h in hashes])
               if hashes else np.zeros((0, 0), dtype=np.float32))
        index = {p: pos[h] for p, h in files if h in pos}
        self._cache = (key, index, mat)
        return index, mat

    def clear(self) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM embeddings")
            c.execute("DELETE FROM files")
        self._cache = None


class _Closing:
    def __init__(self, c):
        self.c = c

    def __enter__(self):
        return self.c

    def __exit__(self, *a):
        self.c.close()


_stores: dict[str, Store] = {}
_stores_lock = threading.Lock()


def get_store(root: Path) -> Store:
    key = str(Path(root).resolve())
    with _stores_lock:
        if key not in _stores:
            _stores[key] = Store(Path(root))
        return _stores[key]


# ---------------------------------------------------------------------- builder
def default_loader(root: Path):
    """2048 px smart preview of a raw (cached beside the library); read-only on the original."""
    import smartpreview

    def load(path: Path) -> np.ndarray:
        return smartpreview.get(path, root)[0]
    return load


class Builder:
    """Embeds a library in the background; resumable (finished files are skipped on any restart)."""

    def __init__(self, root: Path, embedder, loader=None, store: Store | None = None, batch: int = BATCH):
        self.root = Path(root)
        self.embedder = embedder
        self.loader = loader or default_loader(self.root)
        self.store = store or get_store(self.root)
        self.batch = batch
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self.progress = {"running": False, "done": 0, "total": 0, "errors": [], "finished": False}

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, rels: list[str]) -> bool:
        if self.running():
            return False
        self._cancel.clear()
        self.progress = {"running": True, "done": 0, "total": len(rels), "errors": [], "finished": False}
        self._thread = threading.Thread(target=self.run, args=(list(rels),), daemon=True, name="semantic-builder")
        self._thread.start()
        return True

    def cancel(self) -> None:
        self._cancel.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def run(self, rels: list[str]) -> None:
        p, model = self.progress, self.embedder.name
        p.update(running=True, total=len(rels))
        todo: list[tuple[str, str]] = []             # (rel, hash) awaiting embedding

        def flush():
            if not todo:
                return
            imgs, ok = [], []
            for rel, h in todo:
                try:
                    imgs.append(self.loader(self.root / rel))
                    ok.append(h)
                except Exception as exc:
                    p["errors"].append(f"{rel}: {exc}")
            if imgs:
                try:
                    vecs = self.embedder.embed_images(imgs)
                    self.store.put([(h, model, v) for h, v in zip(ok, vecs)])
                except Exception as exc:
                    p["errors"].append(f"batch: {exc}")
            p["done"] += len(todo)
            todo.clear()

        try:
            for rel in rels:
                if self._cancel.is_set():
                    break
                path = self.root / rel
                try:
                    st = path.stat()
                    sig = (st.st_size, st.st_mtime_ns)
                    h = self.store.file_hash(rel, sig)
                    if h is None:
                        h = content_hash(path)
                        self.store.set_file(rel, sig, h)
                except OSError as exc:
                    p["errors"].append(f"{rel}: {exc}")
                    p["done"] += 1
                    continue
                if self.store.has(h, model) or any(h == x for _, x in todo):
                    p["done"] += 1
                    continue
                todo.append((rel, h))
                if len(todo) >= self.batch:
                    flush()
            if not self._cancel.is_set():
                flush()
        finally:
            p.update(running=False, finished=not self._cancel.is_set())


_builders: dict[str, Builder] = {}


def get_builder(root: Path, embedder) -> Builder:
    key = str(Path(root).resolve())
    b = _builders.get(key)
    if b is None or b.embedder is not embedder:
        b = _builders[key] = Builder(root, embedder)
    return b


# ----------------------------------------------------------------------- search
def _scores(root: Path, embedder, qvec: np.ndarray, rels: list[str]) -> dict[str, float]:
    index, mat = get_store(root).matrix(embedder.name)
    if not len(mat):
        return {}
    sims = mat @ np.asarray(qvec, dtype=np.float32)
    return {r: float(sims[index[r]]) for r in rels if r in index}


def rank_text(root: Path, embedder, query: str, items: list[dict], top: int = DEFAULT_TOP) -> list[dict]:
    """Items that have an embedding, best cosine match first (at most ``top``)."""
    sc = _scores(root, embedder, embedder.embed_text(query), [i["path"] for i in items])
    return _order(items, sc, top)


def rank_similar(root: Path, embedder, rel: str, items: list[dict], top: int = DEFAULT_TOP) -> list[dict]:
    """Photos visually closest to ``rel`` (the photo itself first). Raises KeyError if it is not embedded yet."""
    index, mat = get_store(root).matrix(embedder.name)
    if rel not in index:
        raise KeyError("That photo has not been indexed yet. Build the semantic index first.")
    sc = _scores(root, embedder, mat[index[rel]], [i["path"] for i in items])
    return _order(items, sc, top)


def _order(items, scores, top):
    kept = sorted((i for i in items if i["path"] in scores), key=lambda i: -scores[i["path"]])[:top]
    return [dict(i, score=round(scores[i["path"]], 4)) for i in kept]


def keyword_rank(items: list[dict], query: str, top: int = DEFAULT_TOP) -> list[dict]:
    """Fallback without a model: any word matching a name, keyword, camera, lens or date scores."""
    words = [w for w in query.lower().replace(",", " ").split() if w]
    if not words:
        return items[:top]
    out = []
    for it in items:
        name = it["path"].lower()
        kws = [k.lower() for k in (it.get("keywords") or [])]
        other = " ".join(str(it.get(k) or "") for k in ("camera", "lens", "date", "label")).lower()
        s = 0.0
        for w in words:
            if w in name:
                s += 1.0
            if any(w == k for k in kws):
                s += 2.0
            elif any(w in k for k in kws):
                s += 1.5
            if w in other:
                s += 0.5
        if s:
            out.append(dict(it, score=round(s, 3)))
    out.sort(key=lambda i: -i["score"])
    return out[:top]


def similar_fallback(items: list[dict], ref: dict, top: int = DEFAULT_TOP) -> list[dict]:
    """Fallback "find similar": shared keywords, same camera, same day."""
    rk = {k.lower() for k in (ref.get("keywords") or [])}
    day = str(ref.get("date") or "")[:10]
    out = []
    for it in items:
        s = len(rk & {k.lower() for k in (it.get("keywords") or [])}) * 2.0
        if ref.get("camera") and it.get("camera") == ref.get("camera"):
            s += 0.5
        if day and str(it.get("date") or "")[:10] == day:
            s += 1.0
        if it["path"] == ref["path"]:
            s += 100
        if s:
            out.append(dict(it, score=round(s, 3)))
    out.sort(key=lambda i: -i["score"])
    return out[:top]


def search(root: Path, items: list[dict], query: str = "", similar: str = "",
           embedder=None, top: int = DEFAULT_TOP) -> tuple[list[dict], dict]:
    """
    Rank ``items`` for a text query or a reference photo. Returns (ranked, info);
    ``info["mode"]`` is "semantic" or "keyword" and ``info["message"]`` explains a fallback.
    """
    embedder = embedder if embedder is not None else get_embedder()
    info = {"mode": "semantic", "message": "", "indexed": 0, "library": len(items)}
    if embedder is not None:
        info["indexed"] = get_store(root).count(embedder.name, [i["path"] for i in items])
        try:
            if similar:
                return rank_similar(root, embedder, similar, items, top), info
            if query.strip():
                if info["indexed"] == 0:
                    info.update(mode="keyword", message="The semantic index is empty. Build it from the Library filter bar; using keyword search meanwhile.")
                    return keyword_rank(items, query, top), info
                if info["indexed"] < len(items):
                    info["message"] = f"{len(items) - info['indexed']} photos are not indexed yet and are left out."
                return rank_text(root, embedder, query, items, top), info
        except KeyError as exc:
            info.update(mode="keyword", message=exc.args[0])
    else:
        info.update(mode="keyword", message=unavailable_message())
    if similar:
        ref = next((i for i in items if i["path"] == similar), None)
        return (similar_fallback(items, ref, top) if ref else []), info
    return keyword_rank(items, query, top), info


# --------------------------------------------------------------------- download
def download(directory: Path | None = None, repo: str = MODEL_REPO, log=print) -> Path:
    """Fetch the model files. Only ever called from the explicit CLI command."""
    import urllib.request
    d = Path(directory) if directory else model_dir()
    d.mkdir(parents=True, exist_ok=True)
    for local, remote in MODEL_FILES.items():
        dest = d / local
        if dest.exists():
            continue
        log(f"downloading {repo}/{remote}")
        tmp = dest.with_suffix(dest.suffix + ".part")
        with urllib.request.urlopen(f"{repo}/{remote}", timeout=DOWNLOAD_TIMEOUT) as r, open(tmp, "wb") as fh:
            while chunk := r.read(1 << 20):
                fh.write(chunk)
        os.replace(tmp, dest)
    return d


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["download"]:
        if "--yes" not in argv:
            print(f"This downloads about 90 MB of model files from {MODEL_REPO}\ninto {model_dir()}.\n"
                  "Nothing is downloaded without --yes:  python semantic.py download --yes")
            return 1
        print("model ready in", download())
        return 0
    if argv[:1] == ["status"]:
        print(unavailable_reason() or f"ready ({model_dir()})")
        return 0
    print("usage: python semantic.py download --yes | status")
    return 2


if __name__ == "__main__":
    sys.exit(main())
