// ---- Import: drop files or a folder on the Library, or press Import ---------------
// Each raw is streamed to /api/import/upload one at a time. The server copies it
// with a checksum check, never overwrites anything, and skips a file whose bytes
// are already in the library (whatever it is called). Everything else is reported
// line by line; nothing is hidden.
const IMPORT_EXTS = RAW_SUFFIXES.map(s => s.toLowerCase());
const imp = { queue: [], running: false, done: 0, total: 0, bytesDone: 0, bytes: 0, counts: {}, abort: null, cancelled: false };

function importWanted(name) {
  if (!name || name.startsWith(".")) return false;
  const i = name.lastIndexOf(".");
  return i > 0 && IMPORT_EXTS.includes(name.slice(i).toLowerCase());
}

function importLog(text, cls) {
  const row = document.createElement("div");
  if (cls) row.className = cls;
  row.textContent = text;
  const log = $("import-log");
  log.appendChild(row);
  log.scrollTop = log.scrollHeight;
}

function importPaint() {
  $("import-count").textContent = `${imp.done} of ${imp.total}`;
  $("import-bar").max = Math.max(1, imp.bytes);
  $("import-bar").value = imp.bytesDone;
}

function importSummary() {
  const c = imp.counts, parts = [];
  const copied = (c.copied || 0) + (c.renamed || 0);
  if (copied) parts.push(`${copied} imported${c.renamed ? ` (${c.renamed} renamed to keep both)` : ""}`);
  if (c.duplicate) parts.push(`${c.duplicate} already in the library`);
  if (c.skipped) parts.push(`${c.skipped} not raw files`);
  if (c.failed) parts.push(`${c.failed} failed`);
  return parts.join(", ") || "Nothing to import";
}

async function importRun() {
  if (imp.running) return;
  imp.running = true; imp.cancelled = false;
  $("import-panel").hidden = false; $("import-close").hidden = true; $("import-cancel").hidden = false;
  $("import-title").textContent = "Importing";
  while (imp.queue.length && !imp.cancelled) {
    const file = imp.queue.shift();
    imp.abort = new AbortController();
    try {
      const res = await fetch(`/api/import/upload?name=${encodeURIComponent(file.name)}`, {
        method: "POST", body: file, signal: imp.abort.signal, headers: { "Content-Type": "application/octet-stream" },
      });
      let d;
      try { d = await res.json(); } catch (e) { d = { status: "failed", reason: "The server sent an unexpected reply" }; }
      if (d.error) d = { status: "failed", reason: d.error };
      imp.counts[d.status] = (imp.counts[d.status] || 0) + 1;
      if (d.status === "copied") importLog(`Imported ${file.name}`);
      else if (d.status === "renamed") importLog(`Imported ${file.name} as ${d.path} (a different file already had that name)`);
      else if (d.status === "duplicate") importLog(`Skipped ${file.name}: ${d.reason || "already in the library"}${d.path ? ` (${d.path})` : ""}`, "dup");
      else if (d.status === "skipped") importLog(`Skipped ${file.name}: ${d.reason}`, "dup");
      else importLog(`Failed ${file.name}: ${d.reason}`, "bad");
      if (d.status === "failed" && /not enough|disk is full|space/i.test(d.reason || "")) { imp.cancelled = true; toastError("Import", d.reason); }
    } catch (e) {
      if (e.name === "AbortError") break;
      imp.counts.failed = (imp.counts.failed || 0) + 1;
      importLog(`Failed ${file.name}: ${explainError(e).why}`, "bad");
      if (/fetch|network/i.test(String(e))) { imp.cancelled = true; toastError("Import", e); }
    }
    imp.done++; imp.bytesDone += file.size; importPaint();
  }
  imp.abort = null;
  let note = importSummary();
  try {
    const f = await post("/api/import/finish", {});
    if (f.error) throw new Error(f.error);
  } catch (e) { toastError("Import", e); }
  if (imp.cancelled && imp.queue.length) { note = `Stopped. ${note}; ${imp.queue.length} not started`; imp.queue = []; }
  imp.running = false;
  $("import-title").textContent = "Import finished";
  $("import-count").textContent = note;
  $("import-cancel").hidden = true; $("import-close").hidden = false;
  toast(note, { kind: imp.counts.failed ? "error" : "ok", why: imp.counts.failed ? "Open the import log for the reasons." : "", ms: 7000 });
  await libSoftReload();
  loadFiles().then(() => { if (filterMode !== "all") applyFilter(); });
}

function importAdd(files) {
  let n = 0, other = 0;
  for (const f of files) {
    if (importWanted(f.name)) { imp.queue.push(f); imp.total++; imp.bytes += f.size; n++; }
    else if (f.name && !f.name.startsWith(".")) other++;
  }
  if (!n) {
    toast(other ? "No raw photos in that selection" : "Nothing to import",
      { kind: "error", why: other ? `Supported: ${IMPORT_EXTS.join(" ")}. ${other} other file${other === 1 ? " was" : "s were"} ignored.` : "" });
    return 0;
  }
  if (!imp.running) { imp.done = 0; imp.total = n; imp.bytes = imp.queue.reduce((a, f) => a + f.size, 0); imp.bytesDone = 0; imp.counts = {}; $("import-log").innerHTML = ""; }
  if (other) importLog(`${other} non-raw file${other === 1 ? "" : "s"} ignored`, "dup");
  importPaint();
  importRun();
  return n;
}

$("import-cancel").addEventListener("click", () => { imp.cancelled = true; if (imp.abort) imp.abort.abort(); });
$("import-close").addEventListener("click", () => { $("import-panel").hidden = true; });

$("lib-import").addEventListener("click", () => $("import-files").click());
$("lib-import-folder").addEventListener("click", () => $("import-folder").click());
for (const id of ["import-files", "import-folder"]) {
  $(id).addEventListener("change", (e) => { const fs = [...e.target.files]; e.target.value = ""; importAdd(fs); });
}

// ---- Lightroom catalog (.lrcat): report first, write only after the user agrees ----------
// The browser cannot hand the server a path, so it is typed. The server copies the
// catalog and reads the copy; nothing in Lightroom's files is touched.
$("lib-import-lr").addEventListener("click", async () => {
  if (imp.running) return;
  const path = (prompt("Path of the Lightroom catalog (.lrcat) or the folder that holds it:") || "").trim();
  if (!path) return;
  const remapText = (prompt("If the photos moved since Lightroom indexed them, give OLD=NEW path prefixes separated by ; (leave empty otherwise):") || "").trim();
  const remap = remapText ? remapText.split(";").map(s => s.trim()).filter(Boolean) : [];
  $("import-panel").hidden = false; $("import-close").hidden = false; $("import-cancel").hidden = true;
  $("import-title").textContent = "Lightroom catalog"; $("import-count").textContent = "reading…"; $("import-log").innerHTML = "";
  try {
    const d = await post("/api/import/lightroom", { path, remap, apply: false });
    if (d.error) throw new Error(d.error);
    for (const line of d.text.split("\n")) importLog(line);
    $("import-count").textContent = `${d.report.matched} of ${d.report.photos_in_catalog} photos found`;
    if (!d.report.matched) { toast("No photos from that catalog were found in the library", { kind: "error", why: "Check the paths, or give OLD=NEW to remap them." }); return; }
    const force = d.report.sidecars_kept ? confirm(`${d.report.sidecars_kept} photo(s) already have an .xmp sidecar. OK = update those too, Cancel = keep them untouched.`) : false;
    if (!confirm(`Import from this catalog into the library now? (${d.report.matched} photos, ${d.report.virtual_copies} virtual copies, ${d.report.collections} collections)`)) { importLog("Nothing was written."); return; }
    const r = await post("/api/import/lightroom", { path, remap, apply: true, force });
    if (r.error) throw new Error(r.error);
    const a = r.applied || {};
    importLog(`Done: ${a.sidecars_written || 0} sidecars written, ${a.sidecars_kept || 0} kept, ${a.virtual_copies || 0} virtual copies, ${a.collections || 0} collections${a.errors ? `, ${a.errors} failed` : ""}`, a.errors ? "bad" : "");
    for (const e of a.errors_list || []) importLog(e, "bad");
    toast("Lightroom catalog imported", { kind: a.errors ? "error" : "ok", ms: 6000 });
    await libSoftReload();
    loadFiles().then(() => { if (filterMode !== "all") applyFilter(); });
  } catch (e) { toastError("Lightroom import", e); importLog(String(e.message || e), "bad"); }
});

// ---- drag and drop (files and whole folders) ------------------------------------------
function hasFiles(e) { return e.dataTransfer && [...(e.dataTransfer.types || [])].includes("Files"); }

function readAll(reader) {
  return new Promise((resolve, reject) => {
    const out = [];
    (function next() { reader.readEntries((batch) => { if (!batch.length) resolve(out); else { out.push(...batch); next(); } }, reject); })();
  });
}
async function entryFiles(entry, out, depth = 0) {
  if (!entry || depth > 12) return;
  if (entry.isFile) {
    await new Promise((res) => entry.file((f) => { out.push(f); res(); }, () => res()));
  } else if (entry.isDirectory) {
    for (const child of await readAll(entry.createReader())) await entryFiles(child, out, depth + 1);
  }
}
async function droppedFiles(dt) {
  const out = [];
  const plain = [...dt.files];                  // the drop's data is gone after the first await
  const items = [...(dt.items || [])];
  if (items.length && items[0].webkitGetAsEntry) {
    const entries = items.filter(i => i.kind === "file").map(i => [i.webkitGetAsEntry(), i]);
    for (const [entry, item] of entries) {
      if (entry) await entryFiles(entry, out);
      else { const f = item.getAsFile(); if (f) out.push(f); }
    }
    if (out.length) return out;
  }
  return plain;
}

let dragDepth = 0;
const libEl = $("lib");
libEl.addEventListener("dragenter", (e) => { if (!hasFiles(e)) return; e.preventDefault(); dragDepth++; $("lib-drop").hidden = false; });
libEl.addEventListener("dragover", (e) => { if (!hasFiles(e)) return; e.preventDefault(); e.dataTransfer.dropEffect = "copy"; });
libEl.addEventListener("dragleave", (e) => { if (!hasFiles(e)) return; dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) $("lib-drop").hidden = true; });
libEl.addEventListener("drop", async (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault(); dragDepth = 0; $("lib-drop").hidden = true;
  const dt = e.dataTransfer;
  const files = await droppedFiles(dt);
  importAdd(files);
});
// A file dropped outside the Library would make the browser navigate away from the editor.
for (const t of ["dragover", "drop"]) {
  window.addEventListener(t, (e) => { if (hasFiles(e) && !libEl.contains(e.target)) { e.preventDefault(); if (t === "drop") toast("Drop photos on the Library to import them", { why: "Switch to the Library module first." }); } });
}
