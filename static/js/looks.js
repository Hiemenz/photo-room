// ---- Presets, film styles (LUTs) and match-reference ----------------------
// A preset is a set of ordinary slider values, so applying one just edits `values`
// and records one history step. Thumbnails are rendered on the server from the
// photo's cached 160 px thumbnail; hover and the Amount slider render the open photo
// through /api/looks/preset/preview without touching `values` until the user commits.
const looks = {
  presets: [], groups: [], luts: [], category: "",
  active: null,          // id of the preset last applied to this photo (drives Amount)
  base: null,            // values before that preset was applied
  result: null,          // JSON of the values right after it, to notice later edits
  match: null,           // {preset, report} from "Match reference look"
  matchRef: null,        // {data} | {path}
  thumbFor: null,        // photo the tiles were last rendered for
  hoverSrc: null, hoverTimer: null,
};
const LOOK_DEFAULTS = { lut: "", lut_amount: 100 };

function lookBaseline() { return baselineTemp || 5500; }
function lookNote(id, msg, isErr = false) {
  const el = $(id); el.textContent = msg || ""; el.classList.toggle("err", !!isErr);
}
function lookGet(url) { return fetch(url).then(r => r.json()); }

async function loadLooks() {
  const data = await lookGet("/api/looks/presets");
  looks.presets = data.presets || [];
  looks.groups = data.groups || [];
  looks.luts = data.luts || [];
  renderCategories();
  renderPresetGrid();
  renderLutGrid();
}

function renderCategories() {
  const sel = $("looks-cat");
  const cats = [...new Set(looks.presets.map(p => p.builtin ? p.category : "My presets"))];
  sel.innerHTML = "";
  for (const c of ["", ...cats]) {
    const o = document.createElement("option");
    o.value = c; o.textContent = c || "All";
    sel.appendChild(o);
  }
  sel.value = cats.includes(looks.category) ? looks.category : "";
}

function presetThumb(p) {
  if (!current) return "";
  const q = new URLSearchParams({ path: current, id: p.id, amount: 100, baseline_temp: lookBaseline(), v: p.rev });
  return `/api/looks/preset/thumb?${q}`;
}
function lutThumb(l) {
  if (!current) return "";
  const q = new URLSearchParams({ path: current, id: l.id, amount: 100, baseline_temp: lookBaseline() });
  return `/api/looks/lut/thumb?${q}`;
}

function makeTile(label, id, src, onClick) {
  const b = document.createElement("button");
  b.type = "button"; b.className = "look-tile"; b.dataset.id = id; b.title = label;
  if (src !== null) {
    const img = document.createElement("img");
    img.loading = "lazy"; img.alt = ""; img.dataset.src = src;
    if (src) img.src = src;
    b.appendChild(img);
  }
  const span = document.createElement("span"); span.textContent = label;
  b.appendChild(span);
  b.addEventListener("click", onClick);
  return b;
}

function renderPresetGrid() {
  const grid = $("preset-grid");
  grid.innerHTML = "";
  looks.thumbFor = current;
  for (const p of looks.presets) {
    const group = p.builtin ? p.category : "My presets";
    if (looks.category && looks.category !== group) continue;
    const tile = makeTile(p.name, p.id, presetThumb(p), () => applyPresetToPhoto({ id: p.id }, p.name));
    tile.classList.toggle("active", p.id === looks.active);
    tile.addEventListener("mouseenter", () => hoverPreview(p));
    tile.addEventListener("mouseleave", endHover);
    tile.addEventListener("focus", () => hoverPreview(p));
    tile.addEventListener("blur", endHover);
    grid.appendChild(tile);
  }
  syncPresetButtons();
}

function renderLutGrid() {
  const grid = $("lut-grid");
  grid.innerHTML = "";
  const none = makeTile("None", "", null, () => chooseLut(""));
  none.classList.add("none");
  grid.appendChild(none);
  for (const l of looks.luts) {
    grid.appendChild(makeTile(l.name, l.id, lutThumb(l), () => chooseLut(l.id)));
  }
  paintLooks();
}

// Called by paintAll(): keep the panel in step with `values` (history jumps, a new photo, reset).
function paintLooks() {
  if (current !== looks.thumbFor && looks.presets.length) {
    // A different photo: tiles show this photo, and an earlier preset no longer applies to it.
    looks.active = null; looks.base = null; looks.result = null;
    renderPresetGrid();
    renderLutGridThumbsOnly();
  }
  const lut = values.lut || "";
  const amt = values.lut_amount ?? 100;
  $("lut-amount").value = amt; $("lut-amount-val").textContent = Math.round(amt);
  $("lut-amount").disabled = !current || !lut;
  document.querySelectorAll("#lut-grid .look-tile").forEach(t => t.classList.toggle("active", t.dataset.id === lut));
  const entry = looks.luts.find(l => l.id === lut);
  $("lut-export").disabled = !entry;
  $("lut-rename").disabled = !entry || entry.builtin;
  $("lut-delete").disabled = !entry || entry.builtin;
  lookNote("lut-note", lut && !entry ? "This photo uses a LUT that is not in your library. Import its .cube file again." : "",
           lut && !entry);
  if (values.preset_name && !looks.active) {
    lookNote("looks-note", `Last preset: ${values.preset_name}` + (values.preset_amount < 100 ? ` (${values.preset_amount}%)` : ""));
  }
  syncPresetButtons();
}

function renderLutGridThumbsOnly() {
  const imgs = document.querySelectorAll("#lut-grid .look-tile:not(.none) img");
  looks.luts.forEach((l, i) => { if (imgs[i]) imgs[i].src = lutThumb(l); });
}

function syncPresetButtons() {
  const p = looks.presets.find(x => x.id === looks.active);
  const editable = looks.active && looks.result === JSON.stringify(values);
  $("looks-amount").disabled = !current || !editable;
  $("looks-apply-sel").disabled = !current || !looks.active || ![...selected].some(x => x !== current);
  $("looks-export").disabled = !looks.presets.some(x => !x.builtin) && !(p && !p.builtin);
  $("looks-rename").disabled = !p || p.builtin;
  $("looks-delete").disabled = !p || p.builtin;
  document.querySelectorAll("#preset-grid .look-tile").forEach(t => t.classList.toggle("active", t.dataset.id === looks.active));
}

// ---- Applying one preset ---------------------------------------------------
function presetBody(ref, amount, adjustments) {
  return { ...ref, amount, adjustments, baseline_temp: lookBaseline() };
}

async function applyPresetToPhoto(ref, name) {
  if (!current) return;
  await commitChain;
  const base = JSON.parse(JSON.stringify(values));
  const d = await post("/api/looks/preset/apply", presetBody(ref, 100, base));
  if (d.error) return setStatus(d.error, true);
  looks.active = ref.id || null; looks.base = base;
  $("looks-amount").value = 100; $("looks-amount-val").textContent = "100";
  values = d.adjustments;
  looks.result = JSON.stringify(values);
  paintAll(); schedulePreview();
  commit(d.label);
  setStatus(`Applied preset '${name}' — not saved yet`);
  syncPresetButtons();
}

$("looks-amount").addEventListener("input", () => {
  $("looks-amount-val").textContent = $("looks-amount").value;
  if (!looks.active || !current) return;
  clearTimeout(looks.hoverTimer);
  looks.hoverTimer = setTimeout(() => renderPresetAmount(false), 30);
});
$("looks-amount").addEventListener("change", () => renderPresetAmount(true));

async function renderPresetAmount(commitIt) {
  if (!looks.active || !looks.base) return;
  const amount = +$("looks-amount").value;
  if (!commitIt) {
    const d = await post("/api/looks/preset/preview", { ...presetBody({ id: looks.active }, amount, looks.base), path: current, seq: ++previewSeq });
    if (d.preview && !d.stale && !comparing) { $("preview").src = d.preview; drawHistogram(d.histogram); }
    return;
  }
  const d = await post("/api/looks/preset/apply", presetBody({ id: looks.active }, amount, looks.base));
  if (d.error) return setStatus(d.error, true);
  values = d.adjustments;
  looks.result = JSON.stringify(values);
  paintAll(); schedulePreview();
  commit(d.label);
  setStatus(`${d.label} — not saved yet`);
  syncPresetButtons();
}

// ---- Hover preview ----------------------------------------------------------
function hoverPreview(p) {
  if (!current || comparing) return;
  clearTimeout(looks.hoverTimer);
  looks.hoverTimer = setTimeout(async () => {
    if (looks.hoverSrc === null) looks.hoverSrc = $("preview").src;
    const d = await post("/api/looks/preset/preview", {
      ...presetBody({ id: p.id }, 100, values), path: current, seq: ++previewSeq });
    if (looks.hoverSrc !== null && d.preview && !d.stale) $("preview").src = d.preview;
  }, 140);
}
function endHover() {
  clearTimeout(looks.hoverTimer);
  previewSeq++;                                  // a preview still in flight is now stale
  if (looks.hoverSrc !== null) { $("preview").src = looks.hoverSrc; looks.hoverSrc = null; }
}

// ---- Apply to the selection --------------------------------------------------
$("looks-apply-sel").addEventListener("click", async () => {
  if (!looks.active) return;
  const paths = [...selected].filter(p => p !== current);
  if (!paths.length) return;
  const amount = +$("looks-amount").value;
  setStatus(`Applying to ${paths.length} file(s)…`);
  const d = await post("/api/looks/preset/apply_batch", { id: looks.active, amount, paths });
  if (d.error) return setStatus(d.error, true);
  setStatus(d.message, !d.ok);
  if (d.failed.length) console.warn("Preset batch failures:", d.failed);
});

// ---- Category, save, rename, delete ------------------------------------------
$("looks-cat").addEventListener("change", (e) => { looks.category = e.target.value; renderPresetGrid(); });
$("looks-tabs").addEventListener("click", (e) => {
  const tab = e.target.dataset.tab; if (!tab) return;
  document.querySelectorAll("#looks-tabs button").forEach(b => b.classList.toggle("active", b === e.target));
  for (const t of ["presets", "luts", "match"]) $(`looks-${t}`).hidden = t !== tab;
});

$("preset-save").addEventListener("click", () => {
  if (!current) return;
  const box = $("looks-save-groups");
  box.innerHTML = "";
  for (const g of looks.groups) {
    const l = document.createElement("label");
    l.innerHTML = `<input type="checkbox" value="${g.id}"> <span></span>`;
    l.querySelector("input").checked = g.default;
    l.querySelector("span").textContent = g.label;
    box.appendChild(l);
  }
  $("looks-save-name").value = "";
  $("looks-save-dlg").returnValue = "";
  $("looks-save-dlg").showModal();
  $("looks-save-name").focus();
});

async function saveFromDialog(extra = {}) {
  const groups = [...document.querySelectorAll("#looks-save-groups input:checked")].map(i => i.value);
  const name = $("looks-save-name").value.trim();
  if (!name) return setStatus("A preset needs a name", true);
  let d = await post("/api/looks/preset/save", { name, adjustments: values, groups, baseline_temp: lookBaseline(), ...extra });
  if (d.exists && confirm(`A preset called '${name}' already exists. Replace it?`)) {
    d = await post("/api/looks/preset/save", { name, adjustments: values, groups, baseline_temp: lookBaseline(), overwrite: true, ...extra });
  }
  if (d.error) return setStatus(d.error, true);
  looks.presets = d.presets; renderCategories(); renderPresetGrid();
  setStatus(d.message);
}
$("looks-save-dlg").addEventListener("close", () => {
  if ($("looks-save-dlg").returnValue === "ok") saveFromDialog();
});
$("looks-save-name").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); $("looks-save-ok").click(); }
});

$("looks-rename").addEventListener("click", async () => {
  const p = looks.presets.find(x => x.id === looks.active); if (!p || p.builtin) return;
  const name = prompt("New name", p.name); if (!name) return;
  const d = await post("/api/looks/preset/rename", { id: p.id, name });
  if (d.error) return setStatus(d.error, true);
  looks.presets = d.presets; looks.active = d.id; renderCategories(); renderPresetGrid();
});
$("looks-delete").addEventListener("click", async () => {
  const p = looks.presets.find(x => x.id === looks.active); if (!p || p.builtin) return;
  if (!confirm(`Delete preset '${p.name}'?`)) return;
  const d = await post("/api/looks/preset/delete", { id: p.id });
  if (d.error) return setStatus(d.error, true);
  looks.presets = d.presets; looks.active = null; renderCategories(); renderPresetGrid();
  setStatus(d.message);
});

// ---- Import / export ---------------------------------------------------------
function toBase64(buf) {
  let s = ""; const bytes = new Uint8Array(buf);
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}
$("looks-import").addEventListener("click", () => $("looks-import-file").click());
$("looks-import-file").addEventListener("change", async (e) => {
  const file = e.target.files[0]; e.target.value = "";
  if (!file) return;
  const d = await post("/api/looks/preset/import", { filename: file.name, data: toBase64(await file.arrayBuffer()) });
  if (d.error) { lookNote("looks-note", d.error, true); return setStatus(d.error, true); }
  looks.presets = d.presets; renderCategories(); renderPresetGrid();
  lookNote("looks-note", d.skipped.length ? `Not mapped: ${d.skipped.join(", ")}` : "");
  setStatus(d.message);
});
$("looks-export").addEventListener("click", () => {
  const p = looks.presets.find(x => x.id === looks.active);
  const ids = p && !p.builtin ? [p.id] : looks.presets.filter(x => !x.builtin).map(x => x.id);
  if (ids.length) window.location = `/api/looks/preset/export?ids=${encodeURIComponent(ids.join(","))}`;
});

// ---- Film styles (LUTs) ------------------------------------------------------
async function chooseLut(id) {
  if (!current) return;
  await commitChain;
  values.lut = id;
  values.lut_amount = id ? +$("lut-amount").value || 100 : 100;
  const name = id ? (looks.luts.find(l => l.id === id)?.name || id) : "none";
  paintLooks(); schedulePreview();
  commit(id ? `Film style: ${name}` : "Film style: none");
  setStatus(id ? `Film style '${name}' — not saved yet` : "Removed film style — not saved yet");
}
$("lut-amount").addEventListener("input", () => {
  values.lut_amount = +$("lut-amount").value;
  $("lut-amount-val").textContent = $("lut-amount").value;
  schedulePreview();
});
$("lut-amount").addEventListener("change", () => commit(`Film style intensity ${$("lut-amount").value}`));

$("lut-import").addEventListener("click", () => $("lut-file").click());
$("lut-file").addEventListener("change", async (e) => {
  const file = e.target.files[0]; e.target.value = "";
  if (!file) return;
  const d = await post("/api/looks/lut/import", {
    name: file.name.replace(/\.cube$/i, ""), text: await file.text(), space: $("lut-space").value });
  if (d.error) { lookNote("lut-note", d.error, true); return setStatus(d.error, true); }
  looks.luts = d.luts; renderLutGrid();
  lookNote("lut-note", d.message);
});
$("lut-rename").addEventListener("click", async () => {
  const l = looks.luts.find(x => x.id === values.lut); if (!l || l.builtin) return;
  const name = prompt("New name", l.name); if (!name) return;
  const d = await post("/api/looks/lut/rename", { id: l.id, name });
  if (d.error) return setStatus(d.error, true);
  looks.luts = d.luts; renderLutGrid();
});
$("lut-delete").addEventListener("click", async () => {
  const l = looks.luts.find(x => x.id === values.lut); if (!l || l.builtin) return;
  if (!confirm(`Delete LUT '${l.name}' from the library? Photos that use it will show a warning until it is imported again.`)) return;
  const d = await post("/api/looks/lut/delete", { id: l.id });
  if (d.error) return setStatus(d.error, true);
  looks.luts = d.luts; renderLutGrid(); schedulePreview();
});
$("lut-export").addEventListener("click", () => {
  if (values.lut) window.location = `/api/looks/lut/export?id=${encodeURIComponent(values.lut)}`;
});

// ---- Match reference look ----------------------------------------------------
$("match-pick").addEventListener("click", () => $("match-file").click());
$("match-file").addEventListener("change", async (e) => {
  const file = e.target.files[0]; e.target.value = "";
  if (!file) return;
  looks.matchRef = { reference_data: toBase64(await file.arrayBuffer()) };
  $("match-ref").textContent = `Reference: ${file.name}`;
  runMatch();
});
$("match-selected").addEventListener("click", () => {
  const other = [...selected].find(p => p !== current);
  if (!other) return;
  looks.matchRef = { reference_path: other };
  $("match-ref").textContent = `Reference: ${other.split("/").pop()}`;
  runMatch();
});
function syncMatchButtons() {
  $("match-selected").disabled = !current || ![...selected].some(p => p !== current);
}
document.addEventListener("click", syncMatchButtons, true);

async function runMatch() {
  if (!current || !looks.matchRef) return;
  setStatus("Matching the reference look…");
  const d = await post("/api/looks/match", { path: current, ...looks.matchRef });
  if (d.error) { $("match-ref").textContent = d.error; return setStatus(d.error, true); }
  looks.match = d;
  const r = d.report, b = r.distance_before, a = r.distance_after;
  const ul = $("match-report"); ul.innerHTML = "";
  for (const step of r.steps) { const li = document.createElement("li"); li.textContent = step; ul.appendChild(li); }
  const li = document.createElement("li");
  li.textContent = `Difference from the reference (tone / colour cast / saturation): ` +
    `${b.tone} / ${b.colour_cast} / ${b.saturation} before, ${a.tone} / ${a.colour_cast} / ${a.saturation} after`;
  ul.appendChild(li);
  for (const id of ["match-amount", "match-apply", "match-save", "match-discard"]) $(id).disabled = false;
  $("match-amount").value = 100; $("match-amount-val").textContent = "100";
  previewMatch();
  setStatus("Match ready — Apply to keep it, or Discard");
}
async function previewMatch() {
  if (!looks.match) return;
  const d = await post("/api/looks/preset/preview", {
    preset: looks.match.preset, amount: +$("match-amount").value, adjustments: values,
    baseline_temp: lookBaseline(), path: current, seq: ++previewSeq });
  if (d.preview && !d.stale && !comparing) { $("preview").src = d.preview; drawHistogram(d.histogram); }
}
$("match-amount").addEventListener("input", () => {
  $("match-amount-val").textContent = $("match-amount").value;
  clearTimeout(looks.hoverTimer); looks.hoverTimer = setTimeout(previewMatch, 30);
});
$("match-apply").addEventListener("click", async () => {
  if (!looks.match) return;
  const amount = +$("match-amount").value;
  const base = JSON.parse(JSON.stringify(values));
  const d = await post("/api/looks/preset/apply", { preset: looks.match.preset, amount, adjustments: base, baseline_temp: lookBaseline() });
  if (d.error) return setStatus(d.error, true);
  values = d.adjustments; looks.active = null;
  paintAll(); schedulePreview(); commit(`Matched look${amount < 100 ? ` (${amount}%)` : ""}`);
  setStatus("Applied the matched look — every control is editable; not saved yet");
});
$("match-save").addEventListener("click", async () => {
  if (!looks.match) return;
  const name = prompt("Name this preset", "Matched look"); if (!name) return;
  const d = await post("/api/looks/preset/save", { name, preset: looks.match.preset, overwrite: false });
  if (d.exists) return setStatus(d.error, true);
  if (d.error) return setStatus(d.error, true);
  looks.presets = d.presets; renderCategories(); renderPresetGrid(); setStatus(d.message);
});
$("match-discard").addEventListener("click", () => {
  looks.match = null; $("match-report").innerHTML = ""; $("match-ref").textContent = "";
  for (const id of ["match-amount", "match-apply", "match-save", "match-discard"]) $(id).disabled = true;
  schedulePreview();
});
