// ---- Import dialog: pick a card or folder, tick frames, choose where and how they go ----
// The page is built here so the editor template only needs this script. The server does
// the copying (cardwatch.import_card): verified, never overwriting, duplicates by content,
// a .xmp sidecar carried with each raw, an optional verified second copy, then safe eject.
(() => {
  const h = (tag, attrs = {}, ...kids) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") e.className = v; else if (k === "text") e.textContent = v;
      else if (k.startsWith("on")) e.addEventListener(k.slice(2), v); else if (v !== false && v != null) e.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid != null) e.append(kid);
    return e;
  };
  const st = { frames: [], source: "", card: false, sel: new Set(), hideDup: true, busy: false, poll: null, lastJob: null };
  let dlg, ui = {};

  const fmtSize = (n) => n > 2 ** 30 ? (n / 2 ** 30).toFixed(1) + " GB" : n > 2 ** 20 ? (n / 2 ** 20).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB";
  const api = async (url, body) => {
    const r = body === undefined ? await fetch(url, { cache: "no-store" })
      : await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    let d; try { d = await r.json(); } catch (e) { d = { error: "The server sent an unexpected reply" }; }
    return d;
  };
  const msg = (text, bad) => { ui.msg.textContent = text || ""; ui.msg.classList.toggle("err", !!bad); };

  function build() {
    ui.card = h("select", { id: "imd-card", "aria-label": "Card" });
    ui.path = h("input", { id: "imd-path", type: "text", placeholder: "/media/you/EOS_DIGITAL or a folder", spellcheck: "false" });
    ui.browse = h("div", { class: "imd-browse", hidden: true });
    ui.grid = h("div", { class: "imd-grid", id: "imd-grid", role: "list" });
    ui.count = h("span", { class: "imd-count" });
    ui.hide = h("input", { type: "checkbox", id: "imd-hide", checked: true });
    ui.dest = h("input", { id: "imd-dest", type: "text", placeholder: "(library root)", spellcheck: "false" });
    ui.layout = h("select", { id: "imd-layout" },
      h("option", { value: "date", text: "By date  (2026/09/14)" }), h("option", { value: "month", text: "By month  (2026/09)" }),
      h("option", { value: "flat", text: "One folder" }), h("option", { value: "custom", text: "Template…" }));
    ui.layoutTpl = h("input", { id: "imd-layout-tpl", type: "text", value: "{yyyy}/{date}", hidden: true, spellcheck: "false", title: "Fields: {yyyy} {mm} {dd} {date} {camera}" });
    ui.rename = h("input", { id: "imd-rename", type: "text", placeholder: "keep camera names", spellcheck: "false", title: "Fields: {date} {yyyy} {mm} {dd} {time} {seq} {seq:N} {camera} {original}" });
    ui.seq = h("input", { id: "imd-seq", type: "number", min: "1", value: "1", title: "First {seq} number" });
    ui.example = h("div", { class: "hint", id: "imd-example" });
    ui.sidecars = h("input", { type: "checkbox", id: "imd-sidecars", checked: true });
    ui.rating = h("select", { id: "imd-rating" }, h("option", { value: "", text: "(leave unrated)" }),
      ...[1, 2, 3, 4, 5].map(n => h("option", { value: n, text: "★".repeat(n) })));
    ui.kw = h("input", { id: "imd-kw", type: "text", placeholder: "comma, separated", spellcheck: "false" });
    ui.copy = h("input", { id: "imd-copy", type: "text", placeholder: "© 2026 Your Name" });
    ui.preset = h("select", { id: "imd-preset" }, h("option", { value: "", text: "(none)" }));
    ui.presetAmt = h("input", { id: "imd-preset-amt", type: "number", min: "0", max: "100", value: "100", title: "Amount %" });
    ui.backup = h("input", { id: "imd-backup", type: "text", placeholder: "(no second copy) /mnt/backup/photos", spellcheck: "false" });
    ui.eject = h("input", { type: "checkbox", id: "imd-eject" });
    ui.ejectRow = h("label", { class: "imd-check" }, ui.eject, " Eject the card when everything is verified");
    ui.msg = h("div", { class: "hint", id: "imd-msg", role: "status" });
    ui.bar = h("progress", { id: "imd-bar", max: "1", value: "0", hidden: true });
    ui.go = h("button", { id: "imd-go", type: "button", class: "primary", text: "Import", onclick: start });
    ui.cancel = h("button", { id: "imd-cancel", type: "button", text: "Cancel import", hidden: true, onclick: () => api("/api/import/cancel", {}) });
    ui.close = h("button", { id: "imd-close", type: "button", text: "Close", onclick: () => dlg.close() });
    const row = (label, ...kids) => h("label", { class: "imd-row" }, h("span", { text: label }), h("span", { class: "imd-in" }, ...kids));
    dlg = h("dialog", { id: "import-dialog" },
      h("h2", { text: "Import photos" }),
      h("div", { class: "imd-body" },
        h("section", { class: "imd-src" },
          row("From", ui.card, ui.path, h("button", { type: "button", text: "Browse…", onclick: () => browse(ui.path.value.trim() || "") }),
            h("button", { type: "button", id: "imd-scan", text: "Read", onclick: () => scan() })),
          ui.browse),
        h("section", { class: "imd-frames" },
          h("div", { class: "imd-bar" },
            h("button", { type: "button", id: "imd-all", text: "Select all", onclick: () => pick(true) }),
            h("button", { type: "button", id: "imd-none", text: "Select none", onclick: () => pick(false) }),
            h("label", { class: "imd-check" }, ui.hide, " Hide already imported"), ui.count),
          ui.grid),
        h("section", { class: "imd-opts" },
          row("Destination", ui.dest, ui.layout, ui.layoutTpl),
          row("Rename", ui.rename, h("span", { class: "imd-seq" }, "start at ", ui.seq)),
          ui.example,
          h("label", { class: "imd-check" }, ui.sidecars, " Carry each raw’s .xmp sidecar along"),
          row("Rating", ui.rating), row("Keywords", ui.kw), row("Copyright", ui.copy),
          row("Preset", ui.preset, ui.presetAmt),
          row("Backup copy", ui.backup),
          ui.ejectRow)),
      h("div", { class: "imd-foot" }, ui.msg, ui.bar, ui.cancel, ui.close, ui.go));
    document.body.append(dlg);
    ui.hide.addEventListener("change", () => { st.hideDup = ui.hide.checked; paint(); });
    ui.layout.addEventListener("change", () => { ui.layoutTpl.hidden = ui.layout.value !== "custom"; example(); });
    for (const e of [ui.layoutTpl, ui.rename, ui.seq, ui.dest]) e.addEventListener("input", example);
    ui.card.addEventListener("change", () => { if (ui.card.value) { ui.path.value = ui.card.value; scan(); } });
    ui.path.addEventListener("keydown", (e) => { if (e.key === "Enter") scan(); });
    dlg.addEventListener("close", () => { clearTimeout(st.poll); st.poll = null; });
  }

  // ---- folder picker -------------------------------------------------------------------
  async function browse(path) {
    const d = await api("/api/import/browse?path=" + encodeURIComponent(path));
    ui.browse.hidden = false; ui.browse.replaceChildren();
    if (d.error) { ui.browse.append(h("div", { class: "hint err", text: d.error })); return; }
    ui.browse.append(h("div", { class: "imd-here" }, h("b", { text: d.path }),
      h("button", { type: "button", text: "Use this folder", onclick: () => { ui.path.value = d.path; ui.browse.hidden = true; scan(); } })));
    const list = h("div", { class: "imd-dirs" });
    if (d.parent) list.append(h("button", { type: "button", text: "↑ ..", onclick: () => browse(d.parent) }));
    for (const n of d.dirs) list.append(h("button", { type: "button", text: n, onclick: () => browse(d.path.replace(/\/$/, "") + "/" + n) }));
    ui.browse.append(list);
  }

  // ---- frames --------------------------------------------------------------------------
  const visible = () => st.frames.filter(f => !(st.hideDup && f.duplicate));
  function pick(on) {
    for (const f of visible()) { if (on) st.sel.add(f.rel); else st.sel.delete(f.rel); }
    paint();
  }
  function paint() {
    const vis = visible(), hiddenN = st.frames.length - vis.length;
    ui.grid.replaceChildren(...vis.map((f) => {
      const cb = h("input", { type: "checkbox", "aria-label": "Import " + f.name, checked: st.sel.has(f.rel) });
      cb.addEventListener("change", () => { cb.checked ? st.sel.add(f.rel) : st.sel.delete(f.rel); count(); });
      const thumb = f.video ? h("div", { class: "imd-novid", text: "VIDEO" })
        : h("img", { loading: "lazy", alt: "", src: `/api/import/thumb?source=${encodeURIComponent(st.source)}&rel=${encodeURIComponent(f.rel)}` });
      if (thumb.tagName === "IMG") thumb.addEventListener("error", () => thumb.replaceWith(h("div", { class: "imd-novid", text: "no preview" })));
      return h("label", { class: "imd-frame" + (f.duplicate ? " dup" : ""), role: "listitem", title: f.duplicate ? `Already in the library: ${f.duplicate}` : f.name },
        thumb, cb,
        h("span", { class: "imd-name", text: f.name }),
        h("span", { class: "imd-tags" }, f.duplicate ? h("i", { text: "imported" }) : null, f.sidecar ? h("i", { text: "XMP" }) : null,
          h("em", { text: fmtSize(f.size) })));
    }));
    ui.grid.dataset.hidden = hiddenN;
    count(hiddenN);
  }
  function count(hiddenN) {
    const chosen = st.frames.filter(f => st.sel.has(f.rel));
    const bytes = chosen.reduce((a, f) => a + f.size, 0);
    ui.count.textContent = st.frames.length
      ? `${chosen.length} of ${st.frames.length} selected (${fmtSize(bytes)})` + (hiddenN ? `, ${hiddenN} already imported hidden` : "") : "";
    ui.go.textContent = chosen.length ? `Import ${chosen.length} photo${chosen.length === 1 ? "" : "s"}` : "Import";
    ui.go.disabled = st.busy || !chosen.length;
    ui.ejectRow.hidden = !st.card;
    if (!st.card) ui.eject.checked = false;
    example();
  }

  async function scan() {
    const path = ui.path.value.trim();
    if (!path) return msg("Choose a card, or type or browse to a folder.", true);
    msg("Reading…"); ui.grid.replaceChildren(); st.frames = []; st.sel.clear(); count();
    const d = await api("/api/import/scan", { source: path });
    if (d.error) return msg(d.error, true);
    st.source = d.source; st.card = d.card; st.frames = d.frames;
    ui.path.value = d.source;
    for (const f of d.frames) if (!f.duplicate) st.sel.add(f.rel);
    const bits = [];
    if (!d.frames.length) bits.push("No photos or videos found there.");
    if (d.errors && d.errors.length) bits.push(`${d.errors.length} folder(s) could not be read: ${d.errors[0]}`);
    if (d.truncated) bits.push("Only the first 20000 files are shown.");
    msg(bits.join(" "), !!(d.errors && d.errors.length));
    paint();
  }

  // ---- naming preview (the server decides; this shows the idea from file name and date) -----
  function example() {
    const f = st.frames.find(f => st.sel.has(f.rel));
    if (!f) { ui.example.textContent = ""; return; }
    const t = new Date((f.mtime || 0) * 1000), p = (n, w = 2) => String(n).padStart(w, "0");
    const v = { yyyy: p(t.getFullYear(), 4), mm: p(t.getMonth() + 1), dd: p(t.getDate()), time: p(t.getHours()) + p(t.getMinutes()) + p(t.getSeconds()),
      camera: "camera", original: f.name.replace(/\.[^.]*$/, "") };
    v.date = `${v.yyyy}-${v.mm}-${v.dd}`;
    const fill = (tpl) => tpl.replace(/\{(\w+)(?::(\d))?\}/g, (m, n, w) => n === "seq" ? p(+ui.seq.value || 1, +w || 4) : (v[n] ?? m));
    const base = ui.layout.value === "custom" ? ui.layoutTpl.value : ({ date: "{yyyy}/{mm}/{dd}", month: "{yyyy}/{mm}", flat: "" })[ui.layout.value];
    const ext = f.name.slice(f.name.lastIndexOf("."));
    const name = ui.rename.value.trim() ? fill(ui.rename.value.trim()) + ext : f.name;
    ui.example.textContent = "Example: " + [ui.dest.value.trim().replace(/^\/+|\/+$/g, ""), fill(base), name].filter(Boolean).join("/") + "  (dates here come from the file; the server reads the camera’s)";
  }

  // ---- run -----------------------------------------------------------------------------
  function options() {
    const layout = ui.layout.value === "custom" ? ui.layoutTpl.value.trim() : ui.layout.value;
    const preset = ui.preset.value ? { id: ui.preset.value, amount: +ui.presetAmt.value } : null;
    return {
      source: st.source, files: st.frames.filter(f => st.sel.has(f.rel)).map(f => f.rel),
      dest: ui.dest.value.trim(), layout, rename: ui.rename.value.trim(), seq_start: +ui.seq.value || 1,
      sidecars: ui.sidecars.checked, rating: ui.rating.value, keywords: ui.kw.value, copyright: ui.copy.value.trim(),
      preset, backup: ui.backup.value.trim(), eject: ui.eject.checked,
    };
  }
  async function start() {
    msg(""); st.busy = true; count();
    const d = await api("/api/import/run", options());
    if (d.error) { st.busy = false; count(); return msg(d.error, true); }
    watch();
  }
  const STATE = { scanning: "Checking what is already in the library", importing: "Copying", verifying: "Verifying every copy", ejecting: "Ejecting" };
  async function watch() {
    clearTimeout(st.poll);
    const j = await api("/api/import/job");
    const running = ["scanning", "importing", "verifying", "ejecting"].includes(j.state);
    st.busy = running;
    ui.bar.hidden = !running; ui.cancel.hidden = !running; ui.close.hidden = running;
    if (running) {
      ui.bar.max = Math.max(1, j.total || 1); ui.bar.value = j.done || 0;
      msg(`${STATE[j.state]}${j.total ? ` ${j.done || 0} of ${j.total}` : ""}${j.name ? ` - ${j.name}` : ""}`);
      st.poll = setTimeout(watch, 700);
    } else if (j.state && j.state !== "idle" && j.id !== st.lastJob) {
      st.lastJob = j.id;
      const bad = j.state !== "done";
      msg(j.message || "", bad);
      if (j.failed_files && j.failed_files.length) msg(`${j.message} First problem: ${j.failed_files[0].file}: ${j.failed_files[0].error}`, true);
      toast(j.state === "done" ? "Import finished" : "Import needs attention", { kind: bad ? "error" : "ok", why: j.message, ms: 9000 });
      count();
      try { await libSoftReload(); loadFiles().then(() => { if (filterMode !== "all") applyFilter(); }); } catch (e) { /* the live grid catches up */ }
    } else count();
  }

  async function open() {
    if (!dlg) build();
    const s = await api("/api/import/sources");
    if (!s.error) {
      ui.card.replaceChildren(h("option", { value: "", text: s.cards.length ? "Card…" : "No card found" }),
        ...s.cards.map(c => h("option", { value: c.path, text: c.label })));
      ui.preset.replaceChildren(h("option", { value: "", text: "(none)" }), ...s.presets.map(p => h("option", { value: p.id, text: p.name })));
      if (!ui.path.value && s.cards.length === 1) { ui.path.value = s.cards[0].path; scan(); }
    }
    dlg.showModal();
    watch();
  }

  const btn = h("button", { id: "lib-import-dialog", type: "button", title: "Choose a card or folder, pick frames, set where they go and how they are named", text: "Import… (choose)", onclick: open });
  const anchor = document.getElementById("lib-import");
  if (anchor) anchor.before(btn);
  window.openImportDialog = open;
})();
