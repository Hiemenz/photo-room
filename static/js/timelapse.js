// ---- Timelapse dialog -------------------------------------------------------
// Self-contained: builds its own <dialog> and a button beside Export, so it
// needs only one script tag in editor.html. Frames are sorted by capture time
// on the server; the edit of the open photo (or each frame's own) is applied.
(() => {
  const exportBtn = document.getElementById("export-open");
  if (!exportBtn) return;
  const btn = document.createElement("button");
  btn.id = "timelapse-open";
  btn.type = "button";
  btn.textContent = "Timelapse…";
  btn.title = "Render the selected photos (or a collection) as a video";
  exportBtn.after(btn);

  const dlg = document.createElement("dialog");
  dlg.id = "timelapse-dialog";
  dlg.innerHTML = `
    <h2>Timelapse</h2>
    <div class="body">
      <label class="row">Frames
        <select id="tl-scope">
          <option value="selected">Selected photos</option>
          <option value="collection">Collection</option>
          <option value="folder">Whole folder</option>
        </select></label>
      <label class="row" id="tl-col-row" hidden>Collection <input id="tl-col" placeholder="name or id"></label>
      <label class="row">Folder <input id="tl-dest"></label>
      <label class="row">File name <input id="tl-name" value="timelapse.mp4"></label>
      <label class="row">Size <input id="tl-size" value="1920x1080"></label>
      <label class="row">Frames per second <input id="tl-fps" type="number" min="1" max="120" value="24"></label>
      <label class="row"><span>Edits</span>
        <select id="tl-edit">
          <option value="ref">Use the open photo's edit on every frame</option>
          <option value="each">Each frame's own edit</option>
        </select></label>
      <label class="row"><span>Deflicker</span><input id="tl-deflicker" type="checkbox"></label>
      <label class="row"><span>Stabilise</span><input id="tl-stab" type="checkbox"></label>
      <div class="hint" id="tl-note">Frames are sorted by capture time and cropped to the size's shape.</div>
      <progress id="tl-bar" max="1" value="0" hidden></progress>
      <div id="tl-msg" role="status"></div>
    </div>
    <div class="actions">
      <button id="tl-cancel" type="button">Close</button>
      <button id="tl-go" type="button" class="primary">Render</button>
    </div>`;
  document.body.appendChild(dlg);
  const el = (id) => dlg.querySelector("#" + id);
  let jobId = null, timer = null;

  const say = (text, bad) => { el("tl-msg").textContent = text; el("tl-msg").style.color = bad ? "var(--danger, #e66)" : ""; };
  const post = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}) }).then(async (r) => ({ ok: r.ok, data: await r.json().catch(() => ({})) }));

  btn.addEventListener("click", async () => {
    try {
      const d = await (await fetch("/api/timelapse/defaults")).json();
      if (!el("tl-dest").value) el("tl-dest").value = d.dest;
      say(d.ffmpeg ? "" : d.hint + " Frames will be saved as JPEGs instead.", !d.ffmpeg);
    } catch (e) { say(""); }
    dlg.showModal();
  });
  el("tl-scope").addEventListener("change", () => { el("tl-col-row").hidden = el("tl-scope").value !== "collection"; });

  const stop = () => { clearInterval(timer); timer = null; el("tl-go").textContent = "Render"; };
  const poll = async () => {
    const j = await (await fetch("/api/timelapse/status/" + jobId)).json();
    el("tl-bar").hidden = false;
    el("tl-bar").max = j.total || 1;
    el("tl-bar").value = j.done || 0;
    if (j.state === "running") return say(j.label || "Working");
    stop();
    if (j.state === "done" && j.report) {
      const r = j.report;
      return say(r.video ? `Wrote ${r.video} (${r.frames} frames)` : (j.error || `Frames saved in ${r.frames_dir}`), !r.video && !!j.error);
    }
    say(j.state === "cancelled" ? "Cancelled" : (j.error || "Failed"), j.state !== "cancelled");
  };

  el("tl-go").addEventListener("click", async () => {
    if (timer) { await post("/api/timelapse/cancel/" + jobId); return; }
    const scope = el("tl-scope").value;
    const res = await post("/api/timelapse", {
      scope, paths: [...(typeof selected !== "undefined" ? selected : [])],
      collection: el("tl-col").value, dest: el("tl-dest").value, name: el("tl-name").value,
      size: el("tl-size").value, fps: +el("tl-fps").value,
      reference: el("tl-edit").value === "ref" && typeof current !== "undefined" ? current : null,
      per_frame: el("tl-edit").value === "each",
      deflicker: el("tl-deflicker").checked, stabilise: el("tl-stab").checked,
    });
    if (!res.ok) return say(res.data.error || "Could not start", true);
    jobId = res.data.id;
    el("tl-go").textContent = "Cancel render";
    say("Starting");
    timer = setInterval(() => poll().catch(() => {}), 1000);
  });
  el("tl-cancel").addEventListener("click", () => dlg.close());
})();
