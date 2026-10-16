"""
Tests for star ratings and rejection.

Ratings are written as XMP-xmp:Rating, the tag Lightroom and Bridge read,
with -1 for rejected — so marks made here mean the same thing wherever the
photos are opened next.
"""

import json
import subprocess
from pathlib import Path

import pytest

import editor
from editor import REJECTED, read_rating, read_ratings, write_rating


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(editor, "LIBRARY_ROOT", tmp_path)
    for name in ("a.dng", "b.dng", "c.dng"):
        (tmp_path / name).write_bytes(b"dng")
    return tmp_path


@pytest.fixture
def client(library):
    editor.app.config["TESTING"] = True
    with editor.app.test_client() as c:
        yield c


@pytest.fixture
def exiftool_json(monkeypatch):
    """
    A stand-in that answers -j with real JSON, keyed by SourceFile.

    Set ``tool.ratings`` to map a filename to its stored rating.
    """
    class Tool:
        def __init__(self):
            self.ratings: dict[str, int] = {}
            self.calls: list[list[str]] = []

        def __call__(self, cmd, **kwargs):
            self.calls.append(list(cmd))

            if "-j" in cmd:
                sources = (kwargs.get("input") or "").strip().split("\n")
                payload = [
                    {"SourceFile": s, "Rating": self.ratings.get(Path(s).name, 0)}
                    for s in sources if s
                ]
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

            write = next((a for a in cmd if a.startswith("-XMP-xmp:Rating=")), None)
            if write:
                self.ratings[Path(cmd[-1]).name] = int(write.split("=")[1])
                return subprocess.CompletedProcess(cmd, 0, stdout="1 image files updated", stderr="")

            name = Path(cmd[-1]).name
            value = self.ratings.get(name)
            return subprocess.CompletedProcess(
                cmd, 0, stdout=("-" if value is None else str(value)), stderr=""
            )

    tool = Tool()
    monkeypatch.setattr(editor.subprocess, "run", tool)
    return tool


# ---------------------------------------------------------------------------
# Single file
# ---------------------------------------------------------------------------

def test_an_unrated_file_reads_as_zero(exiftool_json):
    assert read_rating(Path("a.dng")) == 0


@pytest.mark.parametrize("value", [0, 1, 3, 5, REJECTED])
def test_ratings_round_trip(exiftool_json, value):
    write_rating(Path("a.dng"), value)
    assert read_rating(Path("a.dng")) == value


def test_the_tag_written_is_the_one_lightroom_reads(exiftool_json):
    write_rating(Path("a.dng"), 4)
    cmd = " ".join(exiftool_json.calls[-1])
    assert "-XMP-xmp:Rating=4" in cmd
    assert "-overwrite_original" in cmd


@pytest.mark.parametrize("given,expected", [(9, 5), (-7, REJECTED), (100, 5)])
def test_out_of_range_ratings_are_clamped(exiftool_json, given, expected):
    write_rating(Path("a.dng"), given)
    assert read_rating(Path("a.dng")) == expected


def test_an_unparseable_rating_reads_as_zero(exiftool_json, monkeypatch):
    monkeypatch.setattr(
        editor.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="not-a-number", stderr=""),
    )
    assert read_rating(Path("a.dng")) == 0


def test_a_write_failure_raises(monkeypatch):
    monkeypatch.setattr(
        editor.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="read-only"),
    )
    with pytest.raises(RuntimeError, match="read-only"):
        write_rating(Path("a.dng"), 3)


# ---------------------------------------------------------------------------
# Bulk read
# ---------------------------------------------------------------------------

def test_bulk_read_matches_each_file_to_its_own_rating(exiftool_json, library):
    """
    The regression this exists for: given several files, exiftool prints a
    "======== <path>" header before each one. Matching values to files by
    line position reported one photo's rating against another.
    """
    exiftool_json.ratings = {"a.dng": 5, "b.dng": 3, "c.dng": REJECTED}
    paths = [library / n for n in ("a.dng", "b.dng", "c.dng")]

    got = read_ratings(paths)

    assert got[str(paths[0])] == 5
    assert got[str(paths[1])] == 3
    assert got[str(paths[2])] == REJECTED


def test_bulk_read_uses_json_not_positional_lines(exiftool_json, library):
    read_ratings([library / "a.dng"])
    assert "-j" in exiftool_json.calls[-1], "bare values cannot be matched to files reliably"


def test_bulk_read_passes_paths_on_stdin(exiftool_json, library):
    """A few hundred paths would overflow the command line."""
    read_ratings([library / "a.dng", library / "b.dng"])
    assert "-@" in exiftool_json.calls[-1]


def test_bulk_read_defaults_unrated_files_to_zero(exiftool_json, library):
    exiftool_json.ratings = {"a.dng": 4}
    got = read_ratings([library / "a.dng", library / "b.dng"])
    assert got[str(library / "a.dng")] == 4
    assert got[str(library / "b.dng")] == 0


def test_bulk_read_of_nothing_is_empty(exiftool_json):
    assert read_ratings([]) == {}


def test_bulk_read_survives_malformed_output(library, monkeypatch):
    """Broken JSON must yield zeros, not a stack trace on page load."""
    monkeypatch.setattr(
        editor.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="}{not json", stderr=""),
    )
    got = read_ratings([library / "a.dng"])
    assert got == {str(library / "a.dng"): 0}


def test_bulk_read_agrees_with_reading_one_at_a_time(exiftool_json, library):
    exiftool_json.ratings = {"a.dng": 2, "b.dng": 5, "c.dng": REJECTED}
    paths = [library / n for n in ("a.dng", "b.dng", "c.dng")]

    bulk = read_ratings(paths)
    for p in paths:
        assert bulk[str(p)] == read_rating(p), f"{p.name} disagrees"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def test_rate_endpoint_writes_and_reports(client, exiftool_json):
    res = client.post("/api/rate", json={"path": "a.dng", "rating": 4})
    data = res.get_json()
    assert res.status_code == 200
    assert data["rating"] == 4
    assert "4 stars" in data["message"]


def test_rejecting_reads_as_rejected(client, exiftool_json):
    data = client.post("/api/rate", json={"path": "a.dng", "rating": -1}).get_json()
    assert "rejected" in data["message"]


def test_one_star_is_singular(client, exiftool_json):
    data = client.post("/api/rate", json={"path": "a.dng", "rating": 1}).get_json()
    assert "1 star" in data["message"] and "stars" not in data["message"]


def test_rating_a_path_outside_the_library_is_refused(client, exiftool_json):
    res = client.post("/api/rate", json={"path": "../../etc/passwd", "rating": 5})
    assert res.status_code == 400


def test_ratings_endpoint_lists_the_library(client, exiftool_json):
    exiftool_json.ratings = {"a.dng": 5, "c.dng": REJECTED}
    data = client.get("/api/ratings").get_json()["ratings"]

    assert data["a.dng"] == 5
    assert data["b.dng"] == 0
    assert data["c.dng"] == REJECTED


def test_ratings_endpoint_excludes_the_thumbnail_cache(client, library, exiftool_json):
    cache = library / ".photo-room-thumbs"
    cache.mkdir()
    (cache / "stale.dng").write_bytes(b"x")

    data = client.get("/api/ratings").get_json()["ratings"]
    assert not any(".photo-room-thumbs" in k for k in data)
