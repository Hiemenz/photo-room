// ---- Library module ------------------------------------------------------
// Items come from /api/library one page at a time, already filtered and
// sorted by the server. Thumbnails load lazily as cells scroll into view and
// the next page loads when the sentinel at the end of the grid appears, so a
// large library never fetches (or decodes) more than what is on screen.
const LABEL_KEYS = { "6": "Red", "7": "Yellow", "8": "Green", "9": "Blue" };
let module_ = "develop";
let libState = { items: [], total: 0, libraryTotal: 0, loading: false, gen: 0 };
let libCollection = "";      // active collection id, "" for the whole library
let libFolder = null;        // active folder path ("" = root), null for no folder filter
let libFolderSub = true;     // include subfolders of the active folder
let libActive = null;        // path of the keyboard cursor
let libMulti = new Set();    // extra selected paths
let libThumbObs = null, libPageObs = null;
const LIB_PAGE = 200;

// [element id, query key] of every filter control in #lib-bar (also used to decide "is a filter active").
const LIB_FILTERS = [["lf-flag","flag"],["lf-label","label"],["lf-keyword","keyword"],["lf-camera","camera"],
  ["lf-lens","lens"],["lf-iso-min","iso_min"],["lf-iso-max","iso_max"],["lf-focal-min","focal_min"],
  ["lf-focal-max","focal_max"],["lf-filetype","filetype"],["lf-edit","edit"],["lf-gps","gps"],
  ["lf-from","date_from"],["lf-to","date_to"],["lf-q","q"]];
const LIB_FILTER_IDS = ["lf-rating", ...LIB_FILTERS.map(f => f[0])];

function libQuery(offset) {
  const q = new URLSearchParams({ offset, limit: LIB_PAGE,
    sort: $("lf-sort").value, dir: $("lf-dir").value });
  if (libCollection) q.set("collection", libCollection);
  if (libFolder !== null) { q.set("folder", libFolder); if (libFolderSub) q.set("subfolders", "1"); }
  const [rmin, rop] = $("lf-rating").value.split("|");
  if (rmin) { q.set("rating_min", rmin); if (rop) q.set("rating_op", rop); }
  for (const [id, key] of LIB_FILTERS) {
    if ($(id).value) q.set(key, $(id).value);
  }
  return q;
}

function fillSelect(id, values, anyLabel) {
  const sel = $(id), keep = sel.value;
  sel.innerHTML = `<option value="">${anyLabel}</option>`;
  for (const v of values) {
    const o = document.createElement("option");
    o.value = o.textContent = v;
    sel.appendChild(o);
  }
  sel.value = keep;
}

async function libLoad(reset) {
  if (libState.loading && !reset) return;
  if (reset) { libState.gen++; libState.items = []; $("lib-grid").innerHTML = ""; }
  else if (libState.items.length >= libState.total && libState.items.length) return;
  const gen = libState.gen;
  libState.loading = true;
  let data;
  try {
    data = await (await fetch("/api/library?" + libQuery(libState.items.length))).json();
  } catch { libState.loading = false; return setStatus("Could not load the library", true); }
  libState.loading = false;
  if (gen !== libState.gen) return;                       // a newer query superseded this one
  if (data.error) return setStatus(data.error, true);
  libState.total = data.total; libState.libraryTotal = data.library_total;
  if (reset || libState.seq === undefined) libState.seq = data.seq;
  if (data.facets) {
    fillSelect("lf-keyword", data.facets.keywords.map(k => k.keyword), "Any keyword");
    fillSelect("lf-camera", data.facets.cameras, "Any camera");
    fillSelect("lf-lens", data.facets.lenses || [], "Any lens");
    fillSelect("lf-filetype", data.facets.filetypes || [], "Any file type");
    $("kw-list").innerHTML = data.facets.keywords.map(k => `<option value="${k.keyword.replace(/"/g, "&quot;")}">`).join("");
  }
  libAppend(data.items);
  if (!libState.thumbsStarted) { libState.thumbsStarted = true; startThumbPrefetch(); }
  $("lib-count").textContent = `${libState.total} of ${libState.libraryTotal} photos`
    + (libMulti.size > 1 ? ` · ${libMulti.size} selected` : "");
  $("lib-rmcol").hidden = !(libCollection && !collectionById(libCollection)?.smart);
}

// Thumbnails for the whole library are built by background workers on the
// server; the grid stays paginated and never waits for them.
async function startThumbPrefetch() {
  try {
    const res = await fetch("/api/thumbs/prefetch", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    paintThumbProgress(await res.json());
  } catch { return; }
  const poll = setInterval(async () => {
    try {
      const st = await (await fetch("/api/thumbs/status")).json();
      paintThumbProgress(st);
      if (!st.active) clearInterval(poll);
    } catch { clearInterval(poll); }
  }, 1000);
}

function paintThumbProgress(st) {
  const box = $("thumb-progress");
  if (!st || st.error || !st.total || !st.active) { box.hidden = true; return; }
  box.hidden = false;
  $("thumb-label").textContent = `Thumbnails ${st.done}/${st.total}`;
  $("thumb-bar").max = st.total;
  $("thumb-bar").value = st.done;
}

function libCell(it) {
  const cell = document.createElement("div");
  const kind = it.kind || "raw";
  cell.className = "lcell" + (it.virtual ? " virtual" : "") + (kind === "video" ? " video" : "") +
    (it.editable === false ? " viewonly" : "");
  cell.dataset.path = it.path;
  cell.draggable = true;
  cell.addEventListener("dragstart", (e) => {
    if (!libMulti.has(it.path)) { libActive = it.path; libMulti = new Set(); libPaintAll(); }
    e.dataTransfer.setData("text/plain", "photo-room:" + JSON.stringify(libTargets()));
    e.dataTransfer.effectAllowed = "copy";
  });
  const img = document.createElement("img");
  img.alt = it.name;
  const ext = (it.name.split(".").pop() || "").toUpperCase();
  const ph = document.createElement("div"); ph.className = "ph";
  ph.textContent = kind === "video" ? "\u25B6 " + ext : "";
  const noThumb = () => { img.style.visibility = "hidden"; cell.classList.add("nothumb"); ph.textContent ||= ext; };
  if (kind === "video") { img.removeAttribute("src"); noThumb(); }
  else img.dataset.src = `/api/thumb?path=${encodeURIComponent(it.path)}`;
  img.addEventListener("error", noThumb);
  const tag = document.createElement("div"); tag.className = "kindtag";
  tag.textContent = kind === "raw" ? "" : (kind === "video" ? "VIDEO" : ext + " \u00B7 view only");
  const badge = document.createElement("div"); badge.className = "badge";
  const meta = document.createElement("div"); meta.className = "meta";
  meta.innerHTML = `<span class="dot"></span><span class="nm"></span><span class="st"></span>`;
  meta.querySelector(".nm").textContent = it.name;
  cell.append(img, ph, tag, badge, meta);
  cell.addEventListener("click", (e) => libClick(e, it.path));
  cell.addEventListener("dblclick", () => {
    if (it.editable === false) {
      toast(kind === "video" ? "Videos can't be edited" : "JPEG/HEIC files are view-only",
        { kind: "info", why: "photo-room edits raw files. This file is listed so you can see, rate and flag it." });
      return;
    }
    openInDevelop(it.path);
  });
  if (kind !== "video") libThumbObs.observe(img);
  return cell;
}

function libAppend(items) {
  const grid = $("lib-grid");
  const sentinel = $("lib-sentinel");
  if (sentinel) sentinel.remove();
  for (const it of items) {
    libState.items.push(it);
    ratings[it.path] = it.rating;
    grid.appendChild(libCell(it));
    libPaintCell(it.path);
  }
  const s = document.createElement("div");
  s.id = "lib-sentinel";
  grid.appendChild(s);
  libPageObs.disconnect();
  if (libState.items.length < libState.total) libPageObs.observe(s);
}

function libItem(path) { return libState.items.find(i => i.path === path); }

function libPaintCell(path) {
  const it = libItem(path);
  const cell = $("lib-grid").querySelector(`.lcell[data-path="${CSS.escape(path)}"]`);
  if (!it || !cell) return;
  cell.classList.toggle("pick", it.flag === "pick");
  cell.classList.toggle("reject", it.flag === "reject");
  cell.classList.toggle("active", path === libActive);
  cell.classList.toggle("multi", libMulti.has(path));
  cell.querySelector(".badge").textContent = it.flag === "pick" ? "⚑" : it.flag === "reject" ? "✕" : "";
  cell.querySelector(".dot").className = "dot" + (it.label ? " lbl-" + it.label : "");
  cell.querySelector(".dot").style.visibility = it.label ? "visible" : "hidden";
  cell.querySelector(".st").textContent = it.rating > 0 ? "★".repeat(it.rating) : "";
  if (typeof libPaintStack === "function") libPaintStack(cell, it);
  cell.title = [it.name, it.camera, it.date, it.keywords.join(", ")].filter(Boolean).join("\n");
}

function libPaintAll() { libState.items.forEach(i => libPaintCell(i.path)); }

function libTargets() {
  if (libMulti.size) return [...libMulti];
  return libActive ? [libActive] : [];
}

function libClick(e, path) {
  if (e.metaKey || e.ctrlKey) {
    libMulti.has(path) ? libMulti.delete(path) : libMulti.add(path);
    libActive = path;
  } else if (e.shiftKey && libActive) {
    const a = libState.items.findIndex(i => i.path === libActive);
    const b = libState.items.findIndex(i => i.path === path);
    libMulti = new Set(libState.items.slice(Math.min(a, b), Math.max(a, b) + 1).map(i => i.path));
  } else {
    libActive = path; libMulti = new Set();
  }
  libPaintAll();
  $("lib-count").textContent = `${libState.total} of ${libState.libraryTotal} photos`
    + (libMulti.size > 1 ? ` · ${libMulti.size} selected` : "");
}

function libMove(delta) {
  if (!libState.items.length) return;
  let i = libState.items.findIndex(x => x.path === libActive);
  i = Math.max(0, Math.min(libState.items.length - 1, (i < 0 ? 0 : i + delta)));
  libActive = libState.items[i].path; libMulti = new Set();
  libPaintAll();
  $("lib-grid").querySelector(`.lcell[data-path="${CSS.escape(libActive)}"]`)?.scrollIntoView({ block: "nearest" });
}

function libColumns() {
  return getComputedStyle($("lib-grid")).gridTemplateColumns.split(" ").length || 1;
}

async function libFlag(flag, paths = libTargets()) {
  if (!paths.length) return;
  const data = await post("/api/flag", { paths, flag });
  if (data.error) return setStatus(data.error, true);
  for (const p of paths) {
    const it = libItem(p); if (!it) continue;
    if (flag === "reject") it.rating = -1;
    else if (it.rating === -1) it.rating = 0;
    it.flag = flag; ratings[p] = it.rating;
  }
  setStatus(data.message); libPaintAll(); libRefilterSoon();
}

async function libLabel(label, paths = libTargets()) {
  if (!paths.length) return;
  const data = await post("/api/label", { paths, label });
  if (data.error) return setStatus(data.error, true);
  paths.forEach(p => { const it = libItem(p); if (it) it.label = data.label; });
  setStatus(data.message); libPaintAll(); libRefilterSoon();
}

async function libRate(rating, paths) {
  for (const p of paths) {
    const data = await post("/api/rate", { path: p, rating });
    if (data.error) return setStatus(data.error, true);
    const it = libItem(p);
    if (it) { it.rating = rating; it.flag = rating === REJECTED ? "reject" : (it.flag === "reject" ? "none" : it.flag); }
    ratings[p] = rating;
  }
  libPaintAll(); libRefilterSoon();
}

async function libKeywords(remove) {
  const paths = libTargets();
  const words = $("lib-kw").value.split(",").map(s => s.trim()).filter(Boolean);
  if (!paths.length || !words.length) return;
  const data = await post("/api/keywords", remove ? { paths, remove: words } : { paths, add: words });
  if (data.error) return setStatus(data.error, true);
  $("lib-kw").value = "";
  setStatus(data.message);
  libLoad(true);        // keywords change the facet list and any keyword filter
}

// Keep the grid honest after an edit: if the change would drop an item out of
// the current filter, re-query (debounced so rapid culling does not thrash).
let libRefilterTimer = null;
function libRefilterSoon() {
  const filtering = libCollection || LIB_FILTER_IDS.some(id => $(id).value)
    || $("lf-sort").value !== "name";
  if (!filtering) return;
  clearTimeout(libRefilterTimer);
  libRefilterTimer = setTimeout(() => libLoad(true), 600);
}

function setModule(name) {
  module_ = name;
  $("lib").classList.toggle("on", name === "library");
  $("mod-library").classList.toggle("on", name === "library");
  $("mod-develop").classList.toggle("on", name === "develop");
  closeOverlays();
  if (name === "library") {
    if (current && !libActive) libActive = current;
    const loading = libLoad(true);
    colLoad();
    return loading;
  }
}

function openInDevelop(path) {
  setModule("develop");
  if (path && path !== current) openFile(path);
}

document.querySelectorAll("#modules button").forEach(b =>
  b.addEventListener("click", () => b.dataset.mod === "library" ? setModule("library") : openInDevelop(libActive)));

libThumbObs = new IntersectionObserver((entries, obs) => {
  for (const en of entries) {
    if (!en.isIntersecting) continue;
    const img = en.target;
    if (img.dataset.src) { img.src = img.dataset.src; delete img.dataset.src; }
    obs.unobserve(img);
  }
}, { root: $("lib-grid"), rootMargin: "400px" });
libPageObs = new IntersectionObserver((entries) => {
  if (entries.some(e => e.isIntersecting)) libLoad(false);
}, { root: $("lib-grid"), rootMargin: "600px" });

[...LIB_FILTER_IDS.filter(id => id !== "lf-q"), "lf-sort", "lf-dir"].forEach(id =>
  $(id).addEventListener("change", (e) => { e.target.blur(); libLoad(true); }));
let libSearchTimer = null;
$("lf-q").addEventListener("input", () => { clearTimeout(libSearchTimer); libSearchTimer = setTimeout(() => libLoad(true), 250); });
$("lf-clear").addEventListener("click", () => {
  LIB_FILTER_IDS.forEach(id => $(id).value = "");
  libLoad(true);
});
document.querySelectorAll("#lib-foot .swatch").forEach(b =>
  b.addEventListener("click", () => libLabel(b.dataset.label)));
$("lib-kw").addEventListener("keydown", (e) => { if (e.key === "Enter") libKeywords(false); });
$("lib-kw-del").addEventListener("click", () => libKeywords(true));

// Library keys. Returns true when the key was handled.
function libraryKey(e) {
  const k = e.key.toLowerCase();
  if (module_ === "library" && typeof cullKey === "function" && cullKey(e, k)) return true;
  if (module_ === "library" && overlayKey(e, k)) return true;
  if (module_ === "library") {
    if (k === "c") { e.preventDefault(); openCompare(); return true; }
    if (k === "n") { e.preventDefault(); openSurvey(); return true; }
    if (k === "k") { e.preventDefault(); cullOpen(); return true; }
    if (k === "v") { e.preventDefault(); libVirtualCopy(); return true; }
    if (k === "e" || k === "d" || k === "enter") { e.preventDefault(); openInDevelop(libActive); return true; }
    if (k === "g") return true;
    if (k === "s") { e.preventDefault(); libToggleStack(); return true; }
    if (k === "p") { libFlag("pick"); return true; }
    if (k === "u") { libFlag("none"); return true; }
    if (k === "x") { libFlag("reject"); return true; }
    if (LABEL_KEYS[k]) { libLabel(LABEL_KEYS[k]); return true; }
    if (k >= "0" && k <= "5" && k.length === 1) { libRate(parseInt(k, 10), libTargets()); return true; }
    const cols = libColumns();
    const moves = { arrowright: 1, arrowleft: -1, arrowdown: cols, arrowup: -cols };
    if (k in moves) { e.preventDefault(); libMove(moves[k]); return true; }
    return false;
  }
  if (k === "g") { e.preventDefault(); if (current) libActive = current; setModule("library"); return true; }
  return false;
}
