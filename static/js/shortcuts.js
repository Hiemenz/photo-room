// ---- Keyboard ------------------------------------------------------------
function step(delta) {
  // Navigate within whatever the filter is showing, so culling down to the
  // picks and then arrowing through does not jump back to hidden frames.
  const visible = files.filter(passesFilter);
  if (!visible.length) return;
  const i = visible.indexOf(current);
  const next = i < 0 ? 0 : Math.min(visible.length - 1, Math.max(0, i + delta));
  if (visible[next] !== current) openFile(visible[next]);
}

// ---- Shortcuts ------------------------------------------------------------
// One table drives the '?' overlay, so the list shown always matches the keys
// handled below. "Mod" is Ctrl on Linux/Windows and Cmd on macOS.
const IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
const MOD = IS_MAC ? "Cmd" : "Ctrl";
const SHORTCUTS = [
  ["Modules", [
    ["G", "Library grid"], ["E", "Loupe / Develop the selected photo"], ["D", "Develop"],
    ["R", "Crop (Develop)"], ["C", "Compare view"], ["N", "Survey view"],
  ]],
  ["Culling", [
    ["1 - 5", "Star rating"], ["0", "Clear rating"], ["X", "Reject"], ["P", "Pick flag"],
    ["U", "Unflag"], ["6 7 8 9", "Red / Yellow / Green / Blue label"],
    ["Left / Right", "Previous / next photo"],
  ]],
  ["Smart cull (K in the Library)", [
    ["K", "Open smart cull"], ["Left / Right", "Previous / next frame in a burst"],
    ["Down / Up, J / K", "Next / previous burst"], ["P / X / U", "Pick / reject / clear the frame"],
    ["A or Enter", "Accept the suggestion for this burst"], ["C / Z", "Compare side by side / at 100%"],
    [`${MOD}+Enter`, "Apply the reviewed choices"],
  ]],
  ["Develop", [
    ["\\", "Before / after (hold, or tap to latch)"], ["O", "Mask overlay"], ["B", "Black & white"],
    [`${MOD}+Z`, "Undo"], [`${MOD}+Shift+Z`, "Redo"],
    [`${MOD}+C / ${MOD}+V`, "Copy / paste settings"], [`${MOD}+S`, "Save"],
    ["Delete", "Delete the selected mask or spot"], ["Esc", "Leave the current tool"],
  ]],
  ["Output and app", [
    [`${MOD}+Shift+E`, "Export"], ["?", "This list"], ["Shift+T", "Toggle dark / light theme"],
  ]],
];

function buildHelp() {
  const body = $("help-body");
  body.innerHTML = "";
  for (const [title, rows] of SHORTCUTS) {
    const h = document.createElement("h3"); h.textContent = title; body.appendChild(h);
    for (const [keys, what] of rows) {
      const row = document.createElement("div"); row.className = "sc";
      const d = document.createElement("span"); d.textContent = what;
      const k = document.createElement("kbd"); k.textContent = keys;
      row.append(d, k); body.appendChild(row);
    }
  }
}
function toggleHelp() {
  const dlg = $("help-dialog");
  if (dlg.open) { dlg.close(); return; }
  if (document.querySelector("dialog[open]")) return;
  buildHelp(); dlg.showModal();
}
$("help-open").addEventListener("click", toggleHelp);
$("help-close").addEventListener("click", () => $("help-dialog").close());
$("help-dialog").addEventListener("keydown", (e) => {
  e.stopPropagation();
  if (e.key === "?") { e.preventDefault(); $("help-dialog").close(); }
});
