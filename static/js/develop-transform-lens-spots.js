// ---- Transform, lens corrections and spot removal (M8) -------------------
// Sliders are flat keys in `values`, so history, copy/paste and presets carry
// them; the spot list is `values.spots` (normalised coordinates).
let spotTool = false;      // spot overlay active (preview shows the whole frame)
let spotSel = -1;          // index of the selected spot
const spotOpts = { mode: "heal", r: 0.02, feather: 50, opacity: 100 };
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
  <div id="spot-count" style="font-size:11px;color:var(--muted);margin-top:6px"></div>`;
$("sliders").appendChild(spotPanel);

const spotLayer = document.createElement("div");
spotLayer.id = "spot-layer";
spotLayer.innerHTML = `<svg id="spot-svg" width="100%" height="100%"></svg>`;
$("stage").appendChild(spotLayer);

function spotList() { return values.spots || (values.spots = []); }

function paintSpotUI() {
  const spots = spotList(), s = spots[spotSel];
  const r = s ? s.r : spotOpts.r, f = s ? s.feather : spotOpts.feather, o = s ? s.opacity : spotOpts.opacity;
  $("spot-size").value = r * 100;   $("spot-size-v").textContent = (r * 100).toFixed(1);
  $("spot-feather").value = f;      $("spot-feather-v").textContent = Math.round(f);
  $("spot-opacity").value = o;      $("spot-opacity-v").textContent = Math.round(o);
  const mode = s ? s.mode : spotOpts.mode;
  document.querySelectorAll("[data-spotmode]").forEach(b => b.classList.toggle("active", b.dataset.spotmode === mode));
  $("spot-delete").disabled = !s;
  $("spot-clear").disabled = spots.length === 0;
  $("spot-count").textContent = spots.length ? `${spots.length} spot${spots.length === 1 ? "" : "s"}` : "";
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
  spotList().forEach((s, i) => {
    const px = f.left + s.x * f.width, py = f.top + s.y * f.height, pr = s.r * f.width;
    const qx = f.left + s.sx * f.width, qy = f.top + s.sy * f.height;
    const col = i === spotSel ? "#ffd24a" : "#ffffff";
    add("line", { x1: px, y1: py, x2: qx, y2: qy, stroke: col, "stroke-opacity": .5, "stroke-width": 1 });
    add("circle", { cx: px, cy: py, r: pr, fill: "rgba(255,255,255,.05)", stroke: col, "stroke-width": 2,
                    "data-i": i, "data-part": "dst", style: "pointer-events:all;cursor:move" });
    add("circle", { cx: qx, cy: qy, r: pr, fill: "none", stroke: col, "stroke-dasharray": "4 3", "stroke-width": 1.5,
                    "data-i": i, "data-part": "src", style: "pointer-events:all;cursor:grab" });
  });
}

function setSpotTool(on) {
  spotTool = on && !!current;
  spotLayer.classList.toggle("on", spotTool);
  $("spot-toggle").classList.toggle("active", spotTool);
  if (spotTool && cropping) $("crop-toggle").click();
  $("crop-toggle").disabled = spotTool || !current;
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
    if (hit.i !== undefined) {
      spotSel = parseInt(hit.i, 10);
      drag = { part: hit.part, i: spotSel, from: p, orig: { ...spotList()[spotSel] } };
      paintSpotUI();
      return;
    }
    const spot = { x: p.x, y: p.y, r: spotOpts.r, feather: spotOpts.feather,
                   opacity: spotOpts.opacity, mode: spotOpts.mode, sx: Math.min(1, p.x + spotOpts.r * 3), sy: p.y };
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
