"""Auto tone, Auto WB, WB presets, the eyedropper, the clipping overlay (J) and the live histogram."""

import pytest

from test_m10_ui import set_range

TONE_KEYS = ("exposure", "contrast", "highlights", "shadows", "whites", "blacks", "vibrance")


def history_labels(page):
    return page.evaluate("histState.entries.map(e => e.label)")


def idle(page):
    page.wait_for_function("!document.getElementById('spinner').classList.contains('on')")


def test_auto_tone_sets_the_seven_sliders_as_one_undoable_step(opened):
    page = opened
    set_range(page, "s-exposure", -2)                 # a deliberately bad start
    page.wait_for_function("histState.entries.some(e => e.label.startsWith('Exposure'))")
    steps = len(history_labels(page))
    page.wait_for_function("!document.getElementById('auto-tone').disabled")
    before = page.evaluate("({...values})")
    page.locator("#auto-tone").click()
    page.wait_for_function("document.getElementById('status').textContent.includes('Auto tone applied')")
    after = page.evaluate("({...values})")
    assert after["exposure"] > before["exposure"], "auto tone should lift an underexposed frame"
    assert any(after[k] != before[k] for k in TONE_KEYS)
    for k in after:
        if k not in TONE_KEYS and k not in ("tone_curve", "masks", "crop"):
            assert after[k] == before[k], f"{k} must be untouched"
    page.wait_for_function("(n) => histState.entries.length === n + 1", arg=steps)
    assert history_labels(page)[-1] == "Auto Tone"
    # the slider thumbs match the values, and one undo returns to the start
    assert float(page.locator("#s-exposure").input_value()) == pytest.approx(after["exposure"])
    page.keyboard.press("Control+z")
    page.wait_for_function("(e) => values.exposure === e", arg=before["exposure"])
    assert page.evaluate("values.exposure") == before["exposure"]
    assert page.errors == []


def test_white_balance_presets_list_and_apply(opened):
    page = opened
    page.wait_for_function("document.getElementById('wb-preset').options.length > 5")
    labels = page.locator("#wb-preset option").all_inner_texts()
    for needle in ("As Shot", "Auto", "Daylight", "Cloudy", "Shade", "Tungsten", "Fluorescent", "Flash"):
        assert any(l.startswith(needle) for l in labels), needle
    page.locator("#wb-preset").select_option(label=next(l for l in labels if l.startswith("Tungsten")))
    page.wait_for_function("values.color_temp < 4000")
    assert float(page.locator("#s-color_temp").input_value()) == pytest.approx(page.evaluate("values.color_temp"))
    assert "Tungsten" in page.locator("#wb-readout").inner_text()
    page.wait_for_function("histState.entries.some(e => e.label === 'White Balance: Tungsten')")
    assert page.locator("#wb-preset").input_value() == ""          # back to the prompt, ready for the next pick
    page.locator("#wb-preset").select_option(label=next(l for l in labels if l.startswith("As Shot")))
    page.wait_for_function("values.color_temp === baselineTemp")
    assert page.errors == []


def test_auto_white_balance_button(opened):
    page = opened
    page.wait_for_function("!document.getElementById('wb-auto').disabled")
    set_range(page, "s-color_temp", 9000)
    page.wait_for_function("values.color_temp === 9000")
    page.locator("#wb-auto").click()
    page.wait_for_function("document.getElementById('status').textContent.includes('Auto white balance applied')")
    assert page.evaluate("values.color_temp") != 9000
    assert "Auto:" in page.locator("#wb-readout").inner_text()
    page.wait_for_function("histState.entries.some(e => e.label === 'Auto White Balance')")
    assert page.errors == []


def test_eyedropper_reads_rgb_under_the_pointer_and_sets_white_balance(opened):
    page = opened
    page.wait_for_function("!document.getElementById('wb-pick').disabled")
    page.locator("#wb-pick").click()
    assert page.locator("#wb-pick").get_attribute("aria-pressed") == "true"
    box = page.locator("#preview").bounding_box()
    x, y = box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.9      # the grey strip
    page.mouse.move(x, y)
    page.wait_for_selector("#wb-hud:not([hidden])")
    hud = page.locator("#wb-hud").inner_text()
    assert hud.startswith("R ") and "G " in hud and "B " in hud
    r, g, b = (int(t) for t in hud.replace("R", "").replace("G", "").replace("B", "").split())
    assert all(0 <= c <= 255 for c in (r, g, b))
    set_range(page, "s-color_temp", 9000)                      # a cast to neutralise
    page.wait_for_function("values.color_temp === 9000")
    temp0 = page.evaluate("values.color_temp")
    page.mouse.click(x, y)
    page.wait_for_function("document.getElementById('wb-readout').textContent.includes('K, tint')")
    assert page.locator("#wb-pick").get_attribute("aria-pressed") == "false"      # one click, then done
    assert page.locator("#wb-hud").is_hidden()
    assert "R " in page.locator("#wb-readout").inner_text()
    assert page.evaluate("values.color_temp") != temp0
    page.wait_for_function("histState.entries.some(e => e.label === 'White Balance Picker')")
    assert page.errors == []


def test_eyedropper_escape_cancels_and_a_dark_pick_explains_itself(opened):
    page = opened
    page.locator("#wb-pick").click()
    page.keyboard.press("Escape")
    assert page.locator("#wb-pick").get_attribute("aria-pressed") == "false"
    temp = page.evaluate("values.color_temp")
    page.route("**/api/develop/wb_pick", lambda r: r.fulfill(
        status=422, content_type="application/json",
        body='{"error": "The sampled area is too dark to judge a neutral"}'))
    page.locator("#wb-pick").click()
    box = page.locator("#preview").bounding_box()
    page.mouse.click(box["x"] + 20, box["y"] + 20)
    page.wait_for_selector("#toasts .toast.error")
    text = page.locator("#toasts .toast.error").inner_text()
    assert "too dark" in text and "brighter" in text
    assert page.evaluate("values.color_temp") == temp


def test_eyedropper_works_in_the_zoomed_view_too(opened):
    page = opened
    page.evaluate("zoomTo(1)")
    page.wait_for_selector("#zoom-pane img:not(#zoom-base):not(.pending)")
    page.locator("#wb-pick").click()
    assert page.locator("#zoom-layer.picking").count() == 1
    r = page.evaluate("(() => { const d = displayedRect(), s = document.getElementById('stage').getBoundingClientRect(); return [s.x + d.left + d.width * 0.5, s.y + d.top + d.height * 0.9]; })()")
    page.mouse.move(*r)
    page.wait_for_selector("#wb-hud:not([hidden])")
    page.mouse.click(*r)
    page.wait_for_function("document.getElementById('wb-readout').textContent.includes('K, tint')")
    assert page.errors == []


def overlay_matches_preview(page):
    return page.evaluate("""(() => { const o = document.getElementById('clip-overlay').getBoundingClientRect();
        const p = document.getElementById('preview').getBoundingClientRect();
        return [o.x - p.x, o.y - p.y, o.width - p.width, o.height - p.height].map(Math.abs).every(v => v < 2); })()""")


def test_clipping_overlay_toggles_with_j_and_shows_per_channel_percentages(opened):
    page = opened
    assert page.locator("#clip-overlay").is_hidden() and page.locator("#clip-detail").is_hidden()
    page.keyboard.press("j")
    page.wait_for_selector("#clip-overlay:not([hidden])")
    assert page.locator("#clip-toggle").get_attribute("aria-pressed") == "true"
    assert overlay_matches_preview(page)
    page.wait_for_selector("#clip-detail:not([hidden])")
    text = page.locator("#clip-detail").inner_text()
    for needle in ("Highlights", "Shadows", "R ", "G ", "B "):
        assert needle in text, text
    # push exposure up: the highlight numbers grow and the overlay follows the next preview
    before = page.evaluate("document.getElementById('clip-detail').textContent")
    set_range(page, "s-exposure", 4)
    page.wait_for_function("(b) => document.getElementById('clip-detail').textContent !== b", arg=before, timeout=15000)
    hi = lambda t: float(t.split("Highlights ")[1].split("%")[0])
    assert hi(page.locator("#clip-detail").inner_text()) > hi(before)
    assert overlay_matches_preview(page)
    page.keyboard.press("j")
    assert page.locator("#clip-overlay").is_hidden() and page.locator("#clip-detail").is_hidden()
    page.locator("#clip-toggle").click()                      # the button does the same
    page.wait_for_selector("#clip-overlay:not([hidden])")
    assert page.errors == []


def test_clipping_overlay_follows_the_photo_when_zoomed(opened):
    page = opened
    page.keyboard.press("j")
    page.wait_for_selector("#clip-overlay:not([hidden])")
    page.evaluate("zoomTo(2)")
    page.wait_for_function("ZM.mode === 'scale'")
    ok = page.evaluate("""(() => { const o = document.getElementById('clip-overlay').getBoundingClientRect();
        const l = document.getElementById('zoom-base').getBoundingClientRect();
        return Math.abs(o.width - l.width) < 2 && Math.abs(o.height - l.height) < 2 && Math.abs(o.x - l.x) < 2; })()""")
    assert ok


def test_clipping_key_is_ignored_while_typing(opened):
    page = opened
    page.locator("#looks-panel > summary").click()
    page.locator("#looks-cat").focus()
    page.keyboard.press("j")
    assert page.locator("#clip-overlay").is_hidden()


def test_histogram_is_live_and_follows_the_sliders(opened):
    page = opened
    snap = "(() => { const c = document.getElementById('histogram'); const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data; let h = 0, n = 0; for (let i = 3; i < d.length; i += 4) { if (d[i]) n++; h = (h * 31 + d[i - 3] + d[i - 2]) | 0; } return [n, h]; })()"
    n0, h0 = page.evaluate(snap)
    assert n0 > 500, "the histogram canvas should be drawn"
    set_range(page, "s-exposure", -2)
    page.wait_for_function(f"(h) => {snap}[1] !== h", arg=h0)
    assert page.locator("#clip-shadows").inner_text().startswith("shadows")
