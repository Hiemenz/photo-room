"""Local adjustment masks driven through the real UI. Marked ``ui``."""

import json


def draw(page, x0, y0, x1, y1, steps=8):
    """Drag across the displayed photo, in fractions of its width and height."""
    b = page.evaluate(
        "(() => { const r = document.getElementById('preview').getBoundingClientRect();"
        " return [r.left, r.top, r.width, r.height]; })()")
    L, T, W, H = b
    page.mouse.move(L + x0 * W, T + y0 * H)
    page.mouse.down()
    for i in range(1, steps + 1):
        page.mouse.move(L + (x0 + (x1 - x0) * i / steps) * W, T + (y0 + (y1 - y0) * i / steps) * H)
    page.mouse.up()


def set_range(page, el_id, value):
    page.evaluate(
        "([id, v]) => { const i = document.getElementById(id); i.value = v;"
        " i.dispatchEvent(new Event('input', {bubbles: true}));"
        " i.dispatchEvent(new Event('change', {bubbles: true})); }", [el_id, value])


def open_panel(page):
    page.locator("#mask-panel > summary").click()
    page.wait_for_function("document.getElementById('mask-layer').classList.contains('on')")


def test_draw_graduated_radial_and_brush_masks(opened):
    page = opened
    open_panel(page)

    page.locator('#mask-tools [data-tool="linear"]').click()
    draw(page, 0.5, 0.1, 0.5, 0.6)
    page.locator('#mask-tools [data-tool="radial"]').click()
    draw(page, 0.3, 0.7, 0.45, 0.85)
    page.locator('#mask-tools [data-tool="brush"]').click()
    draw(page, 0.6, 0.3, 0.9, 0.4, 12)

    kinds = page.evaluate("values.masks.map(m => m.type)")
    assert kinds == ["linear", "radial", "brush"]
    lin, rad, br = page.evaluate("values.masks")
    assert abs(lin["angle"]) < 1 and 0.3 < lin["y"] < 0.4 and lin["feather"] > 30
    assert 0.1 < rad["rx"] < 0.2 and 0.1 < rad["ry"] < 0.2
    assert len(br["strokes"]) == 1 and len(br["strokes"][0]["pts"]) >= 8
    assert page.locator(".mask-row").count() == 3
    assert page.errors == []


def test_local_slider_posts_masks_and_changes_the_preview(opened):
    page = opened
    open_panel(page)
    page.locator('#mask-tools [data-tool="radial"]').click()
    draw(page, 0.5, 0.5, 0.7, 0.7)
    page.wait_for_timeout(250)
    before = page.evaluate("document.getElementById('preview').src")
    with page.expect_request(lambda r: r.url.endswith("/api/preview")) as info:
        set_range(page, "m-exposure", 1.5)
    body = json.loads(info.value.post_data)
    assert body["adjustments"]["masks"][0]["adj"] == {"exposure": 1.5}
    page.wait_for_function(
        "src => document.getElementById('preview').src !== src && "
        "!document.getElementById('spinner').classList.contains('on')", arg=before)


def test_overlay_key_select_duplicate_invert_delete(opened):
    page = opened
    open_panel(page)
    page.locator('#mask-tools [data-tool="radial"]').click()
    draw(page, 0.5, 0.5, 0.7, 0.7)

    page.keyboard.press("o")
    page.wait_for_function("getComputedStyle(document.getElementById('mask-overlay')).display === 'block'")
    assert page.locator("#mask-show").is_checked()
    page.keyboard.press("o")
    assert not page.locator("#mask-show").is_checked()

    page.locator("#mask-dup").click()
    assert page.evaluate("values.masks.length") == 2
    page.locator("#mask-inv").click()
    assert page.evaluate("values.masks.map(m => m.invert)") == [False, True]
    page.keyboard.press("Delete")
    assert page.evaluate("values.masks.length") == 1

    # copy / paste carries masks, and each step is in the history list
    page.locator("#copy").click()
    page.locator("#paste").click()
    assert page.evaluate("values.masks.length") == 1
    labels = page.evaluate("histState.entries.map(e => e.label)")
    assert "Add radial mask" in labels and "Delete mask" in labels
    assert page.errors == []


def test_dragging_a_pin_moves_the_mask(opened):
    page = opened
    open_panel(page)
    page.locator('#mask-tools [data-tool="linear"]').click()
    draw(page, 0.5, 0.2, 0.5, 0.5)
    y0 = page.evaluate("values.masks[0].y")
    box = page.locator("#mask-svg circle.centre").bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx, cy + 40, steps=5)
    page.mouse.up()
    assert page.evaluate("values.masks[0].y") > y0
