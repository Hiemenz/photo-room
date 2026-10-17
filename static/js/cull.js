// ---- Smart cull ----------------------------------------------------------
// Bursts are grouped on the server from cached thumbnails (see docs/AUTO-CULL.md).
// This view only stages choices: nothing is written until Apply, which is one
// undoable batch. Every photo in a reviewed group is logged as accepted or
// overridden so the personal model can learn.
const cull = {
  on: false, groups: [], photos: new Map(), choice: new Map(), reviewed: new Set(),
  gi: 0, fi: 0, last: null, busy: false, token: 0,
  cmp: { on: false, mode: "side", paths: [], focus: 0, view: { ox: 0, oy: 0 }, panes: [], fpt: [0.5, 0.5] },
};

function cullIsOpen() { return cull.on; }

function cullPhoto(path) { return cull.photos.get(path); }
function cullChoiceOf(path) { return cull.choice.get(path) || "none"; }

async function cullGet(url) {
  const res = await fetch(url);
  return res.json();
}

function cullScopeBody() {
  const scope = $("cull-scope").value;
  const body = { scope, singles: $("cull-singles").checked };
  if (scope === "collection") body.collection = libCollection;
  if (scope === "selection") body.paths = libTargets();
  return body;
}

function cullSay(msg, err = false) {
  $("cull-info").textContent = msg;
  $("cull-info").style.color = err ? "var(--danger)" : "";
  if (err) setStatus(msg, true);
}

async function cullPollJob(id, onProgress) {
  for (;;) {
    const j = await cullGet(`/api/cull/job?id=${encodeURIComponent(id)}`);
    if (j.error) throw new Error(j.error);
    onProgress(j);
    if (j.state === "done") return j;
    if (j.state === "error") throw new Error(j.errors.join("; ") || "job failed");
    await new Promise(r => setTimeout(r, 250));
  }
}

function cullProgress(done, total) {
  const p = $("cull-progress");
  p.hidden = !total;
  p.max = total || 1; p.value = done;
}

async function cullOpen() {
  if (cull.on) return;
  if (module_ !== "library") await setModule("library");
  cull.on = true;
  $("cull").classList.add("on");
  if ($("cull-scope").value === "selection" && libTargets().length < 2) $("cull-scope").value = "all";
  await cullRefresh();
}

function cullHide() {
  if (!cull.on) return;
  cull.on = false;
  cullCompareClose();
  $("cull").classList.remove("on");
  cull.token++;
}

async function cullRefresh() {
  if (cull.busy) return;
  cull.busy = true;
  const token = ++cull.token;
  const body = cullScopeBody();
  if (body.scope === "collection" && !body.collection) {
    cull.busy = false; return cullSay("Open a collection in the Library first", true);
  }
  if (body.scope === "selection" && body.paths.length < 2) {
    cull.busy = false; return cullSay("Select two or more photos first", true);
  }
  try {
    cullSay("Analysing thumbnails…");
    const a = await post("/api/cull/analyze", body);
    if (a.error) throw new Error(a.error);
    if (a.missing) await cullPollJob(a.job, (j) => { if (token === cull.token) { cullProgress(j.done, j.total); cullSay(`Analysing ${j.done}/${j.total}…`); } });
    cullProgress(0, 0);
    const s = await post("/api/cull/suggest", body);
    if (s.error) throw new Error(s.error);
    if (token !== cull.token) return;
    cullLoad(s);
  } catch (err) {
    cullSay(err.message, true);
  } finally {
    cull.busy = false;
    cullProgress(0, 0);
  }
}

function cullLoad(s) {
  cull.groups = s.groups;
  cull.photos = new Map();
  cull.choice = new Map();
  cull.reviewed = new Set();
  for (const g of s.groups) {
    g.paths = g.photos.map(p => p.path);
    for (const p of g.photos) {
      p.group = g.id; p.gsize = g.photos.length;
      cull.photos.set(p.path, p);
      // A suggestion never overrides a flag you already set; it only fills the gaps.
      cull.choice.set(p.path, p.action !== "none" ? p.action : p.flag);
    }
  }
  cull.gi = 0; cull.fi = Math.max(0, cull.groups[0]?.photos.findIndex(p => p.best) ?? 0);
  const bursts = s.groups.filter(g => g.photos.length > 1).length;
  cullSay(`${s.photos} photos, ${bursts} bursts` + (s.model.confidence ? ` · learned model ${Math.round(s.model.confidence * 100)}% weight` : ""));
  cullDraw();
  cullFocus(0, cull.fi);
}

function cullTime(p) { return (p.date || "").slice(11, 19); }

function cullDraw() {
  const box = $("cull-groups");
  box.innerHTML = "";
  if (!cull.groups.length) {
    box.innerHTML = `<div class="hint">No bursts found. Tick "singles" to list every photo.</div>`;
    return cullCounts();
  }
  const obs = cullObserver();
  cull.groups.forEach((g, gi) => {
    const row = document.createElement("div");
    row.className = "cg"; row.id = `cg-${gi}`;
    const head = document.createElement("div"); head.className = "cg-head";
    head.innerHTML = `<b></b><span></span><span class="done"></span>`;
    head.querySelector("b").textContent = g.photos.length > 1 ? `Burst ${gi + 1}` : `Single ${gi + 1}`;
    head.children[1].textContent = `${g.photos.length} frame${g.photos.length > 1 ? "s" : ""} · ${cullTime(g.photos[0])}`;
    const frames = document.createElement("div"); frames.className = "cg-frames";
    g.photos.forEach((p, fi) => {
      const f = document.createElement("div");
      f.className = "cf"; f.dataset.path = p.path;
      const img = document.createElement("img"); img.alt = p.name; img.dataset.src = `/api/thumb?path=${encodeURIComponent(p.path)}`;
      img.addEventListener("error", () => { img.style.visibility = "hidden"; });
      obs.observe(img);
      f.append(img);
      if (p.best) { const t = document.createElement("span"); t.className = "tag best"; t.textContent = "★ BEST"; f.append(t); }
      if (p.action !== "none") { const t = document.createElement("span"); t.className = `sug ${p.action}`; t.textContent = `suggest ${p.action}`; f.append(t); }
      const m = document.createElement("div"); m.className = "meta";
      m.innerHTML = `<span class="nm"></span><span class="sc"></span>`;
      m.querySelector(".nm").textContent = p.name; m.querySelector(".sc").textContent = Math.round(p.score);
      f.append(m);
      f.addEventListener("click", () => cullFocus(gi, fi));
      f.addEventListener("dblclick", () => { cullFocus(gi, fi); cullCompareOpen("side"); });
      frames.append(f);
    });
    row.append(head, frames);
    box.append(row);
  });
  cull.groups.forEach((g, gi) => g.photos.forEach(p => cullPaintFrame(p.path)));
  cullCounts();
}

let cullObs = null;
function cullObserver() {
  if (cullObs) cullObs.disconnect();
  cullObs = new IntersectionObserver((entries, o) => {
    for (const en of entries) {
      if (!en.isIntersecting) continue;
      const img = en.target;
      if (img.dataset.src) { img.src = img.dataset.src; delete img.dataset.src; }
      o.unobserve(img);
    }
  }, { root: $("cull-groups"), rootMargin: "500px" });
  return cullObs;
}

function cullFrameEl(path) {
  return $("cull-groups").querySelector(`.cf[data-path="${CSS.escape(path)}"]`);
}

function cullPaintFrame(path) {
  const el = cullFrameEl(path); if (!el) return;
  const c = cullChoiceOf(path);
  el.classList.toggle("c-pick", c === "pick");
  el.classList.toggle("c-reject", c === "reject");
  const p = cullPhoto(path);
  el.classList.toggle("focus", cull.groups[cull.gi]?.photos[cull.fi]?.path === path);
  el.title = `${p.name}\nscore ${p.score}\n` + p.reasons.join("\n");
}

function cullCounts() {
  const total = cull.groups.length;
  const rev = cull.reviewed.size;
  cull.groups.forEach((g, gi) => {
    const row = $(`cg-${gi}`); if (!row) return;
    const done = cull.reviewed.has(g.id);
    row.classList.toggle("reviewed", done);
    row.querySelector(".done").textContent = done ? "reviewed" : "";
  });
  const n = cullPending().length;
  $("cull-apply").disabled = !rev;
  $("cull-apply").textContent = rev ? `Apply (${rev}/${total} reviewed, ${n} change${n === 1 ? "" : "s"})` : "Apply";
}

// The flag changes Apply would make for the reviewed groups.
function cullPending() {
  const out = [];
  for (const g of cull.groups) {
    if (!cull.reviewed.has(g.id)) continue;
    for (const p of g.photos) if (cullChoiceOf(p.path) !== (p.flag || "none")) out.push(p.path);
  }
  return out;
}

function cullFocus(gi, fi) {
  const g = cull.groups[gi]; if (!g) return;
  gi = Math.max(0, Math.min(cull.groups.length - 1, gi));
  const prev = cull.groups[cull.gi]?.photos[cull.fi]?.path;
  cull.gi = gi; cull.fi = Math.max(0, Math.min(cull.groups[gi].photos.length - 1, fi));
  if (prev) cullPaintFrame(prev);
  const p = cull.groups[gi].photos[cull.fi];
  cullPaintFrame(p.path);
  $(`cg-${gi}`)?.scrollIntoView({ block: "nearest" });
  cullDetail(p);
}

function cullDetail(p) {
  const d = $("cull-detail");
  const g = cull.groups[cull.gi];
  d.innerHTML = "";
  const h = document.createElement("h3"); h.textContent = p.name; d.append(h);
  const sug = document.createElement("div");
  sug.textContent = `Suggested: ${p.action === "none" ? "no change" : p.action} · you: ${cullChoiceOf(p.path)}`;
  d.append(sug);
  const ul = document.createElement("ul");
  for (const r of p.reasons.length ? p.reasons : ["no notable issues"]) {
    const li = document.createElement("li"); li.textContent = r; ul.append(li);
  }
  d.append(ul);
  const names = ["Sharpness vs burst", "Sharpness", "Exposure", "Blown", "Crushed", "Contrast", "Face", "Eyes open"];
  const dl = document.createElement("dl");
  const add = (k, v) => { const a = document.createElement("dt"), b = document.createElement("dd"); a.textContent = k; b.textContent = v; dl.append(a, b); };
  add("Score", p.score);
  p.vec.forEach((v, i) => { if (i < 6 || p.vec[6]) add(names[i], Number(v).toFixed(2)); });
  add("Captured", p.date || "unknown");
  d.append(dl);
  const hint = document.createElement("div"); hint.className = "hint";
  hint.textContent = "Sharpness is measured in the focus area (the sharpest tiles, or the face), so a crisp subject on a smooth background is not penalised. It is a ranking within a burst, never a verdict on its own.";
  d.append(hint);
}

function cullSetChoice(path, action) {
  const p = cullPhoto(path); if (!p) return;
  cull.choice.set(path, action);
  cull.reviewed.add(p.group);
  cullPaintFrame(path); cullCounts();
  cullDetail(p);
  cullCmpPaint();
}

function cullAcceptGroup(gi) {
  const g = cull.groups[gi]; if (!g) return;
  for (const p of g.photos) cull.choice.set(p.path, p.action !== "none" ? p.action : (p.flag || "none"));
  cull.reviewed.add(g.id);
  g.photos.forEach(p => cullPaintFrame(p.path));
  cullCounts();
}

function cullGroupMove(delta) {
  const gi = Math.max(0, Math.min(cull.groups.length - 1, cull.gi + delta));
  const g = cull.groups[gi];
  cullFocus(gi, Math.max(0, g.photos.findIndex(p => p.best)));
}

function cullKey(e, k) {
  if (!cull.on) return false;
  if (document.querySelector("dialog[open]")) return false;
  e.preventDefault();
  const c = cull.cmp;
  if (k === "escape") { if (c.on) cullCompareClose(); else cullHide(); return true; }
  if (c.on) {
    if (k === "tab") { cullCmpFocus((c.focus + (e.shiftKey ? c.paths.length - 1 : 1)) % c.paths.length); return true; }
    if (k === "f") { cullCmpCenter(); return true; }
    if (k === "z") { cullCompareOpen(c.mode === "side" ? "actual" : "side"); return true; }
    if (k === "c") { cullCompareClose(); return true; }
    const path = c.paths[c.focus];
    if (k === "p") cullSetChoice(path, "pick");
    else if (k === "x") cullSetChoice(path, "reject");
    else if (k === "u") cullSetChoice(path, "none");
    return true;
  }
  const g = cull.groups[cull.gi]; if (!g) return true;
  const p = g.photos[cull.fi];
  if (k === "arrowright") cullFocus(cull.gi, cull.fi + 1);
  else if (k === "arrowleft") cullFocus(cull.gi, cull.fi - 1);
  else if (k === "arrowdown" || k === "j") cullGroupMove(1);
  else if (k === "arrowup" || k === "k") cullGroupMove(-1);
  else if (k === "p") cullSetChoice(p.path, "pick");
  else if (k === "x") cullSetChoice(p.path, "reject");
  else if (k === "u") cullSetChoice(p.path, "none");
  else if (k === "a" || k === "enter" || k === " ") { cullAcceptGroup(cull.gi); cullGroupMove(1); }
  else if (k === "c") cullCompareOpen("side");
  else if (k === "z") cullCompareOpen("actual");
  return true;
}

// ---- Apply and undo --------------------------------------------------------
async function cullApply() {
  if (!cull.reviewed.size) return;
  const choices = [];
  for (const g of cull.groups) {
    if (!cull.reviewed.has(g.id)) continue;
    for (const p of g.photos) choices.push({ path: p.path, action: cullChoiceOf(p.path), suggested: p.action,
      group: g.id, gsize: g.photos.length, vec: p.vec });
  }
  const data = await post("/api/cull/apply", { choices });
  if (data.error) return cullSay(data.error, true);
  for (const c of choices) { const p = cullPhoto(c.path); if (p) p.flag = c.action; }
  cull.last = { kind: "flags", id: data.batch };
  cull.reviewed = new Set();
  $("cull-undo").disabled = false;
  cull.groups.forEach(g => g.photos.forEach(p => cullPaintFrame(p.path)));
  cullCounts();
  setStatus(data.message);
  cullSay(`${data.message}. ${data.counts.pick} picked, ${data.counts.reject} rejected.`);
  libLoad(true);
}

async function cullUndo() {
  if (!cull.last) return;
  const url = cull.last.kind === "flags" ? "/api/cull/undo" : "/api/cull/autoedit/undo";
  const data = await post(url, { batch: cull.last.id });
  if (data.error) return cullSay(data.error, true);
  cull.last = null; $("cull-undo").disabled = true;
  setStatus(data.message); cullSay(data.message);
  await libLoad(true);
  await cullRefresh();
}

// ---- Compare ---------------------------------------------------------------
function cullCmpSet() {
  const g = cull.groups[cull.gi];
  const cur = g.photos[cull.fi].path;
  const rest = [...g.photos].sort((a, b) => b.score - a.score).map(p => p.path).filter(p => p !== cur);
  return [cur, ...rest].slice(0, 4);
}

function cullCompareOpen(mode) {
  const g = cull.groups[cull.gi]; if (!g) return;
  const c = cull.cmp;
  if (g.photos.length < 2) return cullSay("This frame has no burst siblings to compare with", true);
  c.on = true; c.mode = mode; c.paths = cullCmpSet(); c.focus = 0;
  const best = g.photos.find(p => p.best) || g.photos[0];
  c.fpt = best.focus || [0.5, 0.5];
  $("cull-cmp").classList.add("on");
  $("cull-cmp-mode").textContent = mode === "side" ? "Actual size (Z)" : "Fit (Z)";
  $("cull-cmp-title").textContent = mode === "side" ? "Compare" : "Compare at 100%";
  $("cull-cmp-info").textContent = `${c.paths.length} frames of ${g.photos.length}`;
  if (mode === "side") cullBuildSide(); else cullBuildActual();
}

function cullCompareClose() {
  cull.cmp.on = false;
  $("cull-cmp").classList.remove("on");
  $("cull-cmp-panes").innerHTML = "";
  cull.cmp.panes = [];
}

function cullPaneShell(path, i) {
  const p = cullPhoto(path);
  const pane = document.createElement("div");
  pane.className = "xpane"; pane.dataset.path = path;
  const tag = document.createElement("span"); tag.className = "tag";
  pane.append(tag);
  pane.addEventListener("pointerdown", () => cullCmpFocus(i));
  return pane;
}

function cullCmpPaint() {
  const c = cull.cmp; if (!c.on) return;
  c.panes.forEach((pane, i) => {
    const p = cullPhoto(c.paths[i]);
    const ch = cullChoiceOf(p.path);
    pane.classList.toggle("focus", i === c.focus);
    const tag = pane.querySelector(".tag");
    tag.className = `tag c-${ch}`;
    tag.textContent = `${p.name} · score ${Math.round(p.score)}` + (p.best ? " · ★ best" : "") + (ch !== "none" ? ` · ${ch}` : "");
  });
}

function cullCmpFocus(i) { cull.cmp.focus = i; cullCmpPaint(); }

function cullBuildSide() {
  const box = $("cull-cmp-panes"); box.innerHTML = ""; cull.cmp.panes = [];
  cull.cmp.paths.forEach((path, i) => {
    const pane = cullPaneShell(path, i);
    const img = document.createElement("img"); img.className = "full"; img.alt = path; img.draggable = false;
    img.src = `/api/thumb?path=${encodeURIComponent(path)}&size=1024`;
    pane.prepend(img); box.append(pane); cull.cmp.panes.push(pane);
  });
  cullCmpPaint();
}

// Actual size: the finished render at 100% from the tile server, one shared pan.
const CULL_TILE = 512;
async function cullBuildActual() {
  const c = cull.cmp, box = $("cull-cmp-panes");
  box.innerHTML = ""; c.panes = [];
  const token = ++cull.token;
  c.paths.forEach((path, i) => {
    const pane = cullPaneShell(path, i);
    pane.classList.add("actual");
    const stage = document.createElement("div"); stage.className = "stage";
    pane.prepend(stage); box.append(pane); c.panes.push(pane);
    pane._tiles = new Map();
  });
  cullCmpPaint();
  await Promise.all(c.paths.map(async (path, i) => {
    const pane = c.panes[i];
    try {
      const meta = await post("/api/tile/meta", { path });
      if (meta.error) throw new Error(meta.error);
      const lvl = meta.levels.find(l => l.z === "100");
      pane._meta = { sid: meta.settings_id, w: lvl.width, h: lvl.height, cols: lvl.cols, rows: lvl.rows,
        size: meta.tile_size, renderable: lvl.renderable };
      if (!lvl.renderable) pane.querySelector(".tag").textContent += " · too large for 100%";
    } catch (err) { cullSay(err.message, true); }
  }));
  if (!c.on || c.mode !== "actual") return;
  cullCmpCenter();
  c.panes.forEach((pane, i) => cullPanHandlers(pane));
}

function cullCmpCenter() {
  const c = cull.cmp;
  const m = c.panes.find(p => p._meta)?._meta; if (!m) return;
  const pane = c.panes[0];
  c.view.ox = c.fpt[0] * m.w - pane.clientWidth / 2;
  c.view.oy = c.fpt[1] * m.h - pane.clientHeight / 2;
  cullPanApply();
}

function cullPanApply() {
  const c = cull.cmp, v = c.view;
  for (const pane of c.panes) {
    const m = pane._meta; if (!m) continue;
    const W = pane.clientWidth, H = pane.clientHeight;
    const ox = Math.max(0, Math.min(Math.max(0, m.w - W), v.ox));
    const oy = Math.max(0, Math.min(Math.max(0, m.h - H), v.oy));
    pane.querySelector(".stage").style.transform = `translate(${-ox}px, ${-oy}px)`;
    if (!m.renderable) continue;
    const x0 = Math.floor(ox / m.size), x1 = Math.min(m.cols - 1, Math.floor((ox + W) / m.size));
    const y0 = Math.floor(oy / m.size), y1 = Math.min(m.rows - 1, Math.floor((oy + H) / m.size));
    const want = new Set();
    for (let ty = y0; ty <= y1; ty++) for (let tx = x0; tx <= x1; tx++) {
      const key = `${tx},${ty}`; want.add(key);
      if (pane._tiles.has(key)) continue;
      const img = document.createElement("img");
      img.style.left = `${tx * m.size}px`; img.style.top = `${ty * m.size}px`; img.draggable = false;
      img.src = `/api/tile?photo=${encodeURIComponent(pane.dataset.path)}&z=100&x=${tx}&y=${ty}&sid=${m.sid}`;
      pane.querySelector(".stage").append(img); pane._tiles.set(key, img);
    }
    for (const [key, img] of pane._tiles) if (!want.has(key)) { img.remove(); pane._tiles.delete(key); }
  }
}

function cullPanHandlers(pane) {
  let drag = null;
  pane.addEventListener("pointerdown", (e) => {
    drag = { x: e.clientX, y: e.clientY, ox: cull.cmp.view.ox, oy: cull.cmp.view.oy };
    pane.setPointerCapture(e.pointerId); pane.style.cursor = "grabbing";
  });
  pane.addEventListener("pointermove", (e) => {
    if (!drag) return;
    cull.cmp.view.ox = drag.ox - (e.clientX - drag.x);
    cull.cmp.view.oy = drag.oy - (e.clientY - drag.y);
    cullPanApply();
  });
  pane.addEventListener("pointerup", () => { drag = null; pane.style.cursor = ""; });
}

// ---- Auto edit ---------------------------------------------------------------
async function cullAutoPreview() {
  const picks = [...cull.choice].filter(([, a]) => a === "pick").map(([p]) => p);
  if (!picks.length) return cullSay("Mark some photos as picks first (P)", true);
  const box = $("cull-auto-previews"); box.innerHTML = "";
  $("cull-auto-note").textContent = `Rendering a preview of ${Math.min(3, picks.length)} of ${picks.length} picks…`;
  $("cull-auto-go").disabled = true;
  $("cull-auto-dlg").showModal();
  const data = await post("/api/cull/autoedit/preview", { paths: picks.slice(0, 3) });
  if (data.error) { $("cull-auto-note").textContent = data.error; return; }
  const learned = data.model.photos
    ? `Shifted toward your style using ${data.model.photos} of your past corrections.`
    : "No learned style yet: this is plain auto tone + auto white balance. Your later adjustments teach it.";
  $("cull-auto-note").textContent = `Auto edit will write one history step (“Auto edit”) on each of ${picks.length} pick(s). ${learned} Nothing is written until you press Apply.`;
  for (const ph of data.photos) {
    const wrap = document.createElement("div"); wrap.className = "ap";
    wrap.innerHTML = `<figure><img alt="before"><figcaption>Before</figcaption></figure><figure><img alt="after"><figcaption>After</figcaption></figure><div class="ap-changes"></div>`;
    const [b, a] = wrap.querySelectorAll("img"); b.src = ph.before; a.src = ph.after;
    const ch = Object.entries(ph.changes).map(([k, v]) => `<b>${k}</b> ${Number(v.from ?? 0).toFixed(k === "exposure" ? 2 : 0)} → ${Number(v.to).toFixed(k === "exposure" ? 2 : 0)}`);
    wrap.querySelector(".ap-changes").innerHTML = `${cullEsc(cullPhoto(ph.path)?.name || ph.path)}: ` + (ch.join(" · ") || "no changes");
    box.append(wrap);
  }
  cull.autoPaths = picks;
  $("cull-auto-go").disabled = false;
}

function cullEsc(s) { return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

async function cullAutoApply() {
  const paths = cull.autoPaths || [];
  $("cull-auto-dlg").close();
  if (!paths.length) return;
  try {
    const r = await post("/api/cull/autoedit/apply", { paths });
    if (r.error) throw new Error(r.error);
    const j = await cullPollJob(r.job, (j) => { cullProgress(j.done, j.total); cullSay(`Auto edit ${j.done}/${j.total}…`); });
    cullProgress(0, 0);
    const res = j.result;
    if (res.batch) { cull.last = { kind: "autoedit", id: res.batch }; $("cull-undo").disabled = false; }
    const msg = `Auto edit applied to ${res.applied} photo(s)` + (res.failed.length ? `, ${res.failed.length} failed (${res.failed[0].error})` : "") + ". Undo batch is available.";
    cullSay(msg, res.failed.length > 0); setStatus(msg, res.failed.length > 0);
  } catch (err) { cullProgress(0, 0); cullSay(err.message, true); }
}

// ---- Learning panel ----------------------------------------------------------
async function cullLearning() {
  const s = await cullGet("/api/cull/stats");
  if (s.error) return cullSay(s.error, true);
  const a = s.accuracy, pm = s.pick_model, om = s.offset_model;
  const pct = (v) => v == null ? "–" : `${Math.round(v * 100)}%`;
  const typ = Object.entries(om.typical).map(([k, v]) => `${k} ${v.offset > 0 ? "+" : ""}${v.offset} (${v.samples})`).join(", ") || "none yet";
  $("cull-learn-body").innerHTML = `<dl>
    <dt>Reviewed photos</dt><dd>${a.decisions}</dd>
    <dt>Suggestion agrees with your final choice</dt><dd>${pct(a.overall)} overall, ${pct(a.recent)} over the last 50</dd>
    <dt>Personal pick model</dt><dd>${pm.pick_model ? `active, ${Math.round(pm.confidence * 100)}% weight` : `needs ${Math.max(0, 10 - pm.decisions)}+ more reviewed photos`}</dd>
    <dt>Personal reject model</dt><dd>${pm.reject_model ? "active" : "not enough rejects yet"}</dd>
    <dt>Auto edit style</dt><dd>${om.photos} corrected photo(s): ${cullEsc(typ)}</dd></dl>`;
  const svg = $("cull-acc");
  svg.innerHTML = "";
  const ns = "http://www.w3.org/2000/svg";
  const add = (tag, attrs, text) => { const el = document.createElementNS(ns, tag); for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v); if (text) el.textContent = text; svg.append(el); return el; };
  for (const y of [0, 0.5, 1]) { add("line", { x1: 24, x2: 296, y1: 70 - y * 60, y2: 70 - y * 60 }); add("text", { x: 2, y: 72 - y * 60 }, `${y * 100}%`); }
  if (a.series.length) {
    const n = a.series.length;
    const pts = a.series.map((p, i) => `${24 + (n === 1 ? 136 : (i / (n - 1)) * 272)},${70 - p.rate * 60}`).join(" ");
    add("polyline", { points: n === 1 ? `24,${70 - a.series[0].rate * 60} 296,${70 - a.series[0].rate * 60}` : pts });
    add("text", { x: 24, y: 79 }, "oldest"); add("text", { x: 262, y: 79 }, `${a.decisions} photos`);
  } else add("text", { x: 100, y: 42 }, "No reviewed photos yet");
  $("cull-learn-dlg").showModal();
}

async function cullDataAction(url, confirmText) {
  if (!confirm(confirmText)) return;
  const d = await post(url, {});
  if (d.error) return cullSay(d.error, true);
  setStatus(d.message); cullSay(d.message);
  $("cull-learn-dlg").close();
  await cullRefresh();
}

// ---- Wiring ------------------------------------------------------------------
$("lib-cull").addEventListener("click", cullOpen);
$("cull-close").addEventListener("click", cullHide);
$("cull-apply").addEventListener("click", cullApply);
$("cull-undo").addEventListener("click", cullUndo);
$("cull-compare").addEventListener("click", () => cullCompareOpen("side"));
$("cull-actual").addEventListener("click", () => cullCompareOpen("actual"));
$("cull-cmp-close").addEventListener("click", cullCompareClose);
$("cull-cmp-mode").addEventListener("click", () => cullCompareOpen(cull.cmp.mode === "side" ? "actual" : "side"));
$("cull-accept-rest").addEventListener("click", () => { cull.groups.forEach((g, gi) => { if (!cull.reviewed.has(g.id)) cullAcceptGroup(gi); }); });
$("cull-auto").addEventListener("click", cullAutoPreview);
$("cull-auto-cancel").addEventListener("click", () => $("cull-auto-dlg").close());
$("cull-auto-go").addEventListener("click", cullAutoApply);
$("cull-learn").addEventListener("click", cullLearning);
$("cull-learn-close").addEventListener("click", () => $("cull-learn-dlg").close());
$("cull-export").addEventListener("click", () => { location.href = "/api/cull/export"; });
$("cull-reset").addEventListener("click", () => cullDataAction("/api/cull/reset", "Delete everything cull has learned from your decisions and edits? Ratings and flags are not touched."));
$("cull-clear").addEventListener("click", () => cullDataAction("/api/cull/clear", "Delete all cull data for this library, including cached analysis and undo records?"));
$("cull-scope").addEventListener("change", cullRefresh);
$("cull-singles").addEventListener("change", cullRefresh);
window.addEventListener("resize", () => { if (cull.cmp.on && cull.cmp.mode === "actual") cullPanApply(); });
