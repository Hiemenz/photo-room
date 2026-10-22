"""Import dialog in a real browser: pick a folder, tick frames, rename, import."""
import pytest

import editor


@pytest.fixture
def card(editor_url):
    folder = editor.LIBRARY_ROOT.parent / "card" / "DCIM" / "100CANON"
    folder.mkdir(parents=True)
    for i in range(4):
        (folder / f"IMG_{i:04d}.CR2").write_bytes(b"raw-%d-" % i * 300)
    (folder / "IMG_0000.xmp").write_text("<x:xmpmeta/>")
    return folder.parent.parent


def test_pick_frames_rename_and_import(page, card):
    page.locator("#mod-library").click()
    page.wait_for_selector("#lib-grid .lcell")
    page.locator("#lib-import-dialog").click()
    page.wait_for_selector("#import-dialog[open]")      # opens after the sources request returns
    page.fill("#imd-path", str(card))
    page.click("#imd-scan")
    page.wait_for_selector("#imd-grid .imd-frame")
    assert page.locator("#imd-grid .imd-frame").count() == 4
    assert "4 of 4 selected" in page.locator(".imd-count").inner_text()
    page.click("#imd-none")
    assert "0 of 4" in page.locator(".imd-count").inner_text()
    assert page.locator("#imd-go").is_disabled()
    page.click("#imd-all")
    page.locator("#imd-grid .imd-frame input").nth(3).uncheck()
    assert page.locator("#imd-go").inner_text() == "Import 3 photos"
    page.fill("#imd-rename", "shoot_{seq:3}")
    page.fill("#imd-dest", "Wedding")
    page.select_option("#imd-layout", "flat")
    assert "Wedding/shoot_001.CR2" in page.locator("#imd-example").inner_text()
    page.click("#imd-go")
    page.wait_for_function("document.getElementById('imd-msg').textContent.includes('3 imported')", timeout=20000)
    out = editor.LIBRARY_ROOT / "Wedding"
    assert sorted(p.name for p in out.glob("*.CR2")) == ["shoot_001.CR2", "shoot_002.CR2", "shoot_003.CR2"]
    assert (out / "shoot_001.xmp").read_text() == "<x:xmpmeta/>"

    # Reading again: the three imported frames are marked, unticked and can be hidden.
    page.click("#imd-scan")
    page.wait_for_selector("#imd-grid .imd-frame")
    assert page.locator("#imd-grid .imd-frame").count() == 1
    page.uncheck("#imd-hide")
    assert page.locator("#imd-grid .imd-frame.dup").count() == 3
    assert "1 of 4 selected" in page.locator(".imd-count").inner_text()
    assert page.errors == []


def test_bad_source_is_explained(page):
    page.locator("#mod-library").click()
    page.locator("#lib-import-dialog").click()
    page.fill("#imd-path", "relative/path")
    page.click("#imd-scan")
    page.wait_for_function("document.getElementById('imd-msg').classList.contains('err')")
    assert "full path" in page.locator("#imd-msg").inner_text()
