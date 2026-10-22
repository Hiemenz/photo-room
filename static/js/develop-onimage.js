// ---- On-image tools (group G) -------------------------------------------
// Targeted adjustment (drag on the photo to move the curve point or colour band
// under the cursor), Guided Upright lines, soft proofing, and the reference
// view. Guides, proof set-up and the reference photo are remembered per photo
// in a small sidecar (/api/develop/extras); the sliders themselves still live in
// `values` like everything else.
let tatMode = "";          // "" | curve | hue | sat | lum
let guideDraw = false;     // drawing Guided Upright lines (the preview shows the whole frame)
let guides = [];           // [{x1, y1, x2, y2}] in 0..1 of the lens-corrected frame
let proofSettings = { enabled: false, profile: "srgb", intent: "relative", bpc: true, simulate_paper: false, gamut_warning: false };
let refPath = "";
let refSrc = null;

// ---- per-photo sidecar
let extrasTimer = 0;
function saveExtras(patch) {
  if (!current) return;
  const path = current;
  clearTimeout(extrasTimer);
  extrasTimer = setTimeout(() => {
    post("/api/develop/extras", { path, extras: patch }).catch(() => {});
  }, 300);
}
async function loadExtras() {
  const path = current;
  guides = []; refPath = ""; refSrc = null;
  proofSettings = { ...proofSettings, enabled: false };
  try {
    const res = await fetch("/api/develop/extras?path=" + encodeURIComponent(path));
    const d = await res.json();
    if (path !== current || d.error) return;
    guides = d.upright_guides || [];
    if (d.softproof) proofSettings = { ...proofSettings, ...d.softproof };
    refPath = d.reference || "";
  } catch (e) { /* the extras are a convenience; the photo still opens */ }
  paintGuides(); paintProofUI(); refreshProof();
  if (baMode === "ref") refreshBA();
}

// ---- panel
const onImagePanel = document.createElement("details");
onImagePanel.className = "panel";
onImagePanel.id = "onimage-panel";
onImagePanel.innerHTML = `<summary>On-image tools</summary>
  <div style="font-size:11px;color:var(--muted);margin:2px 0 4px">Targeted adjustment: pick what to change, then drag up or down on the photo.</div>
  <div class="seg" id="tat-bar">
    <button data-tat="curve" title="Drag to move the tone curve at the tone under the cursor">Curve</button>
    <button data-tat="hue" title="Drag to shift the hue of the colour under the cursor">Hue</button>
    <button data-tat="sat" title="Drag to change the saturation of the colour under the cursor">Sat</button>
    <button data-tat="lum" title="Drag to change the luminance of the colour under the cursor">Lum</button>
  </div>
  <div id="tat-note" style="font-size:11px;color:var(--muted);margin:4px 0 8px"></div>`;
$("sliders").appendChild(onImagePanel);

const proofPanel = document.createElement("details");
proofPanel.className = "panel";
proofPanel.id = "proof-panel";
proofPanel.innerHTML = `<summary>Soft proofing</summary>
  <label class="opt"><input type="checkbox" id="proof-on"><span>Proof colours</span></label>
  <div class="ctrl"><div class="ctrl-head"><label for="proof-profile">Profile</label></div>
    <select id="proof-profile" style="width:100%"></select></div>
  <div class="ctrl"><div class="ctrl-head"><label for="proof-intent">Rendering intent</label></div>
    <select id="proof-intent" style="width:100%">
      <option value="perceptual">Perceptual</option><option value="relative">Relative colorimetric</option>
      <option value="saturation">Saturation</option><option value="absolute">Absolute colorimetric</option></select></div>
  <label class="opt"><input type="checkbox" id="proof-paper"><span>Simulate paper &amp; ink</span></label>
  <label class="opt"><input type="checkbox" id="proof-bpc"><span>Black point compensation</span></label>
  <label class="opt"><input type="checkbox" id="proof-gamut"><span>Show out-of-gamut warning</span></label>
  <div id="proof-note" style="font-size:11px;color:var(--muted);margin-top:6px"></div>`;
$("sliders").appendChild(proofPanel);

const TAT_HELP = {
  curve: "Drag up to lighten or down to darken the tone under the cursor (RGB curve).",
  hue: "Drag up or down to shift the hue of the colour under the cursor.",
  sat: "Drag up to saturate or down to desaturate the colour under the cursor.",
  lum: "Drag up to lighten or down to darken the colour under the cursor.",
};
function setTat(mode) {
  tatMode = tatMode === mode ? "" : mode;
  document.querySelectorAll("#tat-bar button").forEach(b => b.classList.toggle("active", b.dataset.tat === tatMode));
  $("tat-note").textContent = tatMode ? TAT_HELP[tatMode] : "";
  $("stage").classList.toggle("picking", !!tatMode);
}
document.querySelectorAll("#tat-bar button").forEach(b => b.addEventListener("click", () => setTat(b.dataset.tat)));

// ---- targeted adjustment
const TAT_PX = 0.5;       // curve levels (0-255) per pixel dragged
const TAT_SLIDER_PX = 0.4; // slider units (-100..100) per pixel dragged
let tatDrag = null;
function tatBlocked() {
  if (guideDraw || wbArmed || (typeof spotTool !== "undefined" && spotTool) || cropping) return true;
  if ($("mask-layer").classList.contains("on") || $("crop-layer").classList.contains("on")) return true;
  return baMode !== "off";
}
$("stage").addEventListener("pointerdown", async (e) => {
  if (!tatMode || !current || e.button !== 0 || tatBlocked()) return;
  if (e.target.closest("#zoom-bar, #ba-bar")) return;
  if (zoomActive()) { toast("Targeted adjustment works at Fit", { why: "Return to Fit, then drag on the photo." }); return; }
  const f = photoFraction(e);
  if (!f) return;
  e.preventDefault(); e.stopPropagation();
  const stage = $("stage"), id = e.pointerId, y0 = e.clientY;
  stage.setPointerCapture(id);
  tatDrag = { pending: true, y0, id };
  const d = await developCall("/api/develop/targeted", { path: current, adjustments: values, x: f.fx, y: f.fy },
    "Targeted adjustment", null);
  if (!d || !tatDrag) { tatDrag = null; return; }
  tatDrag = { y0, id, mode: tatMode, info: d };
  if (tatMode === "curve") {
    const pts = curve().points.rgb;
    let i = pts.findIndex(p => Math.abs(p[0] - d.level) < 9);
    if (i < 0) {
      const lvl = Math.round(Math.min(254, Math.max(1, d.level)));
      pts.push([lvl, Math.round(curveValue(pts, lvl))]);
      pts.sort((a, b) => a[0] - b[0]);
      i = pts.findIndex(p => p[0] === lvl);
    }
    curveChannel = "rgb";
    tatDrag.index = i; tatDrag.start = pts[i][1];
  } else if (d.band) {
    tatDrag.key = `${tatMode}_${d.band.name}`;
    tatDrag.start = values[tatDrag.key] || 0;
    mixerTab = `hsl_${tatMode}`;
    document.querySelectorAll("#mixer-tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === mixerTab));
    syncMixerView();
  } else {
    tatDrag = null;
    $("tat-note").textContent = "That area is neutral grey: pick a colour for Hue, Sat or Lum.";
  }
}, true);
$("stage").addEventListener("pointermove", (e) => {
  const t = tatDrag;
  if (!t || t.pending || e.pointerId !== t.id) return;
  const dy = t.y0 - e.clientY;
  if (t.mode === "curve") {
    const pts = curve().points.rgb, i = t.index, last = pts.length - 1;
    const lo = i > 0 ? pts[i - 1][1] : 0, hi = i < last ? pts[i + 1][1] : 255;
    pts[i] = [pts[i][0], Math.round(Math.min(hi, Math.max(lo, t.start + dy * TAT_PX)))];
    paintCurve();
  } else {
    values[t.key] = Math.round(Math.min(100, Math.max(-100, t.start + dy * TAT_SLIDER_PX)));
    paintAll();
  }
  schedulePreview();
});
const tatEnd = (e) => {
  const t = tatDrag;
  if (!t || e.pointerId !== t.id) return;
  tatDrag = null;
  if (t.pending) return;
  if (t.mode === "curve") commit("Targeted curve adjustment");
  else commit(`Targeted ${t.mode === "hue" ? "hue" : t.mode === "sat" ? "saturation" : "luminance"} (${t.info.band.name})`);
};
$("stage").addEventListener("pointerup", tatEnd);
$("stage").addEventListener("pointercancel", tatEnd);

// ---- Guided Upright
const guideBar = document.createElement("div");
guideBar.id = "guide-bar";
guideBar.innerHTML = `<div class="seg">
    <button id="guide-draw" title="Draw up to four lines along things that should be straight (verticals, horizons)">Guided</button>
    <button data-guide-mode="level" title="Level using the drawn lines">Level</button>
    <button data-guide-mode="vertical" title="Level and straighten verticals using the drawn lines">Vertical</button>
    <button data-guide-mode="full" title="Also correct horizontals using the drawn lines">Full</button>
    <button id="guide-clear" title="Remove the drawn lines">Clear</button>
  </div>
  <div id="guide-note" style="font-size:11px;color:var(--muted);margin:4px 0 6px"></div>`;
$("upright-bar").after(guideBar);
const guideSvg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
guideSvg.id = "guide-svg";
guideSvg.setAttribute("viewBox", "0 0 1 1");
guideSvg.setAttribute("preserveAspectRatio", "none");
$("stage").appendChild(guideSvg);

function placeGuideSvg() {
  const r = (guideDraw || guides.length && guideShown) ? displayedRect() : null;
  guideSvg.style.display = r ? "block" : "none";
  if (r) Object.assign(guideSvg.style, { left: `${r.left}px`, top: `${r.top}px`, width: `${r.width}px`, height: `${r.height}px` });
  guideSvg.style.pointerEvents = guideDraw ? "auto" : "none";
}
let guideShown = false;
zoomHooks.push(placeGuideSvg);
window.addEventListener("resize", placeGuideSvg);

function paintGuides() {
  guideSvg.innerHTML = guides.map((g, i) =>
    `<line x1="${g.x1}" y1="${g.y1}" x2="${g.x2}" y2="${g.y2}" class="guide-line" data-i="${i}"></line>`).join("");
  $("guide-note").textContent = guideDraw
    ? `Drag on the photo to draw a line (${guides.length}/4). Double-click a line to remove it.`
    : guides.length ? `${guides.length} guide line${guides.length > 1 ? "s" : ""} saved` : "";
  $("guide-draw").classList.toggle("active", guideDraw);
  placeGuideSvg();
}
function setGuideDraw(on) {
  if (on && (!current || zoomActive() || tatBlocked())) {
    if (on && current) toast("Close the other tool first", { why: "Guides are drawn on the whole uncropped frame at Fit." });
    return;
  }
  if (on && tatMode) setTat(tatMode);
  guideDraw = on;
  guideShown = on;
  paintGuides();
  renderPreview(false);        // the frame the guides are measured on: no crop, no transform
}
$("guide-draw").addEventListener("click", () => setGuideDraw(!guideDraw));
$("guide-clear").addEventListener("click", () => {
  guides = []; paintGuides(); saveExtras({ upright_guides: [] });
});
document.querySelectorAll("[data-guide-mode]").forEach(btn => btn.addEventListener("click", async () => {
  if (!current) return;
  if (guides.length < 2) { setStatus("Draw at least two guide lines first", true); return; }
  setStatus("Applying guides…");
  // Measured on the lens-corrected frame with the transform removed, like Auto.
  const data = await post("/api/upright", { path: current, mode: btn.dataset.guideMode, adjustments: values, guides });
  if (data.error) return setStatus(data.error, true);
  Object.assign(values, data.values);
  if (guideDraw) setGuideDraw(false);
  paintAll(); schedulePreview();
  commit(`Guided upright ${btn.dataset.guideMode}`);
  setStatus(data.message);
}));
(() => {
  let start = null, line = null;
  const frac = (e) => {
    const r = guideSvg.getBoundingClientRect();
    return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))];
  };
  guideSvg.addEventListener("pointerdown", (e) => {
    if (!guideDraw || e.button !== 0) return;
    if (guides.length >= 4) { setStatus("Four guide lines is the most; double-click one to remove it", true); return; }
    e.preventDefault(); e.stopPropagation();
    guideSvg.setPointerCapture(e.pointerId);
    start = frac(e);
    line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("class", "guide-line");
    for (const [k, v] of [["x1", start[0]], ["y1", start[1]], ["x2", start[0]], ["y2", start[1]]]) line.setAttribute(k, v);
    guideSvg.appendChild(line);
  });
  guideSvg.addEventListener("pointermove", (e) => {
    if (!line) return;
    const [x, y] = frac(e);
    line.setAttribute("x2", x); line.setAttribute("y2", y);
  });
  const end = (e) => {
    if (!line) return;
    const [x, y] = frac(e), r = guideSvg.getBoundingClientRect();
    line.remove(); line = null;
    if (Math.hypot((x - start[0]) * r.width, (y - start[1]) * r.height) > 24) {
      guides.push({ x1: +start[0].toFixed(5), y1: +start[1].toFixed(5), x2: +x.toFixed(5), y2: +y.toFixed(5) });
      saveExtras({ upright_guides: guides });
    }
    start = null; paintGuides();
  };
  guideSvg.addEventListener("pointerup", end);
  guideSvg.addEventListener("pointercancel", end);
  guideSvg.addEventListener("dblclick", (e) => {
    const i = e.target.dataset && e.target.dataset.i;
    if (i === undefined) return;
    guides.splice(+i, 1); paintGuides(); saveExtras({ upright_guides: guides });
  });
})();

// ---- Soft proofing
let proofTimer = 0, proofGen = 0;
const proofImg = document.createElement("img");
proofImg.id = "proof-overlay";
proofImg.alt = "";
proofImg.hidden = true;
$("stage").appendChild(proofImg);

function placeProof() {
  const r = proofSettings.enabled && !zoomActive() && baMode === "off" ? displayedRect() : null;
  if (!r) { proofImg.hidden = true; return; }
  Object.assign(proofImg.style, { left: `${r.left}px`, top: `${r.top}px`, width: `${r.width}px`, height: `${r.height}px` });
  proofImg.hidden = !proofImg.src;
}
zoomHooks.push(placeProof);
window.addEventListener("resize", placeProof);

async function refreshProof() {
  if (!proofSettings.enabled || !current) { proofGen++; proofImg.hidden = true; $("proof-note").textContent = ""; return; }
  const gen = ++proofGen;
  const d = await developCall("/api/develop/softproof", { path: current, adjustments: values, settings: proofSettings },
    "Soft proof", null);
  if (!d || gen !== proofGen || !proofSettings.enabled) return;
  proofImg.src = d.image;
  proofImg.onload = placeProof;
  $("proof-note").textContent = proofSettings.gamut_warning
    ? `${d.out_of_gamut}% of the photo is outside this profile's gamut (shown in red).` : "Showing the photo as the profile would reproduce it.";
  placeProof();
}
function paintProofUI() {
  const s = proofSettings;
  $("proof-on").checked = s.enabled;
  $("proof-profile").value = s.profile;
  $("proof-intent").value = s.intent;
  $("proof-paper").checked = s.simulate_paper;
  $("proof-bpc").checked = s.bpc;
  $("proof-gamut").checked = s.gamut_warning;
  $("proof-intent").disabled = s.simulate_paper;
  $("proof-bpc").disabled = s.simulate_paper;
}
function proofChanged() {
  const s = proofSettings;
  s.enabled = $("proof-on").checked;
  s.profile = $("proof-profile").value || "srgb";
  s.intent = $("proof-intent").value;
  s.simulate_paper = $("proof-paper").checked;
  s.bpc = $("proof-bpc").checked;
  s.gamut_warning = $("proof-gamut").checked;
  paintProofUI();
  saveExtras({ softproof: s });
  refreshProof();
}
for (const id of ["proof-on", "proof-profile", "proof-intent", "proof-paper", "proof-bpc", "proof-gamut"]) {
  $(id).addEventListener("change", proofChanged);
}
fetch("/api/develop/softproof/profiles").then(r => r.json()).then(d => {
  $("proof-profile").innerHTML = (d.profiles || []).map(p => `<option value="${p.id}">${p.name.replace(/</g, "&lt;")}</option>`).join("");
  paintProofUI();
}).catch(() => {});
$("preview").addEventListener("load", () => {
  if (!proofSettings.enabled) return;
  clearTimeout(proofTimer);
  proofTimer = setTimeout(refreshProof, 400);
});

// ---- Reference view (side by side with another photo)
async function loadReference() {
  refSrc = null;
  if (!refPath) return;
  const path = refPath;
  const d = await developCall("/api/develop/reference", { path }, "Reference photo", null);
  if (d && path === refPath) refSrc = d.preview;
}
function fillRefPicker() {
  const sel = $("ref-pick");
  sel.innerHTML = `<option value="">Reference photo…</option>` + files.filter(p => p !== current)
    .map(p => `<option value="${p.replace(/"/g, "&quot;")}">${p.split("/").pop().replace(/</g, "&lt;")}</option>`).join("");
  sel.value = refPath;
}
$("ref-pick").addEventListener("change", async (e) => {
  refPath = e.target.value;
  saveExtras({ reference: refPath });
  await loadReference();
  e.target.hidden = !!refSrc;
  if (refSrc) { baMode = "ref"; refreshBA(); }
});

// ---- opening a photo
(function wrap() {
  const open = openFile;
  openFile = async function (path) {
    if (path !== current) {
      if (guideDraw) { guideDraw = false; guideShown = false; }
      proofImg.hidden = true;
    }
    const r = await open.apply(this, arguments);
    if (current === path) {
      $("guide-draw").disabled = false;
      loadExtras();
    }
    return r;
  };
})();
