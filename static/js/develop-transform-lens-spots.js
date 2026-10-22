// ---- Transform, lens corrections and spot removal (M8) -------------------
// Sliders are flat keys in `values`, so history, copy/paste and presets carry
// them; the spot list is `values.spots` (normalised coordinates).
let spotTool = false;      // spot overlay active (preview shows the whole frame)
let spotSel = -1;          // index of the selected spot
const spotOpts = { mode: "heal", shape: "circle", r: 0.02, feather: 50, opacity: 100 };
let dustCands = [];        // proposed spots from the dust finder, not yet accepted
let spotVisual = false;    // "Visualize spots" map shown over the photo
const M8_XF = M8_CONTROLS.filter(c => c.key.startsWith("xf_"));
const M8_LENS = M8_CONTROLS.filter(c => !c.key.startsWith("xf_"));

const xfPanel = document.createElement("details");
xfPanel.className = "panel";
xfPanel.id = "xf-panel";
xfPanel.innerHTML = `<summary>Transform</summary>
  <div class="seg" id="upright-bar">
    <button data-upright="auto" title="Pick the best correction from detected lines">Auto</button>
    <button data-upright="level" title="Level the horizon">Level</button>
    <button data-upright="vertical" title="Level and straighten converging verticals">Vertical</button>
    <button data-upright="full" title="Also correct converging horizontals">Full</button>
    <button id="upright-reset" title="Clear every transform value">Off</button>
  </div>
  <div id="xf-sliders"></div>`;
$("sliders").appendChild(xfPanel);
for (const c of M8_XF) buildSlider(c, $("xf-sliders"));

const lensPanel = document.createElement("details");
lensPanel.className = "panel";
lensPanel.id = "lens-panel";
lensPanel.innerHTML = `<summary>Lens corrections</summary>
  <label class="opt"><input type="checkbox" id="s-lens_profile"><span>Use lens profile</span></label>
  <div id="lens-note" style="font-size:11px;color:var(--muted);margin:2px 0 6px"></div>
  <div id="lens-sliders"></div>
  <label class="opt"><input type="checkbox" id="s-ca_remove"><span>Remove chromatic aberration</span></label>`;
$("sliders").appendChild(lensPanel);
for (const c of M8_LENS) buildSlider(c, $("lens-sliders"));
for (const t of M8_TOGGLES) {
  $(`s-${t.key}`).addEventListener("change", (e) => {
    values[t.key] = e.target.checked;
    schedulePreview();
    commit(`${t.label} ${e.target.checked ? "on" : "off"}`);
  });
}

function paintM8() {
  for (const t of M8_TOGGLES) $(`s-${t.key}`).checked = !!values[t.key];
  paintSpotUI();
  if (typeof paintRedeyeUI === "function") paintRedeyeUI();
  if (typeof paintCropOptions === "function") paintCropOptions();
}

function setLensNote(info) {
  const note = $("lens-note");
  if (!info) { note.textContent = ""; return; }
  if (!info.lensfun) note.textContent = "lensfun is not installed: manual sliders only (uv sync --extra lens).";
  else if (info.profile) note.textContent = `Profile: ${info.profile}`;
  else note.textContent = "No lensfun profile matched this camera and lens: use the manual sliders.";
}

document.querySelectorAll("#upright-bar button[data-upright]").forEach(btn => {
  btn.addEventListener("click", async () => {
    if (!current) return;
    setStatus("Detecting lines…");
    const data = await post("/api/upright", {
      path: current, mode: btn.dataset.upright, adjustments: values });
    if (data.error) return setStatus(data.error, true);
    Object.assign(values, data.values);
    paintAll();
    schedulePreview();
    commit(`Upright ${btn.dataset.upright}`);
    setStatus(data.message);
  });
});
$("upright-reset").addEventListener("click", () => {
  if (!current) return;
  for (const c of M8_XF) values[c.key] = c.default;
  paintAll();
  schedulePreview();
  commit("Reset transform");
});

// ---- Spot removal
const spotPanel = document.createElement("details");
spotPanel.className = "panel";
spotPanel.id = "spot-panel";
spotPanel.innerHTML = `<summary>Spot removal</summary>
  <div class="seg">
    <button id="spot-toggle" disabled title="Click the photo to place a spot">Spot tool</button>
    <button data-spotmode="heal" class="active">Heal</button>
    <button data-spotmode="clone">Clone</button>
  </div>
  <div class="seg" title="Brush shape">
    <button data-spotshape="circle" class="active">Circle</button>
    <button data-spotshape="square">Square</button>
  </div>
  <div class="ctrl"><div class="ctrl-head"><label for="spot-size">Size</label><span class="val" id="spot-size-v"></span></div>
    <input type="range" id="spot-size" min="0.2" max="15" step="0.1" value="2"></div>
  <div class="ctrl"><div class="ctrl-head"><label for="spot-feather">Feather</label><span class="val" id="spot-feather-v"></span></div>
    <input type="range" id="spot-feather" min="0" max="100" step="1" value="50"></div>
  <div class="ctrl"><div class="ctrl-head"><label for="spot-opacity">Opacity</label><span class="val" id="spot-opacity-v"></span></div>
    <input type="range" id="spot-opacity" min="0" max="100" step="1" value="100"></div>
  <div style="font-size:11px;color:var(--muted);margin:6px 0">
    Click to place; the source is chosen for you. Drag the dashed circle to
    pick it yourself. The photo shows its whole frame while the tool is on.</div>
  <div class="seg"><button id="spot-delete" disabled>Delete spot</button><button id="spot-clear" disabled>Clear all</button></div>
  <div class="seg" style="margin-top:8px">
    <button id="spot-visualise" disabled title="Show fine detail as dark marks on white, so dust stands out">Visualize spots</button>
  </div>
  <div class="ctrl" id="spot-vis-ctrl" hidden><div class="ctrl-head"><label for="spot-vis-threshold">Visualize threshold</label><span class="val" id="spot-vis-threshold-v"></span></div>
    <input type="range" id="spot-vis-threshold" min="0" max="100" step="1" value="50"></div>
  <div class="seg" style="margin-top:8px">
    <button id="spot-dust" disabled title="Look for dust specks and propose spots for them">Find dust</button>
    <button id="spot-dust-accept" disabled>Accept all</button>
    <button id="spot-dust-clear" disabled>Dismiss</button>
  </div>
  <div class="ctrl"><div class="ctrl-head"><label for="spot-dust-sens">Dust sensitivity</label><span class="val" id="spot-dust-sens-v"></span></div>
    <input type="range" id="spot-dust-sens" min="0" max="100" step="1" value="50"></div>
  <div id="spot-count" style="font-size:11px;color:var(--muted);margin-top:6px"></div>`;
$("sliders").appendChild(spotPanel);

const spotLayer = document.createElement("div");
spotLayer.id = "spot-layer";
spotLayer.innerHTML = `<img id="spot-vis-img" alt="" hidden><svg id="spot-svg" width="100%" height="100%"></svg>`;
$("stage").appendChild(spotLayer);

function spotList() { return values.spots || (values.spots = []); }

let dustFor = null;        // the photo the proposals belong to
function paintSpotUI() {
  if (dustFor !== current) { dustFor = current; dustCands = []; spotVisual = false; $("spot-vis-img").removeAttribute("src"); }
  const spots = spotList(), s = spots[spotSel];
  const r = s ? s.r : spotOpts.r, f = s ? s.feather : spotOpts.feather, o = s ? s.opacity : spotOpts.opacity;
  $("spot-size").value = r * 100;   $("spot-size-v").textContent = (r * 100).toFixed(1);
  $("spot-feather").value = f;      $("spot-feather-v").textContent = Math.round(f);
  $("spot-opacity").value = o;      $("spot-opacity-v").textContent = Math.round(o);
  const mode = s ? s.mode : spotOpts.mode;
  document.querySelectorAll("[data-spotmode]").forEach(b => b.classList.toggle("active", b.dataset.spotmode === mode));
  const shape = s ? (s.shape || "circle") : spotOpts.shape;
  document.querySelectorAll("[data-spotshape]").forEach(b => b.classList.toggle("active", b.dataset.spotshape === shape));
  $("spot-delete").disabled = !s;
  $("spot-clear").disabled = spots.length === 0;
  $("spot-visualise").classList.toggle("active", spotVisual);
  $("spot-vis-ctrl").hidden = !spotVisual;
  $("spot-vis-threshold-v").textContent = $("spot-vis-threshold").value;
  $("spot-dust-sens-v").textContent = $("spot-dust-sens").value;
  $("spot-dust-accept").disabled = $("spot-dust-clear").disabled = dustCands.length === 0;
  $("spot-count").textContent = (spots.length ? `${spots.length} spot${spots.length === 1 ? "" : "s"}` : "")
    + (dustCands.length ? `${spots.length ? ", " : ""}${dustCands.length} proposed` : "");
  paintSpots();
}

function paintSpots() {
  const svg = $("spot-svg");
  svg.innerHTML = "";
  if (!spotTool) return;
  const f = cropRectOnScreen();
  const ns = "http://www.w3.org/2000/svg";
  const add = (tag, attrs) => {
    const el = document.createElementNS(ns, tag);
    for (const k in attrs) el.setAttribute(k, attrs[k]);
    svg.appendChild(el);
    return el;
  };
  // A circle, or a square of half-side r, as the spot's shape says.
  const shape = (s, cx, cy, pr, attrs) => s.shape === "square"
    ? add("rect", { x: cx - pr, y: cy - pr, width: 2 * pr, height: 2 * pr, ...attrs })
    : add("circle", { cx, cy, r: pr, ...attrs });
  spotList().forEach((s, i) => {
    const px = f.left + s.x * f.width, py = f.top + s.y * f.height, pr = s.r * f.width;
    const qx = f.left + s.sx * f.width, qy = f.top + s.sy * f.height;
    const col = i === spotSel ? "#ffd24a" : "#ffffff";
    add("line", { x1: px, y1: py, x2: qx, y2: qy, stroke: col, "stroke-opacity": .5, "stroke-width": 1 });
    shape(s, px, py, pr, { fill: "rgba(255,255,255,.05)", stroke: col, "stroke-width": 2,
                           "data-i": i, "data-part": "dst", style: "pointer-events:all;cursor:move" });
    shape(s, qx, qy, pr, { fill: "none", stroke: col, "stroke-dasharray": "4 3", "stroke-width": 1.5,
                           "data-i": i, "data-part": "src", style: "pointer-events:all;cursor:grab" });
  });
  // Dust finder proposals: click one to accept it.
  dustCands.forEach((c, i) => {
    add("circle", { cx: f.left + c.x * f.width, cy: f.top + c.y * f.height, r: Math.max(6, c.r * f.width),
                    fill: "rgba(80,200,255,.12)", stroke: "#50c8ff", "stroke-width": 2, "stroke-dasharray": "3 2",
                    "data-dust": i, style: "pointer-events:all;cursor:copy" });
  });
  const vis = $("spot-vis-img");
  vis.hidden = !spotVisual || !vis.src;
  Object.assign(vis.style, { left: `${f.left}px`, top: `${f.top}px`, width: `${f.width}px`, height: `${f.height}px` });
}

function setSpotTool(on) {
  spotTool = on && !!current;
  spotLayer.classList.toggle("on", spotTool);
  $("spot-toggle").classList.toggle("active", spotTool);
  if (spotTool && cropping) $("crop-toggle").click();
  if (spotTool && redeyeTool) setRedeyeTool(false);
  $("crop-toggle").disabled = spotTool || redeyeTool || !current;
  if (!spotTool) spotSel = -1;
  paintSpotUI();
  schedulePreview();
  if (typeof layoutMaskLayer === "function") layoutMaskLayer();
}
$("spot-toggle").addEventListener("click", () => setSpotTool(!spotTool));

document.querySelectorAll("[data-spotmode]").forEach(b => b.addEventListener("click", () => {
  const s = spotList()[spotSel];
  if (s) { s.mode = b.dataset.spotmode; schedulePreview(); commit(`Spot ${s.mode}`); }
  else spotOpts.mode = b.dataset.spotmode;
  paintSpotUI();
}));
document.querySelectorAll("[data-spotshape]").forEach(b => b.addEventListener("click", () => {
  const s = spotList()[spotSel];
  if (s) { s.shape = b.dataset.spotshape; schedulePreview(); commit(`Spot ${s.shape}`); }
  else spotOpts.shape = b.dataset.spotshape;
  paintSpotUI();
}));

// ---- Visualize spots: fine detail as dark marks on white
let visToken = 0;
async function loadSpotVisual() {
  if (!spotVisual || !current) return;
  const token = ++visToken;
  const d = await post("/api/spot/visualise", { path: current, threshold: parseInt($("spot-vis-threshold").value, 10) });
  if (token !== visToken || !spotVisual) return;
  if (d.error) { setStatus(d.error, true); return; }
  $("spot-vis-img").src = d.image;
  paintSpots();
}
$("spot-visualise").addEventListener("click", () => {
  spotVisual = !spotVisual;
  if (spotVisual && !spotTool) setSpotTool(true);
  paintSpotUI();
  loadSpotVisual();
});
$("spot-vis-threshold").addEventListener("input", () => { paintSpotUI(); loadSpotVisual(); });
$("spot-dust-sens").addEventListener("input", paintSpotUI);

// ---- Dust finder: propose spots, the photographer accepts them
function acceptDust(indexes) {
  const spots = spotList();
  for (const i of indexes) {
    const c = dustCands[i];
    if (!c || spots.length >= 64) continue;
    spots.push({ x: c.x, y: c.y, r: c.r, sx: c.sx, sy: c.sy, feather: spotOpts.feather,
                 opacity: spotOpts.opacity, mode: spotOpts.mode, shape: "circle" });
  }
  dustCands = dustCands.filter((_, i) => !indexes.includes(i));
  paintSpotUI(); schedulePreview(); commit("Add dust spots");
}
$("spot-dust").addEventListener("click", async () => {
  if (!current) return;
  if (!spotTool) setSpotTool(true);
  setStatus("Looking for dust…");
  const d = await post("/api/spot/dust", { path: current, sensitivity: parseInt($("spot-dust-sens").value, 10) });
  if (d.error) return setStatus(d.error, true);
  dustCands = d.candidates;
  paintSpotUI();
  setStatus(dustCands.length ? `${dustCands.length} dust spot${dustCands.length === 1 ? "" : "s"} proposed: click one, or Accept all`
                             : "No dust found at this sensitivity");
});
$("spot-dust-accept").addEventListener("click", () => acceptDust(dustCands.map((_, i) => i)));
$("spot-dust-clear").addEventListener("click", () => { dustCands = []; paintSpotUI(); });

for (const [id, key, scale] of [["spot-size", "r", 100], ["spot-feather", "feather", 1], ["spot-opacity", "opacity", 1]]) {
  $(id).addEventListener("input", () => {
    const v = parseFloat($(id).value) / scale, s = spotList()[spotSel];
    if (s) { s[key] = v; schedulePreview(); } else spotOpts[key] = v;
    paintSpotUI();
  });
  $(id).addEventListener("change", () => { if (spotList()[spotSel]) commit(`Spot ${key}`); });
}
$("spot-delete").addEventListener("click", () => {
  if (spotSel < 0) return;
  spotList().splice(spotSel, 1);
  spotSel = -1;
  paintSpotUI(); schedulePreview(); commit("Delete spot");
});
$("spot-clear").addEventListener("click", () => {
  values.spots = []; spotSel = -1;
  paintSpotUI(); schedulePreview(); commit("Clear spots");
});

(function spotMouse() {
  let drag = null;
  const frac = (e) => {
    const f = cropRectOnScreen(), st = $("stage").getBoundingClientRect();
    return { x: Math.min(1, Math.max(0, (e.clientX - st.left - f.left) / f.width)),
             y: Math.min(1, Math.max(0, (e.clientY - st.top - f.top) / f.height)) };
  };
  spotLayer.addEventListener("mousedown", async (e) => {
    if (!spotTool || !current) return;
    const p = frac(e), hit = e.target.dataset ? e.target.dataset : {};
    if (hit.dust !== undefined) { acceptDust([parseInt(hit.dust, 10)]); return; }
    if (hit.i !== undefined) {
      spotSel = parseInt(hit.i, 10);
      drag = { part: hit.part, i: spotSel, from: p, orig: { ...spotList()[spotSel] } };
      paintSpotUI();
      return;
    }
    const spot = { x: p.x, y: p.y, r: spotOpts.r, feather: spotOpts.feather,
                   opacity: spotOpts.opacity, mode: spotOpts.mode, shape: spotOpts.shape, sx:Math.min(1, p.x + spotOpts.r * 3), sy: p.y };
    const spots = spotList();
    spots.push(spot);
    spotSel = spots.length - 1;
    paintSpotUI();
    const d = await post("/api/spot/suggest", { path: current, x: p.x, y: p.y, r: spot.r });
    if (!d.error) { spot.sx = d.sx; spot.sy = d.sy; }
    paintSpotUI(); schedulePreview(); commit("Add spot");
  });
  window.addEventListener("mousemove", (e) => {
    if (!drag) return;
    const p = frac(e), s = spotList()[drag.i];
    if (!s) return;
    const dx = p.x - drag.from.x, dy = p.y - drag.from.y;
    if (drag.part === "dst") {   // moving the spot carries its source along
      s.x = Math.min(1, Math.max(0, drag.orig.x + dx)); s.y = Math.min(1, Math.max(0, drag.orig.y + dy));
      s.sx = Math.min(1, Math.max(0, drag.orig.sx + dx)); s.sy = Math.min(1, Math.max(0, drag.orig.sy + dy));
    } else {
      s.sx = Math.min(1, Math.max(0, drag.orig.sx + dx)); s.sy = Math.min(1, Math.max(0, drag.orig.sy + dy));
    }
    paintSpots();
  });
  window.addEventListener("mouseup", () => {
    if (!drag) return;
    drag = null;
    schedulePreview(); commit("Move spot");
  });
  window.addEventListener("keydown", (e) => {
    if (spotTool && spotSel >= 0 && (e.key === "Delete" || e.key === "Backspace") &&
        !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)) {
      e.preventDefault(); $("spot-delete").click();
    }
  });
})();
window.addEventListener("resize", paintSpots);
