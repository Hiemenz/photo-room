// ---- Folders panel -------------------------------------------------------
// Tree with photo counts; click filters the grid, drop photos on a folder to
// move them (sidecars, edits, virtual copies and collections follow). Nothing
// is deleted or overwritten; Undo reverses the last move or rename.
let foFolders = [], foUndo = null;
const foCollapsed = new Set();

async function foLoad() {
  let data;
  try { data = await (await fetch("/api/folders")).json(); } catch { return; }
  if (data.error) return;
  foFolders = data.folders || []; foUndo = data.undo;
  foRender();
}

function foVisible(f) {
  for (let p = f.path; p.includes("/");) { p = p.slice(0, p.lastIndexOf("/")); if (foCollapsed.has(p)) return false; }
  return !(f.path && foCollapsed.has(""));
}

function foRender() {
  const list = $("fo-list");
  list.innerHTML = "";
  if (libFolder !== null && foFolders.length && !foFolders.some(f => f.path === libFolder)) { libFolder = null; libLoad(true); }
  for (const f of foFolders) {
    if (!foVisible(f)) continue;
    const kids = foFolders.some(g => g.path !== f.path && (!f.path || g.path.startsWith(f.path + "/")));
    const el = document.createElement("div");
    el.className = "col fo" + (f.path === libFolder ? " active" : "");
    el.style.paddingLeft = (12 + f.depth * 12) + "px";
    el.innerHTML = `<span class="tw">${kids ? (foCollapsed.has(f.path) ? "▸" : "▾") : "·"}</span><span class="nm"></span><span class="ct"></span>`
      + (f.path ? `<button class="act" data-a="ren" title="Rename folder">✎</button>` : "");
    el.querySelector(".nm").textContent = f.name;
    el.querySelector(".ct").textContent = libFolderSub ? f.total : f.count;
    el.title = (f.path || "Library root") + " (" + f.count + " here, " + f.total + " with subfolders)";
    el.addEventListener("click", (e) => {
      const a = e.target.dataset?.a;
      if (a === "ren") return foRename(f);
      if (e.target.classList.contains("tw") && kids) {
        foCollapsed.has(f.path) ? foCollapsed.delete(f.path) : foCollapsed.add(f.path);
        return foRender();
      }
      foSelect(f.path);
    });
    el.addEventListener("dragover", (e) => { e.preventDefault(); el.classList.add("drop"); });
    el.addEventListener("dragleave", () => el.classList.remove("drop"));
    el.addEventListener("drop", (e) => {
      e.preventDefault(); el.classList.remove("drop");
      const t = e.dataTransfer.getData("text/plain");
      if (t.startsWith("photo-room:")) foMovePhotos(JSON.parse(t.slice(11)), f.path);
    });
    list.appendChild(el);
  }
  $("fo-undo").disabled = !foUndo;
  $("fo-undo").title = foUndo ? "Undo: " + foUndo.label : "Nothing to undo";
}

function foSelect(path) {
  libFolder = path; libCollection = ""; libActive = null; libMulti = new Set();
  libLoad(true); colApply({ collections }); foRender();
}

// Choosing "All photographs" or a collection leaves the folder filter.
const _colSelectBase = colSelect;
colSelect = function (id) { libFolder = null; _colSelectBase(id); foRender(); };

async function foAfter(data, msg) {
  if (data.error) { setStatus(data.error, true); return false; }
  foUndo = data.undo;
  (data.warnings || []).forEach(w => setStatus(w, true));
  setStatus(msg || data.label);
  await Promise.all([foLoad(), colLoad()]);
  libLoad(true);
  return true;
}

async function foMovePhotos(paths, dest) {
  if (!paths.length) return;
  const data = await post("/api/folders/move", { paths, dest });
  await foAfter(data, `Moved ${paths.length} photo(s) with their sidecars and edits`);
}

async function foNew() {
  const name = prompt("New folder name" + (libFolder ? ` (inside ${libFolder})` : ""));
  if (!name) return;
  await foAfter(await post("/api/folders/create", { parent: libFolder || "", name }));
}

async function foRename(f) {
  const name = prompt("Rename folder (photos, edits and collections follow)", f.name);
  if (!name || name === f.name) return;
  const data = await post("/api/folders/rename", { path: f.path, name });
  if (!data.error && libFolder !== null && (libFolder === f.path || libFolder.startsWith(f.path + "/"))) {
    libFolder = f.path.slice(0, f.path.length - f.name.length) + name + libFolder.slice(f.path.length);
  }
  await foAfter(data);
}

$("fo-new").addEventListener("click", foNew);
$("fo-undo").addEventListener("click", async () => {
  if (!foUndo || !confirm(`Undo: ${foUndo.label}?`)) return;
  libFolder = null;
  await foAfter(await post("/api/folders/undo", {}));
});
$("fo-sub").addEventListener("change", (e) => {
  libFolderSub = e.target.checked; foRender(); if (libFolder !== null) libLoad(true);
});
foLoad();
