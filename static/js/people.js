// ---- People (optional, on-device faces) -------------------------------------
// Library sidebar panel: "Find faces" scans the library for faces and groups them
// into people (faces.py; nothing is uploaded, nothing runs until you click). Click a
// person to filter the grid (person=<id>), double-click or the pencil to name them,
// "merge" to fold a duplicate into another person, "x" to hide a false person.
// Grouping is approximate: the status line says which detector/embedder is in use.
window.personId = "";
(function () {
  let status = null, people = [], poll = null;
  const esc = (t) => String(t).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const label = (p) => p.name || "Person " + p.id;

  function paintStatus() {
    const el = $("pp-status"), btn = $("pp-scan");
    if (!status) { el.textContent = ""; return; }
    const p = status.progress;
    btn.textContent = status.building ? "Cancel" : "Find faces";
    let msg = status.building && p ? `Scanning ${p.done}/${p.total}, ${p.faces} faces`
      : `${status.indexed}/${status.library} photos scanned, ${status.faces} faces`;
    if (!status.advanced) msg += ". Approximate grouping: name and merge to fix.";
    el.textContent = msg;
    el.title = status.limits + " Detector: " + status.detector + ", grouping: " + status.embedder + ".";
  }

  function render() {
    const box = $("pp-list");
    box.innerHTML = "";
    if (window.personId && !people.some(p => String(p.id) === String(window.personId))) window.personId = "";
    for (const p of people) {
      const el = document.createElement("div");
      el.className = "col pp" + (String(p.id) === String(window.personId) ? " active" : "");
      el.setAttribute("role", "listitem");
      el.innerHTML = `<img alt="" width="22" height="22" src="/api/faces/face/${p.face_id}.jpg">`
        + `<span class="nm">${esc(label(p))}</span><span class="ct">${p.photos}</span>`
        + `<button class="act" data-a="name" title="Name this person">✎</button>`
        + `<button class="act" data-a="merge" title="Merge into another person">⇥</button>`
        + `<button class="act" data-a="remove" title="Not a person worth keeping (hide)">×</button>`;
      el.addEventListener("click", (e) => {
        const a = e.target.closest("button")?.dataset.a;
        if (a) return act(p, a);
        window.personId = String(p.id) === String(window.personId) ? "" : String(p.id);
        render();
        libLoad(true);
      });
      el.addEventListener("dblclick", () => act(p, "name"));
      box.appendChild(el);
    }
  }

  async function act(p, a) {
    let data;
    if (a === "name") {
      const nm = prompt("Name for this person (blank clears the name):", p.name);
      if (nm === null) return;
      data = await post(`/api/faces/people/${p.id}/name`, { name: nm });
    } else if (a === "merge") {
      const others = people.filter(o => o.id !== p.id);
      if (!others.length) return setStatus("There is no other person to merge into", true);
      const pick = prompt("Merge " + label(p) + " into which person? Type the number:\n"
        + others.map((o, i) => `${i + 1}. ${label(o)} (${o.photos})`).join("\n"));
      const o = others[parseInt(pick, 10) - 1];
      if (!o) return;
      data = await post(`/api/faces/people/${p.id}/merge`, { into: o.id });
      if (String(window.personId) === String(p.id)) window.personId = String(o.id);
    } else {
      if (!confirm("Hide " + label(p) + "? Their faces stay hidden when you scan again.")) return;
      data = await post(`/api/faces/people/${p.id}/remove`, {});
    }
    if (data.error) return setStatus(data.error, true);
    await load();
    libLoad(true);
  }

  async function load() {
    try { people = (await (await fetch("/api/faces/people")).json()).people || []; } catch { people = []; }
    render();
  }

  async function refresh() {
    try { status = await (await fetch("/api/faces/status")).json(); } catch { status = null; }
    paintStatus();
    clearTimeout(poll);
    if (status && status.building) poll = setTimeout(refresh, 1500);
    else if (status) load();
    if (status && status.building) load();
  }

  $("pp-scan").addEventListener("click", async () => {
    const running = status && status.building;
    if (!running && !confirm("Scan every photo for faces on this machine? Nothing is uploaded. "
      + (status ? status.limits : ""))) return;
    const data = await post(running ? "/api/faces/cancel" : "/api/faces/build", {});
    if (data.error) setStatus(data.error, true);
    refresh();
  });
  $("lf-clear").addEventListener("click", () => { window.personId = ""; render(); }, true);
  document.getElementById("mod-library")?.addEventListener("click", refresh);
  refresh();
})();
