"""Browser tests for spot removal, transform / Upright and lens controls (M8)."""

import json

from test_editor_ui import js, preview_body


def open_panels(page):
    for d in page.locator("details.panel").all():
        d.evaluate("d => d.open = true")


def test_transform_and_lens_panels_exist_and_post_values(opened):
    page = opened
    open_panels(page)
    body = preview_body(page, lambda: page.locator("#s-xf_vertical").fill("25"))
    assert body["adjustments"]["xf_vertical"] == 25
    body = preview_body(page, lambda: page.locator("#s-lens_distortion").fill("-10"))
    assert body["adjustments"]["lens_distortion"] == -10
    body = preview_body(page, lambda: page.locator("#s-ca_remove").check())
    assert body["adjustments"]["ca_remove"] is True
    assert "lensfun" in page.locator("#lens-note").inner_text().lower() or "Profile" in page.locator("#lens-note").inner_text()
    assert opened.errors == []


def test_upright_buttons_set_sliders(opened):
    page = opened
    open_panels(page)
    with page.expect_response(lambda r: r.url.endswith("/api/upright")) as info:
        page.locator("#upright-bar [data-upright=level]").click()
    assert info.value.status == 200
    page.wait_for_function("!document.getElementById('spinner').classList.contains('on')")
    page.locator("#s-xf_scale").fill("80")
    page.locator("#upright-reset").click()
    assert js(page, "values.xf_scale") == 100
    assert opened.errors == []


def test_spot_tool_places_moves_and_deletes_a_spot(opened):
    page = opened
    open_panels(page)
    page.locator("#spot-toggle").click()
    box = page.locator("#preview").bounding_box()

    body = preview_body(page, lambda: page.mouse.click(box["x"] + box["width"] * 0.3,
                                                       box["y"] + box["height"] * 0.4))
    page.wait_for_function("values.spots.length === 1")
    assert body["raw_frame"] is True
    spot = js(page, "values.spots[0]")
    assert abs(spot["x"] - 0.3) < 0.03 and abs(spot["y"] - 0.4) < 0.03
    assert 0 <= spot["sx"] <= 1 and spot["mode"] == "heal"
    assert page.locator("#spot-svg circle").count() == 2

    page.locator("[data-spotmode=clone]").click()
    assert js(page, "values.spots[0].mode") == "clone"
    page.locator("#spot-size").fill("5")
    assert abs(js(page, "values.spots[0].r") - 0.05) < 1e-6

    # Dragging the source circle changes only the source.
    src = page.locator("#spot-svg circle[data-part=src]").bounding_box()
    page.mouse.move(src["x"] + src["width"] / 2, src["y"] + src["height"] / 2)
    page.mouse.down()
    page.mouse.move(src["x"] + src["width"] / 2 + 30, src["y"] + src["height"] / 2 + 10, steps=4)
    page.mouse.up()
    moved = js(page, "values.spots[0]")
    assert abs(moved["x"] - spot["x"]) < 1e-6 and moved["sx"] != spot["sx"]

    # The saved request carries the spots (normalised) with the usual adjustments.
    page.wait_for_timeout(300)
    with page.expect_request(lambda r: r.url.endswith("/api/save")) as info:
        page.locator("#save").click()
    saved = json.loads(info.value.post_data)["adjustments"]
    assert len(saved["spots"]) == 1 and saved["spots"][0]["mode"] == "clone"

    page.locator("#spot-delete").click()
    assert js(page, "values.spots.length") == 0
    page.locator("#spot-toggle").click()
    body = preview_body(page, lambda: page.locator("#s-exposure").fill("0.5"))
    assert not body.get("raw_frame")
    assert opened.errors == []
