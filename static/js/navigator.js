// ---- Navigator: whole-photo mini view with the visible region (Develop) ----------
// A rectangle shows what the main view displays at Fill / 100% / 200%; click or drag
// moves it (pans the zoomed view). Hovering shows Fit / Fill / 100% / 200% presets.
const NAV = { dragging: false };

function navBox() {            // the photo's rectangle inside #nav-view, in css px
  const v = $("nav-view").getBoundingClientRect();
  const p = $("preview");
  const ar = (ZM.iw && ZM.ih) ? ZM.iw / ZM.ih : (p.naturalWidth / (p.naturalHeight || 1)) || 1;
  let w = v.width, h = w / ar;
  if (h > v.height) { h = v.height; w = h * ar; }
  return { w, h, left: (v.width - w) / 2, top: (v.height - h) / 2 };
}

function navUpdate() {
  const nav = $("navigator"), p = $("preview");
  const show = module_ === "develop" && !!current && !p.hidden && !!p.naturalWidth && !$("grid").classList.contains("on");
  nav.hidden = !show;
  if (!show) return;
  if ($("nav-img").getAttribute("src") !== p.getAttribute("src")) $("nav-img").src = p.src;
  const b = navBox(), img = $("nav-img").style;
  img.left = `${b.left}px`; img.top = `${b.top}px`; img.width = `${b.w}px`; img.height = `${b.h}px`;
  let fx = 0, fy = 0, fw = 1, fh = 1;
  if (zoomActive()) {
    const st = stageBox();
    fx = Math.max(0, -ZM.tx / ZM.s / ZM.iw); fy = Math.max(0, -ZM.ty / ZM.s / ZM.ih);
    fw = Math.min(1 - fx, st.w / ZM.s / ZM.iw); fh = Math.min(1 - fy, st.h / ZM.s / ZM.ih);
  }
  const r = $("nav-rect").style;
  r.left = `${b.left + fx * b.w}px`; r.top = `${b.top + fy * b.h}px`;
  r.width = `${fw * b.w}px`; r.height = `${fh * b.h}px`;
  nav.dataset.visible = [fx, fy, fw, fh].map(v => v.toFixed(4)).join(",");
  document.querySelectorAll("#nav-presets button").forEach(btn => {
    const z = btn.dataset.nav;
    btn.classList.toggle("active", z === "fit" ? ZM.mode === "fit" : z === "fill" ? ZM.mode === "fill"
      : ZM.mode === "scale" && Math.abs(ZM.s * dpr() - parseFloat(z)) < 0.01);
  });
}

// Centre the main view on a point of the photo (0..1 fractions).
function navCentre(fx, fy) {
  const st = stageBox();
  ZM.tx = st.w / 2 - fx * ZM.iw * ZM.s; ZM.ty = st.h / 2 - fy * ZM.ih * ZM.s;
  zoomApply();
}
function navPoint(e) {
  const b = navBox(), v = $("nav-view").getBoundingClientRect();
  return { fx: Math.min(1, Math.max(0, (e.clientX - v.left - b.left) / b.w)), fy: Math.min(1, Math.max(0, (e.clientY - v.top - b.top) / b.h)) };
}

$("nav-view").addEventListener("pointerdown", async (e) => {
  if (!current) return;
  const pt = navPoint(e);
  try { $("nav-view").setPointerCapture(e.pointerId); } catch (err) { /* synthetic pointer */ }
  if (!zoomActive()) {
    await zoomTo(1);                        // like Lightroom: the first click zooms in
    if (!zoomActive()) return;
  }
  NAV.dragging = true;
  navCentre(pt.fx, pt.fy);
});
$("nav-view").addEventListener("pointermove", (e) => {
  if (NAV.dragging && zoomActive()) { const p = navPoint(e); navCentre(p.fx, p.fy); }
});
const navEnd = () => { NAV.dragging = false; };
$("nav-view").addEventListener("pointerup", navEnd);
$("nav-view").addEventListener("pointercancel", navEnd);
document.querySelectorAll("#nav-presets button").forEach(b => b.addEventListener("click", () => {
  const z = b.dataset.nav;
  zoomTo(z === "fit" || z === "fill" ? z : parseFloat(z));
}));

zoomHooks.push(navUpdate);
$("preview").addEventListener("load", navUpdate);
new MutationObserver(navUpdate).observe($("zoom-bar"), { attributes: true, attributeFilter: ["hidden"] });
new MutationObserver(navUpdate).observe($("grid"), { attributes: true, attributeFilter: ["class"] });
window.addEventListener("resize", navUpdate);
