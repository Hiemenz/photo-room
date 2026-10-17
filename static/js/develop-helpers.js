// ---- Develop helpers: Auto tone, Auto WB, WB presets, eyedropper, clipping --------
// The maths lives on the server (analysis.py, develop_api.py). Each button asks
// for the new values, applies them to the sliders and records one History step,
// so every helper is a single undo.

async function developCall(url, body, what, retry) {
  try {
    const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const d = await res.json();
    if (d.error) { setStatus("", false); toastError(what, d.error, retry); return null; }
    return d;
  } catch (e) { toastError(what, e, retry); return null; }
}

function applyValues(partial, label) {
  Object.assign(values, partial);
  paintAll();
  schedulePreview();
  commit(label);
}

// ---- Auto tone ------------------------------------------------------------------
const autoRow = document.createElement("div");
autoRow.className = "autorow";
autoRow.innerHTML = `<button id="auto-tone" type="button" disabled title="Set Exposure, Contrast, Highlights, Shadows, Whites, Blacks and Vibrance automatically (one undo step)">Auto tone</button>`;
accHosts.tone.prepend(autoRow);

async function autoTone() {
  if (!current) return;
  $("auto-tone").disabled = true;
  setStatus("Auto tone…");
  const d = await developCall("/api/develop/auto_tone", { path: current, adjustments: values }, "Auto tone", autoTone);
  $("auto-tone").disabled = !current;
  if (!d) return;
  applyValues(d.settings, d.label || "Auto Tone");
  setStatus("Auto tone applied");
}
$("auto-tone").addEventListener("click", autoTone);

// ---- White balance: presets, Auto, eyedropper -------------------------------------
const wbTools = document.createElement("div");
wbTools.className = "wbtools";
wbTools.innerHTML = `
  <select id="wb-preset" aria-label="White balance preset" disabled><option value="">WB preset…</option></select>
  <button id="wb-auto" type="button" disabled title="Neutralise the light automatically (skin-safe)">Auto</button>
  <button id="wb-pick" type="button" disabled aria-pressed="false" title="Eyedropper: click something that should be neutral grey (Esc cancels)">Pick</button>
  <div id="wb-readout" aria-live="polite"></div>`;
accHosts.wb.prepend(wbTools);

let wbPresets = [];
async function loadWbPresets() {
  const path = current;
  if (!path) return;
  const d = await developCall("/api/develop/wb_presets", { path, adjustments: values }, "White balance presets");
  if (!d || path !== current) return;
  wbPresets = d.presets;
  const sel = $("wb-preset");
  sel.innerHTML = `<option value="">WB preset…</option>` +
    wbPresets.map(p => `<option value="${p.id}">${p.label} (${p.color_temp}K, ${p.tint >= 0 ? "+" : ""}${p.tint})</option>`).join("");
  sel.disabled = false;
}
$("wb-preset").addEventListener("change", (e) => {
  const p = wbPresets.find(x => x.id === e.target.value);
  e.target.value = "";
  if (!p) return;
  applyValues({ color_temp: p.color_temp, tint: p.tint }, `White Balance: ${p.label}`);
  wbReadout(`${p.label}: ${p.color_temp}K, tint ${p.tint >= 0 ? "+" : ""}${p.tint}`);
});

function wbReadout(html) { $("wb-readout").innerHTML = html; }

$("wb-auto").addEventListener("click", async function autoWb() {
  if (!current) return;
  setStatus("Auto white balance…");
  const d = await developCall("/api/develop/auto_wb", { path: current, adjustments: values }, "Auto white balance", autoWb);
  if (!d) return;
  applyValues({ color_temp: d.color_temp, tint: d.tint }, d.label || "Auto White Balance");
  wbReadout(`Auto: ${d.color_temp}K, tint ${d.tint >= 0 ? "+" : ""}${d.tint}`);
  setStatus("Auto white balance applied");
});

// The eyedropper reads the preview locally for the live RGB under the pointer
// and asks the server (full pipeline) for the Temp/Tint when you click.
let wbArmed = false, sampleCanvas = null, sampleSrc = null;
function swatch(rgb) { return `<i style="background:rgb(${rgb.join(",")})"></i>`; }
function setWbArmed(on) {
  wbArmed = on && !!current;
  $("wb-pick").classList.toggle("armed", wbArmed);
  $("wb-pick").setAttribute("aria-pressed", String(wbArmed));
  $("stage").classList.toggle("picking", wbArmed);
  $("zoom-layer").classList.toggle("picking", wbArmed);
  $("wb-hud").hidden = true;
  if (wbArmed) wbReadout("Click a neutral grey or white area (Esc to cancel)");
}
$("wb-pick").addEventListener("click", () => setWbArmed(!wbArmed));

function localRgb(e) {
  const f = photoFraction(e); const img = $("preview");
  if (!f || !img.naturalWidth) return null;
  if (sampleSrc !== img.src) {
    sampleCanvas = document.createElement("canvas");
    sampleCanvas.width = img.naturalWidth; sampleCanvas.height = img.naturalHeight;
    sampleCanvas.getContext("2d", { willReadFrequently: true }).drawImage(img, 0, 0);
    sampleSrc = img.src;
  }
  const r = 3, x = Math.round(f.fx * (sampleCanvas.width - 1)), y = Math.round(f.fy * (sampleCanvas.height - 1));
  const x0 = Math.max(0, x - r), y0 = Math.max(0, y - r);
  const w = Math.min(sampleCanvas.width - x0, 2 * r + 1), h = Math.min(sampleCanvas.height - y0, 2 * r + 1);
  const px = sampleCanvas.getContext("2d").getImageData(x0, y0, w, h).data;
  let R = 0, G = 0, B = 0, n = 0;
  for (let i = 0; i < px.length; i += 4) { R += px[i]; G += px[i + 1]; B += px[i + 2]; n++; }
  return n ? [Math.round(R / n), Math.round(G / n), Math.round(B / n)] : null;
}

$("stage").addEventListener("mousemove", (e) => {
  if (!wbArmed) return;
  const rgb = localRgb(e), hud = $("wb-hud");
  if (!rgb) { hud.hidden = true; return; }
  const st = stageBox();
  hud.hidden = false;
  hud.textContent = `R ${rgb[0]}  G ${rgb[1]}  B ${rgb[2]}`;
  hud.style.left = `${Math.min(st.w - 130, e.clientX - st.left + 16)}px`;
  hud.style.top = `${Math.max(4, e.clientY - st.top - 30)}px`;
});
$("stage").addEventListener("mouseleave", () => { $("wb-hud").hidden = true; });

$("stage").addEventListener("click", async (e) => {
  if (!wbArmed) return;
  const f = photoFraction(e);
  if (!f) return;
  e.preventDefault(); e.stopPropagation();
  setWbArmed(false);
  const d = await developCall("/api/develop/wb_pick", { path: current, adjustments: values, x: f.fx, y: f.fy, radius: 3 },
    "White balance picker", null);
  if (!d) return;
  applyValues({ color_temp: d.color_temp, tint: d.tint }, d.label || "White Balance Picker");
  wbReadout(`${swatch(d.rgb)}R ${d.rgb[0]} G ${d.rgb[1]} B ${d.rgb[2]} &rarr; ${d.color_temp}K, tint ${d.tint >= 0 ? "+" : ""}${d.tint}`);
  if (d.warnings && d.warnings.length) toast("Heads up", { why: d.warnings.join(" ") + " Pick a spot that is not blown out." });
}, true);

// ---- Clipping overlay (J) -------------------------------------------------------------
let clipOn = false, clipTimer = 0, clipGen = 0;
function placeClipOverlay() {
  const img = $("clip-overlay");
  const r = clipOn ? displayedRect() : null;
  if (!r) { img.hidden = true; return; }
  Object.assign(img.style, { left: `${r.left}px`, top: `${r.top}px`, width: `${r.width}px`, height: `${r.height}px` });
  img.hidden = !img.src;
}
zoomHooks.push(placeClipOverlay);

function pctText(p) { return (p < 0.005 ? "0" : p < 10 ? p.toFixed(2).replace(/0$/, "") : p.toFixed(1)) + "%"; }
function paintClipDetail(pc) {
  const ch = (o) => ["r", "g", "b"].map(c => `<span class="${c}">${c.toUpperCase()} ${pctText(o[c])}</span>`).join(" ");
  $("clip-detail").innerHTML =
    `Highlights ${pctText(pc.highlights)}: ${ch(pc.highlights_channels)}<br>Shadows ${pctText(pc.shadows)}: ${ch(pc.shadows_channels)}`;
}

async function refreshClipping() {
  if (!clipOn || !current) return;
  const gen = ++clipGen;
  const d = await developCall("/api/develop/clipping", { path: current, adjustments: values }, "Clipping overlay", null);
  if (!d || gen !== clipGen || !clipOn) return;
  $("clip-overlay").src = d.overlay;
  $("clip-overlay").onload = placeClipOverlay;
  paintClipDetail(d.percent);
  placeClipOverlay();
}
function setClipping(on) {
  clipOn = on && !!current;
  $("clip-toggle").setAttribute("aria-pressed", String(clipOn));
  $("clip-detail").hidden = !clipOn;
  if (!clipOn) { clipGen++; $("clip-overlay").hidden = true; return; }
  refreshClipping();
}
$("clip-toggle").addEventListener("click", () => setClipping(!clipOn));
$("preview").addEventListener("load", () => {
  if (!clipOn) return;
  clearTimeout(clipTimer);
  clipTimer = setTimeout(refreshClipping, 350);
});
window.addEventListener("resize", placeClipOverlay);

// ---- keys and state -----------------------------------------------------------------------
document.addEventListener("keydown", (e) => {
  if (typingTarget(e.target) || document.querySelector("dialog[open]")) return;
  if (e.key === "Escape" && wbArmed) { setWbArmed(false); wbReadout(""); return; }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (module_ !== "develop" || !current) return;
  if (e.key === "j" || e.key === "J") { e.preventDefault(); setClipping(!clipOn); }
});

(function wrap() {
  const open = openFile;
  openFile = async function (path) {
    if (path !== current) { setWbArmed(false); wbReadout(""); }
    const r = await open.apply(this, arguments);
    if (current === path) {
      for (const id of ["auto-tone", "wb-auto", "wb-pick"]) $(id).disabled = false;
      loadWbPresets();
    }
    return r;
  };
})();
