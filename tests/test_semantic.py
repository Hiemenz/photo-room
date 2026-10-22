"""Semantic search: store, resumable builder, ranking, fallback and HTTP, all with a fake embedder."""

import io
import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

import editor
import library
import semantic
import thumbs

COLOURS = {"red": (230, 20, 20), "green": (20, 220, 20), "blue": (20, 20, 230)}


class FakeEmbedder:
    """Image -> normalised mean RGB; text -> the colour word's unit vector. No model, no network."""
    name = "fake-rgb"
    dim = 3

    def __init__(self):
        self.image_calls = 0

    def embed_images(self, images):
        self.image_calls += len(images)
        m = np.array([np.asarray(i, dtype=np.float32).reshape(-1, 3).mean(0) for i in images])
        return m / np.linalg.norm(m, axis=1, keepdims=True)

    def embed_text(self, text):
        v = np.zeros(3, dtype=np.float32)
        for w in text.lower().split():
            if w in COLOURS:
                v += np.asarray(COLOURS[w], dtype=np.float32) / np.linalg.norm(COLOURS[w])
        return v / max(np.linalg.norm(v), 1e-9)


def solid(rgb):
    return np.full((16, 16, 3), rgb, dtype=np.uint8)


def make_lib(root: Path, names=("red", "green", "blue", "red2")):
    imgs = {}
    for n in names:
        (root / f"{n}.CR2").write_bytes(f"raw-{n}".encode() * 50)
        c = list(COLOURS[n.rstrip("2")])
        if n == "red2":
            c[1] += 15                         # a slightly different red
        imgs[f"{n}.CR2"] = solid(tuple(c))
    return imgs


def loader_for(imgs):
    return lambda p: imgs[Path(p).name]


def items(imgs):
    return [{"path": n, "keywords": [], "camera": "", "date": ""} for n in imgs]


@pytest.fixture(autouse=True)
def _reset():
    semantic.set_embedder(None)
    semantic._stores.clear()
    semantic._builders.clear()
    yield
    semantic.set_embedder(None)


def build(root, imgs, emb, **kw):
    b = semantic.Builder(root, emb, loader=loader_for(imgs), **kw)
    b.run(list(imgs))
    return b


def test_build_and_text_ranking(tmp_path):
    imgs = make_lib(tmp_path)
    emb = FakeEmbedder()
    b = build(tmp_path, imgs, emb)
    assert b.progress["done"] == 4 and not b.progress["errors"]
    ranked = semantic.rank_text(tmp_path, emb, "red", items(imgs))
    assert {r["path"] for r in ranked[:2]} == {"red.CR2", "red2.CR2"}
    assert ranked[0]["score"] >= ranked[-1]["score"]
    assert semantic.rank_text(tmp_path, emb, "blue", items(imgs))[0]["path"] == "blue.CR2"


def test_builder_is_resumable_and_skips_done(tmp_path):
    imgs = make_lib(tmp_path)
    emb = FakeEmbedder()
    build(tmp_path, imgs, emb)
    assert emb.image_calls == 4
    build(tmp_path, imgs, emb)                      # everything cached: no new embedding work
    assert emb.image_calls == 4


def test_cancel_then_resume(tmp_path):
    imgs = make_lib(tmp_path)
    emb = FakeEmbedder()
    b = semantic.Builder(tmp_path, emb, loader=loader_for(imgs), batch=1)
    orig = emb.embed_images

    def cancel_after_first(x):
        b.cancel()
        return orig(x)
    emb.embed_images = cancel_after_first
    b.run(list(imgs))
    assert 1 <= semantic.get_store(tmp_path).count("fake-rgb") < 4
    emb.embed_images = orig
    build(tmp_path, imgs, emb)
    assert semantic.get_store(tmp_path).count("fake-rgb") == 4


def test_rename_reuses_embedding_by_hash(tmp_path):
    imgs = make_lib(tmp_path, ("red",))
    emb = FakeEmbedder()
    build(tmp_path, imgs, emb)
    (tmp_path / "red.CR2").rename(tmp_path / "moved.CR2")
    imgs2 = {"moved.CR2": imgs["red.CR2"]}
    build(tmp_path, imgs2, emb)
    assert emb.image_calls == 1
    assert semantic.rank_text(tmp_path, emb, "red", items(imgs2))[0]["path"] == "moved.CR2"


def test_changed_file_is_reembedded(tmp_path):
    imgs = make_lib(tmp_path, ("red",))
    emb = FakeEmbedder()
    build(tmp_path, imgs, emb)
    (tmp_path / "red.CR2").write_bytes(b"edited" * 999)
    build(tmp_path, imgs, emb)
    assert emb.image_calls == 2


def test_originals_untouched_and_db_beside_library(tmp_path):
    imgs = make_lib(tmp_path)
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.glob("*.CR2")}
    build(tmp_path, imgs, FakeEmbedder())
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.glob("*.CR2")}
    assert before == after
    assert (tmp_path / semantic.DB_NAME).is_file()


def test_loader_errors_are_reported_not_fatal(tmp_path):
    imgs = make_lib(tmp_path)

    def loader(p):
        if Path(p).name == "green.CR2":
            raise OSError("bad raw")
        return imgs[Path(p).name]
    b = semantic.Builder(tmp_path, FakeEmbedder(), loader=loader)
    b.run(list(imgs))
    assert any("green.CR2" in e for e in b.progress["errors"])
    assert semantic.get_store(tmp_path).count("fake-rgb") == 3


def test_find_similar(tmp_path):
    imgs = make_lib(tmp_path)
    emb = FakeEmbedder()
    build(tmp_path, imgs, emb)
    r = semantic.rank_similar(tmp_path, emb, "red.CR2", items(imgs))
    assert [x["path"] for x in r[:2]] == ["red.CR2", "red2.CR2"]
    with pytest.raises(KeyError):
        semantic.rank_similar(tmp_path, emb, "nope.CR2", items(imgs))


def test_background_start_runs_and_reports(tmp_path):
    imgs = make_lib(tmp_path)
    b = semantic.Builder(tmp_path, FakeEmbedder(), loader=loader_for(imgs))
    assert b.start(list(imgs))
    b.join(10)
    assert b.progress["finished"] and b.progress["done"] == 4


def test_keyword_fallback_and_message_when_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(semantic, "get_embedder", lambda: None)
    its = [{"path": "beach.CR2", "keywords": ["sunset"], "camera": "Canon", "date": "2024-05-01"},
           {"path": "dog.CR2", "keywords": ["pet"], "camera": "Canon", "date": "2024-05-02"}]
    ranked, info = semantic.search(tmp_path, its, query="sunset")
    assert [r["path"] for r in ranked] == ["beach.CR2"]
    assert info["mode"] == "keyword" and "semantic" in info["message"].lower()
    ranked, _ = semantic.search(tmp_path, its, similar="dog.CR2")
    assert ranked[0]["path"] == "dog.CR2"


def test_empty_index_falls_back_with_message(tmp_path):
    emb = FakeEmbedder()
    its = [{"path": "red.CR2", "keywords": ["red"]}]
    ranked, info = semantic.search(tmp_path, its, query="red", embedder=emb)
    assert info["mode"] == "keyword" and "index" in info["message"].lower()
    assert ranked[0]["path"] == "red.CR2"


def test_missing_extra_or_model_gives_clear_message(monkeypatch, tmp_path):
    monkeypatch.setenv("PHOTO_ROOM_SEMANTIC_MODEL", str(tmp_path / "none"))
    assert semantic.unavailable_reason()
    assert semantic.get_embedder() is None
    assert "semantic" in semantic.unavailable_message().lower()


def test_download_requires_explicit_yes(monkeypatch, tmp_path):
    import urllib.request

    def boom(*a, **k):
        raise AssertionError("network touched")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setenv("PHOTO_ROOM_SEMANTIC_MODEL", str(tmp_path / "m"))
    assert semantic.main(["download"]) == 1
    assert not (tmp_path / "m").exists()


def test_download_uses_timeout(monkeypatch, tmp_path):
    import urllib.request
    seen = []

    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(url, timeout=None):
        seen.append(timeout)
        return R(b"x")
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    semantic.download(tmp_path / "m", log=lambda *_: None)
    assert seen and all(t == semantic.DOWNLOAD_TIMEOUT for t in seen)
    assert (tmp_path / "m" / "tokenizer.json").is_file()


# ------------------------------------------------------------------ HTTP
@pytest.fixture
def client(tmp_path, monkeypatch, fake_rawpy, fake_exiftool):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    imgs = make_lib(tmp_path)
    monkeypatch.setattr(thumbs, "_embedded_preview", lambda p: (b"", 1))
    meta = {"red.CR2": {"Subject": ["sunset"]}}

    def tool(cmd, **kw):
        if "-j" in cmd:
            out = [{"SourceFile": f, **meta.get(Path(f).name, {})} for f in kw["input"].splitlines()]
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(out), stderr="")
        return fake_exiftool(cmd, **kw)
    monkeypatch.setattr(subprocess, "run", tool)
    monkeypatch.setattr(editor.subprocess, "run", tool)
    import process
    monkeypatch.setattr(process.subprocess, "run", tool)
    library.clear_cache()
    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        c.imgs = imgs
        yield c
    library.clear_cache()


def test_status_unavailable_is_clear(client, monkeypatch, tmp_path):
    monkeypatch.setenv("PHOTO_ROOM_SEMANTIC_MODEL", str(tmp_path / "none"))
    d = client.get("/api/semantic/status").get_json()
    assert d["available"] is False and d["message"]
    r = client.post("/api/semantic/build")
    assert r.status_code == 409 and "semantic" in r.get_json()["error"].lower()


def test_library_endpoint_falls_back_without_model(client, monkeypatch, tmp_path):
    monkeypatch.setenv("PHOTO_ROOM_SEMANTIC_MODEL", str(tmp_path / "none"))
    d = client.get("/api/library?sem=red").get_json()
    assert d["semantic"]["mode"] == "keyword"
    assert [i["path"] for i in d["items"]][:2] == ["red.CR2", "red2.CR2"]


def test_http_build_and_ranked_library(client, tmp_path, monkeypatch):
    semantic.set_embedder(FakeEmbedder())
    monkeypatch.setattr(semantic, "default_loader", lambda root: loader_for(client.imgs))
    assert client.get("/api/semantic/status").get_json()["available"] is True
    assert client.post("/api/semantic/build").get_json()["started"] is True
    semantic._builders[str(tmp_path.resolve())].join(15)
    st = client.get("/api/semantic/status").get_json()
    assert st["indexed"] == 4 and st["building"] is False

    d = client.get("/api/library?sem=blue").get_json()
    assert d["semantic"]["mode"] == "semantic"
    assert d["items"][0]["path"] == "blue.CR2"
    d = client.get("/api/library?sem=red&keyword=sunset").get_json()      # other filters still apply
    assert [i["path"] for i in d["items"]] == ["red.CR2"]
    d = client.get("/api/library?similar=red.CR2").get_json()
    assert [i["path"] for i in d["items"]][:2] == ["red.CR2", "red2.CR2"]
    s = client.get("/api/semantic/search?q=green").get_json()
    assert s["items"][0]["path"] == "green.CR2"
    assert client.get("/api/semantic/search").status_code == 400
