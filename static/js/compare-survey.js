// ---- Compare and Survey --------------------------------------------------
const cmp = { sel: null, cand: null, scale: 1, x: 0, y: 0, token: 0, cache: new Map() };
let overlay = null;

function closeOverlays() {
  overlay = null;
  $("cmp").classList.remove("on"); $("svy").classList.remove("on");
}

async function viewPreview(path) {
  if (cmp.cache.has(path)) return cmp.cache.get(path);
  const data = await post("/api/view_preview", { path });
  if (data.error) throw new Error(data.error);
  cmp.cache.set(path, data.preview);
  if (cmp.cache.size > 6) cmp.cache.delete(cmp.cache.keys().next().value);
  return data.preview;
}

function cmpTransform() {
  for (const id of ["cmp-a", "cmp-b"]) {
    $(id).querySelector("img").style.transform = `translate(${cmp.x}px, ${cmp.y}px) scale(${cmp.scale})`;
  }
}
function cmpResetZoom() { cmp.scale = 1; cmp.x = 0; cmp.y = 0; cmpTransform(); }

async function cmpShow() {
  const token = ++cmp.token;
  const name = (p) => libItem(p)?.name || p;
  $("cmp-info").textContent = `${name(cmp.sel)}  vs  ${cmp.cand ? name(cmp.cand) : "(no candidate)"}`;
  const set = async (id, path) => {
    const img = $(id).querySelector("img");
    if (!path) { img.removeAttribute("src"); return; }
    try {
      const src = await viewPreview(path);
      if (token === cmp.token) img.src = src;
    } catch (err) { setStatus(err.message, true); }
  };
  await Promise.all([set("cmp-a", cmp.sel), set("cmp-b", cmp.cand)]);
}

function cmpStep(delta) {
  const items = libState.items.filter(i => i.path !== cmp.sel);
  if (!items.length) return;
  let i = items.findIndex(x => x.path === cmp.cand);
  i = (i + delta + items.length) % items.length;
  cmp.cand = items[i].path; cmpShow();
}

function openCompare() {
  if (!libState.items.length) return;
  cmp.sel = libActive || libState.items[0].path;
  const idx = libState.items.findIndex(i => i.path === cmp.sel);
  const next = libState.items[idx + 1] || libState.items[idx - 1];
  cmp.cand = next ? next.path : null;
  overlay = "cmp"; $("cmp").classList.add("on");
  cmpResetZoom(); cmpShow();
}

for (const id of ["cmp-a", "cmp-b"]) {
  const pane = $(id);
  pane.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = pane.getBoundingClientRect();
    const px = e.clientX - r.left, py = e.clientY - r.top;
    const f = Math.exp(-e.deltaY * 0.0015);
    const s = Math.max(1, Math.min(16, cmp.scale * f));
    const k = s / cmp.scale;
    // Zoom about the pointer; both panes share the same transform so they stay in step.
    cmp.x = px - (px - cmp.x) * k; cmp.y = py - (py - cmp.y) * k; cmp.scale = s;
    if (s === 1) { cmp.x = 0; cmp.y = 0; }
    cmpTransform();
  }, { passive: false });
  let drag = null;
  pane.addEventListener("pointerdown", (e) => {
    drag = { x: e.clientX - cmp.x, y: e.clientY - cmp.y };
    pane.setPointerCapture(e.pointerId); pane.style.cursor = "grabbing";
  });
  pane.addEventListener("pointermove", (e) => {
    if (!drag || cmp.scale === 1) return;
    cmp.x = e.clientX - drag.x; cmp.y = e.clientY - drag.y; cmpTransform();
  });
  pane.addEventListener("pointerup", () => { drag = null; pane.style.cursor = ""; });
  pane.addEventListener("dblclick", (e) => {
    if (cmp.scale > 1) return cmpResetZoom();
    const r = pane.getBoundingClientRect();
    const px = e.clientX - r.left, py = e.clientY - r.top, s = 4;
    cmp.x = px - px * s; cmp.y = py - py * s; cmp.scale = s; cmpTransform();
  });
}
$("cmp-close").addEventListener("click", closeOverlays);

let survey = [];
function openSurvey() {
  const picked = libMulti.size > 1 ? [...libMulti] : [];
  if (picked.length < 2) return setStatus("Select two or more photos (Shift/Ctrl-click) to survey", true);
  survey = libState.items.map(i => i.path).filter(p => picked.includes(p));
  overlay = "svy"; $("svy").classList.add("on");
  surveyDraw();
}
function surveyDraw() {
  const grid = $("svy-grid");
  grid.innerHTML = "";
  const n = survey.length;
  const cols = Math.ceil(Math.sqrt(n * (grid.clientWidth || 1200) / (grid.clientHeight || 700)));
  const c = Math.max(1, Math.min(n, cols));
  grid.style.gridTemplateColumns = `repeat(${c}, 1fr)`;
  grid.style.gridTemplateRows = `repeat(${Math.ceil(n / c)}, 1fr)`;
  $("svy-info").textContent = `${n} photos`;
  for (const p of survey) {
    const cell = document.createElement("div");
    cell.className = "scell"; cell.dataset.path = p;
    const it = libItem(p);
    cell.innerHTML = `<img alt=""><div class="nm"></div>`;
    cell.querySelector("img").src = `/api/thumb?path=${encodeURIComponent(p)}`;
    cell.querySelector(".nm").textContent = (it?.name || p) + (it?.rating > 0 ? "  " + "★".repeat(it.rating) : "");
    cell.addEventListener("click", () => surveyRemove(p));
    cell.addEventListener("dblclick", () => { closeOverlays(); openInDevelop(p); });
    grid.appendChild(cell);
  }
}
function surveyRemove(p) {
  survey = survey.filter(x => x !== p);
  libMulti = new Set(survey);
  if (survey.length < 2) { closeOverlays(); return; }
  surveyDraw();
}
$("svy-close").addEventListener("click", closeOverlays);
$("lib-compare").addEventListener("click", openCompare);
$("lib-survey").addEventListener("click", openSurvey);

// Keys while Compare or Survey is open. Returns true when handled.
function overlayKey(e, k) {
  if (!overlay) return false;
  e.preventDefault();
  if (k === "escape" || k === "g" || (overlay === "cmp" && k === "c") || (overlay === "svy" && k === "n")) {
    closeOverlays(); return true;
  }
  if (overlay === "cmp") {
    if (k === "arrowright" || k === "arrowdown") cmpStep(1);
    else if (k === "arrowleft") cmpStep(-1);
    else if (k === "arrowup" && cmp.cand) { libActive = cmp.sel = cmp.cand; cmpStep(1); libPaintAll(); }
    else if (k >= "0" && k <= "5" && k.length === 1) {
      libRate(parseInt(k, 10), [cmp.cand || cmp.sel]);
    }
    else if (k === "p" || k === "x") { const keep = libActive; libActive = cmp.cand || cmp.sel; libMulti = new Set(); libFlag(k === "p" ? "pick" : "reject").then(() => { libActive = keep; }); }
  } else {
    if (k === "enter") { const p = survey[0]; closeOverlays(); openInDevelop(p); }
  }
  return true;
}
