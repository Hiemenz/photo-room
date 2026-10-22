// ---- Collections ---------------------------------------------------------
// Collections nest inside collection sets (`parent`, listed depth-first with a
// `depth`). The Quick Collection is a manual collection flagged `quick`; B adds
// the selection to the Target Collection (the Quick Collection until another is chosen).
let collections = [], colTarget = null;
const colCollapsed = new Set();
const collectionById = (id) => collections.find(c => c.id === id);

function colVisible(c) {
  for (let p = collectionById(c.parent); p; p = collectionById(p.parent)) if (colCollapsed.has(p.id)) return false;
  return true;
}

async function colLoad() {
  let data;
  try { data = await (await fetch("/api/collections")).json(); } catch { return; }
  if (data.error) return;
  colApply(data);
}

function colApply(data) {
  collections = data.collections || [];
  if ("target" in data) colTarget = data.target;
  if (libCollection && !collectionById(libCollection)) { libCollection = ""; libLoad(true); }
  const list = $("col-list");
  list.innerHTML = "";
  for (const c of collections) {
    if (!colVisible(c)) continue;
    const el = document.createElement("div");
    const manual = !c.smart && !c.set;
    el.className = "col" + (c.id === libCollection ? " active" : "") + (c.set ? " set" : "")
      + (c.id === colTarget ? " target" : "");
    el.dataset.id = c.id;
    el.style.paddingLeft = (12 + (c.depth || 0) * 12) + "px";
    const icon = c.set ? (colCollapsed.has(c.id) ? "▸" : "▾") : c.quick ? "◈" : c.smart ? "◆" : "▤";
    el.innerHTML = `<span class="tw">${icon}</span><span class="nm"></span><span class="ct"></span>`
      + (manual ? `<button class="act" data-a="target" title="${c.id === colTarget ? "This is the Target Collection (B adds the selection)" : "Make this the Target Collection (B adds the selection)"}">◎</button>` : "")
      + (c.quick ? `<button class="act" data-a="clear" title="Empty the Quick Collection (photos stay)">Clear</button>` : "")
      + (c.quick ? "" : `<button class="act" data-a="edit" title="${c.smart ? "Edit rules" : "Rename"}">✎</button>`
        + `<button class="act" data-a="del" title="${c.set ? "Delete set (its collections move up)" : "Delete collection (photos stay)"}">✕</button>`);
    el.querySelector(".nm").textContent = c.name;
    el.querySelector(".ct").textContent = c.set ? "" : c.count;
    el.addEventListener("click", (e) => {
      const a = e.target.dataset?.a;
      if (a === "del") return colDelete(c);
      if (a === "edit") return c.smart ? openSmartDialog(c) : colRename(c);
      if (a === "target") return colSetTarget(c.id === colTarget && !c.quick ? "" : c.id);
      if (a === "clear") return colClear(c);
      if (c.set && e.target.classList.contains("tw")) {
        colCollapsed.has(c.id) ? colCollapsed.delete(c.id) : colCollapsed.add(c.id);
        return colApply({ collections });
      }
      colSelect(c.id);
    });
    if (!c.quick) {
      el.draggable = true;
      el.addEventListener("dragstart", (e) => {
        e.dataTransfer.setData("text/plain", "photo-room-col:" + c.id);
        e.dataTransfer.effectAllowed = "move";
      });
    }
    if (manual || c.set) {
      el.addEventListener("dragover", (e) => { e.preventDefault(); el.classList.add("drop"); });
      el.addEventListener("dragleave", () => el.classList.remove("drop"));
      el.addEventListener("drop", (e) => {
        e.preventDefault(); el.classList.remove("drop");
        const t = e.dataTransfer.getData("text/plain");
        if (t.startsWith("photo-room-col:")) { if (c.set) colMove(t.slice(15), c.id); }
        else if (t.startsWith("photo-room:") && manual) colAdd(c.id, JSON.parse(t.slice(11)));
      });
    }
    list.appendChild(el);
  }
  $("col-all").classList.toggle("active", !libCollection);
  $("col-all-ct").textContent = "";
  const sel = $("lib-addto");
  sel.innerHTML = `<option value="">Add to collection…</option>`;
  for (const c of collections.filter(c => !c.smart && !c.set)) {
    const o = document.createElement("option");
    o.value = c.id; o.textContent = c.name + (c.id === colTarget ? " (target)" : "");
    sel.appendChild(o);
  }
}

function colSelect(id) {
  libCollection = id; libActive = null; libMulti = new Set();
  libLoad(true); colApply({ collections });
}
$("col-all").addEventListener("click", () => colSelect(""));
// Dropping a collection or set on "All photographs" takes it out of its set.
$("col-all").addEventListener("dragover", (e) => { e.preventDefault(); });
$("col-all").addEventListener("drop", (e) => {
  e.preventDefault();
  const t = e.dataTransfer.getData("text/plain");
  if (t.startsWith("photo-room-col:")) colMove(t.slice(15), "");
});

// New collections and sets go inside the selected set.
const colParent = () => (collectionById(libCollection)?.set ? libCollection : "");
async function colNewSet() {
  const name = prompt("Collection set name");
  if (!name) return;
  const data = await post("/api/collections/create", { name, set: true, parent: colParent() });
  if (data.error) return setStatus(data.error, true);
  colApply(data);
  setStatus(`Created collection set ${name}`);
}
async function colMove(id, parent) {
  const data = await post("/api/collections/move", { id, parent });
  if (data.error) return setStatus(data.error, true);
  colApply(data);
}
async function colSetTarget(id) {
  const data = await post("/api/collections/target", { id });
  if (data.error) return setStatus(data.error, true);
  colApply(data);
  setStatus("Target Collection: " + (collectionById(data.target)?.name || "none yet (the Quick Collection is made on first use)"));
}
async function colClear(c) {
  if (!confirm(`Empty "${c.name}"? The photos themselves are not deleted.`)) return;
  const data = await post("/api/collections/clear", { id: c.id });
  if (data.error) return setStatus(data.error, true);
  colApply(data); if (libCollection === c.id) libLoad(true);
}
// B: add the selection to the Target Collection, or take it out when it is all in there already.
async function colToggleTarget() {
  const photos = libTargets();
  if (!photos.length) return setStatus("Select a photo first", true);
  const data = await post("/api/collections/target_toggle", { photos });
  if (data.error) return setStatus(data.error, true);
  colApply(data);
  setStatus(`${data.action === "added" ? "Added" : "Removed"} ${photos.length} photo(s) ${data.action === "added" ? "to" : "from"} ${data.collection}`);
  if (libCollection === colTarget) libLoad(true);
}
$("col-set").addEventListener("click", colNewSet);
$("lib-target").addEventListener("click", colToggleTarget);

async function colNew() {
  const name = prompt("Collection name");
  if (!name) return;
  const photos = libTargets();
  const data = await post("/api/collections/create", { name, photos, parent: colParent() });
  if (data.error) return setStatus(data.error, true);
  colApply(data);
  setStatus(`Created collection ${name}` + (photos.length ? ` with ${photos.length} photo(s)` : ""));
}
async function colRename(c) {
  const name = prompt("Rename collection", c.name);
  if (!name || name === c.name) return;
  const data = await post("/api/collections/rename", { id: c.id, name });
  if (data.error) return setStatus(data.error, true);
  colApply(data);
}
async function colDelete(c) {
  if (!confirm(c.set ? `Delete the set "${c.name}"? Its collections move up a level and no photos are deleted.`
    : `Delete the collection "${c.name}"? The photos themselves are not deleted.`)) return;
  const data = await post("/api/collections/delete", { id: c.id });
  if (data.error) return setStatus(data.error, true);
  colApply(data);
  setStatus(`Deleted collection ${c.name}`);
}
async function colAdd(id, photos) {
  if (!photos.length) return;
  const data = await post("/api/collections/add", { id, photos });
  if (data.error) return setStatus(data.error, true);
  colApply(data);
  setStatus(`Added ${photos.length} photo(s) to ${collectionById(id)?.name}`);
}
$("col-new").addEventListener("click", colNew);
$("lib-addto").addEventListener("change", (e) => {
  const id = e.target.value; e.target.value = ""; e.target.blur();
  if (id) colAdd(id, libTargets());
});
$("lib-rmcol").addEventListener("click", async () => {
  const photos = libTargets();
  if (!libCollection || !photos.length) return;
  const data = await post("/api/collections/remove", { id: libCollection, photos });
  if (data.error) return setStatus(data.error, true);
  colApply(data); libLoad(true);
});

// Smart collection rule editor
const RULE_FIELDS = {
  rating: { ops: [["ge", "at least"], ["le", "at most"], ["eq", "exactly"]], type: "num" },
  flag: { ops: [["is", "is"]], choices: ["pick", "reject", "unflagged"] },
  label: { ops: [["is", "is"]], choices: ["Red", "Yellow", "Green", "Blue", "Purple", "none"] },
  keyword: { ops: [["has", "has"], ["not_has", "does not have"]], type: "text" },
  camera: { ops: [["is", "is"], ["contains", "contains"]], type: "text" },
  lens: { ops: [["is", "is"], ["contains", "contains"]], type: "text" },
  iso: { ops: [["ge", "at least"], ["le", "at most"], ["eq", "exactly"]], type: "int" },
  focal: { ops: [["ge", "at least (mm)"], ["le", "at most (mm)"], ["eq", "exactly (mm)"]], type: "int" },
  edited: { ops: [["is", "is"]], choices: ["edited", "unedited"] },
  filetype: { ops: [["is", "is"]], type: "text" },
  date: { ops: [["after", "after"], ["before", "before"], ["on", "on"]], type: "date" },
};
let smartEditing = null;

function addRuleRow(rule) {
  rule = rule || { field: "rating", op: "ge", value: 3 };
  const row = document.createElement("div");
  row.className = "rule";
  const f = document.createElement("select");
  f.innerHTML = Object.keys(RULE_FIELDS).map(k => `<option>${k}</option>`).join("");
  const op = document.createElement("select");
  const val = document.createElement("span"); val.style.flex = "1"; val.style.display = "flex";
  const rm = document.createElement("button"); rm.textContent = "✕";
  rm.addEventListener("click", () => row.remove());
  function fill(keepOp, keepVal) {
    const d = RULE_FIELDS[f.value];
    op.innerHTML = d.ops.map(([v, t]) => `<option value="${v}">${t}</option>`).join("");
    if (keepOp) op.value = keepOp;
    val.innerHTML = "";
    let input;
    if (d.choices) {
      input = document.createElement("select");
      input.innerHTML = d.choices.map(c => `<option>${c}</option>`).join("");
    } else {
      input = document.createElement("input");
      input.type = d.type === "num" || d.type === "int" ? "number" : d.type === "date" ? "date" : "text";
      if (d.type === "int") { input.min = 0; input.value = f.value === "iso" ? 1600 : 50; input.placeholder = f.value === "iso" ? "ISO" : "mm"; }
      if (d.type === "num") { input.min = -1; input.max = 5; input.value = 3; }
    }
    input.style.flex = "1";
    if (keepVal !== undefined) input.value = keepVal;
    val.appendChild(input);
  }
  f.addEventListener("change", () => fill());
  f.value = rule.field; fill(rule.op, rule.value === "none" && rule.field === "flag" ? "unflagged" : rule.value);
  row.append(f, op, val, rm);
  $("sd-rules").appendChild(row);
}

function openSmartDialog(col) {
  smartEditing = col || null;
  $("sd-title").textContent = col ? `Edit rules: ${col.name}` : "New smart collection";
  $("sd-name").value = col ? col.name : "";
  $("sd-name").disabled = !!col;
  $("sd-match").value = col?.match || "all";
  $("sd-rules").innerHTML = "";
  (col?.rules?.length ? col.rules : [null]).forEach(addRuleRow);
  $("smart-dlg").classList.add("on");
}
function readRules() {
  return [...$("sd-rules").children].map(row => {
    const [f, op] = row.querySelectorAll("select");
    const input = row.querySelector("span").firstChild;
    return { field: f.value, op: op.value, value: input.value };
  });
}
$("col-smart").addEventListener("click", () => openSmartDialog(null));
$("sd-add-rule").addEventListener("click", () => addRuleRow());
$("sd-cancel").addEventListener("click", () => $("smart-dlg").classList.remove("on"));
$("sd-save").addEventListener("click", async () => {
  const rules = readRules(), match = $("sd-match").value;
  const data = smartEditing
    ? await post("/api/collections/rules", { id: smartEditing.id, rules, match })
    : await post("/api/collections/create", { name: $("sd-name").value, smart: true, rules, match, parent: colParent() });
  if (data.error) return setStatus(data.error, true);
  $("smart-dlg").classList.remove("on");
  colApply(data);
  if (libCollection) libLoad(true);
});

// ---- Virtual copies ------------------------------------------------------
async function libVirtualCopy() {
  const paths = libTargets();
  if (!paths.length) return setStatus("Select a photo first", true);
  let made = 0;
  for (const p of paths) {
    const data = await post("/api/virtual/create", { path: p });
    if (data.error) return setStatus(data.error, true);
    made++;
  }
  setStatus(`Created ${made} virtual cop${made === 1 ? "y" : "ies"}`);
  libLoad(true); colLoad();
}
async function libDeleteCopies() {
  const copies = libTargets().filter(p => libItem(p)?.virtual);
  if (!copies.length) return setStatus("Select a virtual copy (page-curl badge) to delete", true);
  if (!confirm(`Delete ${copies.length} virtual cop${copies.length === 1 ? "y" : "ies"}? The raw files are not touched.`)) return;
  for (const p of copies) {
    const data = await post("/api/virtual/delete", { path: p });
    if (data.error) return setStatus(data.error, true);
  }
  libMulti = new Set(); libActive = null;
  setStatus(`Deleted ${copies.length} virtual cop${copies.length === 1 ? "y" : "ies"}`);
  libLoad(true); colLoad();
}
$("lib-vc").addEventListener("click", libVirtualCopy);
$("lib-vc-del").addEventListener("click", libDeleteCopies);
