"""Laptop layout: scrolling side panel, accordion, hide/show, remembered state, export dialog, error toasts."""

import statistics
import time

import pytest

from test_m10_ui import set_range

SIZES = [(1440, 900), (1280, 720)]


def box(page, selector):
    return page.evaluate(
        "(s) => { const r = document.querySelector(s).getBoundingClientRect();"
        " return {x: r.x, y: r.y, w: r.width, h: r.height, b: r.bottom, r: r.right}; }", selector)


@pytest.mark.parametrize("w,h", SIZES)
def test_sliders_stay_usable_at_laptop_sizes(page_at, w, h):
    page = page_at(w, h)
    page.wait_for_selector("#acc-tone[open]")
    scroll = box(page, "#controls-scroll")
    assert scroll["h"] >= 300, f"slider area is only {scroll['h']}px tall"
    # the buttons stay on screen and the page itself never scrolls sideways
    actions = box(page, "#actions")
    assert actions["b"] <= h + 1 and actions["y"] >= scroll["b"] - 1
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    # every slider can be scrolled to and has a real track height and width
    for key in ("exposure", "contrast", "color_temp", "tint", "vibrance", "sharpness"):
        page.evaluate("(k) => { const d = document.getElementById('ctrl-' + k).closest('details'); if (d) d.open = true; }", key)
        page.locator(f"#s-{key}").scroll_into_view_if_needed()
        b = box(page, f"#s-{key}")
        assert b["w"] > 150
        assert box(page, f"#ctrl-{key}")["h"] >= 30            # the whole row, label and track
        assert 0 <= b["y"] and b["b"] <= h
    # dragging a slider still re-renders
    before = page.evaluate("document.getElementById('preview').src")
    set_range(page, "s-exposure", 1.5)
    page.wait_for_function("(b) => document.getElementById('preview').src !== b", arg=before)
    page.screenshot(path=f"tests/ui/artifacts/layout-{w}x{h}.png")
    assert page.errors == []


def test_short_windows_start_with_the_essentials_open(page_at):
    page = page_at(1280, 720)
    opened = page.evaluate("[...document.querySelectorAll('details.acc')].filter(d => d.open).map(d => d.dataset.acc)")
    assert "tone" in opened and "wb" in opened and "presence" not in opened
    tall = page_at(1440, 900)
    assert tall.evaluate("document.querySelector('details.acc[data-acc=presence]').open") is True


def test_panel_hides_with_button_and_bracket_keys_and_is_remembered(page_at):
    page = page_at(1280, 720)
    stage_w = box(page, "#stage")["w"]
    page.keyboard.press("]")
    assert not page.locator("#controls").is_visible()
    assert box(page, "#stage")["w"] > stage_w + 200
    assert page.locator("#panel-toggle").get_attribute("aria-pressed") == "false"
    page.reload()
    page.wait_for_selector("#file-list .file")
    assert not page.locator("#controls").is_visible(), "hidden state should survive a reload"
    page.locator("#panel-toggle").click()
    assert page.locator("#controls").is_visible()
    page.keyboard.press("[")
    assert not page.locator("#files").is_visible()
    page.keyboard.press("[")
    assert page.locator("#files").is_visible()
    # not while typing
    page.locator("#file-list .file").first.click()
    page.locator("#looks-panel > summary").click()
    page.locator("#looks-cat").focus()
    page.keyboard.press("]")
    assert page.locator("#controls").is_visible()
    assert page.errors == []


def test_accordion_folds_remembers_and_solo_mode(page_at):
    page = page_at(1440, 900)
    tone = page.locator("#acc-tone > summary")
    assert page.locator("#s-exposure").is_visible()
    tone.click()
    assert not page.locator("#s-exposure").is_visible()
    # The <details> toggle event (which saves the state) fires after the click returns;
    # reloading before it ran made this test flaky on CI.
    page.wait_for_function("accState().tone === false")
    page.reload()
    page.wait_for_selector("#file-list .file")
    assert page.evaluate("document.getElementById('acc-tone').open") is False, "fold state is remembered"
    page.locator("#acc-expand").click()
    assert page.locator("#s-exposure").is_visible() and page.locator("#s-vibrance").is_visible()
    page.locator("#acc-collapse").click()
    assert page.evaluate("[...document.querySelectorAll('#controls-scroll details')].every(d => !d.open)")
    # solo: opening one folds the others
    page.locator("#acc-solo").check()
    page.locator("#acc-tone > summary").click()
    page.locator("#acc-wb > summary").click()
    page.wait_for_function("!document.getElementById('acc-tone').open")
    assert page.evaluate("document.getElementById('acc-wb').open") is True
    assert page.errors == []


def test_help_lists_the_new_shortcuts(opened):
    opened.keyboard.press("?")
    text = opened.locator("#help-body").inner_text()
    for needle in ("Clipping overlay", "Zoom", "adjustments panel", "Quick look"):
        assert needle in text, needle


@pytest.mark.parametrize("w,h", [(1280, 720), (1280, 560), (1024, 480)])
def test_export_dialog_fits_short_windows(page_at, w, h):
    page = page_at(w, h)
    page.locator("#export-open").click()
    page.wait_for_selector("#export-dialog[open]")
    d = box(page, "#export-dialog")
    assert d["y"] >= 0 and d["b"] <= h + 1, f"dialog overflows: {d}"
    # the header and the buttons are on screen; the options scroll
    assert box(page, "#export-dialog h2")["y"] >= 0
    go = box(page, "#ex-go")
    assert go["b"] <= h + 1 and go["y"] >= 0
    body = page.evaluate("(() => { const b = document.querySelector('#export-dialog .body'); return [b.scrollHeight, b.clientHeight]; })()")
    if h <= 560:
        assert body[0] > body[1], "options should scroll inside the dialog"
    page.locator("#ex-wm").select_option("text")                  # a taller form still fits
    assert box(page, "#export-dialog")["b"] <= h + 1
    page.keyboard.press("Escape")
    assert page.errors == []


def test_slider_latency_stays_within_the_preview_budget(page_at):
    page = page_at(1280, 720)
    times = []
    for i, v in enumerate((0.6, -0.4, 0.9, -0.8, 0.3)):
        page.wait_for_function("!document.getElementById('spinner').classList.contains('on')")
        before = page.evaluate("document.getElementById('preview').src")
        t0 = time.perf_counter()
        set_range(page, "s-exposure", v)
        page.wait_for_function("(b) => document.getElementById('preview').src !== b", arg=before, timeout=5000)
        times.append((time.perf_counter() - t0) * 1000)
    assert statistics.median(times) < 1000, times


# ---- error toasts ---------------------------------------------------------------------------

def toast_text(page):
    return page.locator("#toasts .toast").all_inner_texts()


def test_network_failure_toast_says_what_failed_and_what_to_do(opened):
    page = opened
    page.route("**/api/develop/auto_tone", lambda r: r.abort())
    page.locator("#auto-tone").click()
    page.wait_for_selector("#toasts .toast.error")
    text = " ".join(toast_text(page))
    assert "Auto tone" in text and "photo-room" in text and "reload" in text.lower()
    # and a retry action that works once the server is reachable again
    page.unroute("**/api/develop/auto_tone")
    page.locator("#toasts .toast.error button", has_text="Try again").click()
    page.wait_for_function("document.querySelectorAll('#toasts .toast.error').length === 0")
    page.wait_for_function("document.getElementById('status').textContent.includes('Auto tone applied')")


def test_server_errors_get_plain_language_and_a_next_step(opened):
    page = opened
    page.route("**/api/develop/auto_wb", lambda r: r.fulfill(
        status=409, content_type="application/json",
        body='{"error": "This edit uses the old colour maths; use Upgrade edit first"}'))
    page.locator("#wb-auto").click()
    page.wait_for_selector("#toasts .toast.error")
    text = " ".join(toast_text(page))
    assert "Auto white balance" in text and "Upgrade edit" in text
    page.locator("#toasts .toast .x").first.click()
    assert page.locator("#toasts .toast").count() == 0


def test_generic_status_errors_also_toast_and_do_not_pile_up(opened):
    page = opened
    for _ in range(6):
        page.evaluate("setStatus('Could not save: disk is full', true)")
    page.wait_for_selector("#toasts .toast.error")
    assert page.locator("#toasts .toast").count() == 1                 # identical errors collapse
    assert "disk is full" in " ".join(toast_text(page)).lower()
    page.evaluate("setStatus('Select a photo first', true)")
    assert any("Select a photo first" in t for t in toast_text(page))
    assert page.errors == []
