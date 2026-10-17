"""An edit made before linear-light editing is flagged, left alone, and upgraded on request."""


def open_first(page):
    page.locator("#file-list .file").first.click()
    page.wait_for_function(
        "document.getElementById('preview').complete && "
        "document.getElementById('preview').naturalWidth > 0 && !document.getElementById('save').disabled"
    )


def test_old_edits_offer_an_upgrade_and_change_nothing_until_it_is_clicked(browser, editor_url):
    editor_url.tool.tags.update({"ColorTemperature": "4125", "Exposure2012": "0.8", "Contrast2012": "10"})
    ctx = browser.new_context(viewport={"width": 1500, "height": 1000})
    page = ctx.new_page()
    try:
        page.goto(editor_url)
        open_first(page)
        assert page.evaluate("values.edit_version") == 1
        assert page.evaluate("values.exposure") == 0.8
        assert page.locator("#upgrade-banner").is_visible()

        page.locator("#upgrade-edit").click()
        page.wait_for_function("values.edit_version === 2")
        assert page.evaluate("values.exposure") > 1.0            # 0.8 old stops is about 1.5+ in light
        assert not page.locator("#upgrade-banner").is_visible()
        assert "Upgraded" in page.locator("#status").inner_text()
    finally:
        ctx.close()


def test_a_current_edit_shows_no_banner(opened):
    assert opened.evaluate("values.edit_version") == 2
    assert not opened.locator("#upgrade-banner").is_visible()
