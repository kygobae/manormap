"""OpenStreetMap via Overpass: powiat boundary, candidates, and site context."""
from __future__ import annotations

import json
import logging
import time

from shapely.geometry import LineString, Point, box
from shapely.ops import polygonize, unary_union

from .. import config
from ..classify import is_relevant_osm
from ..geo import fold, geom_to_ll, ll_to_pl, way_polygon_pl
from ..http import Http

log = logging.getLogger(__name__)

NAME_REGEX = "dw[oó]r|dworsk|pa[lł]ac|folwar|maj[aą]t|oficyn|zesp[oó][lł] (dworsk|pa[lł]ac)|gutshaus|herrenhaus|rittergut"


class Overpass:
    """Overpass client that spreads load over the public mirrors: it remembers the
    mirror that answered last and moves on at the first error instead of hammering
    a busy server."""

    def __init__(self, http: Http):
        self.http = http
        self.start = 0

    def query(self, q: str) -> dict:
        urls = config.OVERPASS_URLS
        last = None
        for attempt in range(2 * len(urls)):
            url = urls[(self.start + attempt) % len(urls)]
            try:
                raw = self.http.post(url, data={"data": q}, retries=1, validate=check_response)
                data = json.loads(raw)
                self.start = (self.start + attempt) % len(urls)
                return data
            except Exception as e:
                last = e
                log.warning("Overpass %s failed (%s); trying next mirror", url.split("/")[2], str(e)[:80])
                time.sleep(3 if attempt < len(urls) else 20)
        raise RuntimeError(f"All Overpass mirrors failed: {last}")


def check_response(raw: bytes) -> None:
    """Overpass answers a timed-out or out-of-memory query with HTTP 200, partial
    elements and a "remark". Reject that before it is cached as a full answer."""
    data = json.loads(raw)
    remark = data.get("remark") or ""
    if "error" in remark.lower():
        raise ValueError(f"Overpass incomplete answer: {remark[:200]}")


def bbox_filter(area_pl, pad_m: float = 500) -> str:
    """Overpass bbox filter for the search area. Bboxes are fast; Overpass area
    filters on a whole powiat regularly time out, so we clip locally instead."""
    w, s, e, n = geom_to_ll(area_pl.buffer(pad_m).envelope).bounds
    return f"({s:.5f},{w:.5f},{n:.5f},{e:.5f})"


def boundary(op: Overpass, teryt: str):
    """Powiat polygon in 2180 and its name."""
    q = f"""[out:json][timeout:180];
relation["boundary"="administrative"]["admin_level"="6"]["teryt:terc"="{teryt}"];
out geom;"""
    data = op.query(q)
    rels = [e for e in data.get("elements", []) if e["type"] == "relation"]
    if not rels:
        raise RuntimeError(f"No powiat with teryt:terc={teryt} found in OSM. Use --bbox instead.")
    rel = rels[0]
    rings = {"outer": [], "inner": []}
    for m in rel.get("members", []):
        if m.get("type") == "way" and m.get("geometry"):
            role = "inner" if m.get("role") == "inner" else "outer"
            rings[role].append(LineString([ll_to_pl(p["lon"], p["lat"]) for p in m["geometry"]]))
    outer = unary_union(list(polygonize(unary_union(rings["outer"]))))
    if outer.is_empty:
        raise RuntimeError("Could not build powiat polygon from OSM relation")
    if rings["inner"]:  # e.g. Koszalin city is an enclave inside powiat koszaliński
        outer = outer.difference(unary_union(list(polygonize(unary_union(rings["inner"])))))
    return outer, rel.get("tags", {}).get("name", teryt)


def bbox_polygon(bbox: tuple):
    """bbox = (min_lon, min_lat, max_lon, max_lat) -> polygon in 2180."""
    x1, y1 = ll_to_pl(bbox[0], bbox[1])
    x2, y2 = ll_to_pl(bbox[2], bbox[3])
    return box(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


def candidates(op: Overpass, area_pl) -> list[dict]:
    f = bbox_filter(area_pl)
    inside = area_pl.buffer(200)
    hist = "|".join(config.OSM_HISTORIC_VALUES)
    q = f"""[out:json][timeout:300];
(
  nwr["historic"~"^({hist})$"]{f};
  nwr["building"~"^(manor|palace)$"]{f};
  nwr["castle_type"]{f};
  nwr["leisure"~"^(park|garden)$"]["name"]{f};
  nwr["name"~"{NAME_REGEX}",i]{f};
  nwr["old_name"~"{NAME_REGEX}",i]{f};
);
out center tags;"""
    data = op.query(q)
    out = []
    for e in data.get("elements", []):
        tags = e.get("tags", {})
        if not is_relevant_osm(tags):
            continue
        lat = e.get("lat") or e.get("center", {}).get("lat")
        lon = e.get("lon") or e.get("center", {}).get("lon")
        if lat is None:
            continue
        x, y = ll_to_pl(lon, lat)
        if not inside.contains(Point(x, y)):
            continue
        out.append(candidate_from_tags(e["type"], e["id"], lon, lat, tags, x, y))
    log.info("OSM: %d candidate features", len(out))
    return out


def candidate_from_tags(kind: str, oid: int, lon: float, lat: float, tags: dict, x: float, y: float) -> dict:
    text = " | ".join(f"{k}={v}" for k, v in tags.items()
                      if k in ("name", "old_name", "name:de", "description", "historic",
                               "building", "castle_type", "ruins", "heritage", "leisure"))
    return {
        "uid": f"osm:{kind}/{oid}",
        "source": "osm",
        "name": tags.get("name") or tags.get("old_name") or tags.get("historic", "unnamed"),
        "name_de": tags.get("name:de", ""),
        "text": text,
        "x": x, "y": y,
        "osm_ref": f"{kind}/{oid}",
        "nid_ref": tags.get("ref:nid", ""),
        "registered": bool(tags.get("heritage") or tags.get("ref:nid")),
        "location": "exact",
        "osm_is_park": tags.get("leisure") in ("park", "garden"),
    }


MAIN_PLACES = ("city", "town", "village", "hamlet")


def park_for_village(village: tuple, parks: list[tuple], rivals: list[tuple], radius: float) -> tuple | None:
    """The nearest park within `radius` of the village that is not closer to another village (EPSG:2180 points).
    A register entry for village A must not land in the manor park of neighbouring village B."""
    vx, vy = village
    d = lambda a, b: ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
    others = [r for r in rivals if d(r, village) > 1]
    for p in sorted((p for p in parks if d(p, village) <= radius), key=lambda p: d(p, village)):
        if all(d(p, village) <= d(p, r) for r in others):
            return p
    return None


def locate_records(op: Overpass, records: list[dict], area_pl) -> list[dict]:
    """Give village-only register records a position: nearest OSM park to the
    village node if there is one within NID_PLACE_RADIUS_M, else the village node."""
    if not records:
        return []
    f = bbox_filter(area_pl, pad_m=3000)
    near = area_pl.buffer(3000)
    q = f"""[out:json][timeout:180];
node["place"~"^(city|town|village|hamlet|isolated_dwelling|locality|neighbourhood|suburb)$"]{f};
out;"""
    places: dict[str, list] = {}
    rivals = []
    for e in op.query(q).get("elements", []):
        if not near.contains(Point(*ll_to_pl(e["lon"], e["lat"]))):
            continue
        if e.get("tags", {}).get("place") in MAIN_PLACES:
            rivals.append(ll_to_pl(e["lon"], e["lat"]))
        for key in ("name", "name:pl", "official_name"):
            if e.get("tags", {}).get(key):
                places.setdefault(fold(e["tags"][key]), []).append((e["lon"], e["lat"]))
    villages = {}
    for r in records:
        hits = places.get(fold(r["village"]))
        if hits:
            villages[fold(r["village"])] = hits[0]  # ambiguous names: first wins (flagged below)
    # nearest park per village
    R = config.NID_PLACE_RADIUS_M
    parks: dict[str, tuple] = {}
    keys = list(villages)
    for i in range(0, len(keys), config.CONTEXT_BATCH):
        batch = keys[i:i + config.CONTEXT_BATCH]
        stmts = [f'way["leisure"~"^(park|garden)$"](around:{R},{villages[k][1]:.6f},{villages[k][0]:.6f});'
                 for k in batch]
        data = op.query("[out:json][timeout:300];\n(\n" + "\n".join(stmts) + "\n);\nout center;")
        centres = [ll_to_pl(e["center"]["lon"], e["center"]["lat"]) for e in data.get("elements", []) if "center" in e]
        for k in batch:
            best = park_for_village(ll_to_pl(*villages[k]), centres, rivals, R)
            if best:
                parks[k] = best
    out, missing = [], set()
    for r in records:
        k = fold(r["village"])
        if k not in villages:
            missing.add(r["village"])
            continue
        r = dict(r)
        vxy = ll_to_pl(*villages[k])
        r["village_xy"] = vxy
        r["rivals_xy"] = [p for p in rivals if 1 < ((p[0] - vxy[0]) ** 2 + (p[1] - vxy[1]) ** 2) ** 0.5 <= 3 * R]
        if k in parks:
            r["x"], r["y"] = parks[k]
            r["location"] = "osm park near village"
        else:
            r["x"], r["y"] = ll_to_pl(*villages[k])
            r["location"] = "village centre"
        if len(places[k]) > 1:
            r["location"] += " (village name not unique, check)"
        out.append(r)
    if missing:
        log.warning("NID: no OSM place found for %d villages: %s", len(missing), ", ".join(sorted(missing))[:300])
    log.info("NID: located %d register entries (%d via nearby park)", len(out),
             sum(1 for r in out if r["location"].startswith("osm park")))
    return out


def context(op: Overpass, sites: list[dict]) -> dict[str, dict]:
    """For each site: buildings, parks and land cover within CONTEXT_RADIUS_M."""
    R = config.CONTEXT_RADIUS_M
    result: dict[str, dict] = {}
    for i in range(0, len(sites), config.CONTEXT_BATCH):
        batch = sites[i:i + config.CONTEXT_BATCH]
        stmts = []
        for s in batch:
            lat, lon = s["lat"], s["lon"]
            a = f"(around:{R},{lat:.6f},{lon:.6f})"
            wide = f"(around:{max(R, config.MANOR_LOST_RADIUS_M, config.PARCEL_SEARCH_MAX_M)},{lat:.6f},{lon:.6f})"
            stmts += [f'way["building"]{wide};', f'way["leisure"~"^(park|garden)$"]{a};',
                      f'way["landuse"]{a};', f'way["natural"~"^(wood|scrub|grassland)$"]{a};']
        q = "[out:json][timeout:300];\n(\n" + "\n".join(stmts) + "\n);\nout geom;"
        log.info("Overpass context %d-%d of %d", i + 1, i + len(batch), len(sites))
        data = op.query(q)
        feats = []
        for e in data.get("elements", []):
            if e["type"] != "way" or "geometry" not in e:
                continue
            poly = way_polygon_pl(e["geometry"])
            if poly is None:
                continue
            feats.append((e.get("tags", {}), poly))
        for s in batch:
            result[s["uid"]] = summarise_context(Point(s["x"], s["y"]), feats)
    return result


FOREST = {("landuse", "forest"), ("natural", "wood"), ("natural", "scrub")}
AGRI = {("landuse", v) for v in ("farmland", "meadow", "grass", "orchard", "allotments", "farmyard")} | {("natural", "grassland")}
BUILT = {("landuse", v) for v in ("residential", "industrial", "commercial", "retail", "construction")}


def summarise_context(pt: Point, feats: list) -> dict:
    R = config.CONTEXT_RADIUS_M
    big, n_buildings, max_area = [], 0, 0.0
    park_dist = None
    cover = None
    cover_dist = 1e9
    wide, nearby = 0, []
    for tags, poly in feats:
        d = poly.distance(pt)
        if "building" in tags and poly.area >= config.MANOR_BUILDING_MIN_M2:
            wide += d <= config.MANOR_LOST_RADIUS_M
            if d <= config.PARCEL_SEARCH_MAX_M:
                c = poly.centroid
                nearby.append([round(c.x, 1), round(c.y, 1), round(poly.area)])
        if d > R:
            continue
        if "building" in tags:
            n_buildings += 1
            if d <= config.MANOR_SEARCH_RADIUS_M:
                max_area = max(max_area, poly.area)
                if poly.area >= config.MANOR_BUILDING_MIN_M2:
                    big.append(round(poly.area))
            continue
        kv = {(k, v) for k, v in tags.items()}
        if tags.get("leisure") in ("park", "garden"):
            park_dist = d if park_dist is None else min(park_dist, d)
        cls = "forest" if kv & FOREST else "agri" if kv & AGRI else "built" if kv & BUILT else None
        if cls and d < cover_dist:  # d == 0 means the point lies inside
            cover, cover_dist = cls, d
    return {
        "n_buildings": n_buildings,
        "max_building_m2": round(max_area),
        "big_buildings": big,
        "big_buildings_wide": wide,
        "nearby_big": nearby,   # [x, y, m²] of big buildings: tested against the parcel later
        "park_nearby": park_dist is not None,
        "land_cover": cover or "unknown",
    }


def to_ll(poly):
    return geom_to_ll(poly)


# ------------------------------------------------------------- infrastructure
PUBLIC_ROADS = "motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street"


def _lines(e) -> list:
    """Way or relation (out geom) -> list of LineStrings in 2180."""
    parts = []
    if e.get("geometry"):
        parts.append(e["geometry"])
    for m in e.get("members", []) or []:
        if m.get("geometry"):
            parts.append(m["geometry"])
    out = []
    for p in parts:  # cropped output (out geom(bbox)) marks dropped nodes as null: split there
        run = []
        for c in p + [None]:
            if c:
                run.append(ll_to_pl(c["lon"], c["lat"]))
                continue
            if len(run) >= 2:
                out.append(LineString(run))
            run = []
    return out


def infrastructure(op: Overpass, sites: list[dict]) -> dict[str, dict]:
    """Distances from each site to power lines, public roads, other buildings and water."""
    import numpy as np
    import shapely

    result: dict[str, dict] = {}
    B = config.INFRA_BATCH
    for i in range(0, len(sites), B):
        batch = sites[i:i + B]
        lines_q, bld_q = [], []
        for s in batch:
            ll = f"{s['lat']:.6f},{s['lon']:.6f}"
            # crop returned geometry to ~2 km around the site: long rivers and lines stay small
            dlat, dlon = 0.02, 0.032
            crop = f"({s['lat'] - dlat:.5f},{s['lon'] - dlon:.5f},{s['lat'] + dlat:.5f},{s['lon'] + dlon:.5f})"
            lines_q.append(f'(way["power"~"^(line|minor_line|cable)$"](around:2000,{ll});'
                           f'way["highway"~"^({PUBLIC_ROADS})$"](around:800,{ll});'
                           f'way["natural"="water"](around:2000,{ll});'
                           f'way["waterway"~"^(river|canal)$"](around:2000,{ll}););out geom{crop};')
            bld_q.append(f'way["building"](around:1500,{ll});')
        log.info("Overpass infrastructure %d-%d of %d", i + 1, i + len(batch), len(sites))
        d1 = op.query("[out:json][timeout:300];\n" + "\n".join(lines_q))
        time.sleep(config.OVERPASS_PAUSE_S)
        d2 = op.query("[out:json][timeout:300];\n(\n" + "\n".join(bld_q) + "\n);\nout center;")
        time.sleep(config.OVERPASS_PAUSE_S)
        groups = {"power": [], "road": [], "water": []}
        for e in d1.get("elements", []):
            t = e.get("tags", {})
            key = "power" if "power" in t else "road" if "highway" in t else "water"
            groups[key] += _lines(e)
        arrays = {k: np.array(v, dtype=object) for k, v in groups.items()}
        bxy = np.array([ll_to_pl(e["center"]["lon"], e["center"]["lat"])
                        for e in d2.get("elements", []) if "center" in e] or np.empty((0, 2)))
        for s in batch:
            pt = Point(s["x"], s["y"])
            r = {}
            # nothing found within the search radius -> report the radius ("at least")
            radius = {"power": 2000, "road": 800, "water": 2000}
            for k, arr in arrays.items():
                d = float(shapely.distance(arr, pt).min()) if len(arr) else radius[k]
                r[f"dist_{k}_m"] = round(min(d, radius[k]))
            if len(bxy):
                d = np.hypot(bxy[:, 0] - s["x"], bxy[:, 1] - s["y"])
                r["dist_building_m"] = round(float(min(d.min(), 1500)))
                r["buildings_500m"] = int((d <= 500).sum())
            else:
                r["dist_building_m"], r["buildings_500m"] = 1500, 0
            result[s["uid"]] = r
    return result


def towns(op: Overpass, area_pl, pad_m: float = 40000) -> list[tuple]:
    """(name, x, y) of towns and cities in and around the search area."""
    q = f"""[out:json][timeout:180];
node["place"~"^(city|town)$"]{bbox_filter(area_pl, pad_m)};
out;"""
    out = []
    for e in op.query(q).get("elements", []):
        x, y = ll_to_pl(e["lon"], e["lat"])
        out.append((e.get("tags", {}).get("name", "?"), x, y))
    log.info("OSM: %d towns within %d km", len(out), pad_m // 1000)
    return out


def nearest_town(x: float, y: float, town_list: list[tuple]) -> tuple[str, int] | tuple[None, None]:
    if not town_list:
        return None, None
    name, tx, ty = min(town_list, key=lambda t: (t[1] - x) ** 2 + (t[2] - y) ** 2)
    return name, round(((tx - x) ** 2 + (ty - y) ** 2) ** 0.5)


def powiat_list(op: Overpass) -> list[tuple[str, str]]:
    """All powiats in Poland as (teryt, name), from OSM."""
    q = """[out:json][timeout:180];
relation["boundary"="administrative"]["admin_level"="6"]["teryt:terc"](48.9,14.0,55.0,24.3);
out tags;"""
    seen = {}
    for e in op.query(q).get("elements", []):
        t = e.get("tags", {})
        code = t.get("teryt:terc", "")
        if len(code) == 4 and code.isdigit() and code[:2] in config.VOIVODESHIPS:
            seen[code] = t.get("name", code)
    return sorted(seen.items())


def poland_outline(op: Overpass, tolerance_deg: float = 0.01):
    """Simplified outline of Poland (lon/lat GeoJSON geometry) for the minimap."""
    from shapely.geometry import mapping
    data = op.query("[out:json][timeout:300];\nrelation(49715);\nout geom;")
    rel = next(e for e in data["elements"] if e["type"] == "relation")
    lines = [LineString([(p["lon"], p["lat"]) for p in m["geometry"]])
             for m in rel.get("members", []) if m.get("type") == "way" and m.get("role") == "outer" and m.get("geometry")]
    poly = unary_union(list(polygonize(unary_union(lines))))
    return mapping(poly.simplify(tolerance_deg))



def poi_kind(tags: dict) -> str | None:
    """The kind of a point of interest for config.SURROUNDINGS_RADIUS_M, or None."""
    if tags.get("power") == "generator" and tags.get("generator:source") == "wind":
        return "wind"
    if tags.get("railway") in ("station", "halt"):
        return "station"
    a = tags.get("amenity")
    if a == "school":
        return "school"
    if a in ("doctors", "clinic", "hospital"):
        return "doctor"
    if a == "pharmacy":
        return "pharmacy"
    if tags.get("shop") in ("supermarket", "convenience"):
        return "shop"
    if tags.get("building") in config.LIVESTOCK_BUILDINGS:
        return "livestock"                       # kept only from config.LIVESTOCK_MIN_M2 (see summarise_surroundings)
    return None


def summarise_surroundings(here: Point, found: list[tuple]) -> dict:
    """[(kind, name, distance m[, area m²])] -> {kind: {"count", "nearest_m", "name"}} within each kind's radius.
    Livestock buildings count only from config.LIVESTOCK_MIN_M2 (an area of None, from a mapped point, does not)."""
    out = {}
    big = lambda f: f[0] != "livestock" or (len(f) > 3 and (f[3] or 0) >= config.LIVESTOCK_MIN_M2)
    found = [f for f in found if big(f)]
    for kind, r in config.SURROUNDINGS_RADIUS_M.items():
        hits = sorted((f[2], f[1]) for f in found if f[0] == kind and f[2] <= r)
        out[kind] = {"count": len(hits), "nearest_m": round(hits[0][0]) if hits else None,
                     "name": hits[0][1] if hits else ""}
    return out


def surroundings(op: Overpass, lon: float, lat: float) -> dict:
    R = config.SURROUNDINGS_RADIUS_M
    at = f"{lat:.6f},{lon:.6f}"
    q = (f'[out:json][timeout:120];('
         f'nwr["power"="generator"]["generator:source"="wind"](around:{R["wind"]},{at});'
         f'nwr["railway"~"^(station|halt)$"](around:{R["station"]},{at});'
         f'nwr["amenity"~"^(school|doctors|clinic|hospital|pharmacy)$"](around:{R["school"]},{at});'
         f'nwr["shop"~"^(supermarket|convenience)$"](around:{R["shop"]},{at});'
         f'way["highway"~"^({"|".join(config.MAIN_ROADS)})$"](around:{R["main_road"]},{at});'
         f'way["railway"="rail"](around:{R["railway"]},{at});'
         f'way["building"~"^({"|".join(config.LIVESTOCK_BUILDINGS)})$"](around:{R["livestock"]},{at}););out tags geom qt;')
    here = Point(*ll_to_pl(lon, lat))
    found = []
    for e in op.query(q).get("elements", []):
        tags = e.get("tags", {})
        kind = poi_kind(tags)
        if tags.get("highway") in config.MAIN_ROADS:
            kind = "main_road"
        elif tags.get("railway") == "rail":
            kind = None if tags.get("service") else "railway"
        if not kind:
            continue
        area = None
        if "lat" in e:
            g = Point(*ll_to_pl(e["lon"], e["lat"]))
        elif e.get("geometry"):
            pts = [ll_to_pl(c["lon"], c["lat"]) for c in e["geometry"] if c]
            g = LineString(pts) if len(pts) > 1 else Point(pts[0])
            if len(pts) >= 4 and pts[0] == pts[-1]:
                from shapely.geometry import Polygon
                area = Polygon(pts).area
        else:
            continue
        found.append((kind, tags.get("name", ""), here.distance(g), area))
    return summarise_surroundings(here, found)


class OverpassOSM:
    """OSM backend using the public Overpass API (no download needed, but slow for big runs)."""

    def __init__(self, http: Http):
        self.op = Overpass(http)

    def prepare(self, area_pl):
        pass

    def boundary(self, teryt):
        return boundary(self.op, teryt)

    def powiat_list(self):
        return powiat_list(self.op)

    def poland_outline(self):
        return poland_outline(self.op)

    def candidates(self, area_pl):
        return candidates(self.op, area_pl)

    def locate_records(self, records, area_pl):
        return locate_records(self.op, records, area_pl)

    def context(self, sites):
        return context(self.op, sites)

    def infrastructure(self, sites):
        return infrastructure(self.op, sites)

    def towns(self, area_pl):
        return towns(self.op, area_pl)

    def surroundings(self, lon: float, lat: float) -> dict:
        return surroundings(self.op, lon, lat)
