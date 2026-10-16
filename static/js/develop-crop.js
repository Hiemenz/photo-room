// ---- Crop ----------------------------------------------------------------
// The crop is stored as fractions of the frame rather than pixels, so it
// survives the preview being a different size from the full-resolution file.
function cropRectOnScreen() {
  const img = $("preview");
  const stage = $("stage").getBoundingClientRect();
  const r = img.getBoundingClientRect();
  return { left: r.left - stage.left, top: r.top - stage.top, width: r.width, height: r.height };
}

function paintCropBox() {
  if (!cropping) return;
  const frame = cropRectOnScreen();
  const c = values.crop || { left: 0, top: 0, right: 1, bottom: 1 };
  const box = $("crop-box");
  box.style.left   = `${frame.left + c.left * frame.width}px`;
  box.style.top    = `${frame.top + c.top * frame.height}px`;
  box.style.width  = `${(c.right - c.left) * frame.width}px`;
  box.style.height = `${(c.bottom - c.top) * frame.height}px`;
}

function setCrop(c) {
  const EPS = 0.0005;
  const full = c.left <= EPS && c.top <= EPS && c.right >= 1 - EPS && c.bottom >= 1 - EPS;
  values.crop = full ? null : c;   // a full-frame crop is no crop at all
  paintCropBox();
  schedulePreview();
}

$("crop-toggle").addEventListener("click", () => {
  cropping = !cropping;
  if (typeof layoutMaskLayer === "function") setTimeout(layoutMaskLayer, 0);
  $("crop-layer").classList.toggle("on", cropping);
  $("crop-toggle").classList.toggle("active", cropping);
  if (cropping) {
    if (!values.crop) values.crop = { left: 0, top: 0, right: 1, bottom: 1 };
    paintCropBox();
  }
});

$("crop-reset").addEventListener("click", () => {
  setCrop({ left: 0, top: 0, right: 1, bottom: 1 });
  commit("Reset crop");
});

document.querySelectorAll("#crop-bar button[data-ratio]").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#crop-bar button[data-ratio]")
      .forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    cropRatio = btn.dataset.ratio === "free" ? null : parseFloat(btn.dataset.ratio);
    if (cropRatio) applyRatio();
  });
});

function applyRatio() {
  // Ratios are in screen terms, so the frame's own proportions have to be
  // divided out before the fractional crop can honour them.
  const frame = cropRectOnScreen();
  const frameRatio = frame.width / frame.height;
  let w = 1, h = 1;
  if (cropRatio > frameRatio) h = frameRatio / cropRatio;
  else w = cropRatio / frameRatio;

  setCrop({
    left: (1 - w) / 2, right: (1 + w) / 2,
    top: (1 - h) / 2,  bottom: (1 + h) / 2,
  });
}

// Drag the corners, or the box itself to reposition.
(function enableCropDrag() {
  let mode = null, startX = 0, startY = 0, origin = null;

  function begin(e, newMode) {
    mode = newMode;
    startX = e.clientX; startY = e.clientY;
    origin = { ...(values.crop || { left: 0, top: 0, right: 1, bottom: 1 }) };
    e.preventDefault(); e.stopPropagation();
  }

  $("crop-box").addEventListener("mousedown", (e) => begin(e, "move"));
  document.querySelectorAll(".handle").forEach(h => {
    h.addEventListener("mousedown", (e) => begin(e, h.dataset.corner));
  });

  document.addEventListener("mousemove", (e) => {
    if (!mode) return;
    const frame = cropRectOnScreen();
    const dx = (e.clientX - startX) / frame.width;
    const dy = (e.clientY - startY) / frame.height;
    const c = { ...origin };
    const MIN = 0.05;

    if (mode === "move") {
      const w = origin.right - origin.left, h = origin.bottom - origin.top;
      c.left = Math.min(Math.max(0, origin.left + dx), 1 - w);
      c.top = Math.min(Math.max(0, origin.top + dy), 1 - h);
      c.right = c.left + w;
      c.bottom = c.top + h;
    } else {
      if (mode.includes("w")) c.left = Math.min(Math.max(0, origin.left + dx), origin.right - MIN);
      if (mode.includes("e")) c.right = Math.max(Math.min(1, origin.right + dx), origin.left + MIN);
      if (mode.includes("n")) c.top = Math.min(Math.max(0, origin.top + dy), origin.bottom - MIN);
      if (mode.includes("s")) c.bottom = Math.max(Math.min(1, origin.bottom + dy), origin.top + MIN);

      if (cropRatio) {
        // Keep the locked ratio by driving height from the new width.
        const frameRatio = frame.width / frame.height;
        const w = c.right - c.left;
        const h = Math.min(1, (w * frameRatio) / cropRatio);
        if (mode.includes("n")) c.top = Math.max(0, c.bottom - h);
        else c.bottom = Math.min(1, c.top + h);
      }
    }
    setCrop(c);
  });

  document.addEventListener("mouseup", () => { if (mode) commit("Crop"); mode = null; });
})();

window.addEventListener("resize", paintCropBox);
