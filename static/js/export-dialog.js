// ---- Export dialog (M1b) ---------------------------------------------------
(() => {
  const el = (id) => document.getElementById(id);
  const dlg = el("export-dialog");
  let running = false, previewTimer = null;

  const scopePaths = () => {
    const scope = el("ex-scope").value;
    return { scope, path: current, paths: [...selected] };
  };
  const options = () => ({
    ...scopePaths(),
    dest: el("ex-dest").value,
    template: el("ex-template").value,
    format: el("ex-format").value,
    quality: +el("ex-quality").value,
    bit_depth: ["png", "tiff"].includes(el("ex-format").value) ? +el("ex-depth").value : 8,
    colorspace: el("ex-cs").value || null,
    long_edge: el("ex-resize").value === "edge" ? +el("ex-edge").value : null,
    keep_metadata: el("ex-meta").value !== "none",
    metadata: el("ex-meta").value,
    sharpen: el("ex-sharp").value ? { for: el("ex-sharp").value, amount: el("ex-sharp-amt").value } : null,
    watermark: watermark(),
    dest_mode: el("ex-dest-mode").value,
    subfolder: el("ex-subfolder").value.trim(),
    custom_text: el("ex-text").value,
    post_action: postAction(),
  });
  const postAction = () => {
    const kind = el("ex-post").value;
    if (kind === "open") return { type: "open", app: el("ex-post-app").value.trim() };
    if (kind === "command" && el("ex-post-cmd").value.trim()) return { type: "command", argv: el("ex-post-cmd").value };
    return null;
  };
  const watermark = () => {
    const type = el("ex-wm").value;
    if (!type) return null;
    return { type, text: el("ex-wm-text").value, font: el("ex-wm-font").value, size: +el("ex-wm-size").value,
      color: el("ex-wm-color").value, image: el("ex-wm-image").value, scale: +el("ex-wm-scale").value,
      opacity: +el("ex-wm-opacity").value, position: el("ex-wm-pos").value, margin: +el("ex-wm-margin").value };
  };
  const applySettings = (st) => {
    el("ex-format").value = st.format || "jpeg";
    el("ex-quality").value = st.quality ?? 92;
    el("ex-depth").value = String(st.bit_depth || 8);
    el("ex-cs").value = st.colorspace || "";
    el("ex-resize").value = st.long_edge ? "edge" : "none";
    if (st.long_edge) el("ex-edge").value = st.long_edge;
    el("ex-meta").value = st.keep_metadata === false ? "none" : (st.metadata || "all");
    if (st.template) el("ex-template").value = st.template;
    if (st.dest) el("ex-dest").value = st.dest;
    el("ex-dest-mode").value = st.dest_mode || "folder";
    el("ex-subfolder").value = st.subfolder || "";
    el("ex-text").value = st.custom_text || "";
    const pa = st.post_action;
    el("ex-post").value = pa ? pa.type : "";
    el("ex-post-app").value = pa && pa.type === "open" ? pa.app || "" : "";
    el("ex-post-cmd").value = pa && pa.type === "command" ? (Array.isArray(pa.argv) ? pa.argv.join(" ") : pa.argv) : "";
    el("ex-sharp").value = st.sharpen ? st.sharpen.for : "";
    el("ex-sharp-amt").value = st.sharpen ? st.sharpen.amount : "standard";
    const w = st.watermark;
    el("ex-wm").value = w ? w.type : "";
    if (w) {
      el("ex-wm-text").value = w.text ?? ""; el("ex-wm-font").value = w.font ?? ""; el("ex-wm-size").value = w.size;
      el("ex-wm-color").value = w.color || "#ffffff"; el("ex-wm-image").value = w.image ?? "";
      el("ex-wm-scale").value = w.scale; el("ex-wm-opacity").value = w.opacity;
      el("ex-wm-pos").value = w.position; el("ex-wm-margin").value = w.margin;
    }
  };
  let presets = [];
  async function loadPresets(select) {
    try { presets = (await (await fetch("/api/export/presets")).json()).presets || []; } catch (e) { return; }
    const sel = el("ex-preset");
    sel.innerHTML = '<option value="">(custom)</option>';
    for (const p of presets) {
      const o = document.createElement("option");
      o.value = p.name; o.textContent = p.name + (p.builtin ? " (built-in)" : "");
      sel.appendChild(o);
    }
    sel.value = select || "";
    el("ex-preset-del").disabled = !presets.some((p) => p.name === sel.value && !p.builtin);
  }
  const msg = (t, err = false) => { el("ex-msg").textContent = t; el("ex-msg").classList.toggle("err", err); };

  function syncControls() {
    const fmt = el("ex-format").value;
    el("ex-q-row").hidden = !["jpeg", "webp"].includes(fmt);
    el("ex-depth-row").hidden = !["png", "tiff"].includes(fmt);
    el("ex-q-val").textContent = el("ex-quality").value;
    const mode = el("ex-dest-mode").value, postKind = el("ex-post").value;
    el("ex-dest-row").hidden = mode === "original";
    el("ex-sub-row").hidden = mode !== "subfolder";
    el("ex-text-row").hidden = !el("ex-template").value.includes("{text");
    el("ex-post-app-row").hidden = postKind !== "open";
    el("ex-post-cmd-row").hidden = postKind !== "command";
    el("ex-post-hint").hidden = postKind !== "command";
    el("ex-sheet-go").disabled = !selected.size && !current;
    el("ex-edge").disabled = el("ex-resize").value !== "edge";
    const wm = el("ex-wm").value;
    el("ex-wm-opts").hidden = !wm;
    for (const id of ["text", "font", "size", "color"]) el("ex-wm-" + id + "-row").hidden = wm !== "text";
    for (const id of ["image", "scale"]) el("ex-wm-" + id + "-row").hidden = wm !== "image";
    el("ex-sharp-amt").disabled = !el("ex-sharp").value;
    const scope = el("ex-scope").value;
    el("ex-scope").querySelector('[value="current"]').disabled = !current;
    el("ex-scope").querySelector('[value="selected"]').disabled = !selected.size;
    el("ex-count").textContent = scope === "current" ? (current || "No photo open")
      : scope === "selected" ? `${selected.size} selected photo(s)` : `Every photo in ${files.length ? "the folder (" + files.length + ")" : "the folder"}`;
    schedulePreview();
  }

  function schedulePreview() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(async () => {
      let data;
      try { data = await post("/api/export/preview", options()); } catch (e) { return; }
      el("ex-preview").classList.remove("err");
      if (data.error) { el("ex-preview").textContent = data.error; el("ex-preview").classList.add("err"); return; }
      const first = data.names.slice(0, 3).map((n) => n.name).join(", ");
      el("ex-preview").textContent = first + (data.count > 3 ? ` … (${data.count} files)` : "") + (data.approximate ? " (size approximate)" : "");
    }, 150);
  }

  async function open() {
    if (!dlg.open) {
      if (!el("ex-dest").value) {
        try {
          const d = await (await fetch("/api/export/defaults")).json();
          el("ex-dest").value = d.dest;
          for (const f of ["avif", "jxl"]) {
            const o = el("ex-format").querySelector(`[value="${f}"]`);
            o.hidden = o.disabled = !d.formats.includes(f);
          }
          el("ex-prev").disabled = !d.has_previous;
          if (!d.exiftool) msg("exiftool is not installed; exporting will fail until it is.", true);
        } catch (e) {}
      }
      loadPresets(el("ex-preset").value);
      try {
        const dd = await (await fetch("/api/export/defaults")).json();
        el("ex-fonts").innerHTML = (dd.fonts || []).map((f) => `<option value="${esc(f)}">`).join("");
      } catch (e) {}
      refreshQueue();
      el("ex-scope").value = selected.size > 1 ? "selected" : (current ? "current" : "folder");
      dlg.showModal();
    }
    syncControls();
  }

  const esc = (t) => String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  async function postJson(url) { return (await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })).json(); }

  function renderQueue(jobs) {
    const box = el("ex-queue");
    box.innerHTML = "";
    for (const j of jobs.slice(-6).reverse()) {
      const d = document.createElement("details");
      d.open = j.state === "running" || j.state === "interrupted";
      const bad = j.items.filter((i) => i.status === "error").length;
      const label = j.state === "queued" ? `queued (#${j.queue_position})` : j.state;
      d.innerHTML = `<summary style="font-size:12px;cursor:pointer">${esc(j.dest.split("/").pop() || j.dest)}: ${label}, ${j.done}/${j.total}${bad ? `, ${bad} failed` : ""}</summary>`;
      const ul = document.createElement("div");
      ul.className = "hint";
      ul.innerHTML = j.items.map((i) => `<div${i.status === "error" ? ' class="err"' : ""}>${esc(i.name)}: ${i.status === "ok" ? "written" : i.status === "error" ? esc(i.error) : i.status}</div>`).join("");
      d.appendChild(ul);
      if (j.state === "queued" || j.state === "running" || j.state === "interrupted") {
        const cancel = document.createElement("button");
        cancel.textContent = "Cancel"; cancel.type = "button";
        cancel.onclick = async () => { await postJson("/api/export/cancel/" + j.id); refreshQueue(); };
        d.appendChild(cancel);
      }
      if (j.state === "interrupted") {
        const resume = document.createElement("button");
        resume.textContent = "Resume"; resume.type = "button";
        resume.onclick = async () => { const r = await postJson("/api/export/resume/" + j.id); if (r.error) msg(r.error, true); refreshQueue(); };
        d.appendChild(resume);
      }
      box.appendChild(d);
    }
  }
  let queueTimer = null;
  async function refreshQueue() {
    clearTimeout(queueTimer);
    let jobs = [];
    try { jobs = (await (await fetch("/api/export/jobs")).json()).jobs || []; } catch (e) { return; }
    renderQueue(jobs);
    const active = jobs.filter((j) => j.state === "running" || j.state === "queued");
    const run = active.find((j) => j.state === "running") || active[0];
    if (run) {
      el("ex-progress").hidden = false;
      el("ex-bar").style.width = (100 * run.done / Math.max(1, run.total)) + "%";
      msg(run.state === "running" ? `Exporting ${run.done + 1} of ${run.total}${run.current ? ": " + run.current : ""}` : `Queued (#${run.queue_position})`);
    }
    if (active.length && dlg.open) queueTimer = setTimeout(refreshQueue, 500);
    else if (!active.length && jobs.length) {
      const last = jobs[jobs.length - 1];
      const after = last.post && last.post.message ? `. After export: ${last.post.message}` : "";
      if (last.state !== "interrupted") msg(`Export ${last.state}: ${last.written.length} of ${last.total} to ${last.dest}` + (last.failed.length ? ` -- ${last.failed.length} failed` : "") + after, !!last.failed.length || !!(last.post && !last.post.ok));
    }
  }

  async function saveOpenPhoto() {
    if (!(el("ex-save").checked && current && ["current", "folder", "selected"].includes(el("ex-scope").value)
        && (el("ex-scope").value !== "selected" || selected.has(current)))) return true;
    msg("Saving edits…");
    const saved = await post("/api/save", { path: current, adjustments: { ...values, _baseline_temp: baselineTemp } });
    if (saved.error) { msg(saved.error, true); return false; }
    return true;
  }

  // Export with Previous: the last export's settings, applied to the photos chosen now.
  async function exportPrevious() {
    if (running) return;
    running = true; el("ex-go").disabled = el("ex-prev").disabled = true;
    try {
      if (dlg.open) { el("ex-progress").hidden = false; el("ex-bar").style.width = "0"; }
      const scope = selected.size > 1 ? "selected" : (current ? "current" : "folder");
      if (!(await saveOpenPhoto())) return;
      msg("Starting…");
      const job = await post("/api/export/previous", { scope, path: current, paths: [...selected] });
      if (job.error) { msg(job.error, true); if (!dlg.open) alert(job.error); return; }
      if (!dlg.open) await open();
      await refreshQueue();
    } catch (e) {
      msg(String(e), true);
    } finally {
      running = false; el("ex-go").disabled = el("ex-prev").disabled = false;
    }
  }

  async function go() {
    if (running) return;
    if (el("ex-dest-mode").value === "ask") {
      const where = prompt("Export to which folder?", el("ex-dest").value);
      if (!where) return;
      el("ex-dest").value = where;
    }
    running = true; el("ex-go").disabled = true;
    el("ex-progress").hidden = false; el("ex-bar").style.width = "0";
    try {
      if (!(await saveOpenPhoto())) return;
      msg("Starting…");
      const job = await post("/api/export", options());
      if (job.error) { msg(job.error, true); return; }
      await refreshQueue();
    } catch (e) {
      msg(String(e), true);
    } finally {
      running = false; el("ex-go").disabled = false;
    }
  }

  el("export-open").addEventListener("click", open);
  el("ex-cancel").addEventListener("click", () => dlg.close());
  el("ex-go").addEventListener("click", go);
  el("ex-prev").addEventListener("click", exportPrevious);
  el("ex-sheet-go").addEventListener("click", async () => {
    msg("Building the contact sheet…");
    try {
      const r = await post("/api/export/contact-sheet", {
        scope: selected.size ? "selected" : "current", path: current, paths: [...selected], dest: el("ex-dest").value,
        columns: +el("ex-sheet-cols").value, page: el("ex-sheet-page").value, title: el("ex-sheet-title").value,
      });
      msg(r.error ? r.error : `Contact sheet of ${r.count} photo(s): ${r.path}`, !!r.error);
    } catch (e) { msg(String(e), true); }
  });
  el("ex-preset").addEventListener("change", () => {
    const p = presets.find((x) => x.name === el("ex-preset").value);
    el("ex-preset-del").disabled = !p || p.builtin;
    if (p) { applySettings(p.settings); syncControls(); }
  });
  el("ex-preset-save").addEventListener("click", async () => {
    const name = prompt("Preset name", el("ex-preset").value || "");
    if (!name) return;
    const { scope, path, paths, ...settings } = options();
    const r = await post("/api/export/presets", { name, settings });
    if (r.error) { msg(r.error, true); return; }
    await loadPresets(r.name);
    msg(`Saved preset "${r.name}"`);
  });
  el("ex-preset-del").addEventListener("click", async () => {
    const name = el("ex-preset").value;
    if (!name || !confirm(`Delete preset "${name}"?`)) return;
    const r = await post("/api/export/presets/delete", { name });
    if (r.error) { msg(r.error, true); return; }
    await loadPresets("");
  });
  for (const id of ["ex-sharp", "ex-sharp-amt", "ex-wm", "ex-wm-text", "ex-wm-font", "ex-wm-size", "ex-wm-color", "ex-wm-image", "ex-wm-scale", "ex-wm-opacity", "ex-wm-pos", "ex-wm-margin"]) {
    el(id).addEventListener("input", syncControls);
    el(id).addEventListener("change", syncControls);
  }
  for (const id of ["ex-dest-mode", "ex-subfolder", "ex-text", "ex-post", "ex-post-app", "ex-post-cmd"]) {
    el(id).addEventListener("input", syncControls);
    el(id).addEventListener("change", syncControls);
  }
  for (const id of ["ex-scope", "ex-template", "ex-format", "ex-quality", "ex-depth", "ex-cs", "ex-resize", "ex-edge", "ex-dest"]) {
    el(id).addEventListener("input", syncControls);
    el(id).addEventListener("change", syncControls);
  }
  // Arrow keys in the dialog's controls must not step through photos behind it.
  dlg.addEventListener("keydown", (e) => e.stopPropagation());
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.altKey && (e.key === "E" || e.key === "e")) {
      e.preventDefault();
      exportPrevious();
      return;
    }
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && (e.key === "E" || e.key === "e")) {
      e.preventDefault();
      dlg.open ? dlg.close() : open();
    }
  }, true);
})();
