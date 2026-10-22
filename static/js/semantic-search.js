// ---- Semantic search (optional, on-device) ---------------------------------
// Adds to the Library filter bar: a "Semantic" toggle that turns the search box
// into a natural-language query ("sunset over water") ranked by CLIP-style
// embeddings, a "Find similar" button for the active photo, and an index
// status/Build button. Without the optional extra the box keeps doing the
// keyword/EXIF search and a note says how to enable semantic search.
// library-module.js calls window.semanticParams(q) when it builds a query.
(function () {
  let on = false, similar = "", status = null, poll = null;
  try { on = localStorage.getItem("pr-semantic") === "1"; } catch { /* storage may be blocked */ }

  const q = $("lf-q");
  if (!q) return;
  const wrap = document.createElement("span");
  wrap.id = "sem-wrap";
  wrap.innerHTML = `<button id="sem-toggle" type="button" title="Search by meaning, e.g. sunset over water (on-device)" aria-pressed="false">Semantic</button>
    <button id="sem-similar" type="button" title="Find photos that look like the active one">Find similar</button>
    <button id="sem-build" type="button" hidden>Build index</button>
    <span id="sem-status" role="status" aria-live="polite"></span>`;
  q.insertAdjacentElement("afterend", wrap);
  const $t = $("sem-toggle"), $s = $("sem-similar"), $b = $("sem-build"), $m = $("sem-status");

  window.semanticParams = function (params) {
    if (similar) { params.set("similar", similar); params.delete("q"); }
    else if (on && q.value.trim()) { params.set("sem", q.value.trim()); params.delete("q"); }
  };

  function paint() {
    $t.classList.toggle("on", on);
    $t.setAttribute("aria-pressed", String(on));
    q.placeholder = on ? "Describe the photo (e.g. sunset over water)" : "Search name, keyword, camera";
    $b.hidden = !(status && status.available);
    $b.textContent = status && status.building ? "Cancel indexing" : "Build index";
    let msg = "";
    if (similar) msg = "Similar to " + similar.split("/").pop() + " (click Find similar to clear)";
    else if (on && status && !status.available) msg = "Semantic search not installed: using keyword search. " + status.message;
    else if (on && status) {
      const p = status.progress;
      msg = status.building && p ? `Indexing ${p.done}/${p.total}` : `${status.indexed}/${status.library} indexed`;
    }
    $m.textContent = msg;
    $m.title = msg;
    $s.classList.toggle("on", !!similar);
  }

  async function refresh() {
    try { status = await (await fetch("/api/semantic/status")).json(); } catch { status = null; }
    paint();
    clearTimeout(poll);
    if (status && status.building) poll = setTimeout(refresh, 1500);
  }

  $t.addEventListener("click", () => {
    on = !on;
    try { localStorage.setItem("pr-semantic", on ? "1" : "0"); } catch { /* ignore */ }
    paint();
    if (on) refresh();
    if (q.value.trim()) libLoad(true);
  });
  $s.addEventListener("click", () => {
    if (similar) similar = "";
    else if (libActive) similar = libActive;
    else return setStatus("Select a photo first", true);
    paint();
    if (similar) refresh();
    libLoad(true);
  });
  $b.addEventListener("click", async () => {
    const running = status && status.building;
    const res = await fetch(running ? "/api/semantic/cancel" : "/api/semantic/build", { method: "POST" });
    const data = await res.json();
    if (data.error) setStatus(data.error, true);
    refresh();
  });
  // Clear filters resets "find similar" before library-module.js re-queries.
  $("lf-clear").addEventListener("click", () => { similar = ""; paint(); }, true);
  // Library opened: learn what is available.
  document.getElementById("mod-library")?.addEventListener("click", refresh);
  paint();
  if (on) refresh();
})();
