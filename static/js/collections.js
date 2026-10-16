// ---- Collections ---------------------------------------------------------
let collections = [];
const collectionById = (id) => collections.find(c => c.id === id);

async function colLoad() {
  let data;
  try { data = await (await fetch("/api/collections")).json(); } catch { return; }
  if (data.error) return;
  colApply(data);
}

function colApply(data) {
  collections = data.collections || [];
  if (libCollection && !collectionById(libCollection)) { libCollection = ""; libLoad(true); }
  const list = $("col-list");
  list.innerHTML = "";
  for (const c of collections) {
    const el = document.createElement("div");
    el.className = "col" + (c.id === libCollection ? " active" : "");
    el.dataset.id = c.id;
    el.innerHTML = `<span>${c.smart ? "◆" : "▤"}</span><span class="nm"></span><span class="ct"></span>`
      + `<button class="act" data-a="edit" title="${c.smart ? "Edit rules" : "Rename"}">✎</button>`
      + `<button class="act" data-a="del" title="Delete collection (photos stay)">✕</button>`;
    el.querySelector(".nm").textContent = c.name;
    el.querySelector(".ct").textContent = c.count;
    el.addEventListener("click", (e) => {
      const a = e.target.dataset?.a;
      if (a === "del") return colDelete(c);
      if (a === "edit") return c.smart ? openSmartDialog(c) : colRename(c);
      colSelect(c.id);
    });
    if (!c.smart) {
      el.addEventListener("dragover", (e) => { e.preventDefault(); el.classList.add("drop"); });
      el.addEventListener("dragleave", () => el.classList.remove("drop"));
      el.addEventListener("drop", (e) => {
        e.preventDefault(); el.classList.remove("drop");
        const t = e.dataTransfer.getData("text/plain");
        if (t.startsWith("photo-room:")) colAdd(c.id, JSON.parse(t.slice(11)));
      });
    }
    list.appendChild(el);
  }
  $("col-all").classList.toggle("active", !libCollection);
  $("col-all-ct").textContent = "";
  const sel = $("lib-addto");
  sel.innerHTML = `<option value="">Add to collection…</option>`;
  for (const c of collections.filter(c => !c.smart)) {
    const o = document.createElement("option");
    o.value = c.id; o.textContent = c.name;
    sel.appendChild(o);
  }
}

function colSelect(id) {
  libCollection = id; libActive = null; libMulti = new Set();
  libLoad(true); colApply({ collections });
}
$("col-all").addEventListener("click", () => colSelect(""));

async function colNew() {
  const name = prompt("Collection name");
  if (!name) return;
  const photos = libTargets();
  const data = await post("/api/collections/create", { name, photos });
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
  if (!confirm(`Delete the collection "${c.name}"? The photos themselves are not deleted.`)) return;
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
      input.type = d.type === "num" ? "number" : d.type === "date" ? "date" : "text";
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
    : await post("/api/collections/create", { name: $("sd-name").value, smart: true, rules, match });
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
