// ---- Local adjustments: graduated, radial and brush masks ----------------
// Masks live in values.masks (see masks.py). Geometry is stored as fractions
// of the displayed (cropped, straightened) picture, like the crop, so it is
// the same at any preview size and at full resolution.
let maskSel = null;          // id of the selected mask
let maskTool = null;         // "linear" | "radial" | "brush" while armed to draw a new one
let maskOverlayOn = false;   // O key: red tint of the selected mask
const brushOpts = { size: 0.04, feather: 50, flow: 100, density: 100, erase: false };
const MASK_NAMES = { linear: "Graduated", radial: "Radial", brush: "Brush", range: "Range" };
const COMBINE_LABELS = { new: "Separate adjustment", add: "Add to previous", subtract: "Subtract from previous", intersect: "Intersect with previous" };
const MAX_MASKS = 16;
const SVGNS = "http://www.w3.org/2000/svg";

const maskPanel = document.createElement("details");
maskPanel.className = "panel";
maskPanel.id = "mask-panel";
maskPanel.innerHTML = `<summary>Masks</summary>
  <div class="seg" id="mask-tools">
    <button data-tool="linear" title="Graduated filter: drag from where the effect is full to where it fades out">Graduated</button>
    <button data-tool="radial" title="Radial filter: drag out from the centre">Radial</button>
    <button data-tool="brush" title="Adjustment brush: paint on the photo">Brush</button>
    <button data-tool="range" title="Range mask: select the whole picture by luminance or colour">Range</button>
  </div>
  <div id="mask-hint">Pick a tool, then drag on the photo.</div>
  <div id="mask-list"></div>
  <div class="curve-row" id="mask-actions" hidden>
    <button id="mask-dup">Duplicate</button>
    <button id="mask-inv">Invert</button>
    <button id="mask-del" class="danger">Delete</button>
  </div>
  <label class="opt" style="margin-top:10px"><input type="checkbox" id="mask-show"><span>Show mask (O)</span></label>
  <div id="mask-edit" hidden>
    <div id="mask-geo"></div>
    <div id="mask-combine"></div>
    <div id="mask-range"></div>
    <div id="mask-sliders"></div>
  </div>`;
$("sliders").appendChild(maskPanel);

const maskList = () => (values.masks = Array.isArray(values.masks) ? values.masks : []);
const selMask = () => maskList().find(m => m.id === maskSel) || null;
const newMaskId = () => Math.random().toString(16).slice(2, 10).padEnd(8, "0");
const round4 = (v) => Math.round(v * 10000) / 10000;
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const maskPanelOpen = () => maskPanel.open;

// Frame of the displayed picture in stage pixels, and pointer to 0..1 coordinates.
function frameRect() { return cropRectOnScreen(); }
function toNorm(e) {
  const r = $("preview").getBoundingClientRect();
  return [(e.clientX - r.left) / r.width, (e.clientY - r.top) / r.height];
}

function buildMaskSlider(c) {
  const wrap = document.createElement("div");
  wrap.className = "ctrl";
  wrap.id = `mctrl-${c.key}`;
  wrap.innerHTML = `
    <div class="ctrl-head"><label for="m-${c.key}">${c.label}</label>
      <span class="val" id="mv-${c.key}" title="Click to reset">0</span></div>
    <input type="range" id="m-${c.key}" min="${c.min}" max="${c.max}" step="${c.step}" value="0">`;
  $("mask-sliders").appendChild(wrap);
  const input = $(`m-${c.key}`);
  input.addEventListener("input", () => {
    const m = selMask(); if (!m) return;
    const v = parseFloat(input.value);
    m.adj = m.adj || {};
    if (v) m.adj[c.key] = v; else delete m.adj[c.key];
    paintMaskValue(c);
    maskChanged();
  });
  $(`mv-${c.key}`).addEventListener("click", () => {
    const m = selMask(); if (!m) return;
    if (m.adj) delete m.adj[c.key];
    input.value = 0; paintMaskValue(c);
    maskChanged(`Reset mask ${c.label}`);
  });
}
function paintMaskValue(c) {
  const m = selMask(), v = (m && m.adj && m.adj[c.key]) || 0;
  $(`mv-${c.key}`).textContent = c.key === "exposure" ? (v > 0 ? "+" : "") + v.toFixed(2) : (v > 0 ? "+" : "") + Math.round(v);
  $(`mctrl-${c.key}`).classList.toggle("changed", v !== 0);
}
MASK_CONTROLS.forEach(buildMaskSlider);

// Slider changes (not commits: the shared "change" handler records history).
function maskChanged(commitLabel) {
  schedulePreview();
  drawMaskHandles();
  refreshMaskOverlay();
  if (commitLabel) commit(commitLabel);
}

// A small labelled range for the selected mask's shape or the brush.
function geoSlider(host, label, min, max, step, get, set, fmt) {
  const wrap = document.createElement("div");
  wrap.className = "ctrl";
  wrap.innerHTML = `<div class="ctrl-head"><label>${label}</label><span class="val"></span></div>
    <input type="range" min="${min}" max="${max}" step="${step}">`;
  const input = wrap.querySelector("input"), val = wrap.querySelector(".val");
  input.value = get();
  const paint = () => { val.textContent = fmt ? fmt(parseFloat(input.value)) : input.value; };
  paint();
  input.addEventListener("input", () => { set(parseFloat(input.value)); paint(); });
  host.appendChild(wrap);
}

function buildGeo() {
  const host = $("mask-geo");
  host.innerHTML = "";
  const m = selMask();
  const showBrush = m ? m.type === "brush" : maskTool === "brush";
  if (m && m.type !== "brush") {
    if (m.type === "radial") {
      geoSlider(host, "Width", 1, 150, 1, () => Math.round(m.rx * 100), v => { m.rx = v / 100; maskChanged(); });
      geoSlider(host, "Height", 1, 150, 1, () => Math.round(m.ry * 100), v => { m.ry = v / 100; maskChanged(); });
    }
    geoSlider(host, "Angle", -180, 180, 1, () => Math.round(m.angle), v => { m.angle = v; maskChanged(); }, v => `${Math.round(v)}°`);
    geoSlider(host, "Feather", 0, 100, 1, () => Math.round(m.feather), v => { m.feather = v; maskChanged(); });
  }
  if (showBrush) {
    const note = document.createElement("div");
    note.style.cssText = "font-size:11px;color:var(--muted);margin-top:12px";
    note.textContent = "Brush (applies to new strokes)";
    host.appendChild(note);
    geoSlider(host, "Size", 1, 50, 1, () => Math.round(brushOpts.size * 100), v => { brushOpts.size = v / 100; });
    geoSlider(host, "Feather", 0, 100, 1, () => brushOpts.feather, v => { brushOpts.feather = v; });
    geoSlider(host, "Flow", 1, 100, 1, () => brushOpts.flow, v => { brushOpts.flow = v; });
    geoSlider(host, "Density", 0, 100, 1, () => brushOpts.density, v => { brushOpts.density = v; });
    const erase = document.createElement("label");
    erase.className = "opt";
    erase.innerHTML = `<input type="checkbox" id="brush-erase"><span>Erase (or hold Alt)</span>`;
    erase.querySelector("input").checked = brushOpts.erase;
    erase.querySelector("input").addEventListener("change", e => { brushOpts.erase = e.target.checked; });
    host.appendChild(erase);
  }
}

// Combine with the previous mask: the first mask in the list always starts its own adjustment.
function buildCombine() {
  const host = $("mask-combine");
  host.innerHTML = "";
  const m = selMask(), list = maskList();
  if (!m || list.indexOf(m) < 1) return;
  const wrap = document.createElement("div");
  wrap.className = "ctrl";
  wrap.innerHTML = `<div class="ctrl-head"><label for="mask-op">Combine</label></div>
    <select id="mask-op">${Object.entries(COMBINE_LABELS).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>`;
  const sel = wrap.querySelector("select");
  sel.value = m.op || "new";
  sel.addEventListener("change", () => {
    if (sel.value === "new") delete m.op; else m.op = sel.value;
    paintMasks(); schedulePreview(); refreshMaskOverlay(); commit("Combine mask");
  });
  host.appendChild(wrap);
  if (m.op && m.op !== "new") {
    const note = document.createElement("div");
    note.style.cssText = "font-size:11px;color:var(--muted);margin-top:4px";
    note.textContent = "Uses the sliders of the mask it joins.";
    host.appendChild(note);
  }
}

// Range selection: luminance window and/or a colour (hue) window read from the picture.
const RANGE_LUM = { lo: 0, hi: 1, smooth: 0.2 };
const RANGE_COLOR = { hue: 0, tol: 30, smooth: 0.3, sat_min: 0 };
function rangeSlider(host, label, min, max, step, get, set, fmt) {    // release is committed by the shared handler
  geoSlider(host, label, min, max, step, get, v => { set(v); maskChanged(); }, fmt);
}
function buildRange() {
  const host = $("mask-range");
  host.innerHTML = "";
  const m = selMask();
  if (!m) return;
  const rng = () => (m.range = m.range || {});
  const tidy = () => { if (!Object.keys(m.range || {}).length) delete m.range; };
  const toggle = (label, key, defaults) => {
    const row = document.createElement("label");
    row.className = "opt";
    row.innerHTML = `<input type="checkbox"><span>${label}</span>`;
    const box = row.querySelector("input");
    box.checked = !!(m.range && m.range[key]);
    box.addEventListener("change", () => {
      if (box.checked) rng()[key] = { ...defaults }; else { delete m.range[key]; tidy(); }
      paintMasks(); schedulePreview(); refreshMaskOverlay(); commit(`Mask ${label.toLowerCase()}`);
    });
    host.appendChild(row);
    return box.checked;
  };
  const note = document.createElement("div");
  note.style.cssText = "font-size:11px;color:var(--muted);margin-top:12px";
  note.textContent = "Range (limits the mask to matching pixels)";
  host.appendChild(note);
  if (toggle("Luminance range", "lum", RANGE_LUM)) {
    const l = () => m.range.lum, pct = v => `${Math.round(v)}%`;
    rangeSlider(host, "Lightest from", 0, 100, 1, () => Math.round(l().lo * 100), v => { l().lo = Math.min(v / 100, l().hi); }, pct);
    rangeSlider(host, "Darkest to", 0, 100, 1, () => Math.round(l().hi * 100), v => { l().hi = Math.max(v / 100, l().lo); }, pct);
    rangeSlider(host, "Smoothness", 0, 100, 1, () => Math.round(l().smooth * 100), v => { l().smooth = v / 100; }, pct);
  }
  if (toggle("Colour range", "color", RANGE_COLOR)) {
    const c = () => m.range.color, pct = v => `${Math.round(v)}%`;
    rangeSlider(host, "Hue", 0, 360, 1, () => Math.round(c().hue), v => { c().hue = v; }, v => `${Math.round(v)}°`);
    rangeSlider(host, "Range", 1, 180, 1, () => Math.round(c().tol), v => { c().tol = v; }, v => `±${Math.round(v)}°`);
    rangeSlider(host, "Smoothness", 0, 100, 1, () => Math.round(c().smooth * 100), v => { c().smooth = v / 100; }, pct);
    rangeSlider(host, "Min saturation", 0, 100, 1, () => Math.round(c().sat_min * 100), v => { c().sat_min = v / 100; }, pct);
  }
}

function paintMasks() {
  const list = maskList();
  if (maskSel && !selMask()) maskSel = null;
  const box = $("mask-list");
  box.innerHTML = "";
  list.forEach(m => {
    const row = document.createElement("div");
    row.className = "mask-row" + (m.id === maskSel ? " sel" : "") + (m.enabled === false ? " off" : "");
    row.innerHTML = `<span class="eye" title="Enable / disable">${m.enabled === false ? "○" : "●"}</span><span class="nm"></span><span class="kind"></span>`;
    row.querySelector(".nm").textContent = m.name || MASK_NAMES[m.type];
    row.querySelector(".kind").textContent = (m.op && m.op !== "new" ? m.op + " " : "") + (m.range ? "range " : "")
      + (m.invert ? "inv " : "") + MASK_NAMES[m.type];
    row.addEventListener("click", (e) => {
      if (e.target.classList.contains("eye")) {
        m.enabled = m.enabled === false;
        maskSel = m.id; paintMasks(); maskChanged(m.enabled ? "Enable mask" : "Disable mask");
        return;
      }
      maskSel = m.id; maskTool = null; paintMasks();
    });
    box.appendChild(row);
  });
  document.querySelectorAll("#mask-tools button").forEach(b => b.classList.toggle("armed", b.dataset.tool === maskTool));
  const m = selMask();
  $("mask-actions").hidden = !m;
  $("mask-edit").hidden = !m && maskTool !== "brush";
  const joined = !!m && !!m.op && m.op !== "new" && maskList().indexOf(m) > 0;
  $("mask-sliders").hidden = !m || joined;
  $("mask-inv").hidden = !!m && m.type === "range";
  $("mask-hint").textContent = maskTool === "linear" ? "Drag from where the effect is full to where it fades out."
    : maskTool === "radial" ? "Drag out from the centre of the ellipse."
    : maskTool === "brush" ? "Paint on the photo to create a brush mask."
    : m && m.type === "range" ? "Tick Luminance or Colour range below to choose which pixels the mask selects."
    : m && m.type === "brush" ? "Drag on the photo to add a stroke (hold Alt to erase)."
    : m ? "Drag the pins on the photo to move or reshape the mask." : "Pick a tool, then drag on the photo.";
  $("mask-show").checked = maskOverlayOn;
  MASK_CONTROLS.forEach(paintMaskValue);
  MASK_CONTROLS.forEach(c => { $(`m-${c.key}`).value = (m && m.adj && m.adj[c.key]) || 0; });
  buildGeo();
  buildCombine();
  buildRange();
  layoutMaskLayer();
}

// ---- Canvas: layer placement, handles, overlay -----------------------------
function layoutMaskLayer() {
  const on = !!current && maskPanelOpen() && !cropping && !spotTool && !redeyeTool && !$("preview").hidden && baMode === "off";
  $("mask-layer").classList.toggle("on", on);
  if (!on) { $("mask-svg").innerHTML = ""; $("mask-overlay").style.display = "none"; return; }
  const f = frameRect();
  for (const el of [$("mask-overlay"), $("mask-catch")]) {
    el.style.left = f.left + "px"; el.style.top = f.top + "px";
    el.style.width = f.width + "px"; el.style.height = f.height + "px";
  }
  const m = selMask();
  $("mask-catch").classList.toggle("on", !!maskTool || (!!m && m.type === "brush"));
  drawMaskHandles();
  refreshMaskOverlay();
}

function svgEl(name, attrs, parent) {
  const el = document.createElementNS(SVGNS, name);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (parent) parent.appendChild(el);
  return el;
}

// Pins are plain SVG circles; each drag is a closure over the mask it edits.
function pin(parent, x, y, cls, onDrag, label) {
  const c = svgEl("circle", { cx: x, cy: y, r: 7, class: `pin hit ${cls || ""}` }, parent);
  if (label) svgEl("title", {}, c).textContent = label;
  c.addEventListener("pointerdown", (e) => {
    e.preventDefault(); e.stopPropagation();
    // Listeners live on the window: dragging redraws the handles, which
    // replaces this very element mid-gesture.
    const move = (ev) => onDrag(...toNorm(ev));
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
      paintMasks();
      commit("Edit mask");
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
  });
  return c;
}

// Linear: from the "full" and "zero" end points (0..1) to centre / angle / feather.
function linearFromPoints(m, x0, y0, x1, y1) {
  const f = frameRect(), W = f.width, H = f.height;
  const dx = (x1 - x0) * W, dy = (y1 - y0) * H, len = Math.hypot(dx, dy);
  if (len < 2) return;
  const nx = dx / len, ny = dy / len;
  m.x = (x0 + x1) / 2; m.y = (y0 + y1) / 2;
  m.angle = Math.round(Math.atan2(-nx, ny) * 180 / Math.PI * 10) / 10;
  const extent = Math.abs(W * nx) + Math.abs(H * ny);
  m.feather = Math.round(clamp(len / extent * 100, 0, 100) * 10) / 10;
}
function linearPoints(m) {
  const f = frameRect(), W = f.width, H = f.height, th = m.angle * Math.PI / 180;
  const nx = -Math.sin(th), ny = Math.cos(th), extent = Math.abs(W * nx) + Math.abs(H * ny);
  const half = Math.max(m.feather / 100 * extent, 1) / 2;
  const cx = m.x * W, cy = m.y * H;
  return { cx, cy, nx, ny, half, W, H, extent };
}

function drawMaskHandles() {
  const svg = $("mask-svg");
  svg.innerHTML = "";
  const m = selMask();
  if (!m || !$("mask-layer").classList.contains("on") || m.type === "brush" || m.type === "range") return;
  const f = frameRect();
  const g = svgEl("g", { transform: `translate(${f.left} ${f.top})` }, svg);
  if (m.type === "linear") {
    const p = linearPoints(m), reach = Math.hypot(p.W, p.H);
    const line = (ox, oy, cls) => svgEl("line", {
      x1: ox - p.ny * reach, y1: oy + p.nx * reach, x2: ox + p.ny * reach, y2: oy - p.nx * reach, class: `guide ${cls}`,
    }, g);
    line(p.cx, p.cy, "");
    line(p.cx - p.nx * p.half, p.cy - p.ny * p.half, "soft");
    line(p.cx + p.nx * p.half, p.cy + p.ny * p.half, "soft");
    const full = [(p.cx - p.nx * p.half) / p.W, (p.cy - p.ny * p.half) / p.H];
    const zero = [(p.cx + p.nx * p.half) / p.W, (p.cy + p.ny * p.half) / p.H];
    pin(g, p.cx, p.cy, "centre", (x, y) => { m.x = x; m.y = y; maskChanged(); drawMaskHandles(); }, "Move");
    pin(g, full[0] * p.W, full[1] * p.H, "", (x, y) => { linearFromPoints(m, x, y, zero[0], zero[1]); maskChanged(); drawMaskHandles(); }, "Full effect");
    pin(g, zero[0] * p.W, zero[1] * p.H, "", (x, y) => { linearFromPoints(m, full[0], full[1], x, y); maskChanged(); drawMaskHandles(); }, "Fades out");
  } else {
    const W = f.width, H = f.height, th = m.angle * Math.PI / 180, c = Math.cos(th), s = Math.sin(th);
    const cx = m.x * W, cy = m.y * H, a = m.rx * W, b = m.ry * H;
    svgEl("ellipse", { cx, cy, rx: a, ry: b, class: "guide", transform: `rotate(${m.angle} ${cx} ${cy})` }, g);
    svgEl("ellipse", { cx, cy, rx: a * (1 - m.feather / 100), ry: b * (1 - m.feather / 100), class: "guide soft",
      transform: `rotate(${m.angle} ${cx} ${cy})` }, g);
    pin(g, cx, cy, "centre", (x, y) => { m.x = x; m.y = y; maskChanged(); drawMaskHandles(); }, "Move");
    pin(g, cx + a * c, cy + a * s, "", (x, y) => {
      const proj = (x * W - cx) * c + (y * H - cy) * s;
      m.rx = clamp(Math.abs(proj) / W, 0.005, 1.5); maskChanged(); drawMaskHandles();
    }, "Width");
    pin(g, cx - b * s, cy + b * c, "", (x, y) => {
      const proj = -(x * W - cx) * s + (y * H - cy) * c;
      m.ry = clamp(Math.abs(proj) / H, 0.005, 1.5); maskChanged(); drawMaskHandles();
    }, "Height");
  }
}

let overlayTimer = null, overlayToken = 0;
function refreshMaskOverlay() {
  clearTimeout(overlayTimer);
  const img = $("mask-overlay"), m = selMask();
  if (!maskOverlayOn || !m || !current || !$("mask-layer").classList.contains("on")) {
    img.style.display = "none"; return;
  }
  overlayTimer = setTimeout(async () => {
    const token = ++overlayToken;
    try {
      const d = await post("/api/mask_overlay", { path: current, adjustments: values, mask_id: m.id });
      if (token !== overlayToken || d.error || !maskOverlayOn) return;
      img.src = d.overlay;
      img.style.display = "block";
    } catch (e) { /* the overlay is a convenience */ }
  }, 120);
}

// ---- Canvas: drawing new masks and brush strokes ----------------------------
(() => {
  const catcher = $("mask-catch");
  let draft = null, stroke = null, startPt = null;

  catcher.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || !current) return;
    e.preventDefault();
    catcher.setPointerCapture(e.pointerId);
    const [x, y] = toNorm(e);
    const existing = selMask();
    if (maskTool === "brush" || (!maskTool && existing && existing.type === "brush")) {
      let m = existing && !maskTool ? existing : null;
      if (!m) { m = addMask({ type: "brush", strokes: [] }, "Brush"); if (!m) return; }
      stroke = { size: brushOpts.size, feather: brushOpts.feather, flow: brushOpts.flow, density: brushOpts.density,
                 erase: brushOpts.erase || e.altKey, pts: [round4(x), round4(y)] };
      m.strokes.push(stroke);
      maskTool = null;
      draft = m;
    } else if (maskTool === "linear" || maskTool === "radial") {
      startPt = [x, y];
      draft = addMask(maskTool === "linear"
        ? { type: "linear", x, y, angle: 0, feather: 50 }
        : { type: "radial", x, y, rx: 0.02, ry: 0.02, angle: 0, feather: 50 }, MASK_NAMES[maskTool]);
      if (!draft) return;
      maskTool = null;
    }
    layoutMaskLayer();
  });

  catcher.addEventListener("pointermove", (e) => {
    const [x, y] = toNorm(e);
    if (stroke) {
      const n = stroke.pts.length;
      if (n < 4000 && Math.hypot(x - stroke.pts[n - 2], y - stroke.pts[n - 1]) > 0.003) {
        stroke.pts.push(round4(x), round4(y));
        maskChanged();
      }
    } else if (draft && startPt) {
      if (draft.type === "linear") linearFromPoints(draft, startPt[0], startPt[1], x, y);
      else {
        draft.rx = clamp(Math.abs(x - startPt[0]), 0.005, 1.5);
        draft.ry = clamp(Math.abs(y - startPt[1]), 0.005, 1.5);
      }
      maskChanged();
    }
  });

  const finish = () => {
    if (!draft) return;
    const label = stroke ? "Brush stroke" : `Add ${MASK_NAMES[draft.type].toLowerCase()} mask`;
    // A plain click on the radial tool (no drag) gets a sensible default size.
    if (draft.type === "radial" && draft.rx <= 0.021 && draft.ry <= 0.021) { draft.rx = 0.2; draft.ry = 0.2; }
    draft = null; stroke = null; startPt = null;
    paintMasks();
    schedulePreview();
    commit(label);
  };
  catcher.addEventListener("pointerup", finish);
  catcher.addEventListener("pointercancel", finish);

  // Brush size cursor.
  catcher.addEventListener("pointermove", (e) => {
    const m = selMask();
    if (!(maskTool === "brush" || (m && m.type === "brush"))) return;
    const f = frameRect(), r = brushOpts.size * Math.max(f.width, f.height);
    const svg = $("mask-svg");
    let c = svg.querySelector(".brush-cursor");
    if (!c) c = svgEl("circle", { class: "guide brush-cursor" }, svg);
    const st = $("stage").getBoundingClientRect();
    c.setAttribute("cx", e.clientX - st.left); c.setAttribute("cy", e.clientY - st.top); c.setAttribute("r", r);
  });
  catcher.addEventListener("pointerleave", () => { const c = $("mask-svg").querySelector(".brush-cursor"); if (c) c.remove(); });
})();

function addMask(proto, label) {
  const list = maskList();
  if (list.length >= MAX_MASKS) { setStatus(`At most ${MAX_MASKS} masks per photo`, true); return null; }
  const n = list.filter(m => m.type === proto.type).length + 1;
  const m = { id: newMaskId(), name: `${label} ${n}`, invert: false, enabled: true, adj: {}, ...proto };
  list.push(m);
  maskSel = m.id;
  return m;
}

// ---- Panel wiring ------------------------------------------------------------
document.querySelectorAll("#mask-tools button").forEach(b => b.addEventListener("click", () => {
  if (!current) return;
  if (b.dataset.tool === "range") {
    // No geometry to draw: the mask is the whole picture, narrowed by its range.
    const m = addMask({ type: "range", range: { lum: { ...RANGE_LUM, hi: 0.4 } } }, "Range");
    if (!m) return;
    maskTool = null;
    paintMasks(); schedulePreview(); commit("Add range mask");
    return;
  }
  maskTool = maskTool === b.dataset.tool ? null : b.dataset.tool;
  if (maskTool) { maskSel = maskTool === "brush" ? null : maskSel; }
  paintMasks();
}));
maskPanel.addEventListener("toggle", () => { if (!maskPanel.open) maskTool = null; layoutMaskLayer(); });
window.addEventListener("resize", layoutMaskLayer);
// Overlays (spots, crop box, mask handles) are positioned from the displayed
// frame, so they only need redrawing when that changes. The quick render that
// follows every slider move and the sharp one after it are shown at the same
// size; redrawing then would rebuild handles under a drag in progress.
let previewLayoutKey = "";
$("preview").addEventListener("load", () => {
  const img = $("preview"), r = img.getBoundingClientRect();
  const key = [r.left, r.top, r.width, r.height].map(v => v.toFixed(1)).join() + "|"
    + (img.naturalWidth / img.naturalHeight).toFixed(2) + "|" + current + "|" + spotTool + "|" + redeyeTool + "|" + cropping;
  if (key === previewLayoutKey) return;
  previewLayoutKey = key;
  paintSpots(); paintEyes(); paintCropBox(); layoutMaskLayer();
});
$("mask-show").addEventListener("change", (e) => { setMaskOverlay(e.target.checked); });
function setMaskOverlay(on) {
  maskOverlayOn = on;
  $("mask-show").checked = on;
  if (!on) $("mask-overlay").style.display = "none";
  refreshMaskOverlay();
}

$("mask-dup").addEventListener("click", () => {
  const m = selMask(); if (!m || maskList().length >= MAX_MASKS) return;
  const copy = JSON.parse(JSON.stringify(m));
  copy.id = newMaskId(); copy.name = `${m.name} copy`;
  maskList().splice(maskList().indexOf(m) + 1, 0, copy);
  maskSel = copy.id;
  paintMasks(); schedulePreview(); commit("Duplicate mask");
});
$("mask-inv").addEventListener("click", () => {
  const m = selMask(); if (!m) return;
  m.invert = !m.invert;
  paintMasks(); schedulePreview(); refreshMaskOverlay(); commit("Invert mask");
});
function deleteSelectedMask() {
  const m = selMask(); if (!m) return;
  values.masks = maskList().filter(x => x !== m);
  maskSel = null;
  paintMasks(); schedulePreview(); commit("Delete mask");
}
$("mask-del").addEventListener("click", deleteSelectedMask);
