// ---- Colour mixer and grading --------------------------------------------
// HSL tabs (Hue / Saturation / Luminance) share one set of eight sliders'
// worth of room; with Black & white on, the B&W mixer replaces them.
let mixerTab = "hsl_hue";
const mixerHost = document.createElement("details");
mixerHost.className = "panel";
mixerHost.innerHTML = `<summary>Color mixer</summary>
  <div class="seg" id="mixer-tabs">
    <button data-tab="hsl_hue" class="active">Hue</button>
    <button data-tab="hsl_sat">Saturation</button>
    <button data-tab="hsl_lum">Luminance</button>
  </div>
  <div id="mixer-note" style="font-size:11px;color:var(--muted);margin-top:10px" hidden>Black &amp; white mixer</div>
  <div id="mixer-sliders"></div>`;
$("sliders").appendChild(mixerHost);
for (const c of COLOR_CONTROLS.filter(c => c.band)) {
  buildSlider(c, $("mixer-sliders"));
  $(`ctrl-${c.key}`).dataset.group = c.group;
}
function syncMixerView() {
  const mono = !!values.monochrome;
  $("mixer-tabs").hidden = mono;
  $("mixer-note").hidden = !mono;
  for (const c of COLOR_CONTROLS.filter(c => c.band)) {
    $(`ctrl-${c.key}`).hidden = mono ? c.group !== "gray" : c.group !== mixerTab;
  }
}
$("mixer-tabs").addEventListener("click", (e) => {
  const t = e.target.dataset.tab;
  if (!t) return;
  mixerTab = t;
  document.querySelectorAll("#mixer-tabs button").forEach(b => b.classList.toggle("active", b === e.target));
  syncMixerView();
});

const gradeHost = document.createElement("details");
gradeHost.className = "panel";
gradeHost.innerHTML = `<summary>Color grading</summary><div id="grade-body"></div>`;
$("sliders").appendChild(gradeHost);
const WHEELS = [["shadow", "Shadows"], ["mid", "Midtones"], ["highlight", "Highlights"], ["global", "Global"]];
const wheelCanvases = {};
function drawWheel(cv) {
  const size = 168, ctx = cv.getContext("2d"), img = ctx.createImageData(size, size), r0 = size / 2;
  for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) {
    const dx = x - r0 + .5, dy = y - r0 + .5, r = Math.hypot(dx, dy) / r0;
    if (r > 1) continue;
    const hue = (Math.atan2(dy, dx) * 180 / Math.PI + 360) % 360;
    const f = (n) => { const k = (n + hue / 60) % 6; return 1 - r * Math.max(0, Math.min(k, 4 - k, 1)); };
    const i = (y * size + x) * 4;
    img.data[i] = f(5) * 200 + 30; img.data[i+1] = f(3) * 200 + 30; img.data[i+2] = f(1) * 200 + 30; img.data[i+3] = 255;
  }
  ctx.putImageData(img, 0, 0);
}
function paintWheels() {
  for (const [rng] of WHEELS) {
    const cv = wheelCanvases[rng]; if (!cv) continue;
    drawWheel(cv);
    const ctx = cv.getContext("2d"), r0 = cv.width / 2;
    const a = (values[`grade_${rng}_hue`] ?? 0) * Math.PI / 180, r = (values[`grade_${rng}_sat`] ?? 0) / 100 * r0;
    ctx.beginPath(); ctx.arc(r0 + Math.cos(a) * r, r0 + Math.sin(a) * r, 6, 0, 7);
    ctx.strokeStyle = "#fff"; ctx.lineWidth = 2; ctx.stroke();
  }
}
for (const [rng, label] of WHEELS) {
  const row = document.createElement("div");
  row.innerHTML = `<div class="wheelname">${label}</div><div class="wheelrow"><canvas width="168" height="168"></canvas><div class="wheelsliders"></div></div>`;
  $("grade-body").appendChild(row);
  const cv = row.querySelector("canvas");
  wheelCanvases[rng] = cv;
  for (const what of ["hue", "sat", "lum"]) {
    buildSlider(COLOR_CONTROLS.find(c => c.key === `grade_${rng}_${what}`), row.querySelector(".wheelsliders"));
  }
  const pick = (e) => {
    if (!current) return;
    const b = cv.getBoundingClientRect(), r0 = b.width / 2;
    const dx = e.clientX - b.left - r0, dy = e.clientY - b.top - r0;
    values[`grade_${rng}_hue`] = Math.round((Math.atan2(dy, dx) * 180 / Math.PI + 360) % 360);
    values[`grade_${rng}_sat`] = Math.round(Math.min(1, Math.hypot(dx, dy) / r0) * 100);
    paintAll(); schedulePreview();
  };
  let down = false;
  cv.addEventListener("pointerdown", (e) => { down = true; cv.setPointerCapture(e.pointerId); pick(e); });
  cv.addEventListener("pointermove", (e) => { if (down) pick(e); });
  cv.addEventListener("pointerup", () => { if (down) commit(`${label} colour grade`); down = false; });
}
for (const k of ["grade_blending", "grade_balance"]) {
  buildSlider(COLOR_CONTROLS.find(c => c.key === k), $("grade-body"));
}

function paintAll() {
  if (typeof paintCurve === "function") paintCurve();
  for (const c of ALL_CONTROLS) {
    $(`s-${c.key}`).value = values[c.key] ?? c.default;
    paintValue(c);
  }
  paintWheels();
  syncMixerView();
  $("s-monochrome").checked = !!values.monochrome;
  if (typeof paintMasks === "function") paintMasks();
  paintM8();
}
