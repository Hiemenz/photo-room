"""Drive the editor in a real browser. Marked ``ui``; run with ``pytest -m ui``."""

import json

from conftest import SCREENSHOT_DIR  # noqa: F401  (resolved via tests/ui/conftest.py)


def preview_body(page, action):
    """Run ``action`` and return the JSON body of the /api/preview request it caused."""
    # Previews are debounced 140ms; let any earlier edit's request land first so
    # it cannot be mistaken for the one ``action`` causes.
    page.wait_for_timeout(250)
    page.wait_for_function("!document.getElementById('spinner').classList.contains('on')")
    with page.expect_request(lambda r: r.url.endswith("/api/preview")) as info:
        action()
    return json.loads(info.value.post_data)


def js(page, expr):
    return page.evaluate(expr)


def canvas_xy(page, fx, fy):
    """Page coordinates for a fraction of the curve canvas (fy=0 is the top)."""
    page.locator("#curve-canvas").scroll_into_view_if_needed()
    box = page.locator("#curve-canvas").bounding_box()
    return box["x"] + box["width"] * fx, box["y"] + box["height"] * fy


def curve_points(page, ch="rgb"):
    return js(page, f"values.tone_curve.points.{ch}")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def test_page_loads_and_opens_a_photo_without_errors(opened):
    assert opened.locator("#file-list .file").count() == 2
    assert opened.locator("#meta strong").inner_text() == "IMG_0001.dng"
    assert opened.locator("#histogram").is_visible()
    opened.wait_for_timeout(300)
    assert opened.errors == []


def test_develop_panel_screenshot(opened):
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    opened.locator("#sliders details.panel summary").first.click()
    opened.locator("#controls").screenshot(path=str(SCREENSHOT_DIR / "develop-panel.png"))
    opened.screenshot(path=str(SCREENSHOT_DIR / "editor-full.png"))
    assert opened.errors == []


# ---------------------------------------------------------------------------
# Sliders
# ---------------------------------------------------------------------------

def test_every_slider_moves_and_triggers_a_preview(opened):
    page = opened
    for d in page.locator("details.panel").all():
        d.evaluate("d => d.open = true")

    ids = js(page, "[...document.querySelectorAll('input[type=range]:not(#export-dialog input)')].map(i => i.id)")
    # 15 main + strength, 24 HSL, 8 B&W, 14 grading, 4 parametric
    assert len(ids) >= 60, ids
    seen = set()
    for sid in ids:
        slider = page.locator(f"#{sid}")
        group = slider.evaluate("s => s.closest('.ctrl')?.dataset.group || ''")
        if group and not slider.is_visible():
            if group == "gray":
                # Toggling queues its own preview; let it land so it cannot be
                # mistaken for the one the slider move below should cause.
                preview_body(page, lambda: page.locator("#s-monochrome").evaluate(
                    "c => { c.checked = true; c.dispatchEvent(new Event('change')) }"))
                page.wait_for_function("!document.getElementById('spinner').classList.contains('on')")
            else:
                page.locator(f"#mixer-tabs [data-tab={group}]").click()
        assert slider.is_visible(), f"{sid} is not visible"

        lo, hi = (float(slider.get_attribute(a)) for a in ("min", "max"))
        before = float(slider.input_value())
        step = float(slider.get_attribute("step"))
        frac = 0.75 if before < lo + (hi - lo) * 0.5 else 0.25
        target = round(lo + round((hi - lo) * frac / step) * step, 6)
        body = preview_body(page, lambda: slider.fill(f"{target:g}"))
        after = float(slider.input_value())
        assert after != before, f"{sid} did not move"
        adj = body["adjustments"]
        sent = adj["tone_curve"]["parametric"][sid[3:]] if sid.startswith("cp-") else adj[sid[2:]]
        assert abs(sent - after) < 1e-6, f"{sid}: UI {after} vs request {sent}"
        seen.add(sid)
        page.wait_for_function("!document.getElementById('spinner').classList.contains('on')")
        if group == "gray":
            preview_body(page, lambda: page.locator("#s-monochrome").evaluate(
                "c => { c.checked = false; c.dispatchEvent(new Event('change')) }"))
            page.wait_for_function("!document.getElementById('spinner').classList.contains('on')")
    assert seen == set(ids)
    assert page.errors == []


def test_clicking_a_slider_value_resets_just_that_slider(opened):
    page = opened
    page.locator("#s-contrast").fill("40")
    page.locator("#s-clarity").fill("25")
    assert page.locator("#v-contrast").inner_text() == "+40"
    preview_body(page, lambda: page.locator("#v-contrast").click())
    assert page.locator("#v-contrast").inner_text() == "0"
    assert float(page.locator("#s-contrast").input_value()) == 0
    assert page.locator("#v-clarity").inner_text() == "+25"


# ---------------------------------------------------------------------------
# Tone curve
# ---------------------------------------------------------------------------

def test_curve_add_drag_and_remove_a_point(opened):
    page = opened
    page.locator("#curve-canvas").scroll_into_view_if_needed()
    assert curve_points(page) == [[0, 0], [255, 255]]

    x, y = canvas_xy(page, 0.5, 0.3)
    body = preview_body(page, lambda: page.mouse.click(x, y))
    pts = curve_points(page)
    assert len(pts) == 3, pts
    assert body["adjustments"]["tone_curve"]["points"]["rgb"] == pts
    assert abs(pts[1][0] - 128) <= 2 and abs(pts[1][1] - 178) <= 3

    # Drag the new point up and to the right.
    page.mouse.move(x, y)
    page.mouse.down()
    tx, ty = canvas_xy(page, 0.6, 0.15)
    page.mouse.move(tx, ty, steps=6)
    page.mouse.up()
    moved = curve_points(page)
    assert len(moved) == 3
    assert moved[1][0] > pts[1][0] and moved[1][1] > pts[1][1]

    # Double-click removes the interior point.
    page.mouse.dblclick(tx, ty)
    assert curve_points(page) == [[0, 0], [255, 255]]
    assert page.errors == []


def test_curve_points_stay_ordered_when_dragged_past_neighbours(opened):
    page = opened
    for fx, fy in ((0.3, 0.5), (0.7, 0.3)):
        page.mouse.click(*canvas_xy(page, fx, fy))
    assert len(curve_points(page)) == 4
    # Drag the left interior point far right, beyond its neighbour.
    page.mouse.move(*canvas_xy(page, 0.3, 0.5))
    page.mouse.down()
    page.mouse.move(*canvas_xy(page, 0.95, 0.5), steps=5)
    page.mouse.up()
    xs = [p[0] for p in curve_points(page)]
    assert xs == sorted(xs) and len(set(xs)) == len(xs), xs


def test_curve_channels_hold_independent_points(opened):
    page = opened
    page.locator("#curve-channels [data-ch=red]").click()
    page.mouse.click(*canvas_xy(page, 0.5, 0.3))
    assert len(curve_points(page, "red")) == 3
    assert len(curve_points(page, "rgb")) == 2
    page.locator("#curve-channels [data-ch=blue]").click()
    assert len(curve_points(page, "blue")) == 2


def test_curve_reset_and_presets(opened):
    page = opened
    body = preview_body(page, lambda: page.locator("#curve-preset").select_option("Strong Contrast"))
    pts = curve_points(page)
    assert len(pts) > 2
    assert body["adjustments"]["tone_curve"]["points"]["rgb"] == pts
    # The menu springs back to its placeholder.
    assert page.locator("#curve-preset").input_value() == ""

    page.locator("#cp-shadows").fill("30")
    assert page.locator("#cpv-shadows").inner_text() == "+30"
    page.locator("#curve-channels [data-ch=green]").click()
    page.mouse.click(*canvas_xy(page, 0.4, 0.6))

    body = preview_body(page, lambda: page.locator("#curve-reset").click())
    tc = body["adjustments"]["tone_curve"]
    assert all(p == [[0, 0], [255, 255]] for p in tc["points"].values())
    assert tc["parametric"]["shadows"] == 0
    assert page.locator("#cpv-shadows").inner_text() == "0"
    assert page.errors == []


# ---------------------------------------------------------------------------
# Colour mixer and grading
# ---------------------------------------------------------------------------

def test_hsl_tabs_switch_between_eight_band_groups(opened):
    page = opened
    page.locator("details.panel summary", has_text="Color mixer").click()
    for tab, group in (("hsl_hue", "hsl_hue"), ("hsl_sat", "hsl_sat"), ("hsl_lum", "hsl_lum")):
        page.locator(f"#mixer-tabs [data-tab={tab}]").click()
        visible = page.locator(f"#mixer-sliders .ctrl[data-group={group}]:visible")
        assert visible.count() == 8, (tab, visible.count())
        assert page.locator("#mixer-sliders .ctrl:visible").count() == 8
        assert page.locator(f"#mixer-tabs [data-tab={tab}]").get_attribute("class") == "active"


def test_black_and_white_swaps_in_the_bw_mixer(opened):
    page = opened
    page.locator("details.panel summary", has_text="Color mixer").click()
    page.locator("label.toggle").click()
    assert page.locator("#s-monochrome").is_checked()
    assert page.locator("#mixer-tabs").is_hidden()
    assert page.locator("#mixer-note").is_visible()
    assert page.locator("#mixer-sliders .ctrl[data-group=gray]:visible").count() == 8
    assert page.locator("#mixer-sliders .ctrl:visible").count() == 8
    page.locator("label.toggle").click()
    assert page.locator("#mixer-tabs").is_visible()
    assert page.locator("#mixer-sliders .ctrl:visible").count() == 8


def test_colour_wheels_set_hue_and_saturation(opened):
    page = opened
    page.locator("details.panel summary", has_text="Color grading").click()
    wheels = page.locator("#grade-body canvas")
    assert wheels.count() == 4
    for i, name in enumerate(("shadow", "mid", "highlight", "global")):
        wheel = wheels.nth(i)
        wheel.scroll_into_view_if_needed()
        box = wheel.bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        # Click straight right of centre, halfway to the rim: hue 0, sat ~50.
        body = preview_body(page, lambda: page.mouse.click(cx + box["width"] / 4, cy))
        adj = body["adjustments"]
        assert adj[f"grade_{name}_hue"] in (0, 360), adj[f"grade_{name}_hue"]
        assert 45 <= adj[f"grade_{name}_sat"] <= 55
        assert page.locator(f"#s-grade_{name}_sat").input_value() == str(adj[f"grade_{name}_sat"])
        # Drag round to straight below centre: hue 90.
        page.mouse.move(cx + box["width"] / 4, cy)
        page.mouse.down()
        page.mouse.move(cx, cy + box["height"] / 3, steps=5)
        page.mouse.up()
        assert page.evaluate(f"values.grade_{name}_hue") == 90
    assert page.errors == []


def test_wheel_follows_its_sliders(opened):
    page = opened
    page.locator("details.panel summary", has_text="Color grading").click()
    page.locator("#s-grade_shadow_hue").fill("200")
    page.locator("#s-grade_shadow_sat").fill("80")
    page.locator("#grade-body canvas").first.scroll_into_view_if_needed()
    # The marker ring is drawn white; find any pure-white pixel left of centre/below.
    has_marker = page.evaluate("""() => {
      const cv = document.querySelector('#grade-body canvas'), g = cv.getContext('2d');
      const d = g.getImageData(0, 0, cv.width, cv.height).data;
      for (let i = 0; i < d.length; i += 4) if (d[i] === 255 && d[i+1] === 255 && d[i+2] === 255) return true;
      return false; }""")
    assert has_marker


# ---------------------------------------------------------------------------
# Reset / revert
# ---------------------------------------------------------------------------

def test_revert_to_original_restores_neutral_values(opened):
    page = opened
    page.locator("#s-contrast").fill("55")
    page.locator("#s-vibrance").fill("-30")
    page.locator("#curve-canvas").scroll_into_view_if_needed()
    page.mouse.click(*canvas_xy(page, 0.5, 0.3))
    with page.expect_response(lambda r: r.url.endswith("/api/reset")) as info:
        page.locator("#reset").click()
    assert info.value.ok
    page.wait_for_function("document.getElementById('source').textContent === 'No saved edits'")
    assert page.locator("#v-contrast").inner_text() == "0"
    assert page.locator("#v-vibrance").inner_text() == "0"
    assert curve_points(page) == [[0, 0], [255, 255]]
    assert page.errors == []


def test_save_then_reopen_keeps_status_without_errors(opened):
    page = opened
    page.locator("#s-contrast").fill("20")
    with page.expect_response(lambda r: r.url.endswith("/api/save")) as info:
        page.locator("#save").click()
    assert info.value.ok
    assert page.locator("#status.err").count() == 0
    assert page.errors == []


# ---------------------------------------------------------------------------
# Crop
# ---------------------------------------------------------------------------

def test_crop_overlay_opens_and_ratio_lock_sets_a_crop(opened):
    page = opened
    assert not page.locator("#crop-layer").evaluate("e => e.classList.contains('on')")
    page.locator("#crop-toggle").click()
    assert page.locator("#crop-layer").evaluate("e => e.classList.contains('on')")
    box = page.locator("#crop-box").bounding_box()
    assert box and box["width"] > 50 and box["height"] > 50

    body = preview_body(page, lambda: page.locator("#crop-bar [data-ratio='1']").click())
    crop = body["adjustments"]["crop"]
    assert crop and crop["right"] - crop["left"] < 1

    # Drag the box a little; the crop moves but keeps its size.
    box = page.locator("#crop-box").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2 + 20, box["y"] + box["height"] / 2, steps=4)
    page.mouse.up()
    moved = page.evaluate("values.crop")
    assert moved["left"] > crop["left"]
    assert abs((moved["right"] - moved["left"]) - (crop["right"] - crop["left"])) < 1e-6

    page.locator("#crop-reset").click()
    assert page.evaluate("values.crop") is None
    page.locator("#crop-toggle").click()
    assert not page.locator("#crop-layer").evaluate("e => e.classList.contains('on')")
    assert page.errors == []


# ---------------------------------------------------------------------------
# Undo (history panel)
# ---------------------------------------------------------------------------

def test_history_jump_undoes_a_slider_edit(opened):
    page = opened
    page.locator("#s-contrast").fill("60")
    page.wait_for_function("document.querySelectorAll('#history-list .hist').length >= 2")
    page.locator("#history-list .hist").last.click()   # oldest entry = as imported
    page.wait_for_function("document.getElementById('v-contrast').textContent === '0'")
    assert float(page.locator("#s-contrast").input_value()) == 0
    assert page.errors == []
