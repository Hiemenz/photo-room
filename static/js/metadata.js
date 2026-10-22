// ---- Metadata panel -------------------------------------------------------
// Read-only EXIF/IPTC readout, title / caption / creator / copyright / status /
// usage terms editing written to the XMP sidecar, and named metadata presets.
// One panel element: it sits in the Develop side panel, and moves to the right
// of the Library grid while the Library is showing. Fields save when you leave
// them (only the field you changed is written, so a multi-photo selection with
// differing titles keeps them); Undo restores what the sidecar held before.
(function () {
  const panel = $("meta-panel");
  const FIELDS = ["title", "caption", "creator", "copyright", "copyright_status", "usage_terms"];
  const fieldEl = (f) => panel.querySelector(`[data-field="${f}"]`);
  const INFO_ROWS = [["camera", "Camera"], ["lens", "Lens"], ["iso", "ISO"], ["shutter", "Shutter"],
    ["aperture", "Aperture"], ["focal_length", "Focal length"], ["captured", "Captured"],
    ["gps", "GPS"], ["dimensions", "Dimensions"]];
  let paths = [];
  let key = "";
  let gen = 0;
  let presets = {};
  const undoStack = [];

  function targets() {
    if (module_ === "library") {
      if (typeof libMulti !== "undefined" && libMulti.size) return [...libMulti];
      return libActive ? [libActive] : [];
    }
    if (selected.size) return [...selected];
    return current ? [current] : [];
  }

  function place() {
    const host = module_ === "library" ? $("lib-meta") : $("controls-scroll");
    if (panel.parentElement !== host) {
      if (module_ === "library") host.appendChild(panel);
      else host.insertBefore(panel, $("looks-panel").nextSibling);
    }
  }

  async function api(url, body) {
    const res = await fetch(url, body === undefined ? undefined : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
    return data;
  }

  function showInfo(info) {
    const dl = $("meta-info");
    dl.textContent = "";
    for (const [k, label] of INFO_ROWS) {
      let v = info ? info[k] : "";
      if (k === "gps") v = info && info.gps ? info.gps.text : "";
      if (v === null || v === undefined || v === "") continue;
      const dt = document.createElement("dt"); dt.textContent = label;
      const dd = document.createElement("dd"); dd.textContent = String(v);
      dl.append(dt, dd);
    }
    if (!dl.children.length) {
      const dd = document.createElement("dd"); dd.className = "meta-note";
      dd.textContent = info ? "No camera data in this file." : "";
      dl.append(dd);
    }
  }

  function showFields(items) {
    for (const f of FIELDS) {
      const el = fieldEl(f);
      const vals = new Set(items.map(i => i.fields[f]));
      const mixed = vals.size > 1;
      el.classList.toggle("mixed", mixed);
      el.disabled = !items.length;
      if (el.tagName === "SELECT") {
        let m = el.querySelector('option[value="__mixed"]');
        if (mixed && !m) { m = new Option("Mixed", "__mixed"); m.disabled = true; el.add(m, 0); }
        if (!mixed && m) m.remove();
        el.value = mixed ? "__mixed" : (items[0] ? items[0].fields[f] : "unknown");
      } else {
        el.value = mixed || !items[0] ? "" : items[0].fields[f];
        el.placeholder = mixed ? "Mixed" : (f === "copyright" ? "(c) 2026 Your Name" : "");
      }
    }
  }

  async function refresh(force) {
    place();
    paths = targets();
    const k = module_ + "|" + paths.join("\n");
    if (!force && k === key) return;
    key = k;
    const mine = ++gen;
    const name = (p) => p.split("/").pop();
    $("meta-target").textContent = !paths.length ? "No photo selected"
      : paths.length === 1 ? name(paths[0]) : `${paths.length} photos selected (showing ${name(paths[0])})`;
    if (!paths.length) { showInfo(null); showFields([]); return; }
    try {
      const q = new URLSearchParams(); paths.forEach(p => q.append("path", p));
      const data = await api("/api/metadata?" + q);
      if (mine !== gen) return;
      showInfo(data.items[0].info);
      showFields(data.items);
    } catch (e) {
      if (mine === gen) { showInfo(null); showFields([]); toastError("Reading metadata", e); }
    }
  }

  function pushUndo(previous, label) {
    undoStack.push({ previous, label });
    if (undoStack.length > 20) undoStack.shift();
    $("meta-undo").disabled = false;
  }

  async function save(fields, what) {
    if (!paths.length) return;
    try {
      const data = await api("/api/metadata", { paths, fields });
      pushUndo(data.previous, what);
      toast(data.message);
    } catch (e) { toastError("Saving metadata", e); }
    refresh(true);
  }

  panel.querySelectorAll("[data-field]").forEach(el =>
    el.addEventListener("change", () => {
      if (el.value === "__mixed") return;
      save({ [el.dataset.field]: el.value }, el.dataset.field);
    }));
  panel.querySelectorAll("input[data-field]").forEach(el =>
    el.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); el.blur(); } }));

  $("meta-undo").addEventListener("click", async () => {
    const step = undoStack.pop();
    $("meta-undo").disabled = !undoStack.length;
    if (!step) return;
    try {
      await api("/api/metadata", { per_path: step.previous });
      toast("Metadata change undone");
    } catch (e) { toastError("Undoing the metadata change", e); }
    refresh(true);
  });

  // ---- Presets ------------------------------------------------------------
  function paintPresets(chosen) {
    const sel = $("meta-preset");
    const want = chosen !== undefined ? chosen : sel.value;
    sel.length = 1;
    for (const name of Object.keys(presets).sort((a, b) => a.localeCompare(b))) sel.add(new Option(name, name));
    sel.value = presets[want] ? want : "";
    syncPresetButtons();
  }
  function syncPresetButtons() {
    const has = !!$("meta-preset").value;
    $("meta-preset-apply").disabled = !has;
    $("meta-preset-del").disabled = !has;
  }
  async function loadPresets() {
    try { presets = (await api("/api/metadata/presets")).presets; paintPresets(); } catch (e) { /* offline: no presets */ }
  }
  $("meta-preset").addEventListener("change", syncPresetButtons);

  $("meta-preset-apply").addEventListener("click", async () => {
    const name = $("meta-preset").value;
    if (!name || !paths.length) return;
    try {
      const data = await api("/api/metadata/presets/apply", { name, paths });
      pushUndo(data.previous, "preset " + name);
      toast(data.message);
    } catch (e) { toastError("Applying the preset", e); }
    refresh(true);
  });

  $("meta-preset-save").addEventListener("click", async () => {
    const fields = {};
    for (const f of FIELDS) {
      const el = fieldEl(f);
      if (el.value && el.value !== "__mixed" && !(f === "copyright_status" && el.value === "unknown")) fields[f] = el.value;
    }
    if (!Object.keys(fields).length) { toast("Fill in a field first", { kind: "error", why: "A preset stores the fields that have a value." }); return; }
    const name = prompt("Metadata preset name (for example: Copyright)");
    if (!name || !name.trim()) return;
    try {
      const data = await api("/api/metadata/presets", { name, fields });
      presets = data.presets;
      paintPresets(name.trim().split(/\s+/).join(" "));
      toast(`Preset saved: ${name.trim()}`);
    } catch (e) { toastError("Saving the preset", e); }
  });

  $("meta-preset-del").addEventListener("click", async () => {
    const name = $("meta-preset").value;
    if (!name || !confirm(`Delete the metadata preset "${name}"? Photos already tagged keep their metadata.`)) return;
    try { presets = (await api("/api/metadata/presets/delete", { name })).presets; paintPresets(""); }
    catch (e) { toastError("Deleting the preset", e); }
  });

  // ---- Follow the selection -----------------------------------------------
  // Selection lives in several modules (grid clicks, file list, batch ticks);
  // a light poll of the resulting target list is simpler than hooking them all.
  // A field being edited is saved by its own change event before focus leaves.
  setInterval(() => {
    if (document.hidden || !panel.open && module_ !== "library") return;
    if (panel.contains(document.activeElement) && key === module_ + "|" + targets().join("\n")) return;
    refresh(false);
  }, 400);
  panel.addEventListener("toggle", () => { if (panel.open) refresh(true); });
  window.metadataPanel = { refresh: () => refresh(true) };
  place();
  loadPresets();
  refresh(true);
})();
