"""CSV (review sheet), GeoPackage (QGIS) and review.html (map)."""
from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

import geopandas as gpd
from shapely.geometry import Point, Polygon, mapping

from . import config
from .geo import geom_to_ll

log = logging.getLogger(__name__)

BASE_COLS = ["rank", "score", "status", "location", "verdict", "notes", "name", "also_known_as", "name_de",
             "voivodeship", "powiat", "commune", "precinct", "parcel_no", "parcel_id", "parcel_ha",
             "land_cover", "park", "protection", "protection_names", "dist_power_m", "dist_road_m",
             "dist_building_m", "buildings_500m", "nearest_town", "dist_town_m", "dist_water_m",
             "max_building_m2", "n_buildings", "registered", "kowr_offer", "kowr_tender", "kowr_url",
             "sources", "nid_ref", "osm_ref"]
TAIL_COLS = ["why", "lat", "lon", "link_geoportal", "link_gmaps", "link_osm", "uid", "members"]


def csv_cols() -> list[str]:
    return BASE_COLS + [f"c_{c['key']}" for c in config.CRITERIA] + TAIL_COLS


CSV_COLS = csv_cols()
VERDICTS = ("high", "possible", "skip")


# ------------------------------------------------------------ verdict memory
def load_verdicts(out_dir: Path, extra: Path | None = None) -> list[dict]:
    """Previous review: candidates.csv edits, verdicts.csv from review.html, then --verdicts (last wins)."""
    rows = []
    for p in (out_dir / "candidates.csv", out_dir / "verdicts.csv", extra):
        if not p or not Path(p).exists():
            continue
        with Path(p).open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if (r.get("verdict") or "").strip() or (r.get("notes") or "").strip():
                    rows.append(r)
    return rows


def apply_verdicts(sites: list[dict], previous: list[dict]) -> int:
    by_uid = {}
    for r in previous:
        for u in [r.get("uid", "")] + (r.get("members", "") or "").split(";"):
            if u:
                by_uid[u] = r
    n = 0
    for s in sites:
        hit = None
        members = s["members"] if isinstance(s["members"], list) else s["members"].split(";")
        for u in [s["uid"]] + members:
            if u in by_uid:
                hit = by_uid[u]
        if hit is None:  # fall back to proximity
            for r in previous:
                try:
                    if abs(float(r["lat"]) - s["lat"]) < 0.0005 and abs(float(r["lon"]) - s["lon"]) < 0.0008:
                        hit = r
                except (KeyError, ValueError):
                    pass
        if hit:
            s["verdict"] = (hit.get("verdict") or "").strip().lower()
            s["notes"] = hit.get("notes") or ""
            n += 1
    return n


# ------------------------------------------------------------- serialise
DROP = {"area_geom", "parcel_geom", "names", "nid_names", "text_exact", "big_buildings", "x", "y"}


def serialise(site: dict) -> dict:
    """Plain-JSON version of a site (parcel outline as simplified lon/lat ring)."""
    d = {k: v for k, v in site.items() if k not in DROP}
    d["text"] = (site.get("text") or "")[:300]
    d["lat"], d["lon"] = round(site["lat"], 6), round(site["lon"], 6)
    g = site.get("parcel_geom")
    if g is not None and not g.is_empty:
        g = geom_to_ll(g.simplify(1.0))
        poly = max(g.geoms, key=lambda p: p.area) if hasattr(g, "geoms") else g
        if hasattr(poly, "exterior"):
            d["parcel_ll"] = [[round(x, 6), round(y, 6)] for x, y in poly.exterior.coords]
    return d


def save_sites(sites: list[dict], meta: dict, path: Path):
    path.write_text(json.dumps({"meta": meta, "sites": [serialise(s) for s in sites]},
                               ensure_ascii=False, default=str), encoding="utf-8")


def load_sites(path: Path) -> tuple[dict, list[dict]]:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    return d["meta"], d["sites"]


def rank(sites: list[dict]):
    sites.sort(key=lambda s: (-s["score"], s["name"]))
    for r, s in enumerate(sites, 1):
        s["rank"] = r


# --------------------------------------------------------------------- write
def write_csv(sites: list[dict], path: Path):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=csv_cols(), extrasaction="ignore")
        w.writeheader()
        for s in sites:
            row = dict(s)
            row["members"] = ";".join(s["members"]) if isinstance(s["members"], list) else s["members"]
            w.writerow(row)


def write_gpkg(sites: list[dict], area_ll, path: Path):
    """sites: serialised dicts. area_ll: lon/lat geometry of the search area (or None)."""
    if path.exists():
        path.unlink()
    cols = [c for c in csv_cols() if c not in ("lat", "lon")]
    rows = [{c: (";".join(s[c]) if c == "members" and isinstance(s[c], list) else s.get(c)) for c in cols}
            for s in sites]
    gpd.GeoDataFrame(rows, geometry=[Point(s["lon"], s["lat"]) for s in sites], crs=4326
                     ).to_crs(2180).to_file(path, layer="sites", driver="GPKG")
    parcels = [s for s in sites if s.get("parcel_ll")]
    if parcels:
        gpd.GeoDataFrame(
            [{"uid": s["uid"], "name": s["name"], "score": s["score"], "status": s["status"],
              "parcel_id": s.get("parcel_id"), "parcel_ha": s.get("parcel_ha")} for s in parcels],
            geometry=[Polygon(s["parcel_ll"]) for s in parcels], crs=4326,
        ).to_crs(2180).to_file(path, layer="parcels", driver="GPKG")
    if area_ll is not None:
        gpd.GeoDataFrame({"name": ["search area"]}, geometry=[area_ll], crs=4326).to_crs(2180).to_file(
            path, layer="search_area", driver="GPKG")


def write_html(sites: list[dict], title: str, path: Path, areas_ll: list | None = None,
               poland: dict | None = None, index_href: str | None = None, offers: list | None = None):
    """sites: serialised dicts. areas_ll: search-area outlines (lon/lat geometries)."""
    keep = set(csv_cols()) | {"parcel_ll", "text", "status_note", "status_data"}
    data = {
        "title": title,
        "sites": [{k: v for k, v in s.items() if k in keep} for s in sites],
        "criteria": [{k: c[k] for k in ("key", "label", "weight", "help")} for c in config.CRITERIA],
        "wms": config.WMS_LAYERS,
        "areas": [mapping(a.simplify(0.0005)) for a in (areas_ll or [])],
        "poland": poland,
        "references": config.REFERENCE_POINTS,
        "voivodeships": sorted(set(config.VOIVODESHIPS.values())),
        "index": index_href,
        "kowr": [{k: o.get(k) for k in KOWR_KEYS} for o in (offers or []) if o.get("lat") is not None],
    }
    html = TEMPLATE.replace("__TITLE__", title).replace("__DATA__", _js(data))
    path.write_text(html, encoding="utf-8")


KOWR_KEYS = ("title", "url", "lat", "lon", "voivodeship", "powiat", "gmina", "obreb", "parcels", "area_ha",
             "usable_m2", "kind", "zoning", "tender", "media", "description", "contact_email", "contact_phone")


def _js(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str).replace("</", "<\\/")


TEMPLATE = (Path(__file__).parent / "review_template.html").read_text(encoding="utf-8")


# ------------------------------------------------------------------ index page
def migrate_layout(root: Path):
    """Older versions wrote output/<teryt>/; move those into output/powiats/<teryt>/."""
    import re
    import shutil
    root = Path(root)
    for d in sorted(root.glob("[0-9][0-9][0-9][0-9]")):
        if d.is_dir() and re.fullmatch(r"\d{4}", d.name):
            target = root / "powiats" / d.name
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(d), str(target))
                log.info("Moved %s -> %s", d, target)
            except OSError as e:  # e.g. a file is open in another program
                log.warning("Could not move %s (%s); close programs using it and run again", d, e)


def save_summary(sites: list[dict], meta: dict, path: Path):
    teryt = meta.get("teryt") or ""
    counts = {k: sum(1 for s in sites if s["status"] == k) for k in ("lost", "ruin", "unlocated", "standing", "unknown")}
    top = sorted(sites, key=lambda s: -s["score"])[:3]
    data = {"teryt": teryt, "title": meta.get("title", teryt),
            "powiat": meta.get("title", teryt).split(" (")[0].replace("powiat ", ""),
            "voivodeship": config.VOIVODESHIPS.get(teryt[:2], ""), "finished": meta.get("finished", ""),
            "n": len(sites), "counts": counts,
            "top": [{"name": s["name"], "score": s["score"], "status": s["status"]} for s in top]}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _summary_from_sites(path: Path) -> dict | None:
    """Fallback for powiats run before summary.json existed."""
    try:
        meta, sites = load_sites(path)
    except Exception:
        return None
    tmp = path.parent / "summary.json"
    save_summary(sites, meta, tmp)
    return json.loads(tmp.read_text(encoding="utf-8"))


def write_index(root: Path, total: int | None = None, failed: list | None = None):
    """See below; also lists KOWR heritage offers from <root>/_kowr.json."""
    return _write_index(root, total, failed)


def _write_index(root: Path, total: int | None = None, failed: list | None = None):
    """output/index.html: start page with the national map and every powiat."""
    root = Path(root)
    rows = []
    for d in sorted((root / "powiats").glob("*")):
        sm = d / "summary.json"
        if sm.exists():
            rows.append(json.loads(sm.read_text(encoding="utf-8")))
        elif (d / "sites.json").exists():
            r = _summary_from_sites(d / "sites.json")
            if r:
                rows.append(r)
    if failed is None and (root / "failed.txt").exists():
        failed = [x for x in (root / "failed.txt").read_text(encoding="utf-8").split() if x]
    national = (root / "poland" / "review.html").exists()
    kowr_file = root / "_kowr.json"
    offers = json.loads(kowr_file.read_text(encoding="utf-8")) if kowr_file.exists() else []
    data = {"rows": rows, "total": total, "failed": failed or [], "national": national,
            "kowr": [{k: o.get(k) for k in ("title", "url", "voivodeship", "powiat", "gmina", "area_ha", "tender", "lat")}
                     for o in offers],
            "generated": __import__("time").strftime("%Y-%m-%d %H:%M"),
            "national_n": None}
    if national and (root / "poland" / "candidates.csv").exists():
        with (root / "poland" / "candidates.csv").open(encoding="utf-8-sig") as f:
            data["national_n"] = sum(1 for _ in f) - 1
    html = INDEX_TEMPLATE.replace("__DATA__", _js(data))
    (root / "index.html").write_text(html, encoding="utf-8")


INDEX_TEMPLATE = (Path(__file__).parent / "index_template.html").read_text(encoding="utf-8")
