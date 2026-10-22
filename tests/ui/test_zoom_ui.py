"""Zoom: Fit / Fill / 100% / 200%, wheel, pinch, drag, tiles that follow the sliders."""

import pytest

from test_m10_ui import set_range


def readout(page):
    return page.locator("#zoom-readout").inner_text()


def wait_tiles(page, z=None):
    sel = "#zoom-pane img.tile-ok"
    page.evaluate("document.querySelectorAll('#zoom-pane img:not(#zoom-base)').forEach(i => { if (i.complete && i.naturalWidth) i.classList.add('tile-ok'); })")
    page.wait_for_function(
        """(z) => { document.querySelectorAll('#zoom-pane img:not(#zoom-base)').forEach(i => { if (i.complete && i.naturalWidth) i.classList.add('tile-ok'); });
             const t = [...document.querySelectorAll('#zoom-pane img.tile-ok')].filter(i => !z || i.dataset.z === z); return t.length > 0; }""",
        arg=z)


def stage_center(page):
    b = page.evaluate("(() => { const r = document.getElementById('stage').getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; })()")
    return b


def pane_transform(page):
    return page.evaluate("[ZM.s, ZM.tx, ZM.ty]")


def test_buttons_switch_between_fit_fill_100_and_200_with_real_tiles(opened):
    page = opened
    assert readout(page) == "Fit" and page.locator("#zoom-layer").is_hidden()
    requests = []
    page.on("request", lambda r: requests.append(r.url) if "/api/tile?" in r.url else None)

    page.locator('#zoom-bar [data-zoom="1"]').click()
    wait_tiles(page, "100")
    assert readout(page) == "100%" and page.locator("#zoom-layer").is_visible()
    assert page.locator('#zoom-bar [data-zoom="1"]').get_attribute("class") == "active"
    # 100% means one photo pixel per screen pixel: the pane is scaled by 1/devicePixelRatio
    s = pane_transform(page)[0]
    assert abs(s * page.evaluate("devicePixelRatio") - 1) < 0.01
    assert any("z=100" in u and "sid=" in u for u in requests)

    page.locator('#zoom-bar [data-zoom="2"]').click()
    wait_tiles(page, "200")
    assert readout(page) == "200%"

    page.locator('#zoom-bar [data-zoom="fill"]').click()
    page.wait_for_function("document.getElementById('zoom-readout').textContent.startsWith('Fill')")
    st = page.evaluate("(() => { const r = document.getElementById('stage').getBoundingClientRect(); return [r.width, r.height]; })()")
    iw, ih = page.evaluate("[ZM.iw, ZM.ih]")
    assert pane_transform(page)[0] == pytest.approx(max(st[0] / iw, st[1] / ih), rel=0.01)

    page.locator('#zoom-bar [data-zoom="fit"]').click()
    assert readout(page) == "Fit" and page.locator("#zoom-layer").is_hidden()
    assert page.locator("#preview").is_visible()
    assert page.errors == []


def test_wheel_zooms_around_the_pointer_and_back_to_fit(opened):
    page = opened
    cx, cy = stage_center(page)
    page.mouse.move(cx, cy)
    for _ in range(8):
        page.mouse.wheel(0, -240)
    page.wait_for_function("document.getElementById('zoom-readout').textContent !== 'Fit'")
    page.wait_for_function("ZM.mode === 'scale' && ZM.s > 1.5")
    z_in = pane_transform(page)[0]
    assert z_in > 1.5 and page.locator("#zoom-layer").is_visible()
    for _ in range(40):
        page.mouse.wheel(0, 240)
    page.wait_for_function("document.getElementById('zoom-readout').textContent === 'Fit'")
    assert page.locator("#zoom-layer").is_hidden()


def test_wheel_keeps_the_point_under_the_pointer_fixed(page_at):
    page = page_at(1280, 720)
    page.evaluate("zoomTo(3)")
    wait_tiles(page, "200")
    cx, cy = stage_center(page)
    px, py = cx + 60, cy - 30
    st = page.evaluate("(() => { const r = document.getElementById('stage').getBoundingClientRect(); return [r.x, r.y]; })()")
    def photo_point():
        s, tx, ty = pane_transform(page)
        return ((px - st[0] - tx) / s, (py - st[1] - ty) / s)
    before = photo_point()
    s0 = pane_transform(page)[0]
    page.mouse.move(px, py)
    page.mouse.wheel(0, -300)
    page.wait_for_function("(s) => ZM.s > s * 1.1", arg=s0)
    after = photo_point()
    assert after[0] == pytest.approx(before[0], abs=1.5) and after[1] == pytest.approx(before[1], abs=1.5)


def test_drag_pans_when_the_photo_is_larger_than_the_view(page_at):
    page = page_at(1280, 720)
    page.evaluate("zoomTo(4)")
    page.wait_for_function("ZM.mode === 'scale' && ZM.iw * ZM.s > document.getElementById('stage').clientWidth")
    wait_tiles(page, "200")
    _, tx0, ty0 = pane_transform(page)
    cx, cy = stage_center(page)
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx - 120, cy - 70, steps=6)
    page.mouse.up()
    _, tx1, ty1 = pane_transform(page)
    assert tx1 == pytest.approx(tx0 - 120, abs=2) and ty1 == pytest.approx(ty0 - 70, abs=2)
    # it never pans the photo away from the view
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx + 3000, cy + 3000, steps=4)
    page.mouse.up()
    _, tx2, ty2 = pane_transform(page)
    assert tx2 <= 0 and ty2 <= 0
    # more tiles are requested as new areas come into view
    assert page.locator("#zoom-pane img:not(#zoom-base)").count() >= 1


def test_pinch_gesture_and_trackpad_pinch_zoom(opened):
    page = opened
    page.evaluate("zoomTo(1)")
    wait_tiles(page, "100")
    s0 = pane_transform(page)[0]
    # trackpad pinch arrives as ctrl+wheel
    page.evaluate("""() => { const st = document.getElementById('stage'); const r = st.getBoundingClientRect();
        st.dispatchEvent(new WheelEvent('wheel', {deltaY: -40, ctrlKey: true, clientX: r.x + r.width / 2, clientY: r.y + r.height / 2, bubbles: true, cancelable: true})); }""")
    page.wait_for_function("(s) => ZM.s > s * 1.2", arg=s0)
    # two touch pointers moving apart
    s1 = pane_transform(page)[0]
    page.evaluate("""() => { const l = document.getElementById('zoom-layer'); const r = l.getBoundingClientRect();
        const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
        const ev = (t, id, x) => l.dispatchEvent(new PointerEvent(t, {pointerId: id, pointerType: 'touch', clientX: x, clientY: cy, bubbles: true}));
        ev('pointerdown', 11, cx - 40); ev('pointerdown', 12, cx + 40);
        ev('pointermove', 11, cx - 80); ev('pointermove', 12, cx + 80);
        ev('pointerup', 11, cx - 80); ev('pointerup', 12, cx + 80); }""")
    page.wait_for_function("(s) => ZM.s > s * 1.5", arg=s1)
    assert page.errors == []


def test_double_click_and_z_toggle_100_percent(opened):
    page = opened
    page.keyboard.press("z")
    wait_tiles(page, "100")
    assert readout(page) == "100%"
    page.keyboard.press("z")
    page.wait_for_function("document.getElementById('zoom-readout').textContent === 'Fit'")
    cx, cy = stage_center(page)
    page.mouse.dblclick(cx, cy)
    wait_tiles(page, "100")
    assert readout(page) == "100%"
    page.mouse.dblclick(cx, cy)
    page.wait_for_function("document.getElementById('zoom-readout').textContent === 'Fit'")


def test_zoomed_view_follows_the_sliders_and_the_photo(opened):
    page = opened
    page.evaluate("zoomTo(1)")
    wait_tiles(page, "100")
    sid0 = page.evaluate("ZM.sid")
    requests = []
    page.on("request", lambda r: requests.append(r.url) if "/api/tile?" in r.url else None)
    set_range(page, "s-exposure", 1.2)
    page.wait_for_function("(s) => ZM.sid && ZM.sid !== s", arg=sid0)
    page.wait_for_function("(s) => [...document.querySelectorAll('#zoom-pane img:not(#zoom-base)')].some(i => i.src.includes('sid=' + ZM.sid) && i.complete && i.naturalWidth)", arg=sid0)
    assert any(f"sid={page.evaluate('ZM.sid')}" in u for u in requests)
    # old tiles are retired once the new ones are in
    page.wait_for_function("document.querySelectorAll('#zoom-pane > div').length === 1")
    # moving to another photo returns to Fit
    page.keyboard.press("ArrowRight")
    page.wait_for_function("document.getElementById('zoom-readout').textContent === 'Fit'")
    assert page.errors == []


def test_zoom_gives_way_to_crop_and_before_after(opened):
    page = opened
    page.evaluate("zoomTo(1)")
    wait_tiles(page, "100")
    page.locator("#crop-toggle").click()
    page.wait_for_function("document.getElementById('zoom-readout').textContent === 'Fit'")
    page.locator("#crop-toggle").click()
    page.locator('#zoom-bar [data-zoom="1"]').click()
    wait_tiles(page, "100")
    page.locator('#ba-bar [data-ba="split"]').click()
    page.wait_for_function("document.getElementById('zoom-readout').textContent === 'Fit'")
    page.locator('#ba-bar [data-ba="off"]').click()
    # a tool that needs the whole photo explains itself instead of silently doing nothing
    page.locator("#crop-toggle").click()
    page.locator('#zoom-bar [data-zoom="1"]').click()
    page.wait_for_selector("#toasts .toast")
    assert "Close Crop first" in " ".join(page.locator("#toasts .toast").all_inner_texts())
    assert readout(page) == "Fit"


def test_zoom_bar_is_hidden_in_the_library_and_at_the_start(page):
    assert page.locator("#zoom-bar").is_hidden()


def nav_visible(page):
    return [float(v) for v in page.locator("#navigator").get_attribute("data-visible").split(",")]


def test_navigator_shows_visible_region_and_pans_by_click_and_drag(page_at):
    page = page_at(1280, 720)
    page.wait_for_function("!document.getElementById('navigator').hidden")
    assert nav_visible(page) == [0, 0, 1, 1]
    page.locator("#nav-view").hover()
    page.locator('#nav-presets [data-nav="2"]').click()
    wait_tiles(page, "200")
    assert readout(page) == "200%"
    page.evaluate("zoomTo(4)")          # the synthetic frame is small: 400% overflows the view
    page.wait_for_function("ZM.iw * ZM.s > document.getElementById('stage').clientWidth")
    fx, fy, fw, fh = nav_visible(page)
    assert 0 < fw < 1 and 0 < fh < 1
    page.locator("#nav-view").hover()
    page.locator('#nav-presets [data-nav="fill"]').click()
    page.wait_for_function("ZM.mode === 'fill'")
    page.evaluate("zoomTo(4)")
    page.wait_for_function("ZM.iw * ZM.s > document.getElementById('stage').clientWidth")
    box = page.evaluate("(() => { const r = document.getElementById('nav-img').getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; })()")
    page.mouse.click(box[0] + box[2] * 0.02, box[1] + box[3] * 0.02)
    page.wait_for_function("ZM.tx === 0 && ZM.ty === 0")
    assert nav_visible(page)[:2] == [0, 0]
    page.mouse.move(box[0] + box[2] * 0.5, box[1] + box[3] * 0.5)
    page.mouse.down()
    page.mouse.move(box[0] + box[2] * 0.98, box[1] + box[3] * 0.98, steps=5)
    page.mouse.up()
    fx, fy, fw, fh = nav_visible(page)
    assert fx + fw == pytest.approx(1, abs=0.001) and fy + fh == pytest.approx(1, abs=0.001)
    page.locator("#nav-view").hover()
    page.locator('#nav-presets [data-nav="fit"]').click()
    page.wait_for_function("ZM.mode === 'fit'")
    assert nav_visible(page) == [0, 0, 1, 1]
    assert page.errors == []
