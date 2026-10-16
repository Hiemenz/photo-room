// ---- Contact sheet -------------------------------------------------------
let gridOn = false;
let lazyLoader = null;
let ratings = {};        // path -> -1 (rejected) .. 5
let filterMode = "all";
const REJECTED = -1;

// Thumbnails load only as cells scroll into view. A shoot can run to
// hundreds of frames, and fetching every one up front would stall the
// browser on work the photographer may never look at.
function makeLazyLoader() {
  if (lazyLoader) lazyLoader.disconnect();
  lazyLoader = new IntersectionObserver((entries, obs) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      const img = entry.target;
      if (!img.dataset.src) continue;
      img.src = img.dataset.src;
      delete img.dataset.src;
      obs.unobserve(img);
    }
  }, { root: $("grid"), rootMargin: "300px" });
  return lazyLoader;
}

function buildGrid() {
  const grid = $("grid");
  grid.innerHTML = "";
  const obs = makeLazyLoader();

  files.forEach((path, i) => {
    const cell = document.createElement("div");
    cell.className = "cell"
      + (path === current ? " active" : "")
      + (selected.has(path) ? " selected" : "");
    cell.dataset.path = path;

    const img = document.createElement("img");
    img.alt = path;
    img.loading = "lazy";
    img.dataset.src = `/api/thumb?path=${encodeURIComponent(path)}`;
    // A file with no readable preview must not leave a broken-image icon.
    img.addEventListener("error", () => { img.style.visibility = "hidden"; });

    const name = document.createElement("div");
    name.className = "name";
    name.textContent = path.split("/").pop();

    const stars = document.createElement("div");
    stars.className = "stars";

    cell.append(img, stars, name);
    cell.addEventListener("click", (e) => handleFileClick(e, path, i));
    grid.appendChild(cell);
    obs.observe(img);
  });
}

function syncGridState() {
  document.querySelectorAll(".cell").forEach(cell => {
    const path = cell.dataset.path;
    const r = ratings[path] ?? 0;
    cell.classList.toggle("active", path === current);
    cell.classList.toggle("selected", selected.has(path));
    cell.classList.toggle("rejected", r === REJECTED);
    const stars = cell.querySelector(".stars");
    if (stars) stars.textContent = r > 0 ? "★".repeat(r) : "";
  });
}

$("view-toggle").addEventListener("click", () => {
  gridOn = !gridOn;
  $("grid").classList.toggle("on", gridOn);
  $("view-toggle").textContent = gridOn ? "Edit" : "Grid";
  $("crop-layer").classList.remove("on");
  if (gridOn) { buildGrid(); applyFilter(); }
});
