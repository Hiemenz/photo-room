// ---- Zoom: Fit / Fill / 100% / 200%, wheel, pinch, drag ------------------------
// Fit is the ordinary 1400 px preview. Everything else is drawn from the tile
// server (/api/tile, /api/tile/meta): the fully rendered photo cut into 512 px
// tiles at 50 / 100 / 200 %, so 100% really is one photo pixel per screen pixel.
// Tiles come from the settings on screen, so the zoomed view follows the sliders
// (after a short pause, like the sharp full-size preview does).
const ZM = {
  mode: "fit",            // fit | fill | scale
  s: 1, tx: 0, ty: 0,     // css px per photo px, and the pane offset in the stage
  meta: null, sid: null, iw: 0, ih: 0,
  metaGen: 0, groups: [], group: null, tiles: new Map(), bad: new Set(),
  pointers: new Map(), drag: null, pinchD: 0, raf: 0, settleTimer: 0, warned: new Set(),
};
const ZOOM_MAX_DEVICE = 4;          // 400 %
const zoomHooks = [];               // called after the view moves: overlays follow the photo
const dpr = () => window.devicePixelRatio || 1;

function zoomActive() { return ZM.mode !== "fit"; }
function stageBox() { const r = $("stage").getBoundingClientRect(); return { w: r.width, h: r.height, left: r.left, top: r.top }; }

// The photo's rectangle on screen, in #stage coordinates. null when nothing is shown.
function displayedRect() {
  const st = stageBox();
  if (zoomActive()) return { left: ZM.tx, top: ZM.ty, width: ZM.iw * ZM.s, height: ZM.ih * ZM.s };
  const p = $("preview");
  if (p.hidden || !p.naturalWidth) return null;
  const r = p.getBoundingClientRect();
  return { left: r.left - st.left, top: r.top - st.top, width: r.width, height: r.height };
}
// 0..1 position within the photo for a mouse event, or null when outside it.
function photoFraction(e) {
  const r = displayedRect(); if (!r || !r.width) return null;
  const st = stageBox();
  const fx = (e.clientX - st.left - r.left) / r.width, fy = (e.clientY - st.top - r.top) / r.height;
  return fx < 0 || fx > 1 || fy < 0 || fy > 1 ? null : { fx, fy };
}

function fitCssScale() {
  const p = $("preview");
  if (p.naturalWidth && !p.hidden && !zoomActive()) return p.getBoundingClientRect().width / (ZM.iw || p.naturalWidth);
  const st = stageBox();
  return Math.min(1, (st.w - 56) / ZM.iw, (st.h - 56) / ZM.ih);
}

// ---- metadata ---------------------------------------------------------------
async function zoomFetchMeta() {
  const gen = ++ZM.metaGen;
  const path = current;
  let m;
  try {
    const res = await fetch("/api/tile/meta", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, adjustments: values }),
    });
    m = await res.json();
  } catch (e) { toastError("Zoom", e); return null; }
  if (gen !== ZM.metaGen || path !== current) return null;
  if (m.error) { toastError("Zoom", m.error); return null; }
  ZM.meta = m; ZM.sid = m.settings_id;
  ZM.iw = m.output.width; ZM.ih = m.output.height;
  return m;
}

// ---- entering, leaving, moving ------------------------------------------------
function zoomBlocker() {
  if (typeof cropping !== "undefined" && cropping) return "Crop";
  if (typeof spotTool !== "undefined" && spotTool) return "Spot removal";
  if ($("mask-layer").classList.contains("on") || $("crop-layer").classList.contains("on")) return "Mask or crop";
  if (typeof baMode !== "undefined" && baMode !== "off") return "Before / After";
  return null;
}

function setZoomButtons() {
  const pct = ZM.s * dpr() * 100;
  document.querySelectorAll("#zoom-bar button").forEach(b => {
    const z = b.dataset.zoom;
    b.classList.toggle("active", z === "fit" ? ZM.mode === "fit" : z === "fill" ? ZM.mode === "fill"
      : ZM.mode === "scale" && Math.abs(pct - parseFloat(z) * 100) < 1);
  });
  $("zoom-readout").textContent = ZM.mode === "fit" ? "Fit" : ZM.mode === "fill" ? `Fill ${Math.round(pct)}%` : `${Math.round(pct)}%`;
}

function zoomClamp() {
  const st = stageBox(), w = ZM.iw * ZM.s, h = ZM.ih * ZM.s;
  ZM.tx = w <= st.w ? (st.w - w) / 2 : Math.min(0, Math.max(st.w - w, ZM.tx));
  ZM.ty = h <= st.h ? (st.h - h) / 2 : Math.min(0, Math.max(st.h - h, ZM.ty));
}

function zoomApply() {
  zoomClamp();
  $("zoom-pane").style.transform = `translate(${ZM.tx}px, ${ZM.ty}px) scale(${ZM.s})`;
  setZoomButtons();
  if (!ZM.raf) ZM.raf = requestAnimationFrame(() => { ZM.raf = 0; zoomTiles(); });
  zoomHooks.forEach(f => f());
}

function zoomExit() {
  ZM.mode = "fit"; ZM.metaGen++;
  $("zoom-layer").hidden = true;
  $("zoom-pane").innerHTML = ""; ZM.groups = []; ZM.group = null; ZM.tiles = new Map();
  setZoomButtons();
  zoomHooks.forEach(f => f());
}

// target: "fit" | "fill" | a device-pixel zoom (1 = 100 %), around (ax, ay) in stage coordinates
async function zoomTo(target, ax, ay) {
  if (!current || $("preview").hidden) return;
  if (target === "fit") { zoomExit(); return; }
  const why = zoomBlocker();
  if (why) { toast(`Close ${why} first`, { why: "Zoom works on the finished photo; leave this tool, then zoom." }); return; }
  const wasActive = zoomActive();
  const st = stageBox();
  const before = wasActive ? null : displayedRect();       // continue from the fit view
  if (!ZM.meta || !wasActive) {
    if (!(await zoomFetchMeta())) return;
  }
  if (!wasActive) {
    ZM.s = before ? before.width / ZM.iw : fitCssScale();
    ZM.tx = before ? before.left : 0; ZM.ty = before ? before.top : 0;
  }
  const cx = ax ?? st.w / 2, cy = ay ?? st.h / 2;
  let s;
  if (target === "fill") { s = Math.max(st.w / ZM.iw, st.h / ZM.ih); ZM.mode = "fill"; }
  else if (typeof target === "object") { s = Math.min(ZOOM_MAX_DEVICE / dpr(), ZM.s * target.rel); ZM.mode = "scale"; }
  else { s = Math.min(ZOOM_MAX_DEVICE, target) / dpr(); ZM.mode = "scale"; }
  zoomSetScale(s, cx, cy);
  $("zoom-layer").hidden = false;
  zoomBase();
  zoomApply();
}

// The 1400 px preview, stretched to the photo's size, sits under the tiles so
// nothing flashes blank while they load.
function zoomBase() {
  let b = document.getElementById("zoom-base");
  if (!b) {
    b = document.createElement("img"); b.id = "zoom-base"; b.alt = "";
    b.style.cssText = "position:absolute;left:0;top:0;max-width:none";
    $("zoom-pane").prepend(b);
  }
  b.style.width = `${ZM.iw}px`; b.style.height = `${ZM.ih}px`;
  if (b.src !== $("preview").src) b.src = $("preview").src;
}

function zoomSetScale(s, ax, ay) {
  const px = (ax - ZM.tx) / ZM.s, py = (ay - ZM.ty) / ZM.s;
  ZM.s = s; ZM.tx = ax - px * s; ZM.ty = ay - py * s;
}

async function zoomWheel(factor, ax, ay) {
  const max = ZOOM_MAX_DEVICE / dpr();
  if (!zoomActive()) {
    if (factor <= 1) return;
    await zoomTo({ rel: factor }, ax, ay);
    return;
  }
  let s = Math.min(max, ZM.s * factor);
  if (s <= fitCssScale() * 1.001 && factor < 1) { zoomExit(); return; }
  ZM.mode = "scale";
  zoomSetScale(s, ax, ay);
  zoomApply();
}

// ---- tiles ----------------------------------------------------------------
function zoomLevel() {
  const sd = ZM.s * dpr();
  const lv = ZM.meta.levels.filter(l => l.renderable && !ZM.bad.has(l.z));
  if (!lv.length) return null;
  const ge = lv.filter(l => l.scale >= sd * 0.999);
  const pool = ge.length ? ge : [lv.reduce((a, b) => (b.scale > a.scale ? b : a))];
  const min = Math.min(...pool.map(l => l.scale));
  const tied = pool.filter(l => Math.abs(l.scale - min) < 1e-6);
  return tied.find(l => l.z !== "fit") || tied[0];
}

function tileUrl(lvl, col, row) {
  const q = new URLSearchParams({ photo: current, z: lvl.z, x: col, y: row, sid: ZM.sid, format: "jpeg" });
  return "/api/tile?" + q;
}

function zoomTiles() {
  if (!zoomActive() || !ZM.meta) return;
  const lvl = zoomLevel(); if (!lvl) return;
  const gkey = `${ZM.sid}|${lvl.z}|${current}`;
  const pane = $("zoom-pane");
  if (!ZM.group || ZM.group.key !== gkey) {
    const el = document.createElement("div");
    el.style.cssText = "position:absolute;left:0;top:0";
    el.dataset.level = lvl.z;
    pane.appendChild(el);
    ZM.group = { key: gkey, el, tiles: new Map(), wanted: 0, loaded: 0, lvl };
    ZM.groups.push(ZM.group);
    const g = ZM.group;
    g.timer = setTimeout(() => zoomRetire(g), 2500);
  }
  const g = ZM.group;
  const k = lvl.width / ZM.iw;                       // level px per photo px
  const tsz = ZM.meta.tile_size / k;                 // tile edge in photo px
  const st = stageBox();
  const x0 = -ZM.tx / ZM.s, y0 = -ZM.ty / ZM.s, x1 = (st.w - ZM.tx) / ZM.s, y1 = (st.h - ZM.ty) / ZM.s;
  const c0 = Math.max(0, Math.floor(x0 / tsz)), c1 = Math.min(lvl.cols - 1, Math.floor(x1 / tsz));
  const r0 = Math.max(0, Math.floor(y0 / tsz)), r1 = Math.min(lvl.rows - 1, Math.floor(y1 / tsz));
  let wanted = 0, loaded = 0;
  for (let r = r0; r <= r1; r++) for (let c = c0; c <= c1; c++) {
    wanted++;
    const id = `${c},${r}`;
    let img = g.tiles.get(id);
    if (!img) {
      img = document.createElement("img");
      img.className = "pending"; img.alt = ""; img.draggable = false;
      img.dataset.col = c; img.dataset.row = r; img.dataset.z = lvl.z;
      img.style.left = `${c * tsz}px`; img.style.top = `${r * tsz}px`;
      img.addEventListener("load", () => {
        img.classList.remove("pending");
        img.style.width = `${img.naturalWidth / k}px`; img.style.height = `${img.naturalHeight / k}px`;
        zoomCheckReady(g);
      });
      img.addEventListener("error", () => zoomTileFailed(g, lvl, img));
      img.src = tileUrl(lvl, c, r);
      g.el.appendChild(img); g.tiles.set(id, img);
    }
    if (!img.classList.contains("pending")) loaded++;
  }
  g.wanted = wanted; g.loaded = loaded;
  if (g.tiles.size > 48) {                            // keep memory bounded while panning
    for (const [id, img] of g.tiles) {
      const c = +img.dataset.col, r = +img.dataset.row;
      if (c < c0 - 1 || c > c1 + 1 || r < r0 - 1 || r > r1 + 1) { img.remove(); g.tiles.delete(id); }
    }
  }
  zoomCheckReady(g);
}

function zoomCheckReady(g) {
  if (g !== ZM.group) return;
  const vis = [...g.tiles.values()];
  if (vis.length && vis.every(i => !i.classList.contains("pending") || !i.isConnected)) zoomRetire(g);
  else if (vis.some(i => !i.classList.contains("pending"))) {/* partially ready: keep the old group underneath */}
}
function zoomRetire(g) {                 // the new tiles are in: drop everything older
  clearTimeout(g.timer);
  for (const o of ZM.groups.slice()) {
    if (o === g) break;
    o.el.remove(); ZM.groups.splice(ZM.groups.indexOf(o), 1);
  }
}

async function zoomTileFailed(g, lvl, img) {
  if (g !== ZM.group) return;
  let why = "";
  try { const r = await fetch(img.src); if (!r.ok) why = (await r.json()).error || ""; } catch (e) { why = String(e); }
  if (!why) return;
  if (!ZM.warned.has(lvl.z + why)) {
    ZM.warned.add(lvl.z + why);
    toastError("Zoom", /memory|large|too many/i.test(why)
      ? `This photo is too large to draw at ${lvl.z}% with these edits (${why}). Showing a smaller zoom level instead.` : why);
  }
  ZM.bad.add(lvl.z);
  g.el.remove(); ZM.groups.splice(ZM.groups.indexOf(g), 1); ZM.group = null;
  zoomTiles();
}

// ---- the sliders moved, or the photo changed ------------------------------------
function zoomSettingsChanged() {
  if (!zoomActive()) return;
  clearTimeout(ZM.settleTimer);
  ZM.settleTimer = setTimeout(async () => {
    if (!zoomActive() || !(await zoomFetchMeta())) return;
    ZM.bad.clear();
    zoomApply();
  }, 300);
}
function zoomReset() { if (zoomActive()) zoomExit(); ZM.meta = null; ZM.bad.clear(); ZM.warned.clear(); }

$("preview").addEventListener("load", () => {
  syncZoomBar();
  if (zoomActive()) { zoomBase(); zoomSettingsChanged(); }
});
function syncZoomBar() {
  const show = module_ === "develop" && !!current && !$("preview").hidden && !$("grid").classList.contains("on");
  $("zoom-bar").hidden = !show;
  if (!show && zoomActive()) zoomExit();
}
new MutationObserver(syncZoomBar).observe($("grid"), { attributes: true, attributeFilter: ["class"] });
new MutationObserver(() => {
  if (zoomActive() && zoomBlocker()) zoomExit();
}).observe($("stage"), { subtree: true, attributes: true, attributeFilter: ["class"] });

(function wrap() {
  const open = openFile;
  openFile = async function (path) { if (path !== current) zoomReset(); return open.apply(this, arguments); };
  const sched = schedulePreview;
  schedulePreview = function () { sched.apply(this, arguments); zoomSettingsChanged(); };
})();

// ---- input -------------------------------------------------------------------
document.querySelectorAll("#zoom-bar button").forEach(b => b.addEventListener("click", () => {
  const z = b.dataset.zoom;
  zoomTo(z === "fit" || z === "fill" ? z : parseFloat(z));
}));
function zoomToggle100(e) {
  if (zoomActive() && (ZM.mode !== "scale" || Math.abs(ZM.s * dpr() - 1) > 0.01)) return zoomTo(1, e && e.ax, e && e.ay);
  return zoomActive() ? zoomExit() : zoomTo(1, e && e.ax, e && e.ay);
}

$("stage").addEventListener("wheel", (e) => {
  if (module_ !== "develop" || !current || $("preview").hidden || $("grid").classList.contains("on")) return;
  if (zoomBlocker()) return;
  if (!zoomActive() && !photoFraction(e)) return;
  e.preventDefault();
  const st = stageBox();
  const unit = e.deltaMode === 1 ? 16 : 1;
  const k = e.ctrlKey ? 0.012 : 0.0022;                 // pinch on a trackpad arrives as ctrl+wheel
  zoomWheel(Math.exp(-e.deltaY * unit * k), e.clientX - st.left, e.clientY - st.top);
}, { passive: false });

const layer = $("zoom-layer");
layer.addEventListener("pointerdown", (e) => {
  if (layer.classList.contains("picking")) return;
  try { layer.setPointerCapture(e.pointerId); } catch (err) { /* synthetic or already-ended pointer */ }
  ZM.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (ZM.pointers.size === 1) { ZM.drag = { x: e.clientX, y: e.clientY, tx: ZM.tx, ty: ZM.ty }; layer.classList.add("dragging"); }
  else if (ZM.pointers.size === 2) { ZM.drag = null; ZM.pinchD = pinchDist(); }
});
layer.addEventListener("pointermove", (e) => {
  if (!ZM.pointers.has(e.pointerId)) return;
  ZM.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (ZM.pointers.size === 2) {
    const d = pinchDist(), st = stageBox(), pts = [...ZM.pointers.values()];
    if (ZM.pinchD > 0) {
      const f = d / ZM.pinchD;
      ZM.mode = "scale";
      zoomSetScale(Math.min(ZOOM_MAX_DEVICE / dpr(), Math.max(fitCssScale(), ZM.s * f)),
        (pts[0].x + pts[1].x) / 2 - st.left, (pts[0].y + pts[1].y) / 2 - st.top);
      zoomApply();
    }
    ZM.pinchD = d;
  } else if (ZM.drag) {
    ZM.tx = ZM.drag.tx + (e.clientX - ZM.drag.x); ZM.ty = ZM.drag.ty + (e.clientY - ZM.drag.y);
    zoomApply();
  }
});
function pinchDist() { const p = [...ZM.pointers.values()]; return p.length < 2 ? 0 : Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y); }
function pointerEnd(e) {
  ZM.pointers.delete(e.pointerId);
  if (ZM.pointers.size === 0) { ZM.drag = null; layer.classList.remove("dragging"); }
  else if (ZM.pointers.size === 1) { const p = [...ZM.pointers.values()][0]; ZM.drag = { x: p.x, y: p.y, tx: ZM.tx, ty: ZM.ty }; }
}
layer.addEventListener("pointerup", pointerEnd);
layer.addEventListener("pointercancel", pointerEnd);
layer.addEventListener("dblclick", (e) => {
  if (layer.classList.contains("picking")) return;
  const st = stageBox();
  zoomToggle100({ ax: e.clientX - st.left, ay: e.clientY - st.top });
});
$("preview").addEventListener("dblclick", (e) => {
  if (zoomActive() || module_ !== "develop") return;
  const st = stageBox();
  zoomTo(1, e.clientX - st.left, e.clientY - st.top);
});

document.addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return;
  if (typingTarget(e.target) || document.querySelector("dialog[open]")) return;
  if (module_ !== "develop" || !current) return;
  if (e.key === "z" || e.key === "Z") { e.preventDefault(); zoomToggle100(); }
  else if (e.key === "\\" && zoomActive()) zoomExit();      // Before/After needs the whole photo
});
document.getElementById("ba-bar").addEventListener("click", () => { if (zoomActive()) zoomExit(); }, true);
window.addEventListener("resize", () => {
  if (!zoomActive()) { zoomHooks.forEach(f => f()); return; }
  if (ZM.mode === "fill") { const st = stageBox(); zoomSetScale(Math.max(st.w / ZM.iw, st.h / ZM.ih), st.w / 2, st.h / 2); }
  zoomApply();
});
