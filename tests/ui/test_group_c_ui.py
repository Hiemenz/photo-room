"""Browser tests for group C: crop options, spot shapes / visualise / dust, red eye."""

from test_editor_ui import js, preview_body
from test_m8_ui import open_panels


def test_crop_original_custom_and_orientation(opened):
    page = opened
    page.locator("#crop-toggle").click()
    assert page.evaluate("cropping") is True

    # Original keeps the photo's own proportions, so the box stays the whole frame.
    page.locator("#crop-bar [data-cr=orig]").click()
    assert js(page, "values.crop_ratio") == "orig"
    assert page.locator("#crop-bar [data-cr=orig]").evaluate("e => e.classList.contains('active')")

    # A custom 5:4 ratio.
    page.locator("#crop-cw").fill("5")
    page.locator("#crop-ch").fill("4")
    page.locator("#crop-custom-set").click()
    assert js(page, "values.crop_ratio") == "5:4"
    box = page.locator("#crop-box").bounding_box()
    assert abs(box["width"] / box["height"] - 1.25) < 0.05

    # Portrait / landscape flips the ratio and the box.
    page.locator("#crop-orient").click()
    assert js(page, "values.crop_ratio") == "4:5"
    box = page.locator("#crop-box").bounding_box()
    assert abs(box["width"] / box["height"] - 0.8) < 0.05

    # The whole frame is shown while cropping.
    body = preview_body(page, lambda: page.locator("#crop-bar [data-cr='3:2']").click())
    assert body["uncropped"] is True
    assert page.errors == []


def test_rotate_and_mirror_turn_the_picture_and_follow_the_crop(opened):
    page = opened
    page.locator("#crop-toggle").click()
    page.locator("#crop-bar [data-cr='4:3']").click()
    page.wait_for_function("values.crop")
    before = js(page, "({...values.crop})")
    body = preview_body(page, lambda: page.locator("#crop-rot-r").click())
    assert body["adjustments"]["rot90"] == 1
    assert js(page, "values.rot90") == 1
    after = js(page, "values.crop")
    assert abs(after["left"] - (1 - before["bottom"])) < 1e-6 and abs(after["top"] - before["left"]) < 1e-6
    assert js(page, "values.crop_ratio") == "3:4"

    page.locator("#crop-rot-l").click()
    assert js(page, "values.rot90") == 0
    page.locator("#crop-flip-h").click()
    page.locator("#crop-flip-v").click()
    assert js(page, "[values.flip_h, values.flip_v]") == [True, True]
    assert page.locator("#crop-flip-h").evaluate("e => e.classList.contains('active')")
    assert page.errors == []


def test_crop_overlays_cycle_with_o(opened):
    page = opened
    page.locator("#crop-toggle").click()
    page.locator("#crop-overlay").select_option("grid")
    assert page.locator("#crop-guides line").count() == 11 + 7
    page.locator("#crop-overlay").select_option("spiral")
    assert page.locator("#crop-guides path").count() == 1
    page.locator("#crop-overlay").select_option("thirds")
    assert page.locator("#crop-box").evaluate("e => e.classList.contains('ov-thirds')")
    page.locator("#stage").focus()
    page.keyboard.press("o")
    assert page.locator("#crop-overlay").input_value() == "grid"
    assert page.errors == []


def test_spot_shape_visualise_and_dust_finder(opened):
    page = opened
    open_panels(page)
    page.locator("#spot-toggle").click()
    page.locator("[data-spotshape=square]").click()
    box = page.locator("#preview").bounding_box()
    page.mouse.click(box["x"] + box["width"] * 0.3, box["y"] + box["height"] * 0.4)
    page.wait_for_function("values.spots.length === 1")
    assert js(page, "values.spots[0].shape") == "square"
    assert page.locator("#spot-svg rect").count() == 2

    with page.expect_response(lambda r: r.url.endswith("/api/spot/visualise")) as info:
        page.locator("#spot-visualise").click()
    assert info.value.status == 200
    page.wait_for_function("document.getElementById('spot-vis-img').src.startsWith('data:image/png')")
    assert not page.locator("#spot-vis-img").evaluate("e => e.hidden")

    page.route("**/api/spot/dust", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body='{"ok": true, "candidates": [{"x": 0.6, "y": 0.5, "r": 0.01, "sx": 0.7, "sy": 0.5}, '
             '{"x": 0.2, "y": 0.8, "r": 0.01, "sx": 0.3, "sy": 0.8}]}'))
    page.locator("#spot-dust").click()
    page.wait_for_function("dustCands.length === 2")
    assert page.locator("#spot-svg circle[data-dust]").count() == 2
    page.locator("#spot-svg circle[data-dust]").first.dispatch_event("mousedown")
    page.wait_for_function("values.spots.length === 2")
    page.locator("#spot-dust-accept").click()
    page.wait_for_function("values.spots.length === 3 && dustCands.length === 0")
    assert page.errors == []


def test_red_eye_tool_places_and_edits_an_eye(opened):
    page = opened
    open_panels(page)
    page.locator("#redeye-toggle").click()
    box = page.locator("#preview").bounding_box()
    body = preview_body(page, lambda: page.mouse.click(box["x"] + box["width"] * 0.4,
                                                       box["y"] + box["height"] * 0.3))
    assert body["raw_frame"] is True
    page.wait_for_function("values.redeye.length === 1")
    eye = js(page, "values.redeye[0]")
    assert abs(eye["x"] - 0.4) < 0.03 and abs(eye["y"] - 0.3) < 0.03 and eye["kind"] == "red"
    assert page.locator("#redeye-svg circle").count() == 1

    page.locator("[data-eyekind=pet]").click()
    assert js(page, "values.redeye[0].kind") == "pet"
    page.locator("#eye-size").fill("3")
    assert abs(js(page, "values.redeye[0].r") - 0.03) < 1e-6
    page.locator("#eye-delete").click()
    assert js(page, "values.redeye.length") == 0
    page.locator("#redeye-toggle").click()
    assert page.errors == []
