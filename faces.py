#!/usr/bin/env python3
"""
Faces / People: find faces, group them into people, name them, filter the grid.

Everything is local and opt-in: nothing runs until you click "Find faces" in the
Library People panel (or run ``photo-room faces scan``), and nothing is ever
downloaded. The originals are only read.

Detector (finds face boxes), duck typed::

    name: str
    detect(rgb uint8 array) -> list[(x, y, w, h)] as fractions of the frame

* Default: the OpenCV Haar frontal-face cascade already used for cull scoring
  (``cull._cascade``). Frontal faces only; misses profiles, tilted and small
  faces and produces false positives on busy textures.
* Better, if present: a YuNet ONNX file named ``face_detection_yunet*.onnx``
  in the model folder, run through ``cv2.FaceDetectorYN`` (needs no extra
  Python package, only the file).

Embedder (turns a face crop into a vector), duck typed::

    name: str
    dim: int
    threshold: float          cosine similarity at which two faces are one person
    embed_faces(list[uint8 RGB crop]) -> float array (n, dim), L2-normalised

* Default ``LbpEmbedder``: local-binary-pattern histograms over a 4x4 grid of a
  64 px grey face. This captures texture and rough lighting, NOT identity. It
  groups the same person in similar light and pose and will mix up different
  people or split one person across lighting. Treat its groups as suggestions
  to fix by naming and merging.
* Better, with the optional extra (``uv sync --extra faces``) and a model: an
  ArcFace/SFace-style recognition ONNX named ``face_embedding*.onnx`` (112x112
  RGB input scaled to -1..1, one embedding output) run by onnxruntime.

Model folder: ``$PHOTO_ROOM_FACES_MODEL`` or ``$PHOTO_ROOM_CACHE_DIR/faces-model``
(default ``~/.cache/photo-room/faces-model``).

Storage: ``.photo-room-faces.sqlite`` beside the library. Faces are keyed by the
file's content hash (as in semantic.py) so a rename keeps them. People you named
or merged are never re-clustered away: new faces join the nearest existing
person if close enough, otherwise start a new unnamed person.
"""
from __future__ import annotations

import io
import os
import sqlite3
import sys
import threading
from pathlib import Path

import numpy as np

import semantic
from semantic import _Closing, _normalise, content_hash

DB_NAME = ".photo-room-faces.sqlite"
DETECT_EDGE = 960            # faces are looked for on a copy no larger than this
CROP = 64                    # grey crop edge for the LBP embedder
EMBED_CROP = 112             # crop edge for a recognition model
THUMB = 56                   # stored face thumbnail edge (JPEG)
MARGIN = 0.15                # context kept around a detected box
MIN_FACE = 0.04              # smaller boxes (fraction of the long edge) are ignored
IGNORED = -1                 # person_id of faces the user removed
TOL = 6                      # LBP dead-band in grey levels (0..255 after contrast stretch)

LIMITS = ("Faces use a frontal Haar detector and, without a model, texture-histogram "
          "grouping that is not real face recognition: expect misses, false faces and "
          "mixed-up or split people. Name and merge people to correct it. Install the "
          "faces extra and a face model for better grouping (docs/FACES-PEOPLE.md).")


# --------------------------------------------------------------- model location
def model_dir() -> Path:
    env = os.environ.get("PHOTO_ROOM_FACES_MODEL")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("PHOTO_ROOM_CACHE_DIR")
    root = Path(base) if base else Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "photo-room"
    return root / "faces-model"


def _find(pattern: str) -> Path | None:
    d = model_dir()
    return next(iter(sorted(d.glob(pattern))), None) if d.is_dir() else None


# ------------------------------------------------------------------- detectors
class HaarDetector:
    name = "haar"

    def detect(self, rgb):
        import cv2

        import cull
        c = cull._cascade("haarcascade_frontalface_default.xml")
        if c is None:
            return []
        gray = cv2.cvtColor(np.ascontiguousarray(rgb), cv2.COLOR_RGB2GRAY)
        h, w = gray.shape
        s = min(1.0, DETECT_EDGE / max(h, w))
        small = cv2.resize(gray, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA) if s < 1 else gray
        sh, sw = small.shape
        found = c.detectMultiScale(small, scaleFactor=1.1, minNeighbors=6,
                                   minSize=(max(24, int(max(sh, sw) * MIN_FACE)),) * 2)
        return [(x / sw, y / sh, fw / sw, fh / sh) for (x, y, fw, fh) in found]


class YuNetDetector:
    name = "yunet"

    def __init__(self, model: Path):
        import cv2
        self._cv2, self._model = cv2, str(model)
        cv2.FaceDetectorYN.create(self._model, "", (320, 320))           # fail early if unsupported

    def detect(self, rgb):
        cv2 = self._cv2
        h, w = rgb.shape[:2]
        s = min(1.0, DETECT_EDGE / max(h, w))
        img = cv2.cvtColor(np.ascontiguousarray(rgb), cv2.COLOR_RGB2BGR)
        if s < 1:
            img = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
        ih, iw = img.shape[:2]
        det = cv2.FaceDetectorYN.create(self._model, "", (iw, ih), 0.8)
        _, out = det.detect(img)
        if out is None:
            return []
        return [(float(r[0]) / iw, float(r[1]) / ih, float(r[2]) / iw, float(r[3]) / ih) for r in out]


def default_detector():
    m = _find("face_detection_yunet*.onnx")
    if m is not None:
        try:
            return YuNetDetector(m)
        except Exception:
            pass                                   # unsupported OpenCV build: fall back to Haar
    return HaarDetector()


# ------------------------------------------------------------------ embedders
class LbpEmbedder:
    """Uniform-ish LBP histograms over a grid. Texture, not identity (see the module docstring)."""
    name = "lbp-grid-v1"
    GRID, BINS = 4, 32
    dim = GRID * GRID * BINS
    threshold = 0.90

    def embed_faces(self, crops):
        import cv2
        out = []
        for crop in crops:
            g = cv2.cvtColor(np.ascontiguousarray(crop, dtype=np.uint8), cv2.COLOR_RGB2GRAY)
            g = cv2.GaussianBlur(cv2.resize(g, (CROP, CROP), interpolation=cv2.INTER_AREA), (0, 0), 1.0)
            g = cv2.normalize(g, None, 0, 255, cv2.NORM_MINMAX).astype(np.int16)    # lighting: stretch contrast
            c = g[1:-1, 1:-1]
            code = np.zeros_like(c)
            for bit, (dy, dx) in enumerate(((-1, -1), (-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1))):
                nb = g[1 + dy: g.shape[0] - 1 + dy, 1 + dx: g.shape[1] - 1 + dx]
                code |= ((nb > c + TOL).astype(np.int16) << bit)   # a dead-band: flat regions give code 0, not noise
            q = (code >> 3).astype(np.int32)                 # 256 codes -> 32 bins
            n = q.shape[0] // self.GRID
            feats = []
            for gy in range(self.GRID):
                for gx in range(self.GRID):
                    cell = q[gy * n:(gy + 1) * n, gx * n:(gx + 1) * n]
                    feats.append(np.bincount(cell.ravel(), minlength=self.BINS)[: self.BINS] / max(cell.size, 1))
            out.append(np.sqrt(np.concatenate(feats)) - np.sqrt(1.0 / self.BINS))   # centred: cosine then compares shape, not the shared baseline
        return _normalise(np.asarray(out, dtype=np.float32))


class OnnxFaceEmbedder:
    """ArcFace/SFace-style recognition model run with onnxruntime (extra ``faces``)."""
    threshold = 0.40

    def __init__(self, model: Path):
        import onnxruntime as ort
        self._s = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
        self._in = self._s.get_inputs()[0].name
        self.name = "onnx-face:" + Path(model).stem
        self._lock = threading.Lock()
        self.dim = int(self.embed_faces([np.zeros((EMBED_CROP, EMBED_CROP, 3), np.uint8)]).shape[1])

    def embed_faces(self, crops):
        import cv2
        x = np.stack([(cv2.resize(np.asarray(c, np.uint8), (EMBED_CROP, EMBED_CROP)).astype(np.float32) - 127.5) / 127.5
                      for c in crops]).transpose(0, 3, 1, 2)
        with self._lock:
            infer = self._s.run                            # in-process inference, not a subprocess
            out = infer(None, {self._in: x})[0]
        return _normalise(np.asarray(out, dtype=np.float32).reshape(len(crops), -1))


def advanced_embedder():
    """The model embedder when the extra and a model file are present, else None."""
    m = _find("face_embedding*.onnx")
    if m is None:
        return None
    try:
        return OnnxFaceEmbedder(m)
    except Exception:
        return None


_backend: tuple | None = None
_backend_lock = threading.Lock()


def set_backend(detector, embedder) -> None:
    """Install (detector, embedder); tests pass fakes, detector None resets to auto."""
    global _backend
    with _backend_lock:
        _backend = None if detector is None else (detector, embedder)


def get_backend() -> tuple:
    global _backend
    with _backend_lock:
        if _backend is None:
            _backend = (default_detector(), advanced_embedder() or LbpEmbedder())
        return _backend


def describe_backend() -> dict:
    det, emb = get_backend()
    return {"detector": det.name, "embedder": emb.name,
            "advanced": not isinstance(emb, LbpEmbedder) or det.name == "yunet"}


# ---------------------------------------------------------------------- crops
def crop_face(rgb: np.ndarray, box, margin: float = MARGIN) -> np.ndarray:
    h, w = rgb.shape[:2]
    x, y, bw, bh = box
    mx, my = bw * margin, bh * margin
    x0, y0 = max(0, int((x - mx) * w)), max(0, int((y - my) * h))
    x1, y1 = min(w, int((x + bw + mx) * w) + 1), min(h, int((y + bh + my) * h) + 1)
    return rgb[y0:y1, x0:x1]


def thumb_jpeg(crop: np.ndarray) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(crop)).convert("RGB").resize((THUMB, THUMB)).save(buf, "JPEG", quality=80)
    return buf.getvalue()


# ----------------------------------------------------------------------- store
class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / DB_NAME
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, hash TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS scanned(hash TEXT, model TEXT, PRIMARY KEY(hash, model))")
            c.execute("CREATE TABLE IF NOT EXISTS faces(id INTEGER PRIMARY KEY AUTOINCREMENT, hash TEXT, model TEXT,"
                      " x REAL, y REAL, w REAL, h REAL, dim INTEGER, vec BLOB, thumb BLOB, person_id INTEGER)")
            c.execute("CREATE INDEX IF NOT EXISTS faces_hash ON faces(hash)")
            c.execute("CREATE TABLE IF NOT EXISTS people(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL DEFAULT '')")

    def _conn(self):
        return _Closing(sqlite3.connect(str(self.path), timeout=30, isolation_level=None))

    # files / scan bookkeeping (a "scan" = detector ran, even if it found nothing)
    def file_hash(self, rel, sig):
        with self._conn() as c:
            row = c.execute("SELECT size, mtime_ns, hash FROM files WHERE path=?", (rel,)).fetchone()
        return row[2] if row and (row[0], row[1]) == tuple(sig) else None

    def set_file(self, rel, sig, h):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?)", (rel, sig[0], sig[1], h))

    def is_scanned(self, h, model) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM scanned WHERE hash=? AND model=?", (h, model)).fetchone() is not None

    def add_faces(self, h, model, rows) -> None:
        """rows: [(box, vec, thumb_bytes)] for one image (replaces earlier faces of that hash/model)."""
        with self._conn() as c:
            c.execute("BEGIN")
            c.execute("DELETE FROM faces WHERE hash=? AND model=?", (h, model))
            c.executemany("INSERT INTO faces(hash, model, x, y, w, h, dim, vec, thumb, person_id) VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                          [(h, model, *map(float, b), int(v.shape[0]), np.asarray(v, np.float32).tobytes(), t)
                           for b, v, t in rows])
            c.execute("INSERT OR REPLACE INTO scanned VALUES(?,?)", (h, model))
            c.execute("COMMIT")

    def scanned_count(self, model, rels) -> int:
        with self._conn() as c:
            have = {p: h for p, h in c.execute("SELECT path, hash FROM files")}
            done = {h for (h,) in c.execute("SELECT hash FROM scanned WHERE model=?", (model,))}
        return sum(1 for r in rels if have.get(r) in done)

    # clustering
    def faces_for_cluster(self, model):
        with self._conn() as c:
            rows = c.execute("SELECT id, vec, person_id FROM faces WHERE model=? ORDER BY id", (model,)).fetchall()
        return [(i, np.frombuffer(v, np.float32), p) for i, v, p in rows]

    def assign(self, pairs) -> None:
        with self._conn() as c:
            c.execute("BEGIN")
            c.executemany("UPDATE faces SET person_id=? WHERE id=?", pairs)
            c.execute("COMMIT")

    def new_person(self, name: str = "") -> int:
        with self._conn() as c:
            return c.execute("INSERT INTO people(name) VALUES(?)", (name,)).lastrowid

    # people
    def people(self, model=None) -> list[dict]:
        """People with the number of distinct photos they appear in (named first, then by count)."""
        with self._conn() as c:
            names = dict(c.execute("SELECT id, name FROM people"))
            q = ("SELECT f.person_id, COUNT(DISTINCT fl.path), COUNT(*), MIN(f.id) FROM faces f "
                 "JOIN files fl ON fl.hash = f.hash WHERE f.person_id > 0")
            args: tuple = ()
            if model:
                q += " AND f.model=?"
                args = (model,)
            rows = c.execute(q + " GROUP BY f.person_id", args).fetchall()
        out = [{"id": pid, "name": names.get(pid, ""), "photos": n, "faces": nf, "face_id": fid}
               for pid, n, nf, fid in rows]
        out.sort(key=lambda p: (not p["name"], -p["photos"], p["id"]))
        return out

    def get_person(self, pid):
        with self._conn() as c:
            row = c.execute("SELECT id, name FROM people WHERE id=?", (pid,)).fetchone()
        return {"id": row[0], "name": row[1]} if row else None

    def rename(self, pid, name) -> None:
        with self._conn() as c:
            c.execute("UPDATE people SET name=? WHERE id=?", (name, pid))

    def merge(self, src, dst) -> None:
        with self._conn() as c:
            c.execute("BEGIN")
            c.execute("UPDATE faces SET person_id=? WHERE person_id=?", (dst, src))
            c.execute("DELETE FROM people WHERE id=?", (src,))
            c.execute("COMMIT")

    def remove(self, pid) -> None:
        """Hide a person (false faces, strangers): their faces stay ignored and are not re-clustered."""
        with self._conn() as c:
            c.execute("BEGIN")
            c.execute("UPDATE faces SET person_id=? WHERE person_id=?", (IGNORED, pid))
            c.execute("DELETE FROM people WHERE id=?", (pid,))
            c.execute("COMMIT")

    def paths_of(self, pid) -> set[str]:
        with self._conn() as c:
            return {p for (p,) in c.execute(
                "SELECT DISTINCT fl.path FROM faces f JOIN files fl ON fl.hash=f.hash WHERE f.person_id=?", (pid,))}

    def thumb(self, face_id):
        with self._conn() as c:
            row = c.execute("SELECT thumb FROM faces WHERE id=?", (face_id,)).fetchone()
        return row[0] if row else None

    def face_count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM faces").fetchone()[0]


_stores: dict[str, Store] = {}
_stores_lock = threading.Lock()


def get_store(root: Path) -> Store:
    key = str(Path(root).resolve())
    with _stores_lock:
        if key not in _stores:
            _stores[key] = Store(Path(root))
        return _stores[key]


# ------------------------------------------------------------------ clustering
def cluster(store: Store, embedder) -> int:
    """
    Give every unassigned face a person: the nearest existing person (mean of
    its faces) when cosine similarity >= ``embedder.threshold``, else a new
    unnamed person. Faces already assigned (including by the user) never move.
    Returns the number of faces newly assigned.
    """
    faces = store.faces_for_cluster(embedder.name)
    sums: dict[int, np.ndarray] = {}
    for _, v, p in faces:
        if p is not None and p > 0:
            sums[p] = sums.get(p, 0) + v
    cents = {p: s / max(np.linalg.norm(s), 1e-12) for p, s in sums.items()}
    pairs = []
    for fid, v, p in faces:
        if p is not None:
            continue
        best, score = None, -1.0
        for pid, c in cents.items():
            s = float(v @ c)
            if s > score:
                best, score = pid, s
        if best is None or score < embedder.threshold:
            best = store.new_person()
            sums[best] = np.zeros_like(v)
        sums[best] = sums[best] + v
        cents[best] = sums[best] / max(np.linalg.norm(sums[best]), 1e-12)
        pairs.append((best, fid))
    if pairs:
        store.assign(pairs)
    return len(pairs)


# --------------------------------------------------------------------- builder
class Builder:
    """Scans a library for faces in the background; resumable (scanned files are skipped)."""

    def __init__(self, root: Path, detector, embedder, loader=None, store: Store | None = None):
        self.root = Path(root)
        self.detector, self.embedder = detector, embedder
        self.loader = loader or semantic.default_loader(self.root)
        self.store = store or get_store(self.root)
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self.progress = {"running": False, "done": 0, "total": 0, "faces": 0, "errors": [], "finished": False}

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, rels: list[str]) -> bool:
        if self.running():
            return False
        self._cancel.clear()
        self.progress = {"running": True, "done": 0, "total": len(rels), "faces": 0, "errors": [], "finished": False}
        self._thread = threading.Thread(target=self.run, args=(list(rels),), daemon=True, name="faces-builder")
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
        try:
            for rel in rels:
                if self._cancel.is_set():
                    break
                try:
                    path = self.root / rel
                    st = path.stat()
                    sig = (st.st_size, st.st_mtime_ns)
                    h = self.store.file_hash(rel, sig)
                    if h is None:
                        h = content_hash(path)
                        self.store.set_file(rel, sig, h)
                    if not self.store.is_scanned(h, model):
                        self._scan(path, h, model)
                except Exception as exc:
                    p["errors"].append(f"{rel}: {exc}")
                p["done"] += 1
            cluster(self.store, self.embedder)
        finally:
            p.update(running=False, finished=not self._cancel.is_set())

    def _scan(self, path: Path, h: str, model: str) -> None:
        rgb = self.loader(path)
        boxes = [b for b in self.detector.detect(rgb) if max(b[2] * rgb.shape[1], b[3] * rgb.shape[0]) >= 8]
        rows = []
        if boxes:
            crops = [crop_face(rgb, b) for b in boxes]
            vecs = self.embedder.embed_faces(crops)
            rows = [(b, v, thumb_jpeg(c)) for b, v, c in zip(boxes, vecs, crops)]
        self.store.add_faces(h, model, rows)
        self.progress["faces"] += len(rows)


_builders: dict[str, Builder] = {}


def get_builder(root: Path) -> Builder:
    det, emb = get_backend()
    key = str(Path(root).resolve())
    b = _builders.get(key)
    if b is None or b.embedder is not emb or b.detector is not det:
        b = _builders[key] = Builder(root, det, emb)
    return b


# ---------------------------------------------------------------- grid filter
def filter_items(root: Path, items: list[dict], person: str) -> list[dict]:
    """Items whose photo shows the person with this id."""
    try:
        pid = int(person)
    except (TypeError, ValueError):
        raise ValueError("person must be a person id") from None
    paths = get_store(root).paths_of(pid)
    return [it for it in items if it["path"] in paths]


# ------------------------------------------------------------------------- CLI
def _list_rels(root: Path) -> list[str]:
    import library
    out = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if p.is_file() and p.suffix.lower() in library.RAW_SUFFIXES:
            out.append(rel.as_posix())
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="photo-room faces",
                                 description="Find faces and group them into people (local, opt-in).")
    ap.add_argument("action", choices=["scan", "list", "name", "merge", "remove", "status"])
    ap.add_argument("folder", help="the library folder")
    ap.add_argument("args", nargs="*", help="name: ID NAME...   merge: FROM_ID INTO_ID   remove: ID")
    a = ap.parse_args(argv)
    root = Path(a.folder).expanduser()
    if not root.is_dir():
        print(f"not a folder: {root}", file=sys.stderr)
        return 2
    store = get_store(root)
    try:
        if a.action == "scan":
            det, emb = get_backend()
            print(f"detector {det.name}, embedder {emb.name}")
            if not describe_backend()["advanced"]:
                print("note:", LIMITS)
            b = Builder(root, det, emb)
            b.start(_list_rels(root))
            b.join()
            for e in b.progress["errors"][:10]:
                print("error:", e, file=sys.stderr)
            print(f"{b.progress['done']} photos, {b.progress['faces']} new faces, {len(store.people(emb.name))} people")
        elif a.action == "list":
            for p in store.people(get_backend()[1].name):
                print(f"{p['id']:>4}  {p['name'] or '(unnamed)':<24} {p['photos']} photos")
        elif a.action == "status":
            print(describe_backend(), f"faces stored: {store.face_count()}")
        elif a.action == "name":
            store.rename(int(a.args[0]), " ".join(a.args[1:]).strip())
        elif a.action == "merge":
            store.merge(int(a.args[0]), int(a.args[1]))
        elif a.action == "remove":
            store.remove(int(a.args[0]))
    except (IndexError, ValueError) as exc:
        print(f"bad arguments: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
