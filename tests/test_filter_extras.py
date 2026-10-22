"""Lens / ISO / focal length / file type / edit status / GPS filters and smart-collection rules."""

import pytest

import catalog
import libindex
import library


def it(path, **kw):
    base = {"path": path, "name": path.rsplit("/", 1)[-1], "rating": 0, "flag": "none", "label": "",
            "keywords": [], "camera": "", "date": "", "lens": "", "iso": 0, "focal": 0,
            "edited": False, "gps": False}
    return {**base, **kw}


ITEMS = [
    it("a.cr2", lens="EF 50mm", iso=100, focal=50.0, edited=True),
    it("b.dng", lens="EF 24-70mm", iso=3200, focal=24.0, gps=True),
    it("c.NEF", lens="EF 24-70mm", iso=800, focal=70.0),
    it("d.arw"),
]


def names(f):
    return [i["path"] for i in library.filter_items(ITEMS, f)]


def test_filters():
    assert names({"lens": "ef 50mm"}) == ["a.cr2"]
    assert names({"iso_min": 800}) == ["b.dng", "c.NEF"]
    assert names({"iso_max": 800}) == ["a.cr2", "c.NEF"]
    assert names({"focal_min": 30, "focal_max": 60}) == ["a.cr2"]
    assert names({"filetype": ".nef"}) == ["c.NEF"]
    assert names({"edit": "edited"}) == ["a.cr2"]
    assert names({"edit": "unedited"}) == ["b.dng", "c.NEF", "d.arw"]
    assert names({"gps": "yes"}) == ["b.dng"]
    assert names({"gps": "no"}) == ["a.cr2", "c.NEF", "d.arw"]
    assert names({"edit": "unedited", "gps": "no", "lens": "EF 24-70mm"}) == ["c.NEF"]


def test_bad_values_raise():
    for f in ({"edit": "maybe"}, {"gps": "x"}, {"filetype": "a/b"}):
        with pytest.raises(ValueError):
            library.filter_items(ITEMS, f)


def test_virtual_copy_filters_by_source_type():
    v = it("vc1", src_path="x/img.cr3")
    assert library.item_filetype(v) == "cr3"


def test_facets_list_file_types():
    assert library.facets(ITEMS)["filetypes"] == ["arw", "cr2", "dng", "nef"]


def rule_names(*rules, match="all"):
    rs = catalog.normalize_rules(list(rules))
    return [i["path"] for i in catalog.smart_matches(ITEMS, rs, match)]


def test_smart_rules():
    assert rule_names({"field": "lens", "op": "contains", "value": "24-70"}) == ["b.dng", "c.NEF"]
    assert rule_names({"field": "lens", "op": "is", "value": "ef 50mm"}) == ["a.cr2"]
    assert rule_names({"field": "iso", "op": "ge", "value": "800"}) == ["b.dng", "c.NEF"]
    assert rule_names({"field": "iso", "op": "eq", "value": 100}) == ["a.cr2"]
    assert rule_names({"field": "focal", "op": "le", "value": "50"}) == ["a.cr2", "b.dng"]
    assert rule_names({"field": "edited", "value": "edited"}) == ["a.cr2"]
    assert rule_names({"field": "edited", "value": "unedited"}) == ["b.dng", "c.NEF", "d.arw"]
    assert rule_names({"field": "filetype", "value": ".DNG"}) == ["b.dng"]
    assert rule_names({"field": "filetype", "value": "dng"}, {"field": "iso", "op": "ge", "value": 1},
                      match="any") == ["a.cr2", "b.dng", "c.NEF"]


def test_rule_validation():
    for bad in ({"field": "iso", "value": "high"}, {"field": "edited", "value": "x"},
                {"field": "lens", "value": ""}, {"field": "filetype", "value": ""},
                {"field": "iso", "op": "is", "value": 1}):
        with pytest.raises(ValueError):
            catalog.normalize_rule(bad)


def test_parse_meta_reads_new_fields():
    m = libindex.parse_meta({"Model": "X", "FocalLength": 35.0, "GPSLatitude": 51.5})
    assert m["focal"] == 35.0 and m["gps"] and not m["edited"]
    m = libindex.parse_meta({"Model": "X"}, {"Exposure2012": 0.5})
    assert m["edited"] and not m["gps"] and m["focal"] == 0
    assert libindex.parse_meta({"Model": "X"}, {"Rating": 3})["edited"] is False
