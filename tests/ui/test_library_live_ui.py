"""Live Library grid (change feed), Quick look with smart previews, and in-app import."""

import json

import pytest

import editor


def open_library(page):
    page.locator("#mod-library").click()
    page.wait_for_selector("#lib-grid .lcell")
    page.wait_for_function("liveSource && liveSource.readyState === 1")


def cell(page, name):
    return page.locator(f'#lib-grid .lcell[data-path="{name}"]')


def names(page):
    return page.evaluate("[...document.querySelectorAll('#lib-grid .lcell')].map(c => c.dataset.path)")


def external(page, path, body):
    """A second browser tab (another window, or smart cull elsewhere) edits the library."""
    other = page.context.new_page()
    other.goto(page.url)
    status = other.evaluate("""async ([p, b]) => (await fetch(p, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b)})).status""", [path, body])
    other.close()
    assert status == 200


# ---- live updates -------------------------------------------------------------------------

def test_flags_made_in_another_window_appear_without_a_reload(page):
    open_library(page)
    assert page.locator("#live-badge").is_visible()
    assert "pick" not in (cell(page, "IMG_0001.dng").get_attribute("class") or "")
    page.evaluate("liveStats.reloads = 0")
    external(page, "/api/flag", {"paths": ["IMG_0001.dng"], "flag": "pick"})
    page.wait_for_function("document.querySelector('#lib-grid .lcell[data-path=\"IMG_0001.dng\"]').classList.contains('pick')")
    external(page, "/api/label", {"paths": ["IMG_0002.dng"], "label": "Red"})
    page.wait_for_function("document.querySelector('#lib-grid .lcell[data-path=\"IMG_0002.dng\"] .dot').className.includes('lbl-Red')")
    external(page, "/api/rate", {"path": "IMG_0002.dng", "rating": 4})
    page.wait_for_function("document.querySelector('#lib-grid .lcell[data-path=\"IMG_0002.dng\"] .st').textContent === '★★★★'")
    # patched in place: no full reload, selection and scroll untouched
    assert page.evaluate("liveStats.reloads") == 0 and page.evaluate("liveStats.patched") >= 3
    assert page.errors == []


def test_own_edits_do_not_cause_reloads(page):
    open_library(page)
    page.evaluate("liveStats.reloads = 0; liveStats.events = 0")
    cell(page, "IMG_0001.dng").click()
    page.keyboard.press("p")
    page.wait_for_function("liveStats.events > 0")
    page.wait_for_timeout(700)
    assert page.evaluate("liveStats.reloads") == 0
    assert cell(page, "IMG_0001.dng").get_attribute("class").count("pick") == 1


def test_new_photos_arriving_on_disk_show_up_and_removed_ones_go(page):
    open_library(page)
    root = editor.LIBRARY_ROOT
    (root / "IMG_0003.dng").write_bytes(b"fake-dng-3")
    editor._idx(fresh=False).scan()                              # what a card ingest or import ends with
    page.wait_for_selector('#lib-grid .lcell[data-path="IMG_0003.dng"]')
    assert "fresh" in cell(page, "IMG_0003.dng").get_attribute("class")
    page.wait_for_function("document.getElementById('lib-count').textContent.includes('3 of 3')")
    # the Develop file list learns about it too
    page.wait_for_function("files.includes('IMG_0003.dng')")
    (root / "IMG_0003.dng").unlink()
    editor._idx(fresh=False).scan()
    page.wait_for_function("!document.querySelector('#lib-grid .lcell[data-path=\"IMG_0003.dng\"]')")
    page.wait_for_function("document.getElementById('lib-count').textContent.includes('2 of 2')")


def test_live_updates_respect_the_active_filter(page):
    open_library(page)
    page.select_option("#lf-flag", "pick")
    page.wait_for_function("document.querySelectorAll('#lib-grid .lcell').length === 0")
    external(page, "/api/flag", {"paths": ["IMG_0002.dng"], "flag": "pick"})
    page.wait_for_selector('#lib-grid .lcell[data-path="IMG_0002.dng"]')
    assert names(page) == ["IMG_0002.dng"]
    external(page, "/api/flag", {"paths": ["IMG_0002.dng"], "flag": "none"})
    page.wait_for_function("document.querySelectorAll('#lib-grid .lcell').length === 0")


def test_live_updates_arriving_while_in_develop_are_applied_when_you_return(page):
    open_library(page)
    page.locator("#mod-develop").click()
    external(page, "/api/rate", {"path": "IMG_0001.dng", "rating": 5})
    page.wait_for_function("ratings['IMG_0001.dng'] === 5")
    page.locator("#mod-library").click()
    page.wait_for_selector('#lib-grid .lcell[data-path="IMG_0001.dng"]')
    page.wait_for_function("document.querySelector('#lib-grid .lcell[data-path=\"IMG_0001.dng\"] .st').textContent === '★★★★★'")


def test_a_dropped_connection_shows_in_the_badge_and_recovers(page):
    open_library(page)
    page.evaluate("liveSource.onerror()")
    assert "off" in page.locator("#live-badge").get_attribute("class")
    page.evaluate("liveSource.onopen()")
    assert "off" not in page.locator("#live-badge").get_attribute("class")


def test_smart_cull_apply_updates_the_grid_under_it(page):
    open_library(page)
    page.evaluate("liveStats.events = 0")
    external(page, "/api/flag", {"paths": ["IMG_0001.dng", "IMG_0002.dng"], "flag": "reject"})
    page.wait_for_function("document.querySelectorAll('#lib-grid .lcell.reject').length === 2")


# ---- Quick look and smart previews ------------------------------------------------------------

def test_space_opens_quick_look_and_upgrades_to_a_smart_preview(page):
    open_library(page)
    cell(page, "IMG_0001.dng").click()
    page.keyboard.press(" ")
    assert page.locator("#loupe").is_visible()
    page.wait_for_function("document.getElementById('loupe-info').textContent.includes('smart preview')", timeout=30000)
    page.wait_for_function("document.getElementById('loupe-img').naturalWidth > 0")
    page.keyboard.press("ArrowRight")
    page.wait_for_function("document.getElementById('loupe-info').textContent.includes('IMG_0002')")
    page.keyboard.press("Escape")
    assert page.locator("#loupe").is_hidden()
    page.keyboard.press(" ")
    assert page.locator("#loupe").is_visible()
    page.locator("#loupe").click()
    assert page.locator("#loupe").is_hidden()
    assert page.errors == []


def test_quick_look_ignores_space_while_typing(page):
    open_library(page)
    page.locator("#lf-q").focus()
    page.keyboard.press(" ")
    assert page.locator("#loupe").is_hidden()


def test_smart_previews_are_built_for_imported_photos(page, tmp_path):
    open_library(page)
    page.locator("#import-files").set_input_files(
        [{"name": "NEW_0001.dng", "mimeType": "application/octet-stream", "buffer": b"new-photo-bytes-1"}])
    page.wait_for_function("document.getElementById('import-title').textContent === 'Import finished'")
    import import_api
    import_api._build_q.join()
    r = page.evaluate("fetch('/api/smartpreview?path=NEW_0001.dng').then(r => r.status)")
    assert r == 200


# ---- Import ------------------------------------------------------------------------------------

def upload_files(page, specs):
    page.locator("#import-files").set_input_files(
        [{"name": n, "mimeType": "application/octet-stream", "buffer": b} for n, b in specs])


def done(page):
    page.wait_for_function("document.getElementById('import-title').textContent === 'Import finished'", timeout=30000)


def test_import_button_copies_files_and_shows_them_in_the_grid(page):
    open_library(page)
    upload_files(page, [("NEW_0001.dng", b"alpha-bytes"), ("NEW_0002.dng", b"bravo-bytes")])
    done(page)
    log = page.locator("#import-log").inner_text()
    assert "Imported NEW_0001.dng" in log and "Imported NEW_0002.dng" in log
    assert (editor.LIBRARY_ROOT / "NEW_0001.dng").read_bytes() == b"alpha-bytes"
    page.wait_for_selector('#lib-grid .lcell[data-path="NEW_0002.dng"]')
    assert "2 imported" in page.locator("#import-count").inner_text()
    assert page.locator("#import-bar").evaluate("b => b.value === b.max")
    page.wait_for_function("files.includes('NEW_0001.dng')")
    page.locator("#import-close").click()
    assert page.locator("#import-panel").is_hidden()
    assert page.errors == [] or all("Failed to load resource" in e for e in page.errors)


def test_import_never_overwrites_and_skips_duplicates_by_content(page):
    open_library(page)
    original = (editor.LIBRARY_ROOT / "IMG_0001.dng").read_bytes()
    upload_files(page, [
        ("IMG_0001.dng", b"a different photo with the same name"),     # kept beside the original
        ("TOTALLY_NEW_NAME.dng", original),                            # same bytes as IMG_0001: a duplicate
        ("notes.txt", b"hello"),                                       # not a raw
    ])
    done(page)
    assert (editor.LIBRARY_ROOT / "IMG_0001.dng").read_bytes() == original
    assert (editor.LIBRARY_ROOT / "IMG_0001-1.dng").read_bytes() == b"a different photo with the same name"
    assert not (editor.LIBRARY_ROOT / "TOTALLY_NEW_NAME.dng").exists()
    log = page.locator("#import-log").inner_text()
    assert "IMG_0001-1.dng" in log and "already in the library" in log and "IMG_0001.dng)" in log
    assert "ignored" in log                                            # the .txt never left the browser
    summary = page.locator("#import-count").inner_text()
    assert "1 imported" in summary and "1 already in the library" in summary and "renamed" in summary
    # importing the same set again changes nothing
    upload_files(page, [("IMG_0001.dng", b"a different photo with the same name")])
    done(page)
    page.wait_for_function("document.getElementById('import-count').textContent.includes('already in the library')")
    assert sorted(p.name for p in editor.LIBRARY_ROOT.glob("IMG_0001*.dng")) == ["IMG_0001-1.dng", "IMG_0001.dng"]


def test_import_folder_picker_takes_a_whole_folder(page, tmp_path):
    folder = tmp_path / "card" / "DCIM" / "100CANON"
    folder.mkdir(parents=True)
    (folder / "CARD_0001.dng").write_bytes(b"card-1")
    (folder / "CARD_0002.dng").write_bytes(b"card-2")
    (folder / "THM_0001.jpg").write_bytes(b"thumb")
    (folder / ".hidden.dng").write_bytes(b"x")
    open_library(page)
    page.locator("#import-folder").set_input_files(str(tmp_path / "card"))
    done(page)
    assert (editor.LIBRARY_ROOT / "CARD_0001.dng").exists() and (editor.LIBRARY_ROOT / "CARD_0002.dng").exists()
    assert not list(editor.LIBRARY_ROOT.glob("*.jpg")) and not (editor.LIBRARY_ROOT / ".hidden.dng").exists()


def drop(page, specs, target="#lib-grid"):
    return page.evaluate("""([sel, specs]) => {
        const dt = new DataTransfer();
        for (const [name, text] of specs) dt.items.add(new File([text], name, {type: 'application/octet-stream'}));
        const el = document.querySelector(sel);
        const fire = (t) => el.dispatchEvent(new DragEvent(t, {dataTransfer: dt, bubbles: true, cancelable: true}));
        fire('dragenter'); fire('dragover');
        const shown = !document.getElementById('lib-drop').hidden;
        fire('drop');
        return shown;
    }""", [target, specs])


def test_dropping_files_on_the_library_imports_them(page):
    open_library(page)
    assert page.locator("#lib-drop").is_hidden()
    assert drop(page, [("DROP_0001.dng", "dropped-one"), ("DROP_0002.dng", "dropped-two")]) is True
    done(page)
    assert (editor.LIBRARY_ROOT / "DROP_0001.dng").read_text() == "dropped-one"
    page.wait_for_selector('#lib-grid .lcell[data-path="DROP_0002.dng"]')
    assert page.locator("#lib-drop").is_hidden()


def test_dragging_a_photo_cell_does_not_look_like_an_import(page):
    open_library(page)
    shown = page.evaluate("""() => { const dt = new DataTransfer(); dt.setData('text/plain', 'photo-room:[]');
        document.getElementById('lib-grid').dispatchEvent(new DragEvent('dragenter', {dataTransfer: dt, bubbles: true, cancelable: true}));
        return !document.getElementById('lib-drop').hidden; }""")
    assert shown is False


def test_dropping_non_raw_files_explains_what_is_supported(page):
    open_library(page)
    drop(page, [("holiday.jpg", "jpeg"), ("a.txt", "x")])
    page.wait_for_selector("#toasts .toast.error")
    text = page.locator("#toasts .toast.error").inner_text()
    assert "No raw photos" in text and ".cr2" in text
    assert not list(editor.LIBRARY_ROOT.glob("holiday*"))


def test_import_failure_is_reported_per_file_with_a_reason(page):
    open_library(page)
    page.route("**/api/import/upload?name=BAD_0001.dng", lambda r: r.fulfill(
        status=507, content_type="application/json",
        body=json.dumps({"error": "Not enough free space in the library folder for BAD_0001.dng. Free some space."})))
    upload_files(page, [("BAD_0001.dng", b"x" * 10), ("GOOD_0001.dng", b"good-one")])
    done(page)
    log = page.locator("#import-log").inner_text()
    assert "Failed BAD_0001.dng" in log and "Not enough free space" in log
    assert (editor.LIBRARY_ROOT / "GOOD_0001.dng").exists() is False       # a full disk stops the batch, loudly
    assert "Stopped" in page.locator("#import-count").inner_text()
    page.wait_for_selector("#toasts .toast.error")
    assert "disk is full" in page.locator("#toasts .toast.error").first.inner_text().lower() or \
        "free" in page.locator("#toasts .toast.error").first.inner_text().lower()


def test_import_can_be_cancelled(page):
    open_library(page)
    page.route("**/api/import/upload?*", lambda r: None)                   # hang the first upload
    upload_files(page, [("SLOW_0001.dng", b"s1"), ("SLOW_0002.dng", b"s2")])
    page.wait_for_selector("#import-panel:not([hidden])")
    page.locator("#import-cancel").click()
    done(page)
    assert not (editor.LIBRARY_ROOT / "SLOW_0002.dng").exists()


def test_card_watcher_progress_panel_shows_progress_and_the_outcome(page):
    import time

    import cardwatch
    status = editor.LIBRARY_ROOT / cardwatch.STATUS_NAME
    assert page.locator("#card-panel").is_hidden()
    status.write_text(json.dumps({"id": "c1", "state": "importing", "label": "EOS", "total": 10, "done": 4,
                                  "copied": 3, "duplicates": 1, "failed": 0, "name": "IMG_4.CR2",
                                  "updated": time.time()}))
    page.wait_for_selector("#card-panel:not([hidden])", timeout=6000)
    assert "Importing from card: EOS" in page.locator("#card-title").inner_text()
    assert page.locator("#card-count").inner_text() == "4 of 10"
    status.write_text(json.dumps({"id": "c1", "state": "done", "label": "EOS", "updated": time.time(),
                                  "message": "3 imported. Safe to remove the card."}))
    page.wait_for_function("document.getElementById('card-msg').textContent.includes('Safe to remove')", timeout=6000)
    page.locator("#card-close").click()
    assert page.locator("#card-panel").is_hidden()
    assert page.errors == []
