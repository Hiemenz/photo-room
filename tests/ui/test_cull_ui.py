"""Smart cull in a real browser: grouping, keyboard review, explicit apply and undo, compare, Auto edit."""

import io
from pathlib import Path

import pytest
from PIL import Image

import thumbs
from test_cull import textured


def jpeg(arr):
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="JPEG", quality=88)
    return buf.getvalue()


@pytest.fixture
def burst_files(editor_url, monkeypatch):
    """Three frames of one scene (sharp, soft, blown), added to the UI library before the page loads."""
    import editor

    root = editor.LIBRARY_ROOT
    for name, arr in {"B_0001.dng": textured(seed=1), "B_0002.dng": textured(seed=1, blur=3.0),
                      "B_0003.dng": textured(seed=1, offset=235)}.items():
        (root / name).write_bytes(jpeg(arr))
    real = thumbs._embedded_preview

    def preview(p):
        data = Path(p).read_bytes()
        return (data, 1) if data[:2] == b"\xff\xd8" else (None, None)

    monkeypatch.setattr(thumbs, "_embedded_preview", preview)
    return root


def flags(page):
    return page.evaluate("fetch('/api/library').then(r => r.json()).then(d => Object.fromEntries(d.items.map(i => [i.name, i.flag])))")


def open_cull(page):
    page.locator("#mod-library").click()
    page.wait_for_selector("#lib-grid .lcell")
    page.locator("#lib-cull").click()
    page.wait_for_selector("#cull-groups .cg .cf")
    page.wait_for_function("!document.getElementById('cull-progress') || document.getElementById('cull-progress').hidden")


def test_cull_groups_bursts_marks_the_best_and_changes_nothing_until_apply(burst_files, page):
    open_cull(page)
    burst = page.locator("#cull-groups .cg", has=page.locator(".cf", has_text="B_0001"))
    assert burst.locator(".cf").count() == 3
    assert burst.locator(".cf", has_text="B_0001").locator(".tag.best").count() == 1
    assert burst.locator(".cf", has_text="B_0002").locator(".sug.reject").count() == 1
    assert page.locator("#cull-apply").is_disabled()
    assert set(flags(page).values()) == {"none"}
    assert page.errors == []


def test_keyboard_review_apply_and_undo(burst_files, page):
    open_cull(page)
    page.locator(".cf", has_text="B_0001").click()
    page.keyboard.press("Enter")                                      # accept this burst's suggestions
    assert page.locator("#cull-apply").is_enabled()
    assert set(flags(page).values()) == {"none"}                      # still only staged
    page.keyboard.press("Control+Enter")
    page.wait_for_function("document.getElementById('cull-undo').disabled === false")
    f = flags(page)
    assert f["B_0001.dng"] == "pick" and f["B_0002.dng"] == "reject" and f["B_0003.dng"] == "reject"
    page.locator("#cull-undo").click()
    page.wait_for_function("document.getElementById('cull-undo').disabled === true")
    assert set(flags(page).values()) == {"none"}
    assert page.errors == []


def test_override_with_keys_is_what_gets_written(burst_files, page):
    open_cull(page)
    page.locator(".cf", has_text="B_0002").click()
    page.keyboard.press("p")                                          # the user prefers the soft one
    assert "c-pick" in page.locator(".cf", has_text="B_0002").get_attribute("class")
    page.keyboard.press("ArrowLeft")                                  # to B_0001
    page.keyboard.press("u")
    page.locator("#cull-apply").click()
    page.wait_for_function("document.getElementById('cull-undo').disabled === false")
    f = flags(page)
    assert f["B_0002.dng"] == "pick" and f["B_0001.dng"] == "none"
    page.locator("#cull-learn").click()
    page.wait_for_selector("#cull-learn-dlg[open]")
    text = page.locator("#cull-learn-body").inner_text()
    assert "Reviewed photos" in text and "3" in text and page.locator("#cull-acc polyline").count() == 1
    page.keyboard.press("Escape")
    assert page.errors == []


def test_compare_side_by_side_and_actual_size_with_synced_pan(burst_files, page):
    page.set_viewport_size({"width": 700, "height": 500})            # panes narrower than the 360 px test frame
    open_cull(page)
    page.locator(".cf", has_text="B_0001").click()
    page.keyboard.press("c")
    page.wait_for_selector("#cull-cmp.on .xpane img.full")
    assert page.locator("#cull-cmp .xpane").count() == 3
    page.keyboard.press("Tab")
    assert "focus" in page.locator("#cull-cmp .xpane").nth(1).get_attribute("class")
    page.keyboard.press("z")                                          # 100 %: real tiles from the tile server
    page.wait_for_function("document.querySelectorAll('#cull-cmp .xpane .stage img').length >= 3")
    page.wait_for_function("[...document.querySelectorAll('#cull-cmp .xpane .stage img')].every(i => i.complete && i.naturalWidth > 0)")
    t0 = page.evaluate("[...document.querySelectorAll('#cull-cmp .stage')].map(s => s.style.transform)")
    box = page.locator("#cull-cmp .xpane").first.bounding_box()
    page.mouse.move(box["x"] + 100, box["y"] + 100)
    page.mouse.down(); page.mouse.move(box["x"] + 60, box["y"] + 100, steps=4); page.mouse.up()
    t1 = page.evaluate("[...document.querySelectorAll('#cull-cmp .stage')].map(s => s.style.transform)")
    assert len(set(t1)) == 1 and t1 != t0                             # every pane moved, and together
    page.keyboard.press("Escape")
    assert page.locator("#cull-cmp.on").count() == 0 and page.locator("#cull.on").count() == 1
    page.keyboard.press("Escape")
    assert page.locator("#cull.on").count() == 0
    assert page.errors == []


def test_auto_edit_previews_then_applies_one_undoable_batch(burst_files, page):
    open_cull(page)
    page.locator(".cf", has_text="B_0001").click()
    page.keyboard.press("Enter")                                      # accept: B_0001 becomes a pick
    page.locator("#cull-auto").click()
    page.wait_for_selector("#cull-auto-dlg[open] .ap img")
    page.wait_for_function("document.getElementById('cull-auto-go').disabled === false")
    assert "history step" in page.locator("#cull-auto-note").inner_text()
    imgs = page.locator("#cull-auto-dlg .ap img")
    assert imgs.count() == 2 * page.locator("#cull-auto-dlg .ap").count()
    page.locator("#cull-auto-go").click()
    page.wait_for_function("document.getElementById('cull-info').textContent.includes('Auto edit applied')", timeout=60000)
    h = page.evaluate("fetch('/api/history', {method: 'POST', headers: {'Content-Type': 'application/json'}, "
                      "body: JSON.stringify({path: 'B_0001.dng'})}).then(r => r.json())")
    assert [e["label"] for e in h["history"]["entries"]] == ["Auto edit"]
    page.locator("#cull-undo").click()
    page.wait_for_function("document.getElementById('cull-undo').disabled === true")
    assert page.errors == []
