// ---- Red eye / pet eye ----------------------------------------------------
// `values.redeye` is a list of {x, y, r, kind, amount, darken} in the same
// normalised coordinates as spots (the whole frame, before any crop), so the
// tool shows the uncropped frame just like the spot tool does.
let redeyeTool = false;       // overlay active (preview shows the whole frame)
let eyeSel = -1;              // index of the selected eye
const eyeOpts = { kind: "red", r: 0.015, amount: 100, darken: 50 };

const eyePanel = document.createElement("details");
eyePanel.className = "panel";
eyePanel.id = "redeye-panel";
eyePanel.innerHTML = `<summary>Red eye / pet eye</summary>
  <div class="seg">
    <button id="redeye-toggle" disabled title="Click an eye to correct it">Eye tool</button>
    <button data-eyekind="red" class="active">Red eye</button>
    <button data-eyekind="pet">Pet eye</button>
  </div>
  <div class="ctrl"><div class="ctrl-head"><label for="eye-size">Pupil size</label><span class="val" id="eye-size-v"></span></div>
    <input type="range" id="eye-size" min="0.3" max="6" step="0.1" value="1.5"></div>
  <div class="ctrl"><div class="ctrl-head"><label for="eye-amount">Amount</label><span class="val" id="eye-amount-v"></span></div>
    <input type="range" id="eye-amount" min="0" max="100" step="1" value="100"></div>
  <div class="ctrl"><div class="ctrl-head"><label for="eye-darken">Darken pupil</label><span class="val" id="eye-darken-v"></span></div>
    <input type="range" id="eye-darken" min="0" max="100" step="1" value="50"></div>
  <div style="font-size:11px;color:var(--muted);margin:6px 0">
    Click the middle of the pupil and size the circle to cover it. Red eye
    removes the red cast; pet eye turns green, yellow or blue eyeshine dark.</div>
  <div class="seg"><button id="eye-delete" disabled>Delete eye</button><button id="eye-clear" disabled>Clear all</button></div>
  <div id="eye-count" style="font-size:11px;color:var(--muted);margin-top:6px"></div>`;
$("sliders").appendChild(eyePanel);

const eyeLayer = document.createElement("div");
eyeLayer.id = "redeye-layer";
eyeLayer.innerHTML = `<svg id="redeye-svg" width="100%" height="100%"></svg>`;
$("stage").appendChild(eyeLayer);

function eyeList() { return values.redeye || (values.redeye = []); }

function paintRedeyeUI() {
  const eyes = eyeList(), e = eyes[eyeSel];
  const r = e ? e.r : eyeOpts.r, a = e ? e.amount : eyeOpts.amount, d = e ? e.darken : eyeOpts.darken;
  $("eye-size").value = r * 100;   $("eye-size-v").textContent = (r * 100).toFixed(1);
  $("eye-amount").value = a;       $("eye-amount-v").textContent = Math.round(a);
  $("eye-darken").value = d;       $("eye-darken-v").textContent = Math.round(d);
  const kind = e ? e.kind : eyeOpts.kind;
  document.querySelectorAll("[data-eyekind]").forEach(b => b.classList.toggle("active", b.dataset.eyekind === kind));
  $("eye-delete").disabled = !e;
  $("eye-clear").disabled = eyes.length === 0;
  $("eye-count").textContent = eyes.length ? `${eyes.length} eye${eyes.length === 1 ? "" : "s"}` : "";
  paintEyes();
}

function paintEyes() {
  const svg = $("redeye-svg");
  svg.innerHTML = "";
  if (!redeyeTool) return;
  const f = cropRectOnScreen();
  const ns = "http://www.w3.org/2000/svg";
  eyeList().forEach((e, i) => {
    const c = document.createElementNS(ns, "circle");
    const attrs = { cx: f.left + e.x * f.width, cy: f.top + e.y * f.height, r: Math.max(4, e.r * f.width),
                    fill: "rgba(255,255,255,.05)", stroke: i === eyeSel ? "#ffd24a" : "#ffffff", "stroke-width": 2,
                    "data-i": i, style: "pointer-events:all;cursor:move" };
    for (const k in attrs) c.setAttribute(k, attrs[k]);
    svg.appendChild(c);
  });
}

function setRedeyeTool(on) {
  redeyeTool = on && !!current;
  eyeLayer.classList.toggle("on", redeyeTool);
  $("redeye-toggle").classList.toggle("active", redeyeTool);
  if (redeyeTool && cropping) $("crop-toggle").click();
  if (redeyeTool && spotTool) setSpotTool(false);
  $("crop-toggle").disabled = spotTool || redeyeTool || !current;
  if (!redeyeTool) eyeSel = -1;
  paintRedeyeUI();
  schedulePreview();
  if (typeof layoutMaskLayer === "function") layoutMaskLayer();
}
$("redeye-toggle").addEventListener("click", () => setRedeyeTool(!redeyeTool));

document.querySelectorAll("[data-eyekind]").forEach(b => b.addEventListener("click", () => {
  const e = eyeList()[eyeSel];
  if (e) { e.kind = b.dataset.eyekind; schedulePreview(); commit(e.kind === "pet" ? "Pet eye" : "Red eye"); }
  else eyeOpts.kind = b.dataset.eyekind;
  paintRedeyeUI();
}));
for (const [id, key, scale] of [["eye-size", "r", 100], ["eye-amount", "amount", 1], ["eye-darken", "darken", 1]]) {
  $(id).addEventListener("input", () => {
    const v = parseFloat($(id).value) / scale, e = eyeList()[eyeSel];
    if (e) { e[key] = v; schedulePreview(); } else eyeOpts[key] = v;
    paintRedeyeUI();
  });
  $(id).addEventListener("change", () => { if (eyeList()[eyeSel]) commit(`Eye ${key}`); });
}
$("eye-delete").addEventListener("click", () => {
  if (eyeSel < 0) return;
  eyeList().splice(eyeSel, 1);
  eyeSel = -1;
  paintRedeyeUI(); schedulePreview(); commit("Delete eye");
});
$("eye-clear").addEventListener("click", () => {
  values.redeye = []; eyeSel = -1;
  paintRedeyeUI(); schedulePreview(); commit("Clear eyes");
});

(function eyeMouse() {
  let drag = null;
  const frac = (ev) => {
    const f = cropRectOnScreen(), st = $("stage").getBoundingClientRect();
    return { x: Math.min(1, Math.max(0, (ev.clientX - st.left - f.left) / f.width)),
             y: Math.min(1, Math.max(0, (ev.clientY - st.top - f.top) / f.height)) };
  };
  eyeLayer.addEventListener("mousedown", (ev) => {
    if (!redeyeTool || !current) return;
    const p = frac(ev), hit = ev.target.dataset ? ev.target.dataset : {};
    if (hit.i !== undefined) {
      eyeSel = parseInt(hit.i, 10);
      drag = { i: eyeSel, from: p, orig: { ...eyeList()[eyeSel] } };
      paintRedeyeUI();
      return;
    }
    const eyes = eyeList();
    if (eyes.length >= 16) return setStatus("At most 16 eyes per photo", true);
    eyes.push({ x: p.x, y: p.y, r: eyeOpts.r, kind: eyeOpts.kind, amount: eyeOpts.amount, darken: eyeOpts.darken });
    eyeSel = eyes.length - 1;
    paintRedeyeUI(); schedulePreview(); commit(eyeOpts.kind === "pet" ? "Add pet eye" : "Add red eye");
  });
  window.addEventListener("mousemove", (ev) => {
    if (!drag) return;
    const p = frac(ev), e = eyeList()[drag.i];
    if (!e) return;
    e.x = Math.min(1, Math.max(0, drag.orig.x + p.x - drag.from.x));
    e.y = Math.min(1, Math.max(0, drag.orig.y + p.y - drag.from.y));
    paintEyes();
  });
  window.addEventListener("mouseup", () => {
    if (!drag) return;
    drag = null;
    schedulePreview(); commit("Move eye");
  });
  window.addEventListener("keydown", (ev) => {
    if (redeyeTool && eyeSel >= 0 && (ev.key === "Delete" || ev.key === "Backspace") &&
        !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)) {
      ev.preventDefault(); $("eye-delete").click();
    }
  });
})();
window.addEventListener("resize", paintEyes);
