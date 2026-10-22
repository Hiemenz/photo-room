// ---- Live Library: the change feed, and Quick look --------------------------------
// The server keeps an in-memory index of the library and logs every change to a
// photo (a flag, rating, label, keyword, a new or removed file) with a sequence
// number. This page listens to that log (Server-Sent Events), so culling in
// Smart cull, in another window, or from the command line shows up in the grid
// without a refresh, and photos that appear (an import, a card ingest) are added.
let liveSource = null, liveBadge = null, liveReloadTimer = 0, liveFilesTimer = 0;
const liveStats = { events: 0, patched: 0, reloads: 0 };       // read by the tests

function liveFiltering() {
  return !!libCollection || LIB_FILTER_IDS
    .some(id => $(id).value) || $("lf-sort").value !== "name";
}

// Reload the grid in place: same filters, same scroll position, new cells flagged.
async function libSoftReload() {
  if (module_ !== "library") return;                         // setModule reloads when it is opened
  liveStats.reloads++;
  const grid = $("lib-grid"), top = grid.scrollTop;
  const had = libState.items.length, known = new Set(libState.items.map(i => i.path));
  await libLoad(true);
  while (libState.items.length < Math.min(had, libState.total) && !libState.loading) {
    const n = libState.items.length; await libLoad(false); if (libState.items.length === n) break;
  }
  grid.scrollTop = top;
  if (known.size) grid.querySelectorAll(".lcell").forEach(c => { if (!known.has(c.dataset.path)) c.classList.add("fresh"); });
}
function liveReloadSoon() { clearTimeout(liveReloadTimer); liveReloadTimer = setTimeout(libSoftReload, 400); }
function liveFilesSoon() {
  clearTimeout(liveFilesTimer);
  liveFilesTimer = setTimeout(async () => { await loadFiles(); if (filterMode !== "all") applyFilter(); }, 500);
}

function liveApply(body) {
  liveStats.events++;
  if (body.seq !== undefined) libState.seq = body.seq;
  if (body.reset) { liveReloadSoon(); liveFilesSoon(); return; }
  let structural = false;
  for (const ch of body.changes || []) {
    if (ch.removed) { structural = true; delete ratings[ch.path]; continue; }
    const it = libItem(ch.path);
    if (!it) { structural = true; if (ch.item) ratings[ch.path] = ch.item.rating; continue; }
    if (JSON.stringify({ ...it, ...ch.item }) === JSON.stringify(it)) continue;   // our own edit coming back
    const before = { ...it };
    Object.assign(it, ch.item);
    ratings[ch.path] = it.rating;
    liveStats.patched++;
    if (module_ === "library") libPaintCell(ch.path);
    if (liveFiltering() && (before.rating !== it.rating || before.flag !== it.flag || before.label !== it.label
        || JSON.stringify(before.keywords) !== JSON.stringify(it.keywords))) structural = true;
  }
  if (structural) { liveReloadSoon(); liveFilesSoon(); }
  else if (module_ === "develop") paintRating();
  if (filterMode !== "all" && !structural) applyFilter();
}

function liveSetBadge(ok) {
  if (!liveBadge) {
    liveBadge = document.createElement("span");
    liveBadge.id = "live-badge";
    liveBadge.textContent = "Live";
    $("lib-count").after(liveBadge);
  }
  liveBadge.classList.toggle("off", !ok);
  liveBadge.title = ok ? "This grid updates by itself when photos change or arrive" : "Reconnecting to the library feed…";
}

function liveStart() {
  if (liveSource || !window.EventSource) return;
  const since = libState.seq !== undefined ? `?since=${libState.seq}` : "";
  liveSource = new EventSource("/api/library/events" + since);
  liveSource.addEventListener("changes", (ev) => { try { liveApply(JSON.parse(ev.data)); } catch (e) { /* ignore a malformed event */ } });
  liveSource.onopen = () => liveSetBadge(true);
  liveSource.onerror = () => liveSetBadge(false);
  liveSetBadge(true);
}
window.addEventListener("beforeunload", () => { if (liveSource) liveSource.close(); });

// Start once the first page of the library has been read (it carries the sequence number).
(function wrap() {
  const set = setModule;
  setModule = function (name) {
    const r = set.apply(this, arguments);
    if (name === "library") Promise.resolve(r).then(liveStart);
    return r;
  };
})();
liveStart();

// ---- Quick look (Space) ------------------------------------------------------------
// Large view of the highlighted photo without opening Develop. Uses the smart
// preview (2048 px, built after an import or on first look) and falls back to the
// thumbnail while that is being made.
let loupeOn = false;
async function smartReady(path) {
  try { return (await (await fetch(`/api/smartpreview/status?path=${encodeURIComponent(path)}`)).json()).ready === true; }
  catch (e) { return false; }
}
async function loupeShow() {
  const path = libActive; if (!path) return;
  const it = libItem(path), name = it ? it.name : path;
  const img = $("loupe-img"), info = $("loupe-info");
  const token = ++loupeShow.n;
  info.textContent = `${name} — loading…`;
  const showSmart = () => {
    const big = new Image();
    big.onload = () => { if (token === loupeShow.n) { img.src = big.src; info.textContent = `${name} — smart preview`; } };
    big.src = `/api/smartpreview?path=${encodeURIComponent(path)}&t=${Date.now()}`;
  };
  if (await smartReady(path)) { if (token === loupeShow.n) showSmart(); return; }
  if (token !== loupeShow.n) return;
  img.src = `/api/thumb?path=${encodeURIComponent(path)}`;
  info.textContent = `${name} — thumbnail (building a sharper smart preview…)`;
  try { await post("/api/smartpreview/build", { paths: [path] }); } catch (e) { return; }
  for (let i = 0; i < 30 && token === loupeShow.n; i++) {
    await new Promise(r => setTimeout(r, 700));
    if (token === loupeShow.n && await smartReady(path)) { showSmart(); return; }
  }
}
loupeShow.n = 0;

// Info overlay (I cycles: off, basic, full). Basic comes from the library row; full adds the
// exposure and the place, read on demand with exiftool (/api/metadata), cached per photo.
const LOUPE_INFO = ["off", "basic", "full"];
let loupeInfoMode = "basic";
try { const v = localStorage.getItem("loupeInfo"); if (LOUPE_INFO.includes(v)) loupeInfoMode = v; } catch (e) {}
const loupeMeta = new Map();
function loupeInfoLines(it, md) {
  const lines = [];
  const stars = it.rating > 0 ? "\u2605".repeat(it.rating) : it.rating === -1 ? "rejected" : "";
  lines.push([it.name, stars, it.flag === "pick" ? "picked" : "", it.label].filter(Boolean).join("  "));
  lines.push([it.camera, it.lens].filter(Boolean).join("  \u00B7  "));
  if (md) {
    const i = md.info || {};
    lines.push([i.focal_length, i.aperture, i.shutter, i.iso ? "ISO " + i.iso : "", i.dimensions].filter(Boolean).join("  "));
    if (i.gps) lines.push(i.gps.text);
  } else {
    lines.push([it.focal ? it.focal + " mm" : "", it.iso ? "ISO " + it.iso : ""].filter(Boolean).join("  "));
  }
  lines.push([it.date, it.keywords?.length ? it.keywords.join(", ") : ""].filter(Boolean).join("  \u00B7  "));
  return lines.filter(Boolean);
}
async function loupeInfoPaint() {
  const box = $("loupe-ov"), path = libActive, it = path && libItem(path);
  if (!loupeOn || loupeInfoMode === "off" || !it) { box.hidden = true; return; }
  const paint = (md) => { if (path === libActive && loupeOn) { box.textContent = loupeInfoLines(it, md).join("\n"); box.hidden = false; } };
  paint(loupeMeta.get(path));
  if (loupeInfoMode === "full" && !loupeMeta.has(path)) {
    try {
      const d = await (await fetch("/api/metadata?path=" + encodeURIComponent(path))).json();
      if (d.items && d.items[0]) { loupeMeta.set(path, d.items[0]); paint(d.items[0]); }
    } catch (e) { /* the basic lines stay */ }
  }
}
function loupeInfoCycle() {
  loupeInfoMode = LOUPE_INFO[(LOUPE_INFO.indexOf(loupeInfoMode) + 1) % LOUPE_INFO.length];
  try { localStorage.setItem("loupeInfo", loupeInfoMode); } catch (e) {}
  setStatus("Loupe info: " + loupeInfoMode);
  loupeInfoPaint();
}
function loupeSet(on) {
  loupeOn = on && module_ === "library" && !!libActive;
  $("loupe").hidden = !loupeOn;
  if (loupeOn) { loupeShow(); loupeInfoPaint(); } else { loupeShow.n++; $("loupe-ov").hidden = true; }
}
$("loupe").addEventListener("click", () => loupeSet(false));
document.addEventListener("keydown", (e) => {
  if (module_ !== "library" || e.metaKey || e.ctrlKey || e.altKey) return;
  if (typingTarget(e.target) || document.querySelector("dialog[open]")) return;
  if (document.querySelector("#cmp.on, #svy.on, #cull.on")) return;
  if (e.key === " ") { e.preventDefault(); e.stopImmediatePropagation(); loupeSet(!loupeOn); return; }
  if (!loupeOn) return;
  if (e.key === "i" || e.key === "I") { e.preventDefault(); e.stopImmediatePropagation(); loupeInfoCycle(); return; }
  if (e.key === "Escape") { e.preventDefault(); e.stopImmediatePropagation(); loupeSet(false); }
  else if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
    e.preventDefault(); e.stopImmediatePropagation();
    libMove(e.key === "ArrowRight" ? 1 : -1); loupeShow(); loupeInfoPaint();
  }
}, true);
(function wrap() {
  const set = setModule;
  setModule = function (name) { if (name !== "library") loupeSet(false); return set.apply(this, arguments); };
})();
