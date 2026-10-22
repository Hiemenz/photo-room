// ---- Grid view options ---------------------------------------------------
// Cell size, spacing and which badges the grid cells show. Purely a view setting,
// remembered in this browser (localStorage) and applied with CSS on #lib-grid.
const LIB_BADGES = ["name", "stars", "label", "flag", "gps", "edited", "kind"];
const LIB_BADGES_ON = ["name", "stars", "label", "flag", "kind"];
const LIB_DENSITY = { compact: { gap: 4, pad: 6 }, normal: { gap: 10, pad: 14 }, roomy: { gap: 18, pad: 22 } };
const libView = { cell: 170, density: "normal", badges: [...LIB_BADGES_ON] };

function libViewLoad() {
  try {
    const v = JSON.parse(localStorage.getItem("libView") || "{}");
    if (v.cell >= 100 && v.cell <= 360) libView.cell = v.cell;
    if (LIB_DENSITY[v.density]) libView.density = v.density;
    if (Array.isArray(v.badges)) libView.badges = v.badges.filter(b => LIB_BADGES.includes(b));
  } catch (e) { /* defaults */ }
}

function libViewApply() {
  const grid = $("lib-grid"), d = LIB_DENSITY[libView.density];
  grid.style.setProperty("--cell", libView.cell + "px");
  grid.style.gap = d.gap + "px";
  grid.style.padding = d.pad + "px";
  for (const b of LIB_BADGES) grid.classList.toggle("b-" + b, libView.badges.includes(b));
  $("lib-cell").value = libView.cell;
  $("lib-density").value = libView.density;
  document.querySelectorAll("#lib-badges input[data-badge]").forEach(i => { i.checked = libView.badges.includes(i.dataset.badge); });
}

function libViewSave() {
  libViewApply();
  try { localStorage.setItem("libView", JSON.stringify(libView)); } catch (e) { /* not remembered */ }
}

$("lib-cell").addEventListener("input", (e) => { libView.cell = parseInt(e.target.value, 10); libViewSave(); });
$("lib-density").addEventListener("change", (e) => { libView.density = e.target.value; e.target.blur(); libViewSave(); });
document.querySelectorAll("#lib-badges input[data-badge]").forEach(i => i.addEventListener("change", () => {
  libView.badges = [...document.querySelectorAll("#lib-badges input[data-badge]")].filter(x => x.checked).map(x => x.dataset.badge);
  libViewSave();
}));
document.addEventListener("click", (e) => {            // the Badges popover closes when you click elsewhere
  const d = $("lib-badges");
  if (d.open && !d.contains(e.target)) d.open = false;
});
libViewLoad();
libViewApply();
