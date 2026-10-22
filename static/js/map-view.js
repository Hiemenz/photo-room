// ---- Map -----------------------------------------------------------------
// Geotagged photos grouped into places (/api/map). Works offline: the points are
// drawn on a plain Web-Mercator plot and listed with their coordinates and an
// OpenStreetMap link. "Background map" additionally draws OSM tiles (needs a network).
// Clicking a place filters the grid to the photos inside it (gps_box).
window.mapBox = "";
let mapData = null, mapView = null;
const MAP_W = 640, MAP_H = 360;

const mercY = (lat) => Math.log(Math.tan(Math.PI / 4 + (Math.max(-85, Math.min(85, lat)) * Math.PI / 180) / 2));
function mapProject(lat, lon) {                    // world in [0,1]^2
  return { x: (lon + 180) / 360, y: 0.5 - mercY(lat) / (2 * Math.PI) };
}

// A view that fits the points (with a margin), as world-unit centre and size.
function mapFit(bounds) {
  const a = mapProject(bounds.north, bounds.west), b = mapProject(bounds.south, bounds.east);
  let w = Math.max(b.x - a.x, 0.002), h = Math.max(b.y - a.y, 0.002);
  const cx = (a.x + b.x) / 2, cy = (a.y + b.y) / 2;
  w *= 1.2; h *= 1.2;
  const scale = Math.min(MAP_W / w, MAP_H / h);                 // pixels per world unit
  return { cx, cy, scale };
}

function mapTiles(view) {
  const z = Math.max(0, Math.min(17, Math.floor(Math.log2(view.scale / 256))));
  const n = 2 ** z, ts = 256 * view.scale / (256 * n);          // tile size in pixels
  const x0 = view.cx - MAP_W / 2 / view.scale, y0 = view.cy - MAP_H / 2 / view.scale;
  const x1 = view.cx + MAP_W / 2 / view.scale, y1 = view.cy + MAP_H / 2 / view.scale;
  let out = "";
  for (let tx = Math.floor(x0 * n); tx <= Math.floor(x1 * n); tx++) {
    for (let ty = Math.max(0, Math.floor(y0 * n)); ty <= Math.min(n - 1, Math.floor(y1 * n)); ty++) {
      const px = (tx / n - view.cx) * view.scale + MAP_W / 2, py = (ty / n - view.cy) * view.scale + MAP_H / 2;
      const url = mapData.tiles.replace("{z}", z).replace("{x}", ((tx % n) + n) % n).replace("{y}", ty);
      out += `<image href="${url}" x="${px.toFixed(1)}" y="${py.toFixed(1)}" width="${ts.toFixed(1)}" height="${ts.toFixed(1)}"/>`;
    }
  }
  return out;
}

function mapQuery() {
  const q = libQuery(0);
  for (const k of ["offset", "limit", "sort", "dir", "gps_box"]) q.delete(k);
  q.set("zoom", $("map-zoom").value);
  return q;
}

async function mapLoad() {
  $("map-info").textContent = "loading…";
  let data;
  try { data = await (await fetch("/api/map?" + mapQuery())).json(); } catch { return setStatus("Could not load the map", true); }
  if (data.error) return setStatus(data.error, true);
  mapData = data;
  mapView = data.bounds ? mapFit(data.bounds) : null;
  mapDraw();
}

const mapEsc = (t) => String(t).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function mapDraw() {
  const d = mapData, svg = $("map-plot"), list = $("map-list");
  $("map-info").textContent = d.geotagged ? `${d.geotagged} geotagged photo(s) in ${d.clusters.length} place(s)` : "";
  if (!d.geotagged) {
    svg.innerHTML = "";
    list.textContent = `No geotagged photos here (${d.total} photos in the library). GPS is read from the photo or its sidecar.`;
    return;
  }
  const max = Math.max(...d.clusters.map(c => c.count));
  let body = `<rect width="${MAP_W}" height="${MAP_H}" class="map-bg"/>`;
  if ($("map-tiles").checked) body += mapTiles(mapView);
  d.clusters.forEach((c, i) => {
    const p = mapProject(c.lat, c.lon);
    const x = (p.x - mapView.cx) * mapView.scale + MAP_W / 2, y = (p.y - mapView.cy) * mapView.scale + MAP_H / 2;
    const r = 4 + 10 * Math.sqrt(c.count / max);
    body += `<g class="map-pt" data-i="${i}"><circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${r.toFixed(1)}"/>`
      + (c.count > 1 ? `<text x="${x.toFixed(1)}" y="${(y + 3.5).toFixed(1)}">${c.count}</text>` : "") + `</g>`;
  });
  svg.innerHTML = body;
  list.innerHTML = "";
  d.clusters.forEach((c, i) => {
    const row = document.createElement("div");
    row.className = "map-row";
    row.innerHTML = `<span class="ct">${c.count}</span><span class="nm">${mapEsc(c.name)}${c.count > 1 ? ` + ${c.count - 1} more` : ""}</span>`
      + `<span class="co">${Math.abs(c.lat).toFixed(4)}° ${c.lat >= 0 ? "N" : "S"}, ${Math.abs(c.lon).toFixed(4)}° ${c.lon >= 0 ? "E" : "W"}</span>`
      + `<button data-a="show" title="Show only these photos in the grid">Show</button>`
      + `<a href="${c.osm}" target="_blank" rel="noopener noreferrer" title="Open this place on openstreetmap.org (needs a network)">OSM</a>`;
    row.querySelector("button").addEventListener("click", () => mapShow(c));
    list.appendChild(row);
  });
  svg.querySelectorAll(".map-pt").forEach(g =>
    g.addEventListener("click", () => mapShow(d.clusters[parseInt(g.dataset.i, 10)])));
}

// Filter the grid to one place and leave the map.
function mapShow(c) {
  window.mapBox = c.box;
  $("map-dlg").classList.remove("on");
  mapChip();
  libLoad(true);
}

// A "Place" chip beside Clear filters while a place filter is active.
function mapChip() {
  let chip = $("map-chip");
  if (!window.mapBox) { if (chip) chip.remove(); return; }
  if (!chip) {
    chip = document.createElement("button");
    chip.id = "map-chip";
    chip.title = "Showing one place from the Map; click to show all places";
    chip.addEventListener("click", () => { window.mapBox = ""; mapChip(); libLoad(true); });
    $("lf-clear").before(chip);
  }
  chip.textContent = "Place ✕";
}

function mapOpen() {
  $("map-dlg").classList.add("on");
  mapLoad();
}
$("lib-map").addEventListener("click", mapOpen);
$("map-close").addEventListener("click", () => $("map-dlg").classList.remove("on"));
$("map-zoom").addEventListener("change", mapLoad);
$("map-tiles").addEventListener("change", () => { if (mapData) mapDraw(); });
$("map-dlg").addEventListener("keydown", (e) => { if (e.key === "Escape") $("map-dlg").classList.remove("on"); });
