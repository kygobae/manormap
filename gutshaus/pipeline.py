"""Merge candidates into sites, judge lost/standing, score, attach parcels."""
from __future__ import annotations

import logging

from shapely.geometry import Point

from . import config, criteria
from .classify import house_mapped_as_building, register_names_house, ruin_evidence, text_flags
from .sources import gdos, osm
from .geo import pl_to_ll

log = logging.getLogger(__name__)


# -------------------------------------------------------------------- merge
def merge(cands: list[dict]) -> list[dict]:
    """Greedy spatial clustering. Exactly located candidates first (register
    polygons anchor a site); register entries located only by village attach to
    the nearest exact site within NID_ATTACH_RADIUS_M."""
    order = sorted(cands, key=lambda c: (c.get("location", "exact") != "exact",
                                         c["source"] != "nid", c["uid"]))
    sites: list[dict] = []
    for c in order:
        pt = Point(c["x"], c["y"])
        target = None
        if c.get("location", "exact") == "exact":
            for s in sites:
                area = s.get("area_geom")
                if (Point(s["x"], s["y"]).distance(pt) <= config.MERGE_DISTANCE_M
                        or (area is not None and area.distance(pt) <= 50)):
                    target = s
                    break
        else:
            exact = [(Point(s["x"], s["y"]).distance(pt), i) for i, s in enumerate(sites)
                     if s.get("location", "exact") == "exact"]
            near = [t for t in exact if t[0] <= config.NID_ATTACH_RADIUS_M]
            if near:
                target = sites[min(near)[1]]
            else:  # several register entries for the same village become one site
                target = next((s for s in sites if s.get("location") == c["location"]
                               and Point(s["x"], s["y"]).distance(pt) < 1), None)
        if target is None:
            s = dict(c)
            s["members"] = [c["uid"]]
            s["sources"] = {c["source"]}
            s["names"] = [c["name"]]
            s["nid_names"] = [c["name"]] if c["source"] == "nid" else []
            sites.append(s)
        else:
            t = target
            t["members"].append(c["uid"])
            t["sources"].add(c["source"])
            t["names"].append(c["name"])
            if c["source"] == "nid":
                t["nid_names"].append(c["name"])
            t["text"] = (t["text"] + " || " + c["text"])[:2000]
            t["registered"] = t["registered"] or c["registered"]
            for k in ("nid_ref", "osm_ref", "name_de"):
                if c.get(k) and c[k] not in (t.get(k) or ""):
                    t[k] = ";".join(filter(None, [t.get(k), c[k]]))
            for k in ("osm_is_park",):
                t[k] = t.get(k) or c.get(k, False)
            if t.get("area_geom") is None and c.get("area_geom") is not None:
                t["area_geom"] = c["area_geom"]
    for s in sites:
        # the register's wording first ("park dworski, Dąbrowa"), OSM names as fallback
        s["name"] = best_name(s["nid_names"]) if s["nid_names"] else best_name(s["names"])
        others = [n for n in dict.fromkeys(s["names"]) if n != s["name"]]
        s["also_known_as"] = "; ".join(others)[:300]
        s["sources"] = "+".join(sorted(s["sources"]))
        s["lon"], s["lat"] = pl_to_ll(s["x"], s["y"])
    log.info("Merged %d candidates into %d sites", len(cands), len(sites))
    return sites


def best_name(names: list[str]) -> str:
    estate = [n for n in names if text_flags(n)["estate_kw"]]
    pool = estate or names
    return max(pool, key=len) if pool else "unnamed"


# ------------------------------------------------------------------- assess
def manor_on_parcel(ctx: dict, parcel_geom) -> str:
    """"on_parcel" / "next_to_parcel" when a big building stands on the parcel or just outside it, else ""."""
    if parcel_geom is None or not ctx.get("nearby_big"):
        return ""
    pts = [Point(x, y) for x, y, _ in ctx["nearby_big"]]
    if any(parcel_geom.contains(p) for p in pts):
        return "on_parcel"
    if any(parcel_geom.distance(p) <= config.PARCEL_MARGIN_M for p in pts):
        return "next_to_parcel"
    return ""


def classify_status(site: dict, ctx: dict, parcel_geom=None) -> str:
    location = site.get("location", "exact")
    if location.startswith("village"):
        return "unlocated"  # register entry known only by village: find it on the Messtischblatt
    if ruin_evidence(site["text"]):
        return "ruin"
    if ctx.get("big_buildings") or manor_on_parcel(ctx, parcel_geom) or house_mapped_as_building(site["text"]):
        return "standing"
    if ctx.get("big_buildings_wide"):  # a big building 100-200 m away may well be the manor
        return "unknown"
    if location != "exact" and register_names_house(site["text"]):
        # the register lists the house itself, but we only guessed its place (a park near the village):
        # finding no building there says more about the guess than about the house
        return "unknown"
    return "lost" if ctx else "unknown"


def assess(site: dict, ctx: dict | None, parcel: dict | None, infra: dict | None = None,
           protected: list | None = None, town_list: list | None = None) -> dict:
    ctx = ctx or {}
    flags = text_flags(site["text"] + " " + site["name"])
    out = dict(site)
    pgeom = (parcel or {}).get("parcel_geom")
    out.update({
        "status": classify_status(site, ctx, pgeom),
        # where the big building is, relative to the parcel: dossiers explain "next_to_parcel" (split-off house)
        "manor_building": manor_on_parcel(ctx, pgeom) if not site.get("location", "exact").startswith("village") else "",
        "park": bool(ctx.get("park_nearby") or flags["park_kw"] or site.get("osm_is_park")),
        "land_cover": ctx.get("land_cover", "unknown"),
        "n_buildings": ctx.get("n_buildings"),
        "max_building_m2": ctx.get("max_building_m2"),
    })
    if out["status"] == "unlocated":
        out["land_cover"] = "unknown"
    out.update(infra or {})
    if protected is not None:
        out.update(gdos.assess(Point(site["x"], site["y"]), protected))
    if town_list is not None:
        out["nearest_town"], out["dist_town_m"] = osm.nearest_town(site["x"], site["y"], town_list)
    if parcel:
        out.update(parcel)
    out.update(criteria.compute(out))
    out["score"] = criteria.score(out)
    out["why"] = criteria.explain(out)
    return out


def links(s: dict) -> dict:
    lat, lon = s["lat"], s["lon"]
    d = {
        "link_osm": f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lon:.6f}#map=17/{lat:.6f}/{lon:.6f}",
        "link_gmaps": f"https://www.google.com/maps/@{lat:.6f},{lon:.6f},400m/data=!3m1!1e3",
    }
    if s.get("parcel_id"):
        d["link_geoportal"] = f"https://mapy.geoportal.gov.pl/imap/Imgp_2.html?identifyParcel={s['parcel_id']}"
    return d
