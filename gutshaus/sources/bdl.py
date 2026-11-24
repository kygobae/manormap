"""Forest database (Bank Danych o Lasach, BDL): forest subareas (wydzielenia) that overlap a parcel.

Two layers: the State Forests (PGL LP) and other owners' forests with a simplified management plan (UPUL).
What BDL records as forest is almost always forest land (Ls) in the cadastre, which matters for building:
an OSM "wood" is often just park trees. Queried with ArcGIS REST identify (JSON), parcel polygon in EPSG:2180.
"""
from __future__ import annotations

import json
import logging
import re

from shapely.geometry import Point, Polygon

from .. import config
from ..geo import ll_to_pl
from ..http import Http

log = logging.getLogger(__name__)

LAYERS = {5: "state", 6: "private"}       # wydzielenia PGL LP / poza PGL LP wg PUL
MIN_M2 = 100                               # slivers along the parcel edge are drawing noise


def parcel_and_point(site: dict):
    """The parcel in EPSG:2180 (or a 30 m circle around the site when there is none) and the site point."""
    x, y = ll_to_pl(site["lon"], site["lat"])
    ring = site.get("parcel_ll") or []
    poly = Polygon([ll_to_pl(lon, lat) for lon, lat in ring]) if len(ring) >= 4 else Point(x, y).buffer(30)
    return poly.buffer(0), Point(x, y)


def _clean(v) -> str:
    return "" if v in (None, "Null") else re.sub(r"\s+", " ", str(v)).strip()


def stands(http: Http, site: dict) -> dict:
    """{"status": found|none|unavailable, "ha", "pct", "at_site", "stands": [...]}. Never raises."""
    try:
        parcel, point = parcel_and_point(site)
    except Exception:
        return {"status": "unavailable"}
    x0, y0, x1, y1 = parcel.bounds
    shell = parcel.exterior.coords if parcel.geom_type == "Polygon" else parcel.convex_hull.exterior.coords
    geom = {"rings": [[[round(a, 1), round(b, 1)] for a, b in shell]], "spatialReference": {"wkid": 2180}}
    params = {"geometry": json.dumps(geom, separators=(",", ":")), "geometryType": "esriGeometryPolygon", "sr": 2180,
              "layers": "all:" + ",".join(map(str, LAYERS)), "tolerance": 0, "mapExtent": f"{x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f}",
              "imageDisplay": "600,600,96", "returnGeometry": "true", "f": "json"}
    try:
        data = json.loads(http.get(config.BDL_IDENTIFY, params=params, retries=2))
    except Exception as e:
        log.debug("BDL: %s", e)
        return {"status": "unavailable"}
    if "results" not in data:
        return {"status": "unavailable"}
    out, cover = [], []
    for r in data["results"]:
        rings = (r.get("geometry") or {}).get("rings") or []
        if not rings:
            continue
        g = Polygon(rings[0], rings[1:]).buffer(0)
        part = g.intersection(parcel)
        if part.area < MIN_M2:
            continue
        cover.append(part)
        a = {k: _clean(v) for k, v in (r.get("attributes") or {}).items()}
        age = a.get("species_age", "")
        out.append({"ha": round(part.area / 10000, 2), "owner": LAYERS.get(r.get("layerId"), "private"),
                    "owner_pl": a.get("owner_cat_name", ""), "owner_en": a.get("owner_cat_name_en", ""),
                    "species": a.get("species_cd_d", ""), "age": int(age) if age.isdigit() else None,
                    "kind": a.get("area_type_cd", ""), "habitat": a.get("site_type_cd", ""),
                    "function": a.get("forest_func_cd", ""), "protection": a.get("prot_category_cd", ""),
                    "plan_year": a.get("a_year", ""), "address": a.get("adress_forest", ""),
                    "at_site": g.buffer(5).contains(point)})
    if not out:
        return {"status": "none"}
    from shapely.ops import unary_union
    area = unary_union(cover).area
    out.sort(key=lambda s: -s["ha"])
    return {"status": "found", "ha": round(area / 10000, 2), "pct": round(100 * area / parcel.area),
            "at_site": any(s["at_site"] for s in out), "stands": out[:8]}


def is_forest(f: dict) -> bool:
    """Forest for the grade: the stands cover the manor's spot or most of the parcel."""
    return f.get("status") == "found" and (f.get("at_site") or (f.get("pct") or 0) >= config.FOREST_SHARE_PCT)


def annotate(http: Http, sites: list[dict], every: int = 250) -> int:
    """Store a forest summary on every located site (forest_status, forest_pct, forest_at_site); cached by Http."""
    n, todo = 0, [s for s in sites if s.get("status") != "unlocated" and s.get("lat") is not None]
    for i, s in enumerate(todo, 1):
        f = stands(http, s)
        s["forest_status"] = f["status"]
        s["forest_pct"] = f.get("pct", 0)
        s["forest_at_site"] = bool(f.get("at_site"))
        n += f["status"] == "found"
        if i % every == 0:
            log.info("BDL forest %d/%d (%d with forest)", i, len(todo), n)
    log.info("BDL forest: %d of %d located sites have forest stands on the parcel", n, len(todo))
    return n
