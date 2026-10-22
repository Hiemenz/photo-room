// Keys are never shortcuts while the user is typing or choosing in a control.
function typingTarget(el) {
  if (!el) return false;
  const tag = el.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable) return true;
  return tag === "INPUT" && !["range", "checkbox", "radio", "button"].includes(el.type);
}

// Compare and Survey live in the Library; from Develop, go there first.
async function viaLibrary(open) {
  if (module_ !== "library") {
    if (current) libActive = current;
    await setModule("library");
  }
  open();
}

function undoRedo(direction) {
  if (!current) return;
  const target = histState.entries[histState.index + direction];
  if (target) jumpTo(target.id);
  else setStatus(direction < 0 ? "Nothing to undo" : "Nothing to redo");
}

document.addEventListener("keydown", (e) => {
  if (typingTarget(e.target)) return;
  if (document.querySelector("dialog[open]")) return;

  if (e.metaKey || e.ctrlKey) {
    // Let the browser handle these when the user is selecting page text.
    if (!window.getSelection().isCollapsed) return;
    const k = e.key.toLowerCase();
    if (k === "s" && !e.shiftKey) {
      e.preventDefault();
      if (!$("save").disabled) $("save").click();
      return;
    }
    if (k === "g" && !e.altKey && module_ === "library" && typeof libStack === "function") {
      e.preventDefault(); e.shiftKey ? libUnstack() : libStack(); return;
    }
    if (k === "enter" && typeof cullIsOpen === "function" && cullIsOpen()) {
      e.preventDefault(); cullApply(); return;
    }
    if (k === "z" && !e.altKey) {
      e.preventDefault();
      if (module_ === "develop") undoRedo(e.shiftKey ? 1 : -1);
      return;
    }
    if (k === "c" && !e.shiftKey && current) {
      e.preventDefault(); $("copy").click(); return;
    }
    if (k === "v" && !e.shiftKey && clipboard) {
      e.preventDefault(); $("paste").click(); return;
    }
  }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "?") { e.preventDefault(); toggleHelp(); return; }
  if (e.key === "T") { e.preventDefault(); $("theme-toggle").click(); return; }
  if (libraryKey(e)) return;

  // The Library handles its own digits, flags and labels above; what follows
  // is the Develop module (the photo being edited).
  if (current && e.key >= "0" && e.key <= "5" && e.key.length === 1) {
    e.preventDefault();
    return setRating(parseInt(e.key, 10));
  }
  const key = e.key.toLowerCase();
  if (current && key === "x") {
    e.preventDefault();
    return setRating((ratings[current] ?? 0) === REJECTED ? 0 : REJECTED);
  }
  if (current && key === "p") { e.preventDefault(); return libFlag("pick", [current]); }
  if (current && key === "u") { e.preventDefault(); return libFlag("none", [current]); }
  if (current && LABEL_KEYS[e.key]) { e.preventDefault(); return libLabel(LABEL_KEYS[e.key], [current]); }
  if (current && key === "r" && !$("crop-toggle").disabled) { e.preventDefault(); return $("crop-toggle").click(); }
  if (current && key === "c") { e.preventDefault(); return viaLibrary(openCompare); }
  if (current && key === "n") { e.preventDefault(); return viaLibrary(openSurvey); }

  switch (e.key) {
    case "ArrowRight": e.preventDefault(); step(1); break;
    case "ArrowLeft":  e.preventDefault(); step(-1); break;
    case "\\":
      e.preventDefault();
      if (e.repeat) break;             // ignore auto-repeat while held
      if (latched) { latched = false; suppressUp = true; showEdited(); break; }
      pressedAt = Date.now();
      showOriginal();
      break;
    case "o":
    case "O":
      if (!current || !maskPanelOpen()) break;
      e.preventDefault();
      setMaskOverlay(!maskOverlayOn);
      break;
    case "Delete":
      if (current && maskPanelOpen() && selMask()) { e.preventDefault(); deleteSelectedMask(); }
      break;
    case "Escape":
      if (maskTool) { maskTool = null; paintMasks(); }
      break;
    case "b":
    case "B": {
      if (!current) break;
      e.preventDefault();
      const box = $("s-monochrome");
      box.checked = !box.checked;
      box.dispatchEvent(new Event("change"));
      break;
    }
  }
});

// Hold shows Before while held; a quick tap latches it on until the next tap.
let pressedAt = 0, latched = false, suppressUp = false;
document.addEventListener("keyup", (e) => {
  if (e.key !== "\\") return;
  if (suppressUp) { suppressUp = false; return; }
  if (Date.now() - pressedAt < 250) { latched = true; return; }
  showEdited();
});

// Releasing the key outside the window never fires keyup; recover on blur.
window.addEventListener("blur", () => { latched = false; showEdited(); });
