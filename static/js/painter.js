// ---- Painter ---------------------------------------------------------------
// Pick what to paint (a rating, flag, colour label, keywords, or the open
// photo's develop settings), then click or drag across grid cells to apply it.
// It only calls the endpoints the rest of the Library already uses, one photo
// at a time, so every paint is the same safe edit as doing it by hand.
let painterOn = false;
const painterDone = new Set();     // cells already painted in this stroke
let painterStroke = false;
let painterKeywords = false;       // keywords were painted: refresh the facets once the stroke ends

function painterKind() { return $("painter-kind").value; }

function painterShowOptions() {
  const kind = painterKind();
  for (const k of ["rating", "flag", "label"]) $(`painter-${k}`).hidden = kind !== k;
  $("painter-kw").hidden = kind !== "keywords";
  $("painter-note").textContent = kind === "settings"
    ? (current ? `Paints the develop settings of ${current.split("/").pop()} (framing, spots and masks stay out)` : "Open a photo in Develop first")
    : "";
}

function setPainter(on) {
  painterOn = on;
  $("lib-painter").classList.toggle("active", on);
  $("painter-opts").hidden = !on;
  $("lib-grid").classList.toggle("painting", on);
  painterShowOptions();
  if (!on) { painterStroke = false; painterDone.clear(); }
}
$("lib-painter").addEventListener("click", () => setPainter(!painterOn));
$("painter-kind").addEventListener("change", painterShowOptions);

async function painterApply(path) {
  const it = libItem(path);
  if (!it) return;
  switch (painterKind()) {
    case "rating": return libRate(parseInt($("painter-rating").value, 10), [path]);
    case "flag": return libFlag($("painter-flag").value, [path]);
    case "label": return libLabel($("painter-label").value, [path]);
    case "keywords": {
      const words = $("painter-kw").value.split(",").map(s => s.trim()).filter(Boolean);
      if (!words.length) return setStatus("Type the keywords to paint first", true);
      const d = await post("/api/keywords", { paths: [path], add: words });
      if (d.error) return setStatus(d.error, true);
      painterKeywords = true;
      return setStatus(d.message);
    }
    case "settings": {
      if (!current) return setStatus("Open a photo in Develop first", true);
      if (path === current) return;
      const d = await post("/api/batch_apply", { paths: [path], adjustments: values, baseline_temp: baselineTemp });
      if (d.error) return setStatus(d.error, true);
      return setStatus(d.message, d.failed.length > 0);
    }
  }
}

function painterPaint(e) {
  const cell = e.target.closest && e.target.closest(".lcell");
  if (!cell || painterDone.has(cell.dataset.path)) return;
  painterDone.add(cell.dataset.path);
  return painterApply(cell.dataset.path);
}

// Capture phase, so the cell's own select / open handlers never see a paint.
const painterGrid = $("lib-grid");
painterGrid.addEventListener("mousedown", (e) => {
  if (!painterOn || e.button !== 0) return;
  e.preventDefault(); e.stopPropagation();
  painterStroke = true;
  painterPaint(e);
}, true);
painterGrid.addEventListener("mouseover", (e) => { if (painterOn && painterStroke) painterPaint(e); }, true);
for (const type of ["click", "dblclick"]) {
  painterGrid.addEventListener(type, (e) => { if (painterOn) { e.preventDefault(); e.stopPropagation(); } }, true);
}
window.addEventListener("mouseup", () => {
  if (!painterStroke) return;
  painterStroke = false;
  painterDone.clear();
  if (painterKeywords) { painterKeywords = false; libLoad(true); }
});
// Dragging a cell must not start a native drag-and-drop while painting.
painterGrid.addEventListener("dragstart", (e) => { if (painterOn) { e.preventDefault(); e.stopPropagation(); } }, true);
window.addEventListener("keydown", (e) => {
  if (painterOn && e.key === "Escape") { e.preventDefault(); setPainter(false); }
});
