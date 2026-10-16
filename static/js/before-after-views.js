// ---- Before / after views ------------------------------------------------
// Split: the Before image is clipped to the left of a draggable divider.
// Side by side: two panes. Before = the as-imported (auto) settings.
async function ensureBefore() {
  if (beforeSrc || !current) return;
  const path = current;
  const data = await post("/api/preview", {
    path, adjustments: importValues || { ...neutral, monochrome: false },
  });
  if (!data.error && path === current) beforeSrc = data.preview;
}

function layoutBA() {
  const layer = $("ba-layer");
  layer.classList.toggle("side", baMode === "side");
  $("ba-before").style.clipPath = baMode === "split" ? `inset(0 ${100 - baSplit}% 0 0)` : "none";
  $("ba-divider").style.left = `${baSplit}%`;
}

async function refreshBA() {
  document.querySelectorAll("#ba-bar button").forEach(b => b.classList.toggle("active", b.dataset.ba === baMode));
  const on = baMode !== "off" && !!current;
  $("ba-layer").classList.toggle("on", on);
  $("preview").style.visibility = on ? "hidden" : "";
  if (typeof layoutMaskLayer === "function") layoutMaskLayer();
  if (!on) return;
  $("ba-after").src = $("preview").src;
  await ensureBefore();
  if (beforeSrc) $("ba-before").src = beforeSrc;
  layoutBA();
}

document.querySelectorAll("#ba-bar button").forEach(b => b.addEventListener("click", () => {
  baMode = b.dataset.ba;
  refreshBA();
}));
// Keep the After pane in step with live edits.
$("preview").addEventListener("load", () => { if (baMode !== "off") $("ba-after").src = $("preview").src; });

(() => {
  let drag = false;
  const move = (e) => {
    const r = $("ba-layer").getBoundingClientRect();
    baSplit = Math.min(100, Math.max(0, (e.clientX - r.left) / r.width * 100));
    layoutBA();
  };
  $("ba-divider").addEventListener("pointerdown", (e) => { drag = true; e.target.setPointerCapture(e.pointerId); move(e); });
  $("ba-divider").addEventListener("pointermove", (e) => { if (drag) move(e); });
  $("ba-divider").addEventListener("pointerup", () => { drag = false; });
  $("ba-divider").addEventListener("pointercancel", () => { drag = false; });
})();
