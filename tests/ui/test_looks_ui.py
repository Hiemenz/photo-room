"""Preset panel, film styles and hover preview in a real browser."""


def open_panel(page):
    page.locator("#looks-panel > summary").click()
    page.wait_for_selector("#preset-grid .look-tile")


def tile(page, preset_id):
    return page.locator(f'#preset-grid .look-tile[data-id="{preset_id}"]')


def test_apply_preset_amount_hover_save_and_undo(opened):
    page = opened
    open_panel(page)
    assert page.locator("#preset-grid .look-tile").count() >= 25
    # live thumbnails come from the server and actually load
    page.wait_for_function("""() => [...document.querySelectorAll('#preset-grid .look-tile img')].slice(0, 3)
        .every(i => i.complete && i.naturalWidth > 0)""")

    before = page.evaluate("$('preview').src")
    page.evaluate("values.contrast = 0; values.vibrance = 0")

    # hover previews without touching the edit, leaving restores the picture
    tile(page, "builtin:b-w-high-contrast").hover()
    page.wait_for_function("(b) => $('preview').src !== b", arg=before)
    assert page.evaluate("values.monochrome") is False
    page.locator("#looks-cat").hover()
    page.wait_for_function("(b) => $('preview').src === b", arg=before)

    # click applies as ONE history step
    steps = page.evaluate("histState.entries.length")
    tile(page, "builtin:vivid").click()
    page.wait_for_function("values.vibrance === 40")
    page.wait_for_function("(n) => histState.entries.length === n + 1", arg=steps)
    assert page.evaluate("histState.entries.at(-1).label") == "Preset: Vivid"
    assert page.evaluate("values.preset_name") == "Vivid"

    # amount slider scales it, still a single further step on release
    page.wait_for_function("!document.getElementById('looks-amount').disabled")
    page.evaluate("""() => { const s = $('looks-amount'); s.value = 50;
        s.dispatchEvent(new Event('input')); s.dispatchEvent(new Event('change')); }""")
    page.wait_for_function("values.vibrance === 20")
    page.wait_for_function("histState.entries.at(-1).label === 'Preset: Vivid (50%)'")

    # undo through History returns to the state before the preset
    page.evaluate("jumpTo(histState.entries[%d].id)" % (steps - 1))
    page.wait_for_function("values.vibrance === 0")

    # save the current edit as a preset, choosing groups
    page.evaluate("values.contrast = 33; values.clarity = 12")
    page.locator("#preset-save").click()
    page.fill("#looks-save-name", "My UI look")
    page.locator('#looks-save-groups input[value="effects"]').uncheck()
    assert not page.locator('#looks-save-groups input[value="wb"]').is_checked()
    assert not page.locator('#looks-save-groups input[value="crop"]').is_checked()
    page.locator("#looks-save-ok").click()
    page.wait_for_selector('#preset-grid .look-tile[data-id="user:my-ui-look"]')
    got = page.evaluate("""fetch('/api/looks/preset/export?ids=user:my-ui-look').then(r => r.json())""")
    values = got["presets"][0]["values"]
    assert values["contrast"] == 33 and "clarity" not in values


def test_film_style_tab_applies_a_lut_with_intensity(opened):
    page = opened
    open_panel(page)
    page.locator('#looks-tabs button[data-tab="luts"]').click()
    assert page.locator("#lut-grid .look-tile").count() >= 6          # None + built-ins
    before = page.evaluate("$('preview').src")
    page.locator('#lut-grid .look-tile[data-id="builtin-teal-and-orange"]').click()
    page.wait_for_function("values.lut === 'builtin-teal-and-orange'")
    page.wait_for_function("(b) => $('preview').src !== b", arg=before)
    assert page.evaluate("histState.entries.at(-1).label") == "Film style: Teal and Orange"
    page.evaluate("""() => { const s = $('lut-amount'); s.value = 40;
        s.dispatchEvent(new Event('input')); s.dispatchEvent(new Event('change')); }""")
    page.wait_for_function("values.lut_amount === 40")
    page.locator("#lut-grid .look-tile.none").click()
    page.wait_for_function("values.lut === ''")


def test_importing_a_cube_file_adds_it_to_the_library(opened, tmp_path):
    page = opened
    open_panel(page)
    page.locator('#looks-tabs button[data-tab="luts"]').click()
    cube = tmp_path / "Swap.cube"
    rows = "\n".join(f"{b} {g} {r}" for b in (0, 1) for g in (0, 1) for r in (0, 1))
    cube.write_text("LUT_3D_SIZE 2\n" + rows + "\n")
    page.set_input_files("#lut-file", str(cube))
    page.wait_for_selector('#lut-grid .look-tile[title="Swap"]')
    bad = tmp_path / "Bad.cube"
    bad.write_text("LUT_3D_SIZE 2\n1 1 1\n")
    page.set_input_files("#lut-file", str(bad))
    page.wait_for_function("document.getElementById('lut-note').classList.contains('err')")
    assert "data rows" in page.locator("#lut-note").inner_text()
