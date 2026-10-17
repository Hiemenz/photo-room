// ---- Histogram -----------------------------------------------------------
function drawHistogram(h) {
  const cv = $("histogram");
  const ctx = cv.getContext("2d");
  const W = cv.width, H = cv.height;
  ctx.clearRect(0, 0, W, H);
  if (!h) return;

  // One shared scale across channels so their relative heights stay meaningful.
  const peak = Math.max(1, ...h.r, ...h.g, ...h.b);
  const bins = h.r.length;
  const bw = W / bins;

  // Monochrome collapses the channels; draw one grey curve instead of three.
  const mono = values.monochrome;
  const channels = mono
    ? [["lum", "rgba(210,210,215,.85)"]]
    : [["r", "rgba(224,104,95,.75)"], ["g", "rgba(120,200,130,.75)"], ["b", "rgba(110,168,254,.75)"]];

  ctx.globalCompositeOperation = mono ? "source-over" : "lighter";
  for (const [key, color] of channels) {
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(0, H);
    for (let i = 0; i < bins; i++) {
      // sqrt keeps small counts visible without letting the peak dominate.
      const y = H - Math.sqrt(h[key][i] / peak) * H;
      ctx.lineTo(i * bw, y);
      ctx.lineTo((i + 1) * bw, y);
    }
    ctx.lineTo(W, H);
    ctx.closePath();
    ctx.fill();
  }
  ctx.globalCompositeOperation = "source-over";

  const sh = $("clip-shadows"), hi = $("clip-highlights");
  sh.textContent = `shadows ${h.clipped_shadows}%`;
  hi.textContent = `highlights ${h.clipped_highlights}%`;
  sh.classList.toggle("hot", h.clipped_shadows > 1);
  hi.classList.toggle("hot", h.clipped_highlights > 1);
}

function setStatus(msg, isError = false) {
  $("status").textContent = msg;
  $("status").classList.toggle("err", isError);
  if (isError && msg) toastError(null, msg);
}

// ---- Preview -------------------------------------------------------------
// Slider drags fire continuously. Each change renders a quick, smaller preview
// (one request in flight; later changes coalesce into the next one), and a
// sharp full-size render follows once the controls have been still for a moment.
// Every request carries a rising sequence number so the server can skip any
// that a newer one has overtaken.
let previewSeq = Date.now();
let previewInflight = null;   // {fast, abort}
let previewDirty = false;
let previewIdle = null;
const SETTLE_MS = 220;

function schedulePreview() {
  clearTimeout(previewIdle);
  previewIdle = setTimeout(() => renderPreview(false), SETTLE_MS);
  if (previewInflight && previewInflight.fast) { previewDirty = true; return; }
  renderPreview(true);
}

async function renderPreview(fast = false) {
  if (!current) return;
  if (previewInflight) previewInflight.abort.abort();     // overtaken: drop its response
  const token = ++renderToken;
  const abort = new AbortController();
  previewInflight = { fast, abort };
  $("spinner").classList.add("on");
  try {
    const res = await fetch("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: abort.signal,
      body: JSON.stringify({ path: current, adjustments: values, raw_frame: spotTool, fast, seq: ++previewSeq }),
    });
    const data = await res.json();
    if (token !== renderToken || data.stale) return;   // a newer render superseded this
    if (data.error) { setStatus(data.error, true); return; }
    drawHistogram(data.histogram);
    if (comparing) {
      editedSrc = data.preview;   // hold it until the compare key is released
    } else {
      $("preview").src = data.preview;
    }
  } catch (e) {
    if (token === renderToken && e.name !== "AbortError") setStatus(String(e), true);
  } finally {
    if (previewInflight && previewInflight.abort === abort) previewInflight = null;
    if (token === renderToken) {
      if (previewDirty) { previewDirty = false; renderPreview(true); }
      else $("spinner").classList.remove("on");
    }
  }
}

// ---- Open a file ---------------------------------------------------------
async function openFile(path) {
  current = path;
  comparing = false;
  editedSrc = null;
  $("compare-badge").classList.remove("on");
  document.querySelectorAll(".file").forEach(f => {
    f.classList.toggle("active", f.dataset.path === path);
  });
  const active = document.querySelector(".file.active");
  if (active) active.scrollIntoView({ block: "nearest" });
  syncGridState();
  // Opening a photo from the contact sheet should show it.
  if (gridOn) $("view-toggle").click();

  setStatus("Loading…");
  $("spinner").classList.add("on");
  ++renderToken;   // discard any in-flight render for the previous photo

  try {
    const res = await fetch("/api/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    const data = await res.json();
    if (data.error) { setStatus(data.error, true); return; }

    values = data.adjustments;
    importValues = data.import_adjustments || null;
    beforeSrc = null;
    setHistory(data.history);
    drawHistogram(data.histogram);
    paintCropBox();
    // "Unedited" means the neutral defaults, but keyed to this camera's own
    // white balance so reverting doesn't throw the photo to a fixed 5500K.
    neutral = Object.fromEntries(
      ALL_CONTROLS.map(c => [c.key, c.default]));
    // The camera's own white balance, not the saved one. Using the saved
    // value meant hold-to-compare rendered the "before" at the edited
    // temperature, hiding the whole white-balance part of the edit.
    baselineTemp = data.baseline_temp ?? data.adjustments.color_temp;
    neutral.color_temp = baselineTemp;

    setSpotTool(false);
    paintAll();
    setLensNote(data.lens);
    $("preview").src = data.preview;
    $("preview").hidden = false;
    $("empty").hidden = true;
    $("meta").querySelector("strong").textContent =
      (typeof libItem === "function" && libItem(path)?.name) || path.split("/").pop();
    // Warn before the photographer overwrites work done elsewhere.
    if (data.foreign_editor) {
      const where = data.has_sidecar ? " and has an .xmp sidecar" : "";
      setWarning(`Edited in ${data.foreign_editor}${where} — saving will replace those settings`);
    }

    const q = data.quality || {};
    const badge = $("quality");
    if (q.verdict) {
      badge.innerHTML = `<b>${q.verdict}</b>${q.reason} — no adjustment recovers this.`;
      badge.hidden = false;
    } else {
      badge.hidden = true;
    }
    // Reported without a verdict on purpose: this measures how much detail a
    // scene contains, not whether focus was missed.
    $("sharp").textContent =
      `detail ${q.sharpness ?? "–"} · clipping ${q.clipped_high ?? 0}% high / ${q.clipped_low ?? 0}% low`;
    $("sharp").hidden = false;
    if (typeof data.rating === "number") ratings[current] = data.rating;
    paintRating();

    $("source").textContent = data.source === "saved"
      ? "Showing previously saved edits"
      : "Showing auto-computed edits (not yet saved)";

    for (const id of ["save", "reset", "copy", "crop-toggle", "spot-toggle", "preset-save", "reset-import", "snap-new"]) $(id).disabled = false;
    $("ba-bar").hidden = false;
    refreshBA();
    $("paste").disabled = !clipboard;
    updateSelectionUI();
    $("recover").disabled = !data.has_original;
    $("recover").title = data.has_original
      ? "Write the embedded original raw back out as a file"
      : "This file has no embedded original raw";
    setStatus("");
  } catch (e) {
    setStatus(String(e), true);
  } finally {
    $("spinner").classList.remove("on");
  }
}
