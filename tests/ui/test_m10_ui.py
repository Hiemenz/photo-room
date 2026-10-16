"""M10 polish in a real browser: theme, shortcut help, keyboard shortcuts. Marked ``ui``."""

import json


def set_range(page, el_id, value):
    page.evaluate(
        "([id, v]) => { const i = document.getElementById(id); i.value = v;"
        " i.dispatchEvent(new Event('input', {bubbles: true}));"
        " i.dispatchEvent(new Event('change', {bubbles: true})); }", [el_id, value])


def luminance_of_body(page) -> float:
    r, g, b = page.evaluate(
        "getComputedStyle(document.body).backgroundColor.match(/\\d+/g).slice(0, 3).map(Number)")
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255


def test_dark_theme_is_the_default_and_the_choice_is_remembered(opened):
    page = opened
    assert page.evaluate("document.documentElement.dataset.theme || 'dark'") == "dark"
    assert luminance_of_body(page) < 0.2
    # Neutral grey, not blue-tinted.
    r, g, b = page.evaluate(
        "getComputedStyle(document.body).backgroundColor.match(/\\d+/g).slice(0, 3).map(Number)")
    assert max(r, g, b) - min(r, g, b) <= 4

    page.locator("#theme-toggle").click()
    assert luminance_of_body(page) > 0.6
    assert page.evaluate("localStorage.getItem('photoroom-theme')") == "light"

    page.reload()
    page.wait_for_selector("#file-list .file")
    assert page.evaluate("document.documentElement.dataset.theme") == "light"
    assert luminance_of_body(page) > 0.6

    page.keyboard.press("Shift+T")
    assert page.evaluate("localStorage.getItem('photoroom-theme')") == "dark"
    assert luminance_of_body(page) < 0.2
    assert page.errors == []


def test_question_mark_opens_a_shortcut_list_and_escape_closes_it(opened):
    page = opened
    page.keyboard.press("?")
    assert page.locator("#help-dialog[open]").count() == 1
    text = page.locator("#help-body").inner_text()
    for needle in ("Library grid", "Crop", "Compare", "Survey", "Reject", "Pick", "Unflag",
                   "Undo", "Redo", "Export", "Before / after", "Mask overlay", "Copy / paste"):
        assert needle in text, needle
    page.keyboard.press("Escape")
    assert page.locator("#help-dialog[open]").count() == 0
    page.locator("#help-open").click()
    assert page.locator("#help-dialog[open]").count() == 1
    page.keyboard.press("?")                     # also toggles it closed
    assert page.locator("#help-dialog[open]").count() == 0
    assert page.errors == []


def test_shortcuts_do_not_fire_while_typing(opened):
    page = opened
    page.evaluate("""() => { const t = document.createElement('input'); t.type = 'text';
        t.id = 'scratch'; document.body.appendChild(t); t.focus(); }""")
    before = page.evaluate("JSON.stringify([ratings[current], document.documentElement.dataset.theme])")
    page.keyboard.type("?T5xprb0")
    assert page.locator("#help-dialog[open]").count() == 0
    assert page.evaluate("document.getElementById('scratch').value") == "?T5xprb0"
    assert page.evaluate("JSON.stringify([ratings[current], document.documentElement.dataset.theme])") == before
    assert not page.evaluate("values.monochrome")
    assert page.evaluate("cropping") is False


def test_rating_flag_crop_and_undo_shortcuts(opened):
    page = opened
    page.keyboard.press("3")
    page.wait_for_function("ratings[current] === 3")
    page.keyboard.press("x")
    page.wait_for_function("ratings[current] === -1")
    page.keyboard.press("0")
    page.wait_for_function("ratings[current] === 0")

    page.keyboard.press("r")
    page.wait_for_function("cropping === true")
    page.keyboard.press("r")
    page.wait_for_function("cropping === false")

    original = page.evaluate("values.exposure")
    set_range(page, "s-exposure", 1.5)
    page.wait_for_function("histState.entries.length >= 1")
    page.evaluate("commitChain")
    page.keyboard.press("Control+Shift+Z")       # nothing to redo: no change
    assert page.evaluate("values.exposure") == 1.5
    page.keyboard.press("Control+z")
    page.wait_for_function(f"values.exposure !== 1.5")
    assert page.evaluate("values.exposure") != 1.5
    page.keyboard.press("Control+Shift+z")
    page.wait_for_function("values.exposure === 1.5")
    assert original != 1.5
    assert page.errors == []


def test_p_u_and_labels_work_in_develop(opened):
    page = opened
    sent = []
    page.on("request", lambda r: sent.append((r.url.rsplit("/", 1)[-1], json.loads(r.post_data)))
            if r.url.endswith(("/api/flag", "/api/label")) and r.post_data else None)
    for key in ("p", "7", "u"):
        page.keyboard.press(key)
        page.wait_for_timeout(300)
    assert [(u, b.get("flag") or b.get("label")) for u, b in sent] == [
        ("flag", "pick"), ("label", "Yellow"), ("flag", "none")]
    assert all(len(b["paths"]) == 1 for _, b in sent)
    assert page.errors == []


def test_c_from_develop_opens_compare_in_the_library(opened):
    page = opened
    page.keyboard.press("c")
    page.wait_for_function("overlay === 'cmp'")
    page.keyboard.press("Escape")
    page.wait_for_function("overlay === null || overlay === ''")
    assert page.errors == []


def test_slider_drag_sends_sequence_numbers_and_the_last_sharp_render_wins(opened):
    page = opened
    bodies = []
    page.on("request", lambda r: bodies.append(json.loads(r.post_data))
            if r.url.endswith("/api/preview") and r.post_data else None)
    for v in (0.2, 0.4, 0.6, 0.8):
        page.evaluate("""(v) => { const i = document.getElementById('s-exposure'); i.value = v;
            i.dispatchEvent(new Event('input', {bubbles: true})); }""", v)
    page.wait_for_timeout(1200)
    seqs = [b["seq"] for b in bodies]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert bodies[-1]["fast"] is False and bodies[-1]["adjustments"]["exposure"] == 0.8
    assert any(b["fast"] for b in bodies)
    assert page.evaluate("document.getElementById('spinner').classList.contains('on')") is False
    assert page.errors == []
