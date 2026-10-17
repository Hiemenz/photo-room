// ---- Toasts and error wording --------------------------------------------
// Every error says what failed and what to do next. setStatus(msg, true) also
// raises a toast, so the old one-line status keeps working and nothing is
// missed when the side panel is hidden or scrolled.
const ERROR_HELP = [
  [/failed to fetch|networkerror|load failed|network request failed/i,
    "Lost contact with photo-room",
    "The editor server is not answering. Check that photo-room is still running in its terminal, then reload this page. Your saved edits are safe."],
  [/unexpected token|is not valid json|json\.parse|unexpected end of json/i,
    "The server sent an unexpected reply",
    "It may have hit an error. Look at the terminal photo-room is running in, then try again."],
  [/session token|reload the editor page/i,
    "This page's session has expired",
    "Reload the page (the server was restarted). Unsaved slider changes on this page will be lost."],
  [/unexpected host|another site/i,
    "The request was refused",
    "Open the editor at http://127.0.0.1 or http://localhost on the port photo-room printed."],
  [/old colour maths|upgrade edit/i,
    "This edit uses the old colour maths",
    "Press Upgrade edit in the Develop panel first, then try again."],
  [/too dark to judge/i,
    "That spot is too dark to be a neutral",
    "Click a brighter area that should be grey or white, such as a grey card, a white shirt or overcast sky."],
  [/no such file|not found/i,
    "That file is no longer in the library",
    "It may have been moved or deleted. Reload the file list, or restore the file."],
  [/outside the library/i,
    "That path is outside the library folder",
    "photo-room only touches the folder it was started in."],
  [/truncated or corrupt|could not read the image/i,
    "The photo could not be read",
    "The file may be damaged or still copying. Re-copy it from the card and try again."],
  [/not enough (free )?space|no space left/i,
    "The disk is full",
    "Free some space on the library disk, then try again."],
  [/time(d)? ?out/i,
    "That took too long",
    "The Pi may be busy with a large file. Wait a moment and try again, or lower the size."],
  [/exiftool/i,
    "exiftool is missing or failed",
    "Install exiftool (run `photo-room doctor` for the exact command), then try again."],
  [/choose a destination|destination/i,
    "The export destination is a problem",
    "Pick a folder you can write to, then export again."],
];

function explainError(message, what) {
  const text = String(message ?? "").replace(/^(TypeError|Error):\s*/, "").trim() || "Unknown error";
  for (const [re, title, why] of ERROR_HELP) {
    if (re.test(text)) return { title: what ? `${what}: ${title.charAt(0).toLowerCase()}${title.slice(1)}` : title, why, detail: text };
  }
  // Short guidance ("Select a photo first") is its own title; anything that
  // reads like a failure gets the reason plus a next step.
  if (!what && text.length < 70 && !/fail|error|could not|couldn't|cannot|unable|refused|invalid/i.test(text)) {
    return { title: text, why: "", detail: text };
  }
  return {
    title: what || "That did not work",
    why: `${text.replace(/\.$/, "")}. Try again; if it keeps happening, check the terminal photo-room is running in.`,
    detail: text,
  };
}

// A heads-up that is not a failure (e.g. a photo was edited elsewhere).
function setWarning(msg) {
  $("status").textContent = msg;
  $("status").classList.add("err");
  toast("Heads up", { kind: "info", why: msg, ms: 10000 });
}

const _toastSeen = new Map();
function toast(title, opts = {}) {
  const host = document.getElementById("toasts");
  if (!host) return null;
  const key = `${opts.kind || "info"}|${title}|${opts.why || ""}`;
  const prior = _toastSeen.get(key);
  if (prior && prior.isConnected) { clearTimeout(prior._t); prior._t = setTimeout(() => prior.remove(), opts.ms ?? 9000); return prior; }
  const el = document.createElement("div");
  el.className = "toast " + (opts.kind || "info");
  el.setAttribute("role", opts.kind === "error" ? "alert" : "status");
  const body = document.createElement("div");
  const b = document.createElement("b"); b.textContent = title; body.appendChild(b);
  if (opts.why) { const w = document.createElement("span"); w.className = "why"; w.textContent = opts.why; body.appendChild(w); }
  const x = document.createElement("button"); x.className = "x"; x.type = "button"; x.textContent = "×"; x.title = "Dismiss"; x.setAttribute("aria-label", "Dismiss");
  x.addEventListener("click", () => el.remove());
  el.append(body, x);
  if (opts.action) {
    const acts = document.createElement("div"); acts.className = "acts";
    const a = document.createElement("button"); a.type = "button"; a.textContent = opts.action.label;
    a.addEventListener("click", () => { el.remove(); opts.action.run(); });
    acts.appendChild(a); el.appendChild(acts);
  }
  host.appendChild(el);
  while (host.children.length > 4) host.firstChild.remove();
  el._t = setTimeout(() => el.remove(), opts.ms ?? (opts.kind === "error" ? 12000 : 5000));
  _toastSeen.set(key, el);
  return el;
}

// toastError("Auto tone", err) or toastError(null, "message")
function toastError(what, err, retry) {
  const msg = err && err.message !== undefined ? err.message : err;
  const e = explainError(msg, what);
  return toast(e.title, { kind: "error", why: e.why, action: retry ? { label: "Try again", run: retry } : null });
}

window.addEventListener("unhandledrejection", (ev) => {
  if (ev.reason && ev.reason.name === "AbortError") return;
  toastError(null, ev.reason);
});
