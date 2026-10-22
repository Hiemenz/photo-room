// ---- Grain, post-crop vignette, Calibration, Point Color, sharpening detail -------
// The maths is on the server (effects.py, render_kernels.detail_stage); these panels only
// edit values. The sharpening radius / detail / masking sliders are built here and then moved
// under Sharpness by the accordion (panels.js).
function effectPanel(title, group, id) {
  const host = document.createElement("details");
  host.className = "panel";
  host.id = id;
  host.innerHTML = `<summary>${title}</summary><div class="effect-body"></div>`;
  $("sliders").appendChild(host);
  for (const c of EFFECT_CONTROLS.filter(c => c.group === group)) buildSlider(c, host.querySelector(".effect-body"));
  return host;
}
effectPanel("Effects: grain", "grain", "panel-grain");
effectPanel("Effects: post-crop vignette", "vignette", "panel-vignette");
effectPanel("Calibration", "calibration", "panel-calibration");
for (const c of EFFECT_CONTROLS.filter(c => c.group === "sharpen")) buildSlider(c, $("sliders"));

// ---- Point Color ------------------------------------------------------------------
const MAX_POINT_COLORS = 8;
const POINT_FIELDS = [
  ["hue", "Hue", 0, 360], ["range", "Range", 0, 100],
  ["hue_shift", "Hue shift", -100, 100], ["sat_shift", "Saturation shift", -100, 100],
  ["lum_shift", "Luminance shift", -100, 100],
];
const pointHost = document.createElement("details");
pointHost.className = "panel";
pointHost.id = "panel-point-color";
pointHost.innerHTML = `<summary>Point color</summary>
  <div class="autorow">
    <button id="pt-add" type="button" disabled title="Add a point and shift the colours near it">Add point</button>
    <button id="pt-pick" type="button" disabled aria-pressed="false" title="Pick the colour from the photo (Esc cancels)">Pick</button>
  </div>
  <div id="pt-list"></div>`;
$("sliders").appendChild(pointHost);

function pointList() { return Array.isArray(values.point_colors) ? values.point_colors : (values.point_colors = []); }
function pointSwatch(p) { return `hsl(${p.hue}, ${Math.max(10, p.sat)}%, ${Math.min(80, Math.max(25, p.lum))}%)`; }

function paintPoints() {
  const list = $("pt-list"), pts = pointList();
  list.innerHTML = "";
  pts.forEach((p, i) => {
    const row = document.createElement("div");
    row.className = "pt-row";
    row.innerHTML = `<div class="ctrl-head"><label>Point ${i + 1}</label>
        <i class="pt-swatch" style="display:inline-block;width:14px;height:14px;border-radius:50%;background:${pointSwatch(p)}"></i>
        <span class="val pt-del" title="Remove this point">remove</span></div>`;
    for (const [field, label, lo, hi] of POINT_FIELDS) {
      const wrap = document.createElement("div");
      wrap.className = "ctrl";
      wrap.innerHTML = `<div class="ctrl-head"><label for="pt-${i}-${field}">${label}</label>
        <span class="val" id="ptv-${i}-${field}">${Math.round(p[field])}</span></div>
        <input type="range" id="pt-${i}-${field}" min="${lo}" max="${hi}" step="1" value="${p[field]}">`;
      row.appendChild(wrap);
      wrap.querySelector("input").addEventListener("input", (e) => {
        p[field] = parseFloat(e.target.value);
        $(`ptv-${i}-${field}`).textContent = Math.round(p[field]);
        row.querySelector(".pt-swatch").style.background = pointSwatch(p);
        schedulePreview();
      });
    }
    row.querySelector(".pt-del").addEventListener("click", () => {
      pointList().splice(i, 1);
      paintPoints(); schedulePreview(); commit("Remove point color");
    });
    list.appendChild(row);
  });
  $("pt-add").disabled = !current || pts.length >= MAX_POINT_COLORS;
  $("pt-pick").disabled = !current || pts.length >= MAX_POINT_COLORS;
}

function addPoint(p) {
  pointList().push({ hue: 0, sat: 50, lum: 50, hue_shift: 0, sat_shift: 0, lum_shift: 0, range: 50, ...p });
  paintPoints();
}
$("pt-add").addEventListener("click", () => { if (current) { addPoint({}); commit("Add point color"); } });

let pointArmed = false;
function setPointArmed(on) {
  pointArmed = on && !!current;
  $("pt-pick").classList.toggle("armed", pointArmed);
  $("pt-pick").setAttribute("aria-pressed", String(pointArmed));
  $("stage").classList.toggle("picking", pointArmed);
}
$("pt-pick").addEventListener("click", () => setPointArmed(!pointArmed));
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && pointArmed) setPointArmed(false); });
function rgbToHsv(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
  let h = 0;
  if (d > 0) {
    if (mx === r) h = ((g - b) / d + 6) % 6; else if (mx === g) h = (b - r) / d + 2; else h = (r - g) / d + 4;
  }
  return { hue: Math.round(h * 60), sat: Math.round((mx ? d / mx : 0) * 100), lum: Math.round(mx * 100) };
}
$("stage").addEventListener("click", (e) => {
  if (!pointArmed) return;
  const rgb = typeof localRgb === "function" ? localRgb(e) : null;
  if (!rgb) return;
  e.preventDefault(); e.stopPropagation();
  setPointArmed(false);
  addPoint(rgbToHsv(...rgb));
  commit("Add point color");
}, true);
