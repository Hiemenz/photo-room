// ---- Crop options: ratios, orientation, rotate / mirror, overlays --------
// `values.crop_ratio` is "" (free), "orig" or "w:h" in screen terms, so a
// portrait crop is simply "2:3" and flipping orientation inverts it.
// `values.rot90` (clockwise quarter turns), `flip_h` and `flip_v` turn the
// picture itself; the server applies them before the perspective transform.
const RATIO_RE = /^([\d.]+):([\d.]+)$/;

function ratioFromId(id) {
  if (id === "orig") {
    const img = $("preview");
    return img.naturalWidth && img.naturalHeight ? img.naturalWidth / img.naturalHeight : null;
  }
  const m = RATIO_RE.exec(id || "");
  return m && parseFloat(m[2]) > 0 ? parseFloat(m[1]) / parseFloat(m[2]) : null;
}
function ratioId(w, h) {
  const t = (n) => String(Math.round(n * 1000) / 1000);
  return `${t(w)}:${t(h)}`;
}
function invertRatioId(id) {
  const m = RATIO_RE.exec(id || "");
  return m ? `${m[2]}:${m[1]}` : id;
}

function paintCropOptions() {
  const id = values.crop_ratio || "";
  cropRatio = ratioFromId(id);
  let preset = false;
  document.querySelectorAll("#crop-bar button[data-ratio]").forEach(b => {
    const on = b.dataset.cr === id;
    b.classList.toggle("active", on);
    preset = preset || on;
  });
  const m = RATIO_RE.exec(id);
  if (m && !preset) { $("crop-cw").value = m[1]; $("crop-ch").value = m[2]; }
  $("crop-custom-set").classList.toggle("active", !!m && !preset);
  $("crop-flip-h").classList.toggle("active", !!values.flip_h);
  $("crop-flip-v").classList.toggle("active", !!values.flip_v);
}

// "Original" depends on the picture's own proportions, known once it has loaded.
$("preview").addEventListener("load", () => { if (values.crop_ratio === "orig") cropRatio = ratioFromId("orig"); });

function setCropRatio(id, label) {
  values.crop_ratio = id;
  paintCropOptions();
  if (cropRatio) applyRatio();
  commit(label || "Crop ratio");
}

document.querySelectorAll("#crop-bar button[data-ratio]").forEach(btn => {
  btn.addEventListener("click", () => setCropRatio(btn.dataset.cr));
});

$("crop-custom-set").addEventListener("click", () => {
  const w = parseFloat($("crop-cw").value), h = parseFloat($("crop-ch").value);
  if (!(w > 0 && h > 0)) return setStatus("Enter a ratio such as 5 : 4", true);
  setCropRatio(ratioId(w, h), "Custom crop ratio");
});

// Portrait <-> landscape: invert a locked ratio, or swap the box's own shape.
function flipCropOrientation() {
  if (!current) return;
  const frame = cropRectOnScreen();
  const c = values.crop || { left: 0, top: 0, right: 1, bottom: 1 };
  const W = (c.right - c.left) * frame.width, H = (c.bottom - c.top) * frame.height;
  let w = H / frame.width, h = W / frame.height;
  const k = Math.min(1, 1 / w, 1 / h);
  w *= k; h *= k;
  const place = (centre, size) => Math.min(Math.max(centre, size / 2), 1 - size / 2);
  const x = place((c.left + c.right) / 2, w), y = place((c.top + c.bottom) / 2, h);
  if (values.crop_ratio) {
    values.crop_ratio = invertRatioId(values.crop_ratio === "orig" ? ratioId(frame.width, frame.height) : values.crop_ratio);
    paintCropOptions();
  }
  setCrop({ left: x - w / 2, right: x + w / 2, top: y - h / 2, bottom: y + h / 2 });
  commit("Flip crop orientation");
}
$("crop-orient").addEventListener("click", flipCropOrientation);

// ---- Rotate 90 degrees and mirror ----------------------------------------
// Turns the picture, and the crop box and ratio follow it so what you were
// framing stays framed. Mirrors act on the picture as shown; because the
// stored order is mirror-then-turn, a mirror also reverses the turn and the
// straighten angle.
function turnOrientation(op) {
  if (!current) return;
  const c = values.crop || { left: 0, top: 0, right: 1, bottom: 1 };
  let k = (((values.rot90 || 0) % 4) + 4) % 4, next;
  if (op === "left" || op === "right") {
    k = (k + (op === "right" ? 1 : 3)) % 4;
    next = op === "right"
      ? { left: 1 - c.bottom, top: c.left, right: 1 - c.top, bottom: c.right }
      : { left: c.top, top: 1 - c.right, right: c.bottom, bottom: 1 - c.left };
    if (values.crop_ratio) values.crop_ratio = invertRatioId(values.crop_ratio);
  } else {
    k = (4 - k) % 4;
    values.straighten = -(values.straighten || 0);
    next = op === "h"
      ? { left: 1 - c.right, top: c.top, right: 1 - c.left, bottom: c.bottom }
      : { left: c.left, top: 1 - c.bottom, right: c.right, bottom: 1 - c.top };
    const key = op === "h" ? "flip_h" : "flip_v";
    values[key] = !values[key];
  }
  values.rot90 = k;
  const EPS = 0.0005;
  values.crop = (next.left <= EPS && next.top <= EPS && next.right >= 1 - EPS && next.bottom >= 1 - EPS) ? null : next;
  if (cropping && !values.crop) values.crop = { left: 0, top: 0, right: 1, bottom: 1 };
  paintAll();
  paintCropBox();
  schedulePreview();
  commit({ left: "Rotate left", right: "Rotate right", h: "Flip horizontal", v: "Flip vertical" }[op]);
}
$("crop-rot-l").addEventListener("click", () => turnOrientation("left"));
$("crop-rot-r").addEventListener("click", () => turnOrientation("right"));
$("crop-flip-h").addEventListener("click", () => turnOrientation("h"));
$("crop-flip-v").addEventListener("click", () => turnOrientation("v"));

// ---- Overlays ------------------------------------------------------------
const PHI = (1 + Math.sqrt(5)) / 2;
const CROP_OVERLAYS = ["thirds", "grid", "diagonal", "triangle", "golden", "spiral", "none"];

function guideMarkup(kind) {
  const line = (x1, y1, x2, y2) => `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"/>`;
  let out = "";
  if (kind === "grid") {
    for (let i = 1; i < 12; i++) out += line(i * 100 / 12, 0, i * 100 / 12, 100);
    for (let i = 1; i < 8; i++) out += line(0, i * 100 / 8, 100, i * 100 / 8);
  } else if (kind === "diagonal") {
    out = line(0, 0, 100, 100) + line(100, 0, 0, 100);
  } else if (kind === "triangle") {
    // One diagonal, then a perpendicular from each of the other two corners onto it.
    out = line(0, 100, 100, 0);
    for (const [x, y] of [[0, 0], [100, 100]]) {
      const k = (x * 100 + (y - 100) * -100) / 20000;
      out += line(x, y, k * 100, 100 - k * 100);
    }
  } else if (kind === "golden") {
    const a = 100 / PHI, b = 100 - a;
    out = line(a, 0, a, 100) + line(b, 0, b, 100) + line(0, a, 100, a) + line(0, b, 100, b);
  } else if (kind === "spiral") {
    // Quarter arcs of squares shrinking by 1/phi, drawn in a phi x 1 box and
    // stretched to the crop; the first arc starts on the left edge.
    const dirs = [[-1, 0], [0, -1], [1, 0], [0, 1]];
    let cx = 1, cy = 1, r = 1, d = "M 0 1";
    for (let i = 0; i < 9; i++) {
      const [ex, ey] = dirs[(i + 1) % 4];
      d += ` A ${r} ${r} 0 0 1 ${cx + ex * r} ${cy + ey * r}`;
      const r2 = r / PHI;
      cx += ex * (r - r2); cy += ey * (r - r2);
      r = r2;
    }
    return `<path transform="scale(${100 / PHI} 100)" d="${d}"/>`;
  }
  return out;
}

function paintCropGuides(remember = true) {
  const kind = $("crop-overlay").value;
  $("crop-box").classList.toggle("ov-thirds", kind === "thirds");
  $("crop-guides").innerHTML = guideMarkup(kind);
  if (remember) uiSave({ cropOverlay: kind });
}
$("crop-overlay").addEventListener("change", () => paintCropGuides());
function cycleCropOverlay() {
  const sel = $("crop-overlay");
  sel.value = CROP_OVERLAYS[(CROP_OVERLAYS.indexOf(sel.value) + 1) % CROP_OVERLAYS.length];
  paintCropGuides();
}
window.addEventListener("DOMContentLoaded", () => {      // panels.js (uiLoad) loads after this file
  const saved = uiLoad().cropOverlay;
  if (CROP_OVERLAYS.includes(saved)) $("crop-overlay").value = saved;
  paintCropGuides(false);
});
// O cycles the overlay and X swaps portrait / landscape, as in Lightroom.
window.addEventListener("keydown", (e) => {
  if (!cropping || e.metaKey || e.ctrlKey || e.altKey) return;
  if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)) return;
  const key = e.key.toLowerCase();
  if (key === "o") { e.preventDefault(); e.stopImmediatePropagation(); cycleCropOverlay(); }
  else if (key === "x") { e.preventDefault(); e.stopImmediatePropagation(); flipCropOrientation(); }
}, true);
