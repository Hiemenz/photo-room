"""Metadata panel: EXIF readout, saving a field to the sidecar, undo, presets, and its Library placement."""

import pytest

import metadata

INFO = {"camera": "Canon EOS 1000D", "lens": "EF 50mm f/1.8", "iso": 400, "shutter": "1/250 s",
        "aperture": "f/2.8", "focal_length": "50 mm", "captured": "2026-09-14 18:42:07",
        "gps": {"lat": 47.6, "lon": -122.3, "alt": 56.0, "text": "47.60620° N, 122.33210° W, 56 m"},
        "width": 3888, "height": 2592, "dimensions": "3888 × 2592"}


@pytest.fixture(autouse=True)
def canned_readout(monkeypatch, editor_url):
    """The fake exiftool cannot answer -j reads; serve the readout and echo what it was told to write."""
    def read(path):
        t = editor_url.tool.tags
        status = {"True": "copyrighted", "False": "public_domain"}.get(t.get("Marked", ""), "unknown")
        return {"info": INFO, "fields": {"title": t.get("Title", ""), "caption": t.get("Description", ""),
                                         "creator": t.get("Creator", ""), "copyright": t.get("Rights", ""),
                                         "copyright_status": status, "usage_terms": t.get("UsageTerms", "")}}
    monkeypatch.setattr(metadata, "read", read)


def test_readout_shows_the_camera_settings_in_develop(opened):
    page = opened
    page.wait_for_function("document.getElementById('meta-info').textContent.includes('Canon EOS 1000D')")
    text = page.locator("#meta-info").inner_text()
    for needle in ("EF 50mm f/1.8", "400", "1/250 s", "f/2.8", "50 mm", "2026-09-14 18:42:07", "47.60620", "3888 × 2592"):
        assert needle in text, needle
    assert page.locator("#meta-panel").evaluate("p => p.closest('#controls') !== null")
    assert page.errors == []


def test_editing_a_field_writes_the_sidecar_and_undo_restores_it(opened, editor_url):
    page = opened
    page.wait_for_function("document.getElementById('meta-info').textContent.includes('Canon')")
    page.locator("#meta-title").fill("Ferry at dusk")
    page.locator("#meta-title").press("Enter")
    page.wait_for_function("!document.getElementById('meta-undo').disabled")
    assert editor_url.tool.tags["Title"] == "Ferry at dusk"
    page.locator("#meta-status").select_option("copyrighted")
    page.wait_for_function("document.getElementById('toasts').textContent.includes('Metadata saved')")
    assert editor_url.tool.tags["Marked"] == "True"
    page.locator("#meta-undo").click()
    page.wait_for_function("document.getElementById('toasts').textContent.includes('undone')")
    assert editor_url.tool.tags["Marked"] == ""
    assert editor_url.tool.tags["Title"] == "Ferry at dusk"          # only the last step was undone
    assert page.errors == []


def test_library_shows_the_panel_on_the_right_and_presets_apply(page, editor_url):
    page.locator("#mod-library").click()
    page.wait_for_selector("#lib-grid .lcell")
    page.locator('#lib-grid .lcell[data-path="IMG_0001.dng"]').click()
    page.wait_for_function("document.querySelector('#lib-meta #meta-panel') !== null")
    page.wait_for_function("document.getElementById('meta-info').textContent.includes('Canon')")
    page.locator("#meta-copyright").fill("(c) 2026 Kevin")
    page.locator("#meta-copyright").press("Enter")
    page.wait_for_function("document.getElementById('toasts').textContent.includes('Metadata saved')")
    page.once("dialog", lambda d: d.accept("Copyright"))
    page.locator("#meta-preset-save").click()
    page.wait_for_function("[...document.getElementById('meta-preset').options].some(o => o.value === 'Copyright')")
    page.locator("#meta-preset").select_option("Copyright")
    page.locator('#lib-grid .lcell[data-path="IMG_0002.dng"]').click()
    page.wait_for_function("document.getElementById('meta-target').textContent.includes('IMG_0002')")
    editor_url.tool.tags.pop("Rights", None)
    page.locator("#meta-preset-apply").click()
    page.wait_for_function("document.getElementById('toasts').textContent.includes('applied')")
    assert editor_url.tool.tags["Rights"] == "(c) 2026 Kevin"
    page.once("dialog", lambda d: d.accept())
    page.locator("#meta-preset-del").click()
    page.wait_for_function("![...document.getElementById('meta-preset').options].some(o => o.value === 'Copyright')")
    page.locator("#mod-develop").click()
    page.wait_for_function("document.querySelector('#controls #meta-panel') !== null")
    assert page.errors == []
