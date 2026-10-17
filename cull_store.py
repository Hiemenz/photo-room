"""
Local storage and learning for smart culling.

One SQLite file in the library directory (``.photo-room-cull.sqlite``) holds

- ``features``: the cached per-photo feature dicts from cull.extract, keyed by the
  thumbnail's identity (path, size, mtime) so a changed file is re-analysed;
- ``decisions``: one row per photo in a reviewed group, with what was suggested,
  what the user finally chose, and the score features at that moment;
- ``batches``: what an applied batch replaced, so it can be undone;
- ``autoedit`` / ``offsets``: the settings Auto edit wrote and the corrections the
  user then made on top, which become the learned style.

Learning is numpy only: a ridge-regularised logistic regression (Newton steps) for
pick and for reject over cull.FEATURE_NAMES, and a shrunk per-slider offset model
for Auto edit. Nothing leaves the machine, and every table can be exported,
reset or cleared. All of it is a pure function of the database, so tests need no
Flask or browser.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import numpy as np

import cull

DB_NAME = ".photo-room-cull.sqlite"
MIN_FIT = 10               # decisions needed before the personal model has any say
CONFIDENCE_HALF = 40       # ...and it reaches half weight at this many more
REJECT_MIN = 8             # rejects (and non-rejects) needed before it may suggest one
REJECT_P = 0.85
L2 = 0.3

# Sliders whose post-Auto corrections are learned, with the largest offset ever applied.
OFFSET_LIMITS = {
    "exposure": 1.5, "contrast": 40, "highlights": 40, "shadows": 40, "whites": 40,
    "blacks": 40, "vibrance": 30, "saturation": 30, "clarity": 30, "tint": 30,
    "temp_ratio": 0.25,
}
OFFSET_MIN_SAMPLES = 3
OFFSET_PRIOR = 8           # shrinkage: weight = n / (n + OFFSET_PRIOR)
OFFSET_RIDGE_MIN = 20      # below this many samples the offset is a plain shrunk mean

_SCHEMA = """
CREATE TABLE IF NOT EXISTS features (path TEXT PRIMARY KEY, fkey TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, path TEXT NOT NULL, grp INTEGER,
  gsize INTEGER NOT NULL, suggested TEXT NOT NULL, final TEXT NOT NULL, agreed INTEGER NOT NULL,
  vec TEXT NOT NULL, model_n INTEGER NOT NULL DEFAULT 0, batch TEXT);
CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL, undone INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS autoedit (
  path TEXT PRIMARY KEY, ts REAL NOT NULL, entry_id TEXT, applied TEXT NOT NULL, feats TEXT, checked REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS offsets (path TEXT PRIMARY KEY, ts REAL NOT NULL, offsets TEXT NOT NULL, feats TEXT);
"""


def sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def fit_logistic(X: np.ndarray, y: np.ndarray, l2: float = L2, iters: int = 40) -> np.ndarray:
    """Ridge logistic regression by Newton's method. Returns d weights followed by the bias."""
    n, d = X.shape
    Xb = np.hstack([X, np.ones((n, 1))])
    w = np.zeros(d + 1)
    reg = np.full(d + 1, l2)
    reg[-1] = 0.0                                   # the bias is not penalised
    for _ in range(iters):
        p = sigmoid(Xb @ w)
        grad = Xb.T @ (p - y) + reg * w
        hess = (Xb * (p * (1 - p))[:, None]).T @ Xb + np.diag(reg) + 1e-9 * np.eye(d + 1)
        step = np.linalg.solve(hess, grad)
        w -= step
        if np.abs(step).max() < 1e-6:
            break
    return w


class PickModel:
    """The personal pick and reject models, fitted from logged decisions."""

    def __init__(self):
        self.n = 0
        self.pick_w: np.ndarray | None = None
        self.reject_w: np.ndarray | None = None
        self.n_reject = 0
        self.n_keep = 0

    @classmethod
    def fit(cls, rows: list[tuple[list[float], int, str]]) -> "PickModel":
        """``rows``: (vec, group size, final action)."""
        m = cls()
        m.n = len(rows)
        if m.n < MIN_FIT:
            return m
        multi = [(v, a) for v, g, a in rows if g > 1]
        if len(multi) >= MIN_FIT:
            y = np.array([1.0 if a == "pick" else 0.0 for _, a in multi])
            if 3 <= y.sum() <= len(y) - 3:
                m.pick_w = fit_logistic(np.array([v for v, _ in multi]), y)
        y = np.array([1.0 if a == "reject" else 0.0 for _, _, a in rows])
        m.n_reject, m.n_keep = int(y.sum()), int(len(y) - y.sum())
        if m.n_reject >= REJECT_MIN and m.n_keep >= REJECT_MIN:
            m.reject_w = fit_logistic(np.array([v for v, _, _ in rows]), y)
        return m

    @property
    def confidence(self) -> float:
        return 0.0 if self.pick_w is None else self.n / (self.n + CONFIDENCE_HALF)

    @staticmethod
    def _prob(w: np.ndarray, vec) -> float:
        return float(sigmoid(np.dot(w[:-1], vec) + w[-1]))

    def blend(self, heuristic: float, vec) -> float:
        """The heuristic score pulled toward the learned pick probability (0..100) by confidence."""
        c = self.confidence
        if c <= 0:
            return heuristic
        return (1 - c) * heuristic + c * 100.0 * self._prob(self.pick_w, vec)

    def rejects(self, vec) -> bool:
        return self.reject_w is not None and self._prob(self.reject_w, vec) >= REJECT_P

    def info(self) -> dict:
        return {"decisions": self.n, "pick_model": self.pick_w is not None,
                "reject_model": self.reject_w is not None, "confidence": round(self.confidence, 3),
                "weights": ({n: round(float(x), 3) for n, x in zip(cull.FEATURE_NAMES, self.pick_w[:-1])}
                            if self.pick_w is not None else None)}


class OffsetModel:
    """Typical corrections the user makes on top of Auto edit, per slider."""

    def __init__(self):
        self.n = 0
        self.mean: dict[str, float] = {}
        self.count: dict[str, int] = {}
        self._ridge: dict[str, tuple[np.ndarray, float]] = {}

    @staticmethod
    def _x(feats: dict | None) -> np.ndarray:
        f = feats or {}
        return np.array([float(f.get("median", 0.45)) - 0.45, float(f.get("range", 0.6)) - 0.6])

    @classmethod
    def fit(cls, samples: list[tuple[dict, dict | None]]) -> "OffsetModel":
        m = cls()
        m.n = len(samples)
        for key in OFFSET_LIMITS:
            pts = [(o[key], f) for o, f in samples if key in o]
            m.count[key] = len(pts)
            if len(pts) < OFFSET_MIN_SAMPLES:
                continue
            y = np.array([p[0] for p in pts], float)
            m.mean[key] = float(np.mean(y))
            if len(pts) >= OFFSET_RIDGE_MIN:
                X = np.array([cls._x(p[1]) for p in pts])
                xm = X.mean(axis=0)
                Xc = X - xm
                w = np.linalg.solve(Xc.T @ Xc + 2.0 * np.eye(2), Xc.T @ (y - y.mean()))
                m._ridge[key] = (w, float(xm @ w))
        return m

    def predict(self, feats: dict | None = None) -> dict:
        out = {}
        for key, mean in self.mean.items():
            n = self.count[key]
            val = mean
            if key in self._ridge:
                w, centre = self._ridge[key]
                val = mean + float(self._x(feats) @ w) - centre
            val *= n / (n + OFFSET_PRIOR)
            lim = OFFSET_LIMITS[key]
            out[key] = float(np.clip(val, -lim, lim))
        return out

    def info(self) -> dict:
        return {"photos": self.n,
                "typical": {k: {"offset": round(v, 3), "samples": self.count[k]}
                            for k, v in self.mean.items()}}


def apply_offsets(auto: dict, offsets: dict) -> dict:
    """Shift an auto-edit adjustment set by the learned offsets (units as in the editor)."""
    out = dict(auto)
    for key, delta in offsets.items():
        if key == "temp_ratio":
            if out.get("color_temp"):
                out["color_temp"] = int(round(float(out["color_temp"]) * (1.0 + delta)))
            continue
        out[key] = float(out.get(key, 0) or 0) + delta
    if "exposure" in out:
        out["exposure"] = round(round(float(out["exposure"]) * 20) / 20, 2)
    for key in OFFSET_LIMITS:
        if key not in ("exposure", "temp_ratio") and key in out:
            out[key] = int(round(float(out[key])))
    return out


class CullStore:
    """The SQLite file for one library."""

    def __init__(self, root: Path):
        self.path = Path(root) / DB_NAME
        self._lock = threading.RLock()
        self._model: tuple[int, PickModel] | None = None
        self._omodel: tuple[int, OffsetModel] | None = None
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def _run(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self._lock, self._conn() as c:
            return c.execute(sql, args).fetchall()

    # -- features -----------------------------------------------------------
    def get_features(self, keyed: dict[str, str]) -> dict[str, dict]:
        """Cached features for {path: current fkey}; stale or missing paths are left out."""
        out = {}
        with self._lock, self._conn() as c:
            for row in c.execute("SELECT path, fkey, data FROM features"):
                if keyed.get(row["path"]) == row["fkey"]:
                    d = json.loads(row["data"])
                    if d.get("v") == cull.FEATURE_VERSION:
                        out[row["path"]] = d
        return out

    def put_features(self, path: str, fkey: str, data: dict) -> None:
        self._run("INSERT OR REPLACE INTO features VALUES (?,?,?)", (path, fkey, json.dumps(data)))

    # -- models -------------------------------------------------------------
    def _count(self, table: str) -> int:
        return self._run(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]

    def pick_model(self) -> PickModel:
        with self._lock:
            n = self._count("decisions")
            if self._model is None or self._model[0] != n:
                rows = [(json.loads(r["vec"]), r["gsize"], r["final"]) for r in
                        self._run("SELECT vec, gsize, final FROM decisions ORDER BY id")]
                self._model = (n, PickModel.fit(rows))
            return self._model[1]

    def offset_model(self) -> OffsetModel:
        with self._lock:
            n = self._count("offsets")
            stamp = self._run("SELECT COALESCE(MAX(ts),0) AS t FROM offsets")[0]["t"]
            key = (n, stamp)
            if self._omodel is None or self._omodel[0] != key:
                rows = [(json.loads(r["offsets"]), json.loads(r["feats"]) if r["feats"] else None)
                        for r in self._run("SELECT offsets, feats FROM offsets")]
                self._omodel = (key, OffsetModel.fit(rows))
            return self._omodel[1]

    # -- decisions and batches -----------------------------------------------
    def new_batch(self, kind: str, data: dict) -> str:
        bid = uuid.uuid4().hex[:12]
        self._run("INSERT INTO batches VALUES (?,?,?,?,0)", (bid, time.time(), kind, json.dumps(data)))
        return bid

    def get_batch(self, bid: str) -> dict | None:
        rows = self._run("SELECT * FROM batches WHERE id=?", (bid,))
        if not rows:
            return None
        r = rows[0]
        return {"id": r["id"], "ts": r["ts"], "kind": r["kind"], "data": json.loads(r["data"]),
                "undone": bool(r["undone"])}

    def mark_undone(self, bid: str) -> None:
        self._run("UPDATE batches SET undone=1 WHERE id=?", (bid,))

    def log_decisions(self, rows: list[dict], batch: str) -> int:
        """``rows``: path, group, gsize, suggested, final, vec. Returns how many were logged."""
        n_model = self.pick_model().n
        now = time.time()
        with self._lock, self._conn() as c:
            c.executemany(
                "INSERT INTO decisions (ts,path,grp,gsize,suggested,final,agreed,vec,model_n,batch) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(now, r["path"], r.get("group"), int(r.get("gsize", 1)), r["suggested"], r["final"],
                  int(r["suggested"] == r["final"]), json.dumps(r["vec"]), n_model, batch) for r in rows])
        return len(rows)

    def drop_batch_decisions(self, batch: str) -> None:
        """An undone batch was not a considered choice, so it must not train the model."""
        self._run("DELETE FROM decisions WHERE batch=?", (batch,))

    def accuracy(self, bucket: int | None = None) -> dict:
        """Agreement between suggestion and final choice, overall and over time."""
        rows = self._run("SELECT agreed, suggested, final FROM decisions ORDER BY id")
        n = len(rows)
        flags = [r["agreed"] for r in rows]
        bucket = bucket or (20 if n >= 100 else 10 if n >= 40 else 5)
        series = [{"upto": i + len(flags[i:i + bucket]), "rate": round(sum(flags[i:i + bucket]) / len(flags[i:i + bucket]), 3)}
                  for i in range(0, n, bucket)]
        overridden = {"pick": 0, "reject": 0, "none": 0}
        for r in rows:
            if not r["agreed"]:
                overridden[r["suggested"]] += 1
        return {"decisions": n, "overall": round(sum(flags) / n, 3) if n else None,
                "recent": round(sum(flags[-50:]) / len(flags[-50:]), 3) if n else None,
                "bucket": bucket, "series": series, "overridden_suggestions": overridden}

    # -- auto edit ------------------------------------------------------------
    def record_autoedit(self, path: str, entry_id: str | None, applied: dict, feats: dict | None) -> None:
        self._run("INSERT OR REPLACE INTO autoedit VALUES (?,?,?,?,?,0)",
                  (path, time.time(), entry_id, json.dumps(applied), json.dumps(feats) if feats else None))

    def restore_autoedit(self, path: str) -> None:
        self._run("DELETE FROM autoedit WHERE path=?", (path,))
        self._run("DELETE FROM offsets WHERE path=?", (path,))

    def learn_offsets(self, current_of, mtime_of=None) -> int:
        """
        Turn "Auto edit, then the user adjusted it" into offset samples.

        ``current_of(path)`` returns (history entry id, adjustments) for the photo as it
        stands now, or None. A photo whose current step is still the Auto step teaches
        nothing (we cannot tell "happy" from "never looked"). Returns samples changed.
        """
        changed = 0
        for row in self._run("SELECT * FROM autoedit"):
            if mtime_of is not None:
                m = mtime_of(row["path"])
                if m is not None and m <= row["checked"]:
                    continue
            cur = current_of(row["path"])
            self._run("UPDATE autoedit SET checked=? WHERE path=?", (time.time(), row["path"]))
            if cur is None or cur[0] == row["entry_id"]:
                continue
            applied = json.loads(row["applied"])
            offs = {}
            for key in OFFSET_LIMITS:
                if key == "temp_ratio":
                    a, f = applied.get("color_temp"), cur[1].get("color_temp")
                    if a and f:
                        offs[key] = float(f) / float(a) - 1.0
                elif key in applied or key in cur[1]:
                    offs[key] = float(cur[1].get(key, 0) or 0) - float(applied.get(key, 0) or 0)
            offs = {k: v for k, v in offs.items() if abs(v) > 1e-9}
            if not offs:
                continue
            self._run("INSERT OR REPLACE INTO offsets VALUES (?,?,?,?)",
                      (row["path"], time.time(), json.dumps(offs), row["feats"]))
            changed += 1
        return changed

    # -- data controls ----------------------------------------------------------
    def reset_learning(self) -> None:
        """Forget what was learned (decisions and offsets); the feature cache stays."""
        for t in ("decisions", "offsets", "autoedit"):
            self._run(f"DELETE FROM {t}")
        self._model = self._omodel = None

    def clear_all(self) -> None:
        for t in ("decisions", "offsets", "autoedit", "batches", "features"):
            self._run(f"DELETE FROM {t}")
        self._model = self._omodel = None
        with self._lock, self._conn() as c:
            c.execute("VACUUM")

    def export(self) -> dict:
        """Everything learned, as plain JSON (features are a cache and are left out)."""
        def rows(sql):
            return [dict(r) for r in self._run(sql)]
        decisions = rows("SELECT ts,path,grp,gsize,suggested,final,agreed,vec,model_n FROM decisions ORDER BY id")
        for d in decisions:
            d["vec"] = json.loads(d["vec"])
        offsets = rows("SELECT path,ts,offsets,feats FROM offsets")
        for o in offsets:
            o["offsets"] = json.loads(o["offsets"])
            o["feats"] = json.loads(o["feats"]) if o["feats"] else None
        return {"format": "photo-room-cull-export", "version": 1, "exported": time.time(),
                "feature_names": list(cull.FEATURE_NAMES), "decisions": decisions, "offsets": offsets,
                "accuracy": self.accuracy(), "pick_model": self.pick_model().info(),
                "offset_model": self.offset_model().info()}
