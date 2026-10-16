// ---- History & snapshots -------------------------------------------------
// Each committed step (slider release, curve change, preset, crop, reset) is
// posted to the server, which keeps it in a sidecar JSON beside the photo.
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function setHistory(h) {
  histState = h || { entries: [], index: -1, snapshots: [] };
  const list = $("history-list");
  list.innerHTML = "";
  // Newest first, like Lightroom.
  for (let i = histState.entries.length - 1; i >= 0; i--) {
    const e = histState.entries[i];
    const row = document.createElement("div");
    row.className = "hist" + (i === histState.index ? " current" : "") + (i > histState.index ? " future" : "");
    row.innerHTML = `<span class="l"></span><span class="t">${fmtTime(e.ts)}</span>`;
    row.querySelector(".l").textContent = e.label;
    row.title = "Jump back to this state";
    row.addEventListener("click", () => jumpTo(e.id));
    list.appendChild(row);
  }
  const snaps = $("snapshot-list");
  snaps.innerHTML = "";
  for (const s of histState.snapshots) {
    const row = document.createElement("div");
    row.className = "snap";
    row.innerHTML = `<span class="l"></span><span class="x" title="Delete snapshot">&times;</span>`;
    row.querySelector(".l").textContent = s.name;
    row.title = "Apply this snapshot";
    row.addEventListener("click", async (ev) => {
      if (ev.target.classList.contains("x")) {
        const d = await post("/api/snapshots/delete", { path: current, id: s.id });
        if (d.history) setHistory(d.history);
        return;
      }
      applyState(await post("/api/snapshots/apply", { path: current, id: s.id, name: s.name }),
                 `Applied snapshot '${s.name}' — not saved yet`);
    });
    snaps.appendChild(row);
  }
}

let commitChain = Promise.resolve();
function commit(label) {
  if (!current) return;
  const path = current, body = { path, label, adjustments: JSON.parse(JSON.stringify(values)) };
  // Serialised, so steps land in the order they were made.
  commitChain = commitChain.then(async () => {
    try {
      const d = await post("/api/history/record", body);
      if (d.history && path === current) setHistory(d.history);
    } catch (e) { /* history is a convenience; never interrupt editing */ }
  });
  return commitChain;
}

function applyState(data, msg) {
  if (data.error) return setStatus(data.error, true);
  values = data.adjustments;
  paintAll();
  $("preview").src = data.preview;
  drawHistogram(data.histogram);
  setHistory(data.history);
  setStatus(msg);
}

async function jumpTo(id) {
  if (!current) return;
  await commitChain;
  applyState(await post("/api/history/jump", { path: current, id }), "Jumped in history — not saved yet");
}

$("reset-import").addEventListener("click", async () => {
  if (!current) return;
  await commitChain;
  applyState(await post("/api/history/reset_to_import", { path: current }),
             "Reset to the as-imported settings — not saved yet");
});

$("snap-new").addEventListener("click", async () => {
  if (!current) return;
  const name = prompt("Name this snapshot:");
  if (!name) return;
  const d = await post("/api/snapshots/create", { path: current, name, adjustments: values });
  if (d.error) return setStatus(d.error, true);
  setHistory(d.history);
  setStatus(`Snapshot '${name}' created`);
});
