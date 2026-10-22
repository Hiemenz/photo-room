#!/usr/bin/env python3
"""
The Library index: the in-memory source of truth the grid is served from.

* Reads never touch exiftool or the disk. Every photo's rating, flag, label,
  keywords, camera, lens, ISO and date live in memory (and in a small SQLite
  database in WAL mode, so a restart does not rescan 20,000 files).
* Writes (rating, flag, label, keywords) update memory and the database in
  one transaction and return. The XMP sidecars are written afterwards by a
  background writer that debounces, coalesces (ten changes to one photo are
  one write; fifty photos given the same rating are one exiftool command) and
  writes atomically. The queue of unwritten changes is the ``pending`` table,
  which is the write-ahead log: if the process dies, the next start finds the
  rows and finishes the writes. Nothing acknowledged is lost on a crash.
* A change feed (sequence numbers + long-poll / SSE in ``libapi``) lets the
  grid update without a reload, whether the change came from this tool or from
  another program editing a sidecar.
* A rescan compares (size, mtime) of each photo and its sidecar with what the
  index remembers and asks exiftool only about files that changed.
"""

from __future__ import annotations

import atexit
import json
import os
import sqlite3
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import exifpool
import fsutil
import library

DB_NAME = ".photo-room-index.sqlite"
DB_ENABLED = True            # tests that only need memory switch this off
SYNC = False                 # True: set_fields() waits for the sidecar write (legacy behaviour)
RESCAN_MAX_AGE = 3.0         # seconds before a query kicks a background rescan; 0 = rescan inline
DEBOUNCE = 0.35              # quiet period before queued sidecar writes start
MAX_DELAY = 2.0              # ...but never hold a change longer than this
CHUNK = 400
CHANGE_LOG = 20000
SQLITE_SYNCHRONOUS = "NORMAL"   # WAL + NORMAL survives a process crash; FULL also survives power loss

RAW_SUFFIXES = library.RAW_SUFFIXES
READ_TAGS = ["-XMP-xmp:Rating", "-XMP-xmp:Label", "-XMP-dc:Subject", "-Model", "-LensModel",
             "-ISO", "-DateTimeOriginal", "-CreateDate", "-FocalLength#", "-GPSLatitude#",
             "-XMP-crs:ProcessVersion", "-XMP-crs:Exposure2012", "-XMP-crs:Contrast2012",
             "-XMP-crs:Highlights2012", "-XMP-crs:Shadows2012", "-XMP-crs:ColorTemperature",
             "-XMP-crs:Vibrance", "-XMP-crs:Saturation", "-XMP-crs:Temperature"]
# Any of these in the photo or its sidecar means it has been developed ("edited").
EDIT_TAGS = ("ProcessVersion", "Exposure2012", "Contrast2012", "Highlights2012", "Shadows2012",
             "ColorTemperature", "Vibrance", "Saturation", "Temperature")
MUTABLE = ("rating", "label", "keywords", "pick")


def _flag(rating: int, pick: bool) -> str:
    return "reject" if rating == -1 else ("pick" if pick else "none")


def _record(rel: str, meta: dict, pick: bool) -> dict:
    return {"path": rel, "name": rel.rsplit("/", 1)[-1], "rating": meta["rating"], "label": meta["label"],
            "keywords": meta["keywords"], "camera": meta["camera"], "lens": meta.get("lens", ""),
            "iso": meta.get("iso", 0), "focal": meta.get("focal", 0), "edited": bool(meta.get("edited")),
            "gps": bool(meta.get("gps")), "date": meta["date"], "flag": _flag(meta["rating"], pick),
            "kind": library.kind_of(rel) or "raw", "editable": library.is_editable(rel)}


def parse_meta(entry: dict, side: dict | None = None, dng: bool = False) -> dict:
    """
    Index fields from one exiftool record plus its sidecar's record. A sidecar
    wins for the XMP fields; next to a DNG (which keeps its own metadata) it
    only overrides the tags it actually carries.
    """
    meta = library.parse_entry(entry)
    if side is not None:
        s = library.parse_entry(side)
        for key, tag in (("rating", "Rating"), ("label", "Label"), ("keywords", "Subject")):
            if not dng or tag in side:
                meta[key] = s[key]
    try:
        iso = int(float(entry.get("ISO") or 0))
    except (TypeError, ValueError):
        iso = 0
    meta["lens"] = " ".join(str(entry.get("LensModel") or "").split())
    meta["iso"] = iso
    try:
        meta["focal"] = round(float(entry.get("FocalLength") or 0), 1)
    except (TypeError, ValueError):
        meta["focal"] = 0
    both = [entry] + ([side] if side is not None else [])
    meta["gps"] = any(e.get("GPSLatitude") not in (None, "") for e in both)
    meta["edited"] = any(t in e for e in both for t in EDIT_TAGS)
    return meta


class Index:
    def __init__(self, root: Path | None, db_path: Path | None = None, writer: bool = True):
        self.root = Path(root).resolve() if root is not None else None
        self._lock = threading.RLock()
        self._recs: dict[str, dict] = {}          # rel -> public record (copy-on-write)
        self._picks: set[str] = set()
        self._sigs: dict[str, tuple] = {}
        self._gen: dict[str, int] = {}            # rel -> mutation counter (guards rescans)
        self._pending: dict[str, tuple[int, dict]] = {}
        self._seq = 0
        self._pend_seq = 0
        self._changes: deque = deque(maxlen=CHANGE_LOG)
        self._cond = threading.Condition(self._lock)
        self.version = 0
        self._fver: dict[str, int] = {}
        self._cache: dict = {}
        self._scanned_at = 0.0
        self._scan_lock = threading.Lock()
        self._scan_thread: threading.Thread | None = None
        self.errors: list[str] = []
        self._closed = False

        self._db: sqlite3.Connection | None = None
        self._dblock = threading.Lock()
        if db_path is not None and DB_ENABLED:
            self._open_db(db_path)

        self._wake = threading.Event()
        self._flush_req = threading.Event()
        self._idle = threading.Condition(threading.Lock())
        self._writer: threading.Thread | None = None
        self._first_pending_at = 0.0
        self._last_pending_at = 0.0
        if writer and self.root is not None:
            self._writer = threading.Thread(target=self._writer_loop, daemon=True, name="library-writer")
            self._writer.start()
            if self._pending:
                self._wake.set()

    @classmethod
    def in_memory(cls, items: list[dict]) -> "Index":
        """A rootless index over ready-made records (benchmarks, tests)."""
        idx = cls(None, writer=False)
        for it in items:
            rec = dict(it)
            idx._recs[rec["path"]] = rec
            if rec.get("flag") == "pick":
                idx._picks.add(rec["path"])
        idx.version += 1
        idx._scanned_at = time.monotonic()
        return idx

    # ------------------------------------------------------------------ db
    def _open_db(self, path: Path) -> None:
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        db = self._db
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(f"PRAGMA synchronous={SQLITE_SYNCHRONOUS}")
        db.execute("CREATE TABLE IF NOT EXISTS photos(rel TEXT PRIMARY KEY, sig TEXT, data TEXT, pick INTEGER)")
        db.execute("CREATE TABLE IF NOT EXISTS pending(rel TEXT PRIMARY KEY, seq INTEGER, fields TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        for rel, sig, data, pick in db.execute("SELECT rel, sig, data, pick FROM photos"):
            try:
                meta = json.loads(data)
                if "gps" in meta:       # rows from before focal/edit/GPS were indexed get re-read
                    self._sigs[rel] = tuple(json.loads(sig))
            except ValueError:
                continue
            if pick:
                self._picks.add(rel)
            self._recs[rel] = _record(rel, meta, bool(pick))
        for rel, seq, fields in db.execute("SELECT rel, seq, fields FROM pending ORDER BY seq"):
            try:
                self._pending[rel] = (seq, json.loads(fields))
            except ValueError:
                continue
            self._pend_seq = max(self._pend_seq, seq)
        if self._pending:
            self._first_pending_at = self._last_pending_at = time.monotonic()
        if self._recs:
            self.version += 1
            self._scanned_at = 0.0       # loaded from disk: rescan in the background

    def _db_exec(self, sql: str, rows=None, many: bool = False) -> None:
        if self._db is None:
            return
        with self._dblock:
            self._db.execute("BEGIN")
            try:
                if many:
                    self._db.executemany(sql, rows)
                else:
                    self._db.execute(sql, rows or ())
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def _db_tx(self, statements: list[tuple[str, list, bool]]) -> None:
        if self._db is None:
            return
        with self._dblock:
            self._db.execute("BEGIN")
            try:
                for sql, rows, many in statements:
                    if many:
                        self._db.executemany(sql, rows)
                    else:
                        self._db.execute(sql, rows)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def carry_picks(self, mapping: dict[str, str]) -> None:
        """Photos were moved on disk (old rel -> new rel): keep their pick flags.

        Call ``flush()`` before the move and ``scan()`` after it; the scan drops
        the old paths and indexes the new ones with the picks carried here.
        """
        with self._lock:
            moved = [n for o, n in mapping.items() if o in self._picks]
            self._picks.update(moved)
        if moved:
            self._db_tx([("UPDATE photos SET pick=1 WHERE rel=?", [(n,) for n in moved], True)])

    def _meta_of(self, rec: dict) -> dict:
        out = {k: rec[k] for k in ("rating", "label", "keywords", "camera", "lens", "iso", "date")}
        out.update(focal=rec.get("focal", 0), edited=bool(rec.get("edited")), gps=bool(rec.get("gps")))
        return out

    # ------------------------------------------------------------- reading
    def touch(self) -> None:
        with self._lock:
            self.version += 1
            self._cache.clear()

    def get(self, rel: str) -> dict | None:
        with self._lock:
            return self._recs.get(rel)

    def get_many(self, rels) -> dict[str, dict]:
        with self._lock:
            return {r: self._recs[r] for r in rels if r in self._recs}

    def items(self) -> list[dict]:
        with self._lock:
            return list(self._recs.values())

    def __len__(self) -> int:
        return len(self._recs)

    def _sorted(self, key: str, desc: bool) -> list[dict]:
        deps = {"name": (), "date": ("date",), "rating": ("rating",), "label": ("label",),
                "flag": ("rating", "pick"), "camera": ("camera",), "lens": ("lens",), "iso": ("iso",)}[key]
        stamp = (self._fver.get("_struct", 0), *(self._fver.get(d, 0) for d in deps))
        ck = ("sorted", key, desc)
        hit = self._cache.get(ck)
        if hit and hit[0] == stamp:
            return hit[1]
        out = library.sort_items(list(self._recs.values()), key, desc)
        self._cache[ck] = (stamp, out)
        return out

    def query(self, criteria: dict | None, sort: str = "name", desc: bool = False,
              offset: int = 0, limit: int = 200) -> tuple[int, list[dict], int]:
        """(total matching, one page, library size). Raises ValueError on bad input."""
        criteria = {k: v for k, v in (criteria or {}).items() if v not in (None, "")}
        with self._lock:
            if sort not in library.SORT_KEYS:
                raise ValueError(f"Unknown sort key: {sort!r}")
            ck = ("match", self.version, tuple(sorted(criteria.items())), sort, desc)
            matched = self._cache.get(ck)
            if matched is None:
                ordered = self._sorted(sort, desc)
                matched = library.filter_items(ordered, criteria) if criteria else ordered
                for k in [k for k in self._cache if k[0] == "match"]:
                    del self._cache[k]            # keep only the latest generation
                self._cache[ck] = matched
            return len(matched), matched[offset:offset + limit], len(self._recs)

    def facets(self) -> dict:
        with self._lock:
            hit = self._cache.get(("facets",))
            if hit and hit[0] == self.version:
                return hit[1]
            out = library.facets(list(self._recs.values()))
            self._cache[("facets",)] = (self.version, out)
            return out

    # ------------------------------------------------------------- scanning
    def _walk(self) -> dict[str, tuple[Path, tuple]]:
        """Every photo under the root (a DNG hides a same-named raw), with its (size, mtime) signature."""
        root = str(self.root)
        chosen: dict[tuple[str, str], tuple[str, bool, set]] = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            names = None
            raw_stems = {os.path.splitext(f)[0] for f in filenames
                         if os.path.splitext(f)[1].lower() in RAW_SUFFIXES}
            for fn in filenames:
                stem, ext = os.path.splitext(fn)
                ext = ext.lower()
                kind = library.kind_of(fn)
                if not kind:
                    continue
                if kind == "image" and stem in raw_stems:
                    continue         # RAW+JPEG pair: the raw is the photo, and both would share one .xmp
                if names is None:
                    names = set(filenames)
                key = (dirpath, stem if kind == "raw" else fn)
                prev = chosen.get(key)
                if prev is None or (not prev[1] and ext == ".dng"):
                    chosen[key] = (fn, ext == ".dng", names)
        found = {}
        for (dirpath, stem), (fn, is_dng, names) in chosen.items():
            full = os.path.join(dirpath, fn)
            sig = self._sig(Path(full), names)
            if sig is None:
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            found[rel] = (Path(full), sig)
        return found

    @staticmethod
    def _sig(path: Path, names: set | None = None) -> tuple | None:
        """(size, mtime_ns) of the photo and of its sidecar (zeros when it has none)."""
        try:
            st = os.stat(path)
        except OSError:
            return None
        tgt = library.meta_target(path)
        side = (0, 0)
        if tgt != path and (names is None or tgt.name in names):
            try:
                ss = os.stat(tgt)
                side = (ss.st_size, ss.st_mtime_ns)
            except OSError:
                pass
        return (st.st_size, st.st_mtime_ns, *side)

    def scan(self) -> dict:
        """Bring the index in line with the disk. Returns counts of added / changed / removed."""
        if self.root is None:
            return {"added": 0, "changed": 0, "removed": 0}
        with self._scan_lock:
            found = self._walk()
            with self._lock:
                gone = [r for r in self._recs if r not in found]
                stale = [r for r, (_, sig) in found.items() if self._sigs.get(r) != sig and r not in self._pending]
                gens = {r: self._gen.get(r, 0) for r in stale}
            reads = self._read_batch([(r, found[r][0]) for r in stale])
            added = changed = 0
            db_rows = []
            events = []
            with self._lock:
                for rel in gone:
                    if rel in self._pending:
                        continue
                    self._recs.pop(rel, None)
                    self._sigs.pop(rel, None)
                    self._picks.discard(rel)
                    events.append((rel, None))
                for rel, meta in reads.items():
                    if rel in self._pending or self._gen.get(rel, 0) != gens.get(rel, 0):
                        continue         # we changed it while the read was running: ours is newer
                    sig = found[rel][1]
                    new = rel not in self._recs
                    rec = _record(rel, meta, rel in self._picks)
                    old = self._recs.get(rel)
                    self._recs[rel] = rec
                    self._sigs[rel] = sig
                    if new:
                        added += 1
                    elif old != rec:
                        changed += 1
                    if new or old != rec:
                        events.append((rel, rec))
                    db_rows.append((rel, json.dumps(sig), json.dumps(meta), int(rel in self._picks)))
                # unchanged-but-resigned files (touched, same content) are covered by reads above
                if gone or reads:
                    self._fver["_struct"] = self._fver.get("_struct", 0) + 1
                    for f in ("rating", "label", "camera", "lens", "iso", "date", "pick"):
                        self._fver[f] = self._fver.get(f, 0) + 1
                    self.version += 1
                    self._cache.clear()
                for rel, rec in events:
                    self._emit(rel, rec)
                self._scanned_at = time.monotonic()
            stmts = []
            if db_rows:
                stmts.append(("INSERT OR REPLACE INTO photos(rel, sig, data, pick) VALUES(?,?,?,?)", db_rows, True))
            if gone:
                stmts.append(("DELETE FROM photos WHERE rel=?", [(r,) for r in gone], True))
            if stmts:
                self._db_tx(stmts)
            if reads and self._db is None and not self._pending:
                pass
            self._import_legacy_flags()
            return {"added": added, "changed": changed, "removed": len(gone)}

    def _read_batch(self, jobs: list[tuple[str, Path]]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(jobs), CHUNK):
            batch = jobs[i:i + CHUNK]
            paths: list[str] = []
            by_path: dict[str, str] = {}
            side_of: dict[str, str] = {}
            for rel, p in batch:
                paths.append(str(p))
                by_path[os.path.realpath(p)] = rel
                tgt = library.meta_target(p)
                if tgt != p and tgt.exists():
                    paths.append(str(tgt))
                    side_of[os.path.realpath(tgt)] = rel
            result = exifpool.run(["-fast2", "-j", "-d", "%Y-%m-%d %H:%M:%S", *READ_TAGS],
                                  input="\n".join(paths), timeout=max(exifpool.DEFAULT_TIMEOUT, len(paths) // 10))
            try:
                entries = json.loads(result.stdout or "[]")
            except ValueError:
                entries = []
            main: dict[str, dict] = {}
            side: dict[str, dict] = {}
            for e in entries:
                src = os.path.realpath(e.get("SourceFile", ""))
                if src in by_path:
                    main[by_path[src]] = e
                elif src in side_of:
                    side[side_of[src]] = e
            for rel, _ in batch:           # a file exiftool choked on is still listed, with no metadata
                out[rel] = parse_meta(main.get(rel, {}), side.get(rel), dng=rel.lower().endswith('.dng'))
        return out

    def _import_legacy_flags(self) -> None:
        """One-time import of picks from the old ``.photo-room-library.json`` store."""
        if self._db is None or self.root is None:
            return
        with self._dblock:
            done = self._db.execute("SELECT v FROM meta WHERE k='legacy_flags'").fetchone()
        if done:
            return
        flags = library.load_store(self.root)["flags"]
        picks = [r for r, v in flags.items() if v == "pick" and r in self._recs and r not in self._picks]
        with self._lock:
            for rel in picks:
                self._picks.add(rel)
                self._recs[rel] = {**self._recs[rel], "flag": _flag(self._recs[rel]["rating"], True)}
            if picks:
                self._fver["pick"] = self._fver.get("pick", 0) + 1
                self.version += 1
                self._cache.clear()
        stmts = [("UPDATE photos SET pick=1 WHERE rel=?", [(r,) for r in picks], True)] if picks else []
        stmts.append(("INSERT OR REPLACE INTO meta(k, v) VALUES('legacy_flags', '1')", [], False))
        self._db_tx(stmts)

    def ensure_fresh(self) -> None:
        """Call before serving a query: blocks only for the very first scan, otherwise rescans in the background."""
        age = time.monotonic() - self._scanned_at
        if self._scanned_at == 0.0 and not self._recs:
            self.scan()
        elif RESCAN_MAX_AGE <= 0:
            self.scan()
        elif age > RESCAN_MAX_AGE:
            with self._lock:
                t = self._scan_thread
                if t is None or not t.is_alive():
                    self._scan_thread = threading.Thread(target=self._safe_scan, daemon=True, name="library-scan")
                    self._scan_thread.start()

    def _safe_scan(self) -> None:
        try:
            self.scan()
        except Exception as exc:                      # a failed rescan must not kill the thread pool
            self.errors.append(f"scan: {exc}")

    # -------------------------------------------------------------- changes
    def _emit(self, rel: str, rec: dict | None) -> None:
        self._seq += 1
        self._changes.append((self._seq, rel, rec))
        self._cond.notify_all()

    @property
    def seq(self) -> int:
        return self._seq

    def changes_since(self, since: int, timeout: float = 0.0) -> dict:
        """Changes after ``since``. Waits up to ``timeout`` seconds for one. ``reset`` means: reload."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._seq <= since and timeout > 0 and not self._closed:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(left)
            if since > self._seq:
                return {"seq": self._seq, "reset": True, "changes": []}
            oldest = self._changes[0][0] if self._changes else self._seq + 1
            if self._seq > since and oldest > since + 1:
                return {"seq": self._seq, "reset": True, "changes": []}
            latest: dict[str, dict | None] = {}
            for seq, rel, rec in self._changes:
                if seq > since:
                    latest[rel] = rec
            return {"seq": self._seq, "reset": False,
                    "changes": [{"path": r, "item": rec, "removed": rec is None} for r, rec in latest.items()]}

    # --------------------------------------------------------------- writes
    def set_fields(self, updates: dict[str, dict]) -> int:
        """
        Change rating / label / keywords / pick on many photos.

        ``updates`` maps a relative path to the fields to set. Memory and the
        write-ahead log are updated before this returns; the sidecars follow in
        the background (or before returning when ``SYNC`` is on).
        Unknown paths are ignored. Returns the number of photos changed.
        """
        rows_photos = []
        rows_pending = []
        n = 0
        with self._lock:
            for rel, fields in updates.items():
                rec = self._recs.get(rel)
                if rec is None:
                    continue
                fields = {k: v for k, v in fields.items() if k in MUTABLE}
                if "keywords" in fields:
                    fields["keywords"] = library.normalize_keywords(fields["keywords"])
                if "label" in fields:
                    fields["label"] = library.normalize_label(fields["label"])
                if "rating" in fields:
                    fields["rating"] = max(-1, min(5, int(fields["rating"])))
                pick = fields.get("pick", rel in self._picks)
                new = dict(rec)
                for k in ("rating", "label", "keywords"):
                    if k in fields:
                        new[k] = fields[k]
                new["flag"] = _flag(new["rating"], bool(pick))
                if new == rec and "pick" not in fields:
                    continue
                changed = {k for k in MUTABLE if k in fields and (k == "pick" or new[k] != rec[k])}
                if pick:
                    self._picks.add(rel)
                else:
                    self._picks.discard(rel)
                self._recs[rel] = new
                self._gen[rel] = self._gen.get(rel, 0) + 1
                for k in changed:
                    self._fver[k] = self._fver.get(k, 0) + 1
                n += 1
                # merge into whatever is already waiting for this photo
                self._pend_seq += 1
                merged = dict(self._pending.get(rel, (0, {}))[1])
                merged.update({k: v for k, v in fields.items()})
                self._pending[rel] = (self._pend_seq, merged)
                rows_pending.append((rel, self._pend_seq, json.dumps(merged)))
                rows_photos.append((json.dumps(self._meta_of(new)), int(bool(pick)), rel))
                self._emit(rel, new)
            if n:
                self.version += 1
                self._cache.clear()
                now = time.monotonic()
                if self._first_pending_at == 0.0:
                    self._first_pending_at = now
                self._last_pending_at = now
        if n:
            self._db_tx([
                ("UPDATE photos SET data=?, pick=? WHERE rel=?", rows_photos, True),
                ("INSERT OR REPLACE INTO pending(rel, seq, fields) VALUES(?,?,?)", rows_pending, True),
            ])
            self._wake.set()
            if SYNC:
                self.flush(raise_errors=True)
        return n

    # ---------------------------------------------------------- the writer
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def flush(self, timeout: float = 60.0, raise_errors: bool = False) -> bool:
        """Write everything queued now. True when nothing is left pending."""
        if self.root is None:
            return True
        if self._writer is None or not self._writer.is_alive():
            self._drain_once()
        else:
            self._flush_req.set()
            self._wake.set()
            deadline = time.monotonic() + timeout
            with self._idle:
                while self.pending_count() and time.monotonic() < deadline:
                    self._idle.wait(0.05)
                    if self.errors and raise_errors:
                        break
        left = self.pending_count()
        if left and raise_errors and self.errors:
            raise RuntimeError(self.errors[-1])
        return left == 0

    def _writer_loop(self) -> None:
        backoff = 0.0
        while not self._closed:
            self._wake.wait(1.0)
            self._wake.clear()
            if self._closed or not self._pending:
                self._flush_req.clear()
                with self._idle:
                    self._idle.notify_all()
                continue
            while not self._flush_req.is_set() and not self._closed:
                now = time.monotonic()
                quiet = now - self._last_pending_at
                age = now - self._first_pending_at
                if quiet >= DEBOUNCE or age >= MAX_DELAY:
                    break
                time.sleep(min(0.05, DEBOUNCE))
            ok = self._drain_once()
            with self._idle:
                self._idle.notify_all()
            if ok:
                backoff = 0.0
                if self._pending:
                    self._wake.set()
                else:
                    self._flush_req.clear()
            else:
                backoff = min(30.0, max(0.5, backoff * 2))
                self._flush_req.clear()
                time.sleep(backoff)
                self._wake.set()

    def _drain_once(self) -> bool:
        """Write the current snapshot of pending changes. True if every write succeeded."""
        with self._lock:
            snapshot = dict(self._pending)
            self._first_pending_at = 0.0
        if not snapshot:
            return True
        done: dict[str, int] = {}
        ok = True
        picks = {rel: bool(f["pick"]) for rel, (_, f) in snapshot.items() if "pick" in f}
        try:
            if picks:
                library.apply_picks(self.root, picks)
        except Exception as exc:
            self.errors.append(f"flags: {exc}")
            ok = False
            snapshot = {r: v for r, v in snapshot.items() if r not in picks or set(v[1]) - {"pick"}}
            picks = {}
        # group photos that want the identical exiftool arguments into one command
        groups: dict[tuple, list[str]] = {}
        for rel, (seq, f) in snapshot.items():
            args = sidecar_args(f)
            if not args:
                done[rel] = seq
                continue
            groups.setdefault(tuple(args), []).append(rel)
        for args, rels in groups.items():
            existing = [r for r in rels if self._target_exists(r)]
            fresh = [r for r in rels if r not in set(existing)]
            try:
                if existing:
                    self._write_existing(list(args), existing)
                for r in fresh:
                    self._write_new(list(args), r)
                for r in rels:
                    done[r] = snapshot[r][0]
            except Exception as exc:
                self.errors.append(f"{rels[0]}: {exc}")
                del self.errors[:-20]
                ok = False
        self._finish(done)
        return ok

    def _abs(self, rel: str) -> Path:
        return self.root / rel

    def _target_exists(self, rel: str) -> bool:
        p = self._abs(rel)
        t = library.meta_target(p)
        return t == p or t.exists()

    def _write_existing(self, args: list[str], rels: list[str]) -> None:
        targets = [str(library.meta_target(self._abs(r))) for r in rels]
        # exiftool replaces each file via a temporary name + rename: atomic per file
        with _locked(targets):
            result = exifpool.run([*args, "-overwrite_original"], files=targets, timeout=exifpool.DEFAULT_TIMEOUT)
        if result.returncode != 0 and "Error" in (result.stderr or ""):
            raise RuntimeError(f"exiftool failed: {result.stderr.strip()}")

    def _write_new(self, args: list[str], rel: str) -> None:
        target = library.meta_target(self._abs(rel))
        tmp = target.with_name(f".{target.stem}.{uuid.uuid4().hex[:8]}.tmp.xmp")
        try:
            with _locked([str(target)]):
                if target.exists():              # someone created it meanwhile: update it instead
                    return self._write_existing(args, [rel])
                result = exifpool.run([*args, "-o", str(tmp), os.devnull], timeout=exifpool.DEFAULT_TIMEOUT)
                # seeding from an empty source warns "no writable tags set" yet succeeds
                if not tmp.exists():
                    raise RuntimeError(f"exiftool failed: {(result.stderr or '').strip()}")
                os.replace(tmp, target)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    def _finish(self, done: dict[str, int]) -> None:
        if not done:
            return
        sig_rows = []
        with self._lock:
            for rel, seq in done.items():
                cur = self._pending.get(rel)
                if cur is not None and cur[0] == seq:
                    del self._pending[rel]
                elif cur is not None:
                    continue                     # changed again while we wrote: stays queued
                sig = self._sig(self._abs(rel))
                if sig is None:
                    continue
                self._sigs[rel] = sig
                sig_rows.append((json.dumps(list(sig)), rel))
            if self._pending:
                self._first_pending_at = self._last_pending_at = time.monotonic()
        self._db_tx([
            ("DELETE FROM pending WHERE rel=? AND seq=?", [(r, s) for r, s in done.items()], True),
            ("UPDATE photos SET sig=? WHERE rel=?", sig_rows, True),
        ])

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.flush(timeout=30)
        finally:
            self._closed = True
            self._wake.set()
            with self._cond:
                self._cond.notify_all()
            if self._db is not None:
                with self._dblock:
                    try:
                        self._db.close()
                    except sqlite3.Error:
                        pass


class _locked:
    """Hold the repo-wide per-file sidecar locks (security fix) for several targets, in a stable order."""

    def __init__(self, targets):
        self.locks = [fsutil.lock_for(Path(t)) for t in sorted(set(map(str, targets)))]

    def __enter__(self):
        for lock in self.locks:
            lock.acquire()

    def __exit__(self, *exc):
        for lock in reversed(self.locks):
            lock.release()


def sidecar_args(fields: dict) -> list[str]:
    """The exiftool arguments that write the XMP part (rating, label, keywords) of a change."""
    args: list[str] = []
    if "rating" in fields:
        args.append(f"-XMP-xmp:Rating={max(-1, min(5, int(fields['rating'])))}")
    args.extend(library.xmp_args(fields.get("label"), fields.get("keywords")))
    return args


# ---------------------------------------------------------------------------
# One index per library root
# ---------------------------------------------------------------------------

_indexes: dict[str, Index] = {}
_reg_lock = threading.Lock()


def get_index(root: Path) -> Index:
    key = str(Path(root).resolve())
    with _reg_lock:
        idx = _indexes.get(key)
        if idx is None or idx._closed:
            idx = Index(Path(key), Path(key) / DB_NAME)
            _indexes[key] = idx
        return idx


def flush_all(timeout: float = 60.0) -> bool:
    with _reg_lock:
        todo = list(_indexes.values())
    return all(i.flush(timeout) for i in todo)


def shutdown_all() -> None:
    with _reg_lock:
        todo = list(_indexes.values())
        _indexes.clear()
    for i in todo:
        try:
            i.close()
        except Exception:
            pass


atexit.register(shutdown_all)
