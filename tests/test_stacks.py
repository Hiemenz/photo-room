"""Library stacks: catalog state, the grid's collapse/expand view, and auto-stacking from Smart cull groups."""

import json

import pytest

import catalog
from test_cull_api import DATES, analyse_and_suggest, client, lib  # noqa: F401  (fixtures)

A, B, C, D = sorted(DATES)


def paths(client, **q):
    return [i["path"] for i in client.get("/api/library", query_string=q).get_json()["items"]]


def test_stack_create_unstack_and_dissolve(tmp_path):
    st = catalog.stack_photos(tmp_path, ["a", "b", "c"])
    assert st["top"] == "a" and st["collapsed"]
    catalog.stack_photos(tmp_path, ["c", "d"])          # c moves to the new stack
    stacks = {s["top"]: s["photos"] for s in catalog.list_stacks(tmp_path)}
    assert stacks == {"a": ["a", "b"], "c": ["c", "d"]}
    assert catalog.unstack(tmp_path, ["b"]) == 1         # a stack of one dissolves
    assert [s["photos"] for s in catalog.list_stacks(tmp_path)] == [["c", "d"]]
    with pytest.raises(ValueError):
        catalog.stack_photos(tmp_path, ["a"])


def test_top_moves_when_removed_and_state_validation(tmp_path):
    st = catalog.stack_photos(tmp_path, ["a", "b", "c"])
    catalog.unstack(tmp_path, ["a"])
    assert catalog.list_stacks(tmp_path)[0]["top"] == "b"
    catalog.set_stack_state(tmp_path, [st["id"]], False, top="c")
    got = catalog.list_stacks(tmp_path)[0]
    assert got["top"] == "c" and not got["collapsed"]
    with pytest.raises(ValueError):
        catalog.set_stack_state(tmp_path, [st["id"]], True, top="zzz")
    with pytest.raises(KeyError):
        catalog.set_stack_state(tmp_path, ["nope"])


def test_auto_stack_leaves_manual_stacks_alone(tmp_path):
    catalog.stack_photos(tmp_path, ["a", "b"])
    made = catalog.auto_stack(tmp_path, [["b", "c", "d"], ["e", "f"], ["g"]])
    assert [s["photos"] for s in made] == [["c", "d"], ["e", "f"]]
    assert ["a", "b"] in [s["photos"] for s in catalog.list_stacks(tmp_path)]


def test_stacks_share_the_catalog_store_with_collections(tmp_path):
    catalog.stack_photos(tmp_path, ["a", "b"])
    catalog.create_collection(tmp_path, "X")
    data = json.loads((tmp_path / catalog.STORE_NAME).read_text())
    assert len(data["stacks"]) == 1 and len(data["collections"]) == 1


def test_grid_collapses_and_expands(client):
    r = client.post("/api/stacks/create", json={"paths": [B, C, A]}).get_json()
    assert r["ok"] and len(r["stacks"]) == 1
    page = client.get("/api/library").get_json()
    assert [i["path"] for i in page["items"]] == [B, D]            # A and C hidden; B (top) stays in place
    top = page["items"][0]["stack"]
    assert top["count"] == 3 and top["top"] and top["collapsed"]
    assert page["library_total"] == 4                              # the library itself is unchanged
    client.post("/api/stacks/state", json={"paths": [A], "collapsed": False})
    page = client.get("/api/library").get_json()
    assert [i["path"] for i in page["items"]] == [A, B, C, D]
    assert [i["stack"]["pos"] for i in page["items"][:3]] == [3, 1, 2]       # stack order is B, C, A
    assert "stack" not in page["items"][3]
    client.post("/api/stacks/state", json={"collapsed": True})     # every stack
    assert paths(client) == [B, D]
    assert paths(client, stacks="0") == [A, B, C, D]


def test_collapsed_stack_shows_a_member_when_top_is_filtered_out(client):
    client.post("/api/stacks/create", json={"paths": [A, B, C]})
    client.post("/api/rate", json={"path": B, "rating": 3})
    client.post("/api/rate", json={"path": C, "rating": 3})
    assert paths(client, rating_min="3") == [B]


def test_unstack_endpoint_and_validation(client):
    client.post("/api/stacks/create", json={"paths": [A, B]})
    assert client.post("/api/stacks/create", json={"paths": [A]}).status_code == 400
    assert client.post("/api/stacks/create", json={"paths": ["../x"]}).status_code == 400
    assert client.post("/api/stacks/state", json={"ids": ["nope"]}).status_code == 404
    r = client.post("/api/stacks/remove", json={"paths": [A]}).get_json()
    assert r["stacks"] == [] and paths(client) == [A, B, C, D]


def test_auto_stack_uses_smart_cull_groups(client):
    # without analysis nothing can be stacked, and it says so
    r = client.post("/api/stacks/auto", json={"scope": "all"}).get_json()
    assert r["created"] == 0 and r["missing"] == 4
    analyse_and_suggest(client)
    r = client.post("/api/stacks/auto", json={"scope": "all"}).get_json()
    assert r["created"] == 1 and r["photos"] == 3 and r["missing"] == 0
    st = r["stacks"][0]
    assert set(st["photos"]) == {A, B, C} and st["top"] == A       # the sharp frame leads
    assert paths(client) == [A, D]
    assert client.post("/api/stacks/auto", json={"scope": "all"}).get_json()["created"] == 0
    assert client.post("/api/stacks/auto", json={"scope": "bogus"}).status_code == 400
