// ---- Keyword list panel (Library sidebar) --------------------------------
// Shows every keyword with its photo count, indented under its parent.
// Click a name to filter the grid by it; the + button (or dragging the row
// onto the grid) adds it to the selection; the pencil edits parent, synonyms,
// "include on export", renames or deletes it (across all photos and sidecars).
let kwRows = [];
const kwCollapsed = new Set();
let kwEditing = "";            // key of the row whose editor is open
let kwTimer = null;
const KW_MIME = "application/x-photo-room-keyword";

function kwRefresh() {          // debounced: libLoad calls this on every reload
  clearTimeout(kwTimer);
  kwTimer = setTimeout(kwLoad, 120);
}

async function kwLoad() {
  let data;
  try { data = await (await fetch("/api/keywords/list")).json(); } catch { return; }
  if (data.error) return;
  kwRows = data.keywords || [];
  kwRender();
}

function kwVisible() {
  const q = $("kw-search").value.trim().toLowerCase();
  const rows = [];
  let hideBelow = -1;
  for (const r of kwRows) {
    if (hideBelow >= 0 && r.depth > hideBelow) continue;
    hideBelow = -1;
    if (q) {
      const hit = (r.keyword + " " + r.synonyms.join(" ")).toLowerCase().includes(q);
      if (!hit) continue;
    } else if (kwCollapsed.has(r.key)) hideBelow = r.depth;
    rows.push(r);
  }
  return rows;
}

function kwHasKids(i) { return kwRows[i + 1] && kwRows[i + 1].depth > kwRows[i].depth; }

function kwRender() {
  const box = $("kw-tree");
  box.innerHTML = "";
  const active = ($("lf-keyword").value || "").toLowerCase();
  const search = $("kw-search").value.trim();
  for (const r of kwVisible()) {
    const i = kwRows.indexOf(r);
    const el = document.createElement("div");
    el.className = "kw" + (r.key === active ? " active" : "") + (r.export ? "" : " noexp");
    el.dataset.key = r.key;
    el.setAttribute("role", "treeitem");
    el.draggable = true;
    el.style.paddingLeft = (6 + (search ? 0 : r.depth) * 12) + "px";
    el.innerHTML = `<span class="tg"></span><span class="nm"></span><span class="ct"></span>`
      + `<button class="act" data-a="apply" title="Add to the selected photos">+</button>`
      + `<button class="act" data-a="edit" title="Edit, rename or delete">✎</button>`;
    el.querySelector(".tg").textContent = !search && kwHasKids(i) ? (kwCollapsed.has(r.key) ? "▸" : "▾") : "";
    el.querySelector(".nm").textContent = r.keyword;
    el.querySelector(".ct").textContent = r.total !== r.count ? `${r.count} (${r.total})` : r.count;
    el.title = r.path + (r.synonyms.length ? "\nSynonyms: " + r.synonyms.join(", ") : "")
      + `\n${r.count} photo(s) with this keyword` + (r.total !== r.count ? `, ${r.total} including sub-keywords` : "");
    el.addEventListener("click", (e) => {
      const a = e.target.dataset?.a;
      if (a === "apply") return kwApply(r.keyword);
      if (a === "edit") return kwEdit(r);
      if (e.target.classList.contains("tg") && kwHasKids(i)) {
        kwCollapsed.has(r.key) ? kwCollapsed.delete(r.key) : kwCollapsed.add(r.key);
        return kwRender();
      }
      kwFilter(r);
    });
    el.addEventListener("dblclick", (e) => { if (!e.target.dataset?.a) kwApply(r.keyword); });
    el.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData(KW_MIME, r.keyword);
      e.dataTransfer.setData("text/plain", r.keyword);
      e.dataTransfer.effectAllowed = "copy";
    });
    box.appendChild(el);
    if (kwEditing === r.key) box.appendChild(kwEditor(r));
  }
}

function kwFilter(r) {
  const sel = $("lf-keyword");
  const off = (sel.value || "").toLowerCase() === r.key;
  if (!off && ![...sel.options].some(o => o.value.toLowerCase() === r.key)) {
    const o = document.createElement("option");
    o.value = o.textContent = r.keyword;
    sel.appendChild(o);
  }
  sel.value = off ? "" : [...sel.options].find(o => o.value.toLowerCase() === r.key).value;
  kwRender();
  libLoad(true);
}

async function kwApply(name, paths = libTargets()) {
  if (!paths.length) return setStatus("Select photos first", true);
  const data = await post("/api/keywords", { paths, add: [name] });
  if (data.error) return setStatus(data.error, true);
  setStatus(`Added '${name}' to ${paths.length} photo(s)`);
  libLoad(true);
}

function kwEditor(r) {
  const ed = document.createElement("div");
  ed.className = "kw-edit";
  const parents = kwRows.filter(x => x.key !== r.key && !x.path.toLowerCase().split(" > ").includes(r.keyword.toLowerCase()));
  ed.innerHTML = `<label>Name<input type="text" class="e-name"></label>`
    + `<label>Parent<input type="text" class="e-parent" list="kw-parents" placeholder="(top level)"></label>`
    + `<datalist id="kw-parents"></datalist>`
    + `<label>Synonyms (comma separated)<input type="text" class="e-syn"></label>`
    + `<label class="row"><input type="checkbox" class="e-exp"> Include on export</label>`
    + `<div class="row"><button class="e-save">Save</button><button class="e-del">Delete</button><button class="e-close">Close</button></div>`;
  ed.querySelector(".e-name").value = r.keyword;
  ed.querySelector(".e-parent").value = r.parent;
  ed.querySelector(".e-syn").value = r.synonyms.join(", ");
  ed.querySelector(".e-exp").checked = r.export;
  ed.querySelector("#kw-parents").innerHTML = parents.map(p => `<option value="${p.keyword.replace(/"/g, "&quot;")}">`).join("");
  ed.querySelector(".e-close").addEventListener("click", () => { kwEditing = ""; kwRender(); });
  ed.querySelector(".e-save").addEventListener("click", () => kwSave(r, ed));
  ed.querySelector(".e-del").addEventListener("click", () => kwDelete(r));
  return ed;
}

async function kwSave(r, ed) {
  const name = ed.querySelector(".e-name").value.trim();
  const upd = await post("/api/keywords/update", {
    keyword: r.keyword, parent: ed.querySelector(".e-parent").value.trim(),
    synonyms: ed.querySelector(".e-syn").value, export: ed.querySelector(".e-exp").checked });
  if (upd.error) return setStatus(upd.error, true);
  if (name && name !== r.keyword) {
    let res = await post("/api/keywords/rename", { keyword: r.keyword, new_name: name, dry_run: true });
    if (res.error && !/already exists/.test(res.error)) return setStatus(res.error, true);
    const merge = !!res.error;
    const msg = merge ? `'${name}' already exists. Merge '${r.keyword}' into it?`
                      : `Rename '${r.keyword}' to '${name}' on ${res.photos} photo(s)?`;
    if (!confirm(msg)) return;
    res = await post("/api/keywords/rename", { keyword: r.keyword, new_name: name, merge });
    if (res.error) return setStatus(res.error, true);
    setStatus(res.message);
  } else setStatus(upd.message);
  kwEditing = "";
  libLoad(true);
}

async function kwDelete(r) {
  const dry = await post("/api/keywords/delete", { keyword: r.keyword, dry_run: true });
  if (dry.error) return setStatus(dry.error, true);
  if (!confirm(`Delete keyword '${r.keyword}' from ${dry.photos} photo(s)? Its sub-keywords move up one level.`)) return;
  const res = await post("/api/keywords/delete", { keyword: r.keyword });
  if (res.error) return setStatus(res.error, true);
  kwEditing = "";
  setStatus(res.message);
  libLoad(true);
}

$("kw-new").addEventListener("click", async () => {
  const name = (prompt("New keyword") || "").trim();
  if (!name) return;
  const res = await post("/api/keywords/create", { name });
  if (res.error) return setStatus(res.error, true);
  setStatus(res.message);
  kwLoad();
});
$("kw-search").addEventListener("input", kwRender);
$("lf-keyword").addEventListener("change", kwRender);

// Dropping a keyword on the grid tags the selection (or the photo it lands on).
const kwGrid = $("lib-grid");
const kwIsDrag = (e) => [...(e.dataTransfer?.types || [])].includes(KW_MIME);
kwGrid.addEventListener("dragover", (e) => {
  if (!kwIsDrag(e)) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = "copy";
  kwGrid.querySelectorAll(".kwdrop").forEach(c => c.classList.remove("kwdrop"));
  e.target.closest?.(".lcell")?.classList.add("kwdrop");
});
kwGrid.addEventListener("dragleave", () => kwGrid.querySelectorAll(".kwdrop").forEach(c => c.classList.remove("kwdrop")));
kwGrid.addEventListener("drop", (e) => {
  if (!kwIsDrag(e)) return;
  e.preventDefault();
  e.stopPropagation();
  kwGrid.querySelectorAll(".kwdrop").forEach(c => c.classList.remove("kwdrop"));
  const name = e.dataTransfer.getData(KW_MIME);
  const cell = e.target.closest?.(".lcell");
  const hit = cell?.dataset.path;
  const paths = hit && !libMulti.has(hit) ? [hit] : libTargets();
  if (name) kwApply(name, paths);
}, true);
