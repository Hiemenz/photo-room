// ---- Auto Sync (Develop) and Quick Develop (Library) -----------------------
// Both talk to /api/sync_settings (quick_api.py): each target photo keeps its
// own other settings, white balance is re-anchored per camera, and framing,
// spots and masks are never touched. One History step is recorded per photo.
let autoSync = false;
let lastSyncUndo = null;

// Photos other than the open one that are ticked in the filmstrip or grid.
function syncTargets() {
  const set = new Set(selected);
  if (typeof libMulti !== "undefined") libMulti.forEach(p => set.add(p));
  set.delete(current);
  return [...set];
}

async function runSync(paths, body, label) {
  const d = await post("/api/sync_settings", { paths, label, ...body });
  if (d.error) { setStatus(d.error, true); return d; }
  lastSyncUndo = d.undo && d.undo.length ? d.undo : null;
  const undo = $("qd-undo");
  if (undo) undo.disabled = !lastSyncUndo;
  setStatus(d.message, !d.ok);
  if (d.failed?.length) console.warn("Sync failures:", d.failed);
  return d;
}

async function undoSync() {
  if (!lastSyncUndo) return;
  const d = await post("/api/sync_undo", { undo: lastSyncUndo });
  if (d.error) return setStatus(d.error, true);
  lastSyncUndo = null;
  $("qd-undo").disabled = true;
  setStatus(d.message, !d.ok);
}

// ---- Auto Sync switch ------------------------------------------------------
const syncRow = document.createElement("label");
syncRow.className = "toggle autosync";
syncRow.title = "With several photos selected, slider changes apply to all of them (each keeps its own other settings)";
syncRow.innerHTML = `<input type="checkbox" id="auto-sync"><span>Auto Sync <small id="auto-sync-n"></small></span>`;
$("controls").insertBefore(syncRow, $("controls-scroll"));

function paintSyncCount() {
  const n = syncTargets().length;
  $("auto-sync-n").textContent = autoSync && n ? `(+${n})` : "";
}
$("auto-sync").addEventListener("change", (e) => { autoSync = e.target.checked; paintSyncCount(); });
document.addEventListener("click", () => setTimeout(paintSyncCount, 0));

// Slider release in Develop: send just that slider's value to the others.
$("controls").addEventListener("change", (e) => {
  const el = e.target;
  if (!autoSync || !current || el.type !== "range" || !el.id.startsWith("s-")) return;
  const key = el.id.slice(2);
  const others = syncTargets();
  if (!others.length || !(key in values)) return;
  const name = el.closest(".ctrl")?.querySelector("label")?.textContent || key;
  runSync(others, { changes: { [key]: values[key] }, baseline_temp: baselineTemp },
          `Auto Sync: ${name} ${el.value}`);
});

// ---- Quick Develop panel ---------------------------------------------------
// [key, label, small step, large step]
const QUICK = [
  ["exposure", "Exposure", 0.33, 1], ["contrast", "Contrast", 5, 20],
  ["highlights", "Highlights", 5, 20], ["shadows", "Shadows", 5, 20],
  ["whites", "Whites", 5, 20], ["blacks", "Blacks", 5, 20],
  ["color_temp", "Temp", 100, 500], ["tint", "Tint", 5, 20],
  ["vibrance", "Vibrance", 5, 20], ["saturation", "Saturation", 5, 20],
  ["clarity", "Clarity", 5, 20], ["texture", "Texture", 5, 20],
  ["dehaze", "Dehaze", 5, 20],
];

const qd = document.createElement("div");
qd.id = "quick-develop";
qd.innerHTML = `<b>Quick Develop</b>` + QUICK.map(([k, label, s, l]) =>
  `<span class="qd-group" title="${label}: step ${s}, big step ${l}">
     <button data-k="${k}" data-d="${-l}">&laquo;</button><button data-k="${k}" data-d="${-s}">&lsaquo;</button>
     <span>${label}</span>
     <button data-k="${k}" data-d="${s}">&rsaquo;</button><button data-k="${k}" data-d="${l}">&raquo;</button>
   </span>`).join("") + `<button id="qd-undo" disabled title="Undo the last Quick Develop step on every photo it changed">Undo</button>`;
$("lib").insertBefore(qd, $("lib-foot"));

qd.addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-k]");
  if (!b) return;
  const paths = libTargets();
  if (!paths.length) return setStatus("Select one or more photos first", true);
  const k = b.dataset.k, d = parseFloat(b.dataset.d);
  const name = QUICK.find(q => q[0] === k)[1];
  const label = `Quick Develop: ${name} ${d > 0 ? "+" : ""}${d}`;
  await runSync(paths, { steps: { [k]: d } }, label);
  // The open photo's on-screen values are stale now; reload them from disk (it was edited behind the open editor).
  if (current && paths.includes(current)) openFile(current);
});
$("qd-undo").addEventListener("click", undoSync);
