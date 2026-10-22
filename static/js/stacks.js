// ---- Library stacks --------------------------------------------------------
// A stack groups photos (a burst) into one grid cell. The server owns the state
// (catalog.py, beside the library; no raw or sidecar is touched) and sends only
// the visible cells: a collapsed stack shows its top photo with a count badge,
// an expanded one shows every member, each marked with its position.
//   Ctrl/Cmd+G stack the selection   Ctrl/Cmd+Shift+G unstack   S collapse/expand
function libPaintStack(cell, it) {
  let b = cell.querySelector(".stk");
  const st = it.stack;
  cell.classList.toggle("stacked", !!st);
  cell.classList.toggle("stack-open", !!st && !st.collapsed);
  if (!st) { if (b) b.remove(); return; }
  if (!b) {
    b = document.createElement("button");
    b.className = "stk";
    b.addEventListener("click", (e) => { e.stopPropagation(); libToggleStack(cell.dataset.path); });
    cell.appendChild(b);
  }
  b.textContent = st.collapsed ? String(st.count) : `${st.pos}/${st.count}`;
  b.title = st.collapsed ? `Stack of ${st.count} photos: click to expand (S)` : `Photo ${st.pos} of ${st.count} in a stack: click to collapse (S)`;
}

async function libStackReload() {
  if (typeof libSoftReload === "function") await libSoftReload(); else await libLoad(true);
}

async function libStack() {
  const paths = libTargets();
  if (paths.length < 2) return setStatus("Select two or more photos to stack (Shift or Ctrl-click)", true);
  const data = await post("/api/stacks/create", { paths });
  if (data.error) return setStatus(data.error, true);
  libActive = paths[0]; libMulti = new Set();
  setStatus(data.message); libStackReload();
}

async function libUnstack() {
  const paths = libTargets();
  if (!paths.length) return;
  const data = await post("/api/stacks/remove", { paths });
  if (data.error) return setStatus(data.error, true);
  setStatus(data.message); libStackReload();
}

async function libToggleStack(path) {
  const it = libItem(path || libActive);
  if (!it || !it.stack) return setStatus("Not in a stack");
  const data = await post("/api/stacks/state", { paths: [it.path], collapsed: !it.stack.collapsed });
  if (data.error) return setStatus(data.error, true);
  libActive = it.path;
  libStackReload();
}

async function libStackAll(collapsed) {
  const data = await post("/api/stacks/state", { collapsed });
  if (data.error) return setStatus(data.error, true);
  setStatus(collapsed ? "Collapsed all stacks" : "Expanded all stacks"); libStackReload();
}

async function libAutoStack() {
  const body = libMulti.size > 1 ? { scope: "selection", paths: [...libMulti] }
    : libCollection ? { scope: "collection", collection: libCollection } : { scope: "all" };
  setStatus("Stacking bursts…");
  const data = await post("/api/stacks/auto", body);
  if (data.error) return setStatus(data.error, true);
  setStatus(data.message, !!data.missing && !data.created); libStackReload();
}

$("lib-stack").addEventListener("click", libStack);
$("lib-unstack").addEventListener("click", libUnstack);
$("lib-autostack").addEventListener("click", libAutoStack);
$("lib-stack-toggle").addEventListener("click", () => {
  libStackAll(!libState.items.some(i => i.stack && i.stack.collapsed));
});
