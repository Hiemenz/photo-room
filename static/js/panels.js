// ---- Panel layout: hide/show, accordion sections, remembered state --------
// A laptop screen cannot show every panel at once, so the right panel scrolls
// as one column, its groups fold away, and both side panels can be hidden.
// What was open and what was hidden is remembered between sessions.
const UI_KEY = "photoroom-ui-v1";
function uiLoad() {
  try { return JSON.parse(localStorage.getItem(UI_KEY)) || {}; } catch (e) { return {}; }
}
function uiSave(patch) {
  try { localStorage.setItem(UI_KEY, JSON.stringify({ ...uiLoad(), ...patch })); } catch (e) { /* private mode: just not remembered */ }
}

// ---- Hide / show the side panels ------------------------------------------
function setPanel(which, hidden) {
  const cls = which === "controls" ? "hide-controls" : "hide-files";
  document.body.classList.toggle(cls, hidden);
  if (which === "controls") {
    $("panel-toggle").setAttribute("aria-pressed", String(!hidden));
    $("panel-toggle").title = (hidden ? "Show" : "Hide") + " the adjustments panel (])";
  }
  uiSave({ [`hide_${which}`]: hidden });
  window.dispatchEvent(new Event("resize"));      // overlays and the zoom view re-fit
}
function togglePanel(which) { setPanel(which, !document.body.classList.contains(which === "controls" ? "hide-controls" : "hide-files")); }
$("panel-toggle").addEventListener("click", () => togglePanel("controls"));
{
  const st = uiLoad();
  if (st.hide_controls) setPanel("controls", true);
  if (st.hide_files) setPanel("files", true);
}

document.addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (typingTarget(e.target) || document.querySelector("dialog[open]")) return;
  if (e.key === "]") { e.preventDefault(); togglePanel("controls"); }
  else if (e.key === "[") { e.preventDefault(); togglePanel("files"); }
});

// ---- Accordion --------------------------------------------------------------
const ACC_GROUPS = [
  ["tone", "Tone", ["exposure", "contrast", "highlights", "shadows", "whites", "blacks"]],
  ["wb", "White balance", ["color_temp", "tint"]],
  ["presence", "Presence", ["vibrance", "saturation", "clarity", "texture", "dehaze"]],
  ["detail", "Detail & geometry", ["sharpness", "sharpen_radius", "sharpen_detail", "sharpen_masking", "straighten", "luminance_nr", "color_nr"]],
];
const accHosts = {};            // group id -> .acc-body

function accState() { return uiLoad().acc || {}; }
function accRemember(key, open) { uiSave({ acc: { ...accState(), [key]: open } }); }

function makeAcc(id, title, defaultOpen) {
  const d = document.createElement("details");
  d.className = "acc"; d.dataset.acc = id; d.id = `acc-${id}`;
  const stored = accState()[id];
  d.open = stored === undefined ? defaultOpen : !!stored;
  const sum = document.createElement("summary");
  sum.textContent = title;
  const body = document.createElement("div"); body.className = "acc-body";
  d.append(sum, body);
  return [d, body];
}

{
  const short = window.innerHeight < 800;             // laptop: start with the essentials open
  const claimed = new Set(ACC_GROUPS.flatMap(g => g[2]));
  const sliders = $("sliders");
  const made = [];
  const groups = ACC_GROUPS.map(g => g.slice());
  const rest = CONTROLS.filter(c => !claimed.has(c.key)).map(c => c.key);
  if (rest.length) groups.push(["other", "More", rest]);
  for (const [id, title, keys] of groups) {
    const [d, body] = makeAcc(id, title, short ? (id === "tone" || id === "wb") : true);
    for (const k of keys) { const el = $(`ctrl-${k}`); if (el) body.appendChild(el); }
    accHosts[id] = body;
    made.push(d);
  }
  sliders.prepend(...made);
  // Edit strength and B&W live in their own block; fold it like the rest.
  const [dg, bg] = makeAcc("overall", "Overall", true);
  const globals = $("globals");
  globals.parentNode.insertBefore(dg, globals);
  bg.appendChild(globals);
  globals.style.borderBottom = "0";
  accHosts.overall = bg;
}

function accAll() { return [...document.querySelectorAll("#controls-scroll details.acc, #controls-scroll details.panel")]; }
function accKey(d) {
  return d.dataset.acc || d.id || ("p:" + (d.querySelector(":scope > summary")?.textContent || "").trim().slice(0, 30));
}
{
  const st = accState();
  for (const d of accAll()) {
    if (d.classList.contains("panel") && st[accKey(d)] !== undefined) d.open = !!st[accKey(d)];
  }
}

let accSolo = !!uiLoad().acc_solo;
document.addEventListener("toggle", (e) => {
  const d = e.target;
  if (!(d instanceof HTMLDetailsElement) || !d.closest("#controls-scroll")) return;
  if (!(d.classList.contains("acc") || d.classList.contains("panel"))) return;
  accRemember(accKey(d), d.open);
  if (accSolo && d.open) {
    for (const o of accAll()) if (o !== d && o.open && !o.contains(d) && !d.contains(o)) o.open = false;
  }
}, true);

{
  const bar = document.createElement("div"); bar.id = "acc-toolbar";
  bar.innerHTML = `<button id="acc-collapse" type="button" title="Fold every section">Collapse all</button>
    <button id="acc-expand" type="button" title="Open every section">Expand all</button>
    <label title="Opening a section folds the others"><input type="checkbox" id="acc-solo"> Solo</label>`;
  $("controls-scroll").prepend(bar);
  $("acc-solo").checked = accSolo;
  $("acc-solo").addEventListener("change", (e) => { accSolo = e.target.checked; uiSave({ acc_solo: accSolo }); });
  $("acc-collapse").addEventListener("click", () => accAll().forEach(d => { d.open = false; }));
  $("acc-expand").addEventListener("click", () => accAll().forEach(d => { d.open = true; }));
}

// A section holding a slider that someone is looking for must be open.
function revealControl(key) {
  const el = $(`ctrl-${key}`);
  const d = el && el.closest("details.acc");
  if (d && !d.open) d.open = true;
  if (el) el.scrollIntoView({ block: "nearest" });
}
