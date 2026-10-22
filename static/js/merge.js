// ---- Merge to HDR / Merge to Panorama (Library selection) -------------------------
// Needs 2+ selected photos. The job runs on the server (POST /api/merge); this polls it,
// shows progress in the status line and offers Cancel. The result is a new TIFF beside
// the first photo; the live Library grid shows it when the server has indexed it.
(function () {
  const batch = document.getElementById("batch");
  if (!batch) return;
  const row = document.createElement("div");
  row.id = "merge-row";
  row.style.cssText = "display:flex;gap:6px;margin-top:6px;flex-wrap:wrap";
  const mk = (id, label, title) => {
    const b = document.createElement("button");
    b.id = id; b.type = "button"; b.textContent = label; b.title = title; b.disabled = true;
    row.appendChild(b);
    return b;
  };
  const hdr = mk("merge-hdr", "Merge to HDR", "Select 2 or more bracketed exposures of one scene");
  const pano = mk("merge-pano", "Merge to Panorama", "Select 2 or more overlapping photos");
  const cancel = mk("merge-cancel", "Cancel merge", "Stop the running merge");
  cancel.hidden = true; cancel.disabled = false;
  batch.insertAdjacentElement("afterend", row);

  let jobId = null;
  const sync = () => { hdr.disabled = pano.disabled = !(selected.size >= 2 && !jobId); };
  // updateSelectionUI is a global function declaration; later callers resolve to the wrapper.
  const prior = window.updateSelectionUI;
  window.updateSelectionUI = function () { const r = prior.apply(this, arguments); sync(); return r; };

  async function start(kind) {
    if (jobId || selected.size < 2) return;
    const label = kind === "hdr" ? "HDR merge" : "Panorama";
    const data = await post("/api/merge", { kind, paths: [...selected] });
    if (data.error) return toast(`${label} failed`, { kind: "error", why: data.error });
    jobId = data.id; cancel.hidden = false; sync();
    for (;;) {
      await new Promise(r => setTimeout(r, 700));
      let st;
      try { st = await (await fetch(`/api/merge/status/${jobId}`)).json(); } catch (e) { continue; }
      if (!st.state) break;
      setStatus(`${label}: ${st.message} (${Math.round((st.progress || 0) * 100)}%)`);
      if (st.state === "done") {
        toast(`${label} saved`, { kind: "info", ms: 10000,
          why: `${st.result.path || st.result.output} (${st.result.size[0]}x${st.result.size[1]}). Your originals were not changed.` });
        break;
      }
      if (st.state === "error") { toast(`${label} failed`, { kind: "error", why: st.error }); setStatus(st.error, true); break; }
      if (st.state === "cancelled") { setStatus(`${label} cancelled`); break; }
    }
    jobId = null; cancel.hidden = true; sync();
  }
  hdr.addEventListener("click", () => start("hdr"));
  pano.addEventListener("click", () => start("pano"));
  cancel.addEventListener("click", () => { if (jobId) post(`/api/merge/cancel/${jobId}`, {}); });
  sync();
})();
