"""Faces / People: detection hook, embedder, clustering, store, builder, HTTP and CLI, all synthetic."""

import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

import editor
import faces
import library
import thumbs

H = W = 128


def pattern(kind: int, seed: int) -> np.ndarray:
    """A 40x40 'face' texture: kind 0 vertical stripes, 1 checkerboard, 2 diagonal. Noise per seed."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:40, 0:40]
    base = {0: (xx // 4) % 2, 1: ((xx // 5) + (yy // 5)) % 2, 2: ((xx + yy) // 4) % 2}[kind] * 180 + 30
    g = np.clip(base + rng.normal(0, 8, base.shape), 1, 255).astype(np.uint8)
    return np.stack([g, g, g], axis=-1)


def photo(*kinds_seeds) -> np.ndarray:
    img = np.zeros((H, W, 3), np.uint8)
    for slot, (kind, seed) in enumerate(kinds_seeds):
        x0 = 8 + slot * 64
        img[20:60, x0:x0 + 40] = pattern(kind, seed)
    return img


class BlobDetector:
    """Finds the lit 40x40 blocks of the synthetic photos; no OpenCV involved."""
    name = "blobs"

    def detect(self, rgb):
        boxes = []
        for slot in range(2):
            x0 = 8 + slot * 64
            if rgb[20:60, x0:x0 + 40].any():
                boxes.append((x0 / W, 20 / H, 40 / W, 40 / H))
        return boxes


def make_lib(root: Path):
    spec = {                      # name -> faces in the photo
        "a1.CR2": [(0, 1)], "a2.CR2": [(0, 2)], "a3.CR2": [(0, 3), (1, 4)],
        "b1.CR2": [(1, 5)], "b2.CR2": [(1, 6)],
        "none.CR2": [],
    }
    imgs = {}
    for n, f in spec.items():
        (root / n).write_bytes(f"raw-{n}".encode() * 40)
        imgs[n] = photo(*f)
    return imgs


def loader_for(imgs):
    return lambda p: imgs[Path(p).name]


@pytest.fixture(autouse=True)
def _reset():
    faces.set_backend(None, None)
    faces._stores.clear()
    faces._builders.clear()
    yield
    faces.set_backend(None, None)


def scan(root, imgs, rels=None):
    b = faces.Builder(root, BlobDetector(), faces.LbpEmbedder(), loader=loader_for(imgs))
    b.start(rels or sorted(imgs))
    b.join(30)
    return b


# ------------------------------------------------------------------ embedder
def test_lbp_groups_same_texture_and_separates_different_ones():
    e = faces.LbpEmbedder()
    v = e.embed_faces([pattern(0, 1), pattern(0, 2), pattern(1, 3), pattern(1, 4)])
    assert v.shape == (4, e.dim)
    np.testing.assert_allclose(np.linalg.norm(v, axis=1), 1.0, atol=1e-5)
    assert v[0] @ v[1] >= e.threshold and v[2] @ v[3] >= e.threshold
    assert v[0] @ v[2] < e.threshold


def test_haar_detector_is_safe_on_a_flat_frame():
    assert faces.HaarDetector().detect(np.full((200, 300, 3), 120, np.uint8)) == []


# ------------------------------------------------------------ builder + store
def test_scan_clusters_people_and_counts_photos(tmp_path):
    imgs = make_lib(tmp_path)
    b = scan(tmp_path, imgs)
    assert b.progress["finished"] and b.progress["faces"] == 6 and not b.progress["errors"]
    people = faces.get_store(tmp_path).people("lbp-grid-v1")
    counts = sorted(p["photos"] for p in people)
    assert counts == [3, 3]                       # stripes in a1,a2,a3; checker in a3,b1,b2
    assert all(p["face_id"] for p in people)


def test_rescan_is_resumable_and_does_not_duplicate(tmp_path):
    imgs = make_lib(tmp_path)
    scan(tmp_path, imgs)
    n = faces.get_store(tmp_path).face_count()
    b = scan(tmp_path, imgs)
    assert b.progress["faces"] == 0 and faces.get_store(tmp_path).face_count() == n


def test_named_people_survive_new_faces(tmp_path):
    imgs = make_lib(tmp_path)
    scan(tmp_path, {k: v for k, v in imgs.items() if k != "a3.CR2"})
    st = faces.get_store(tmp_path)
    stripes = next(p for p in st.people("lbp-grid-v1") if p["photos"] == 2 and p["name"] == "" and
                   "a1.CR2" in st.paths_of(p["id"]))
    st.rename(stripes["id"], "Ann")
    scan(tmp_path, imgs)                          # a3 adds a stripes face and a checker face
    ann = next(p for p in st.people("lbp-grid-v1") if p["name"] == "Ann")
    assert st.paths_of(ann["id"]) == {"a1.CR2", "a2.CR2", "a3.CR2"}


def test_merge_remove_and_ignored_faces_stay_hidden(tmp_path):
    imgs = make_lib(tmp_path)
    scan(tmp_path, imgs)
    st = faces.get_store(tmp_path)
    a, b = st.people("lbp-grid-v1")
    st.remove(b["id"])
    assert [p["id"] for p in st.people("lbp-grid-v1")] == [a["id"]]
    scan(tmp_path, imgs)                          # nothing new: the hidden person is not resurrected
    assert [p["id"] for p in st.people("lbp-grid-v1")] == [a["id"]]
    assert st.get_person(b["id"]) is None


def test_edited_file_is_rescanned_and_unreadable_file_is_reported(tmp_path):
    imgs = make_lib(tmp_path)
    scan(tmp_path, imgs)
    (tmp_path / "gone.CR2").write_bytes(b"x")
    b = scan(tmp_path, imgs, ["gone.CR2", "a1.CR2"])      # loader has no 'gone.CR2'
    assert len(b.progress["errors"]) == 1 and b.progress["done"] == 2


def test_cancel_keeps_finished_work(tmp_path):
    imgs = make_lib(tmp_path)
    b = faces.Builder(tmp_path, BlobDetector(), faces.LbpEmbedder(), loader=loader_for(imgs))
    b._cancel.set()
    b.run(sorted(imgs))
    assert b.progress["done"] == 0 and b.progress["finished"] is False


def test_filter_items(tmp_path):
    imgs = make_lib(tmp_path)
    scan(tmp_path, imgs)
    st = faces.get_store(tmp_path)
    p = next(p for p in st.people("lbp-grid-v1") if "b1.CR2" in st.paths_of(p["id"]))
    items = [{"path": n} for n in imgs]
    got = {i["path"] for i in faces.filter_items(tmp_path, items, str(p["id"]))}
    assert got == {"a3.CR2", "b1.CR2", "b2.CR2"}
    with pytest.raises(ValueError):
        faces.filter_items(tmp_path, items, "bob")


def test_store_lives_beside_the_library(tmp_path):
    faces.get_store(tmp_path)
    assert (tmp_path / ".photo-room-faces.sqlite").is_file()


# ------------------------------------------------------------------ HTTP
@pytest.fixture
def client(tmp_path, monkeypatch, fake_rawpy, fake_exiftool):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    imgs = make_lib(tmp_path)
    monkeypatch.setattr(thumbs, "_embedded_preview", lambda p: (b"", 1))

    def tool(cmd, **kw):
        if "-j" in cmd:
            out = [{"SourceFile": f} for f in kw["input"].splitlines()]
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(out), stderr="")
        return fake_exiftool(cmd, **kw)
    monkeypatch.setattr(subprocess, "run", tool)
    monkeypatch.setattr(editor.subprocess, "run", tool)
    import process
    monkeypatch.setattr(process.subprocess, "run", tool)
    import semantic
    monkeypatch.setattr(semantic, "default_loader", lambda root: loader_for(imgs))
    faces.set_backend(BlobDetector(), faces.LbpEmbedder())
    library.clear_cache()
    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        c.imgs = imgs
        yield c
    library.clear_cache()


def test_http_flow_build_name_filter_merge(client, tmp_path):
    s = client.get("/api/faces/status").get_json()
    assert s["embedder"] == "lbp-grid-v1" and s["advanced"] is False and "not real face recognition" in s["limits"]
    assert s["indexed"] == 0 and s["library"] == 6
    assert client.get("/api/faces/people").get_json()["people"] == []        # opt-in: nothing until built
    assert client.post("/api/faces/build").get_json()["started"] is True
    faces._builders[str(tmp_path.resolve())].join(30)
    s = client.get("/api/faces/status").get_json()
    assert s["indexed"] == 6 and s["faces"] == 6 and not s["building"]
    people = client.get("/api/faces/people").get_json()["people"]
    assert [p["photos"] for p in people] == [3, 3]
    jpg = client.get(f"/api/faces/face/{people[0]['face_id']}.jpg")
    assert jpg.status_code == 200 and jpg.data[:2] == b"\xff\xd8"
    assert client.get("/api/faces/face/9999.jpg").status_code == 404

    pid = next(p["id"] for p in people if p["photos"] == 3)
    assert client.post(f"/api/faces/people/{pid}/name", json={"name": " Ann "}).get_json()["name"] == "Ann"
    people = client.get("/api/faces/people").get_json()["people"]
    assert people[0]["name"] == "Ann"                                      # named people sort first

    d = client.get(f"/api/library?person={people[0]['id']}").get_json()
    assert d["total"] == 3 and d["library_total"] == 6
    d = client.get(f"/api/library?person={people[0]['id']}&q=a3").get_json()  # other filters still apply
    assert [i["path"] for i in d["items"]] == ["a3.CR2"]
    assert client.get("/api/library?person=bob").status_code == 400

    other = people[1]["id"]
    assert client.post(f"/api/faces/people/{other}/merge", json={"into": other}).status_code == 400
    assert client.post(f"/api/faces/people/{other}/merge", json={"into": people[0]["id"]}).status_code == 200
    merged = client.get("/api/faces/people").get_json()["people"]
    assert len(merged) == 1 and merged[0]["name"] == "Ann" and merged[0]["photos"] == 6 - 1  # all but none.CR2
    assert client.post("/api/faces/people/999/name", json={"name": "x"}).status_code == 404
    assert client.post(f"/api/faces/people/{merged[0]['id']}/remove").status_code == 200
    assert client.get("/api/faces/people").get_json()["people"] == []


def test_library_without_person_is_unchanged(client):
    assert client.get("/api/library").get_json()["total"] == 6


# ------------------------------------------------------------------- CLI
def test_cli_scan_list_name(tmp_path, monkeypatch, capsys):
    imgs = make_lib(tmp_path)
    import semantic
    monkeypatch.setattr(semantic, "default_loader", lambda root: loader_for(imgs))
    faces.set_backend(BlobDetector(), faces.LbpEmbedder())
    assert faces.main(["scan", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "6 new faces" in out and "2 people" in out
    assert faces.main(["list", str(tmp_path)]) == 0
    pid = int(capsys.readouterr().out.split()[0])
    assert faces.main(["name", str(tmp_path), str(pid), "Ann", "Lee"]) == 0
    faces.main(["list", str(tmp_path)])
    assert "Ann Lee" in capsys.readouterr().out
    assert faces.main(["merge", str(tmp_path), "x"]) == 2
    assert faces.main(["status", str(tmp_path / "missing")]) == 2


def test_cli_is_dispatched_by_photo_room(monkeypatch):
    import photo_room_cli
    seen = {}
    monkeypatch.setattr(photo_room_cli, "preflight", lambda: [])
    monkeypatch.setattr(faces, "main", lambda argv: seen.setdefault("argv", argv) and 0)
    with pytest.raises(SystemExit) as e:
        photo_room_cli.main(["faces", "list", "/x"])
    assert e.value.code == 0 and seen["argv"] == ["list", "/x"]
