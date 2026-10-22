#!/usr/bin/env python3
"""
Map view: the geotagged photos of the library grouped into places.

``GET /api/map?cell=<degrees>[&collection=<id>][&folder=<path>][&<any library filter>]``
    ``{geotagged, total, bounds, clusters: [...]}``. Photos fall into square cells of
    ``cell`` degrees (``zoom=<0..18>`` is the same thing as 360 / 2**zoom). A cluster is
    ``{lat, lon, count, name, paths (first 12), box "south,west,north,east", osm}``.
    Pass ``box`` back as the library's ``gps_box`` filter to list a cluster's photos.

Nothing here touches the network: ``osm`` is only a link the browser may follow.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

import catalog
import folders
import library

bp = Blueprint("map_api", __name__)

MAX_CLUSTERS = 2000
SAMPLE = 12
OSM_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"


def osm_link(lat: float, lon: float, zoom: int = 14) -> str:
    return f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lon:.6f}#map={zoom}/{lat:.5f}/{lon:.5f}"


def cluster(items: list[dict], cell: float) -> list[dict]:
    """Group geotagged items into ``cell``-degree squares; biggest first."""
    cell = max(0.0005, min(180.0, float(cell)))
    cells: dict[tuple[int, int], list[dict]] = {}
    for it in items:
        lat, lon = it.get("lat"), it.get("lon")
        if lat is None or lon is None:
            continue
        cells.setdefault((int((lat + 90) // cell), int((lon + 180) // cell)), []).append(it)
    out = []
    for group in cells.values():
        lats, lons = [g["lat"] for g in group], [g["lon"] for g in group]
        lat, lon = sum(lats) / len(lats), sum(lons) / len(lons)
        out.append({"lat": round(lat, 5), "lon": round(lon, 5), "count": len(group),
                    "name": group[0]["name"], "paths": [g["path"] for g in group[:SAMPLE]],
                    "box": f"{min(lats) - 1e-6:.6f},{min(lons) - 1e-6:.6f},{max(lats) + 1e-6:.6f},{max(lons) + 1e-6:.6f}",
                    "osm": osm_link(lat, lon)})
    out.sort(key=lambda c: (-c["count"], c["lat"], c["lon"]))
    return out[:MAX_CLUSTERS]


@bp.route("/api/map")
def api_map():
    import editor            # late: editor owns LIBRARY_ROOT (and imports this module)
    args = request.args
    try:
        if args.get("zoom"):
            cell = 360.0 / 2 ** max(0, min(18, int(args["zoom"])))
        else:
            cell = float(args.get("cell", 1.0))
        criteria = {k: args.get(k, "") for k in library.FILTER_KEYS}
        criteria["gps"] = "yes"
        idx = editor._idx()
        everything = idx.items() + (catalog.virtual_items(editor.LIBRARY_ROOT, idx.items())
                                    if catalog.has_virtuals(editor.LIBRARY_ROOT) else [])
        matched = library.filter_items(everything, criteria)
        if args.get("collection"):
            matched = catalog.collection_items(catalog.get_collection(editor.LIBRARY_ROOT, args["collection"]), matched)
        if args.get("folder") is not None:
            matched = [it for it in matched if folders.in_folder(
                it["path"], args["folder"].strip("/"), args.get("subfolders") == "1")]
    except KeyError as exc:
        return jsonify({"error": exc.args[0]}), 404
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400
    located = [it for it in matched if it.get("lat") is not None and it.get("lon") is not None]
    bounds = None
    if located:
        lats, lons = [i["lat"] for i in located], [i["lon"] for i in located]
        bounds = {"south": min(lats), "west": min(lons), "north": max(lats), "east": max(lons)}
    return jsonify({"geotagged": len(located), "total": len(everything), "cell": cell, "bounds": bounds,
                    "tiles": OSM_TILES, "clusters": cluster(located, cell)})
