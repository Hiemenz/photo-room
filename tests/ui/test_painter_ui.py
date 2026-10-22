"""Browser tests for the Library Painter."""

from test_library_live_ui import cell, open_library


def test_painter_paints_ratings_flags_and_labels_by_click_and_drag(page):
    open_library(page)
    page.locator("#lib-painter").click()
    assert page.locator("#painter-opts").is_visible()

    page.locator("#painter-kind").select_option("rating")
    page.locator("#painter-rating").select_option("4")
    cell(page, "IMG_0001.dng").click()
    page.wait_for_function("libItem('IMG_0001.dng').rating === 4")
    # A paint does not select or open the photo.
    assert page.evaluate("libActive") != "IMG_0001.dng" or page.evaluate("libMulti.size") == 0

    page.locator("#painter-kind").select_option("flag")
    page.locator("#painter-flag").select_option("pick")
    a, b = cell(page, "IMG_0001.dng").bounding_box(), cell(page, "IMG_0002.dng").bounding_box()
    page.mouse.move(a["x"] + 5, a["y"] + 5)
    page.mouse.down()
    page.mouse.move(b["x"] + 5, b["y"] + 5, steps=6)
    page.mouse.up()
    page.wait_for_function("libItem('IMG_0001.dng').flag === 'pick' && libItem('IMG_0002.dng').flag === 'pick'")

    page.locator("#painter-kind").select_option("label")
    page.locator("#painter-label").select_option("Green")
    cell(page, "IMG_0002.dng").click()
    page.wait_for_function("libItem('IMG_0002.dng').label === 'Green'")

    page.keyboard.press("Escape")
    assert not page.locator("#painter-opts").is_visible()
    assert page.errors == []


def test_painter_keywords_need_words(page):
    open_library(page)
    page.locator("#lib-painter").click()
    page.locator("#painter-kind").select_option("keywords")
    cell(page, "IMG_0001.dng").click()
    assert "keywords" in page.locator("#status").inner_text().lower()
    page.locator("#painter-kw").fill("dust test")
    cell(page, "IMG_0002.dng").click()
    page.wait_for_function("libItem('IMG_0002.dng') && libItem('IMG_0002.dng').keywords.includes('dust test')")
    assert page.errors == []
