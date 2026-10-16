// ---- Ratings -------------------------------------------------------------
async function loadRatings() {
  try {
    const data = await (await fetch("/api/ratings")).json();
    ratings = data.ratings || {};
  } catch { ratings = {}; }
}

function paintRating() {
  const r = current ? (ratings[current] ?? 0) : 0;
  document.querySelectorAll("#rating-row .star").forEach(s => {
    s.classList.toggle("on", r >= parseInt(s.dataset.star, 10));
  });
  $("reject-btn").classList.toggle("on", r === REJECTED);
}

async function setRating(rating) {
  if (!current) return;
  // Repaint immediately; the write is confirmed afterwards. Rating is a
  // rapid-fire action while culling and must not feel like it waits.
  const previous = ratings[current] ?? 0;
  ratings[current] = rating;
  paintRating();
  syncGridState();

  const data = await post("/api/rate", { path: current, rating });
  if (data.error) {
    ratings[current] = previous;      // put it back if the write failed
    paintRating();
    syncGridState();
    return setStatus(data.error, true);
  }
  setStatus(data.message);
  if (filterMode !== "all") applyFilter();
}

document.querySelectorAll("#rating-row .star").forEach(s => {
  s.addEventListener("click", () => {
    const value = parseInt(s.dataset.star, 10);
    // Clicking the current rating clears it, as in every photo tool.
    setRating((ratings[current] ?? 0) === value ? 0 : value);
  });
});
$("reject-btn").addEventListener("click", () => {
  setRating((ratings[current] ?? 0) === REJECTED ? 0 : REJECTED);
});

// ---- Filtering -----------------------------------------------------------
function passesFilter(path) {
  const r = ratings[path] ?? 0;
  switch (filterMode) {
    case "unrated":  return r === 0;
    case "picks":    return r >= 1;
    case "best":     return r >= 4;
    case "rejected": return r === REJECTED;
    default:         return true;
  }
}

function applyFilter() {
  document.querySelectorAll(".file").forEach(el => {
    el.hidden = !passesFilter(el.dataset.path);
  });
  document.querySelectorAll(".cell").forEach(el => {
    el.hidden = !passesFilter(el.dataset.path);
  });
  const shown = files.filter(passesFilter).length;
  if (filterMode !== "all") setStatus(`${shown} of ${files.length} shown`);
}

$("filter").addEventListener("change", (e) => {
  filterMode = e.target.value;
  applyFilter();
});
