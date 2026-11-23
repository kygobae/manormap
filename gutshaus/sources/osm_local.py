"""Local OpenStreetMap backend.

`gutshaus osm-extract` reads a Geofabrik extract (poland-latest.osm.pbf) once and
writes the few layers this tool needs into a GeoPackage (EPSG:2180). The
LocalOSM class then answers the same questions as the Overpass backend from
that file: fast, offline, and without loading the public Overpass servers.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pyogrio
import shapely
from shapely.geometry import Point, box

from .. import config
from ..classify import is_relevant_osm
from ..geo import fold, geom_to_ll, ll_to_pl
from . import osm as osm_remote

log = logging.getLogger(__name__)

GEOFABRIK_URL = "https://download.geofabrik.de/europe/poland-latest.osm.pbf"
KEYS = ("building", "highway", "power", "natural", "waterway", "landuse", "leisure", "place", "historic",
        "boundary", "name", "old_name", "castle_type", "heritage", "ruins", "railway", "amenity", "shop")
PUBLIC_ROADS = set(osm_remote.PUBLIC_ROADS.split("|"))
LANDCOVER = {**{("landuse", v): "forest" for v in ("forest",)},
             **{("natural", v): "forest" for v in ("wood", "scrub")},
             **{("landuse", v): "agri" for v in ("farmland", "meadow", "grass", "orchard", "allotments", "farmyard")},
             ("natural", "grassland"): "agri",
             **{("landuse", v): "built" for v in ("residential", "industrial", "commercial", "retail", "construction")}}
PLACES = {"city", "town", "village", "hamlet", "isolated_dwelling", "locality", "neighbourhood", "suburb"}
CAND_TAGS = ("name", "old_name", "name:de", "description", "historic", "building", "castle_type", "ruins",
             "heritage", "leisure", "ref:nid", "heritage:name", "place")


# ===================================================================== extract
def download(dest: Path, url: str = GEOFABRIK_URL) -> Path:
    import requests
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    with requests.get(url, stream=True, timeout=(15, 300), headers={"User-Agent": config.USER_AGENT}) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done, t0 = 0, time.time()
        with tmp.open("wb") as f:
            for chunk in r.iter_content(chunk_size=8 << 20):
                f.write(chunk)
                done += len(chunk)
                if time.time() - t0 > 10:
                    log.info("Downloaded %.0f / %.0f MB", done / 1e6, total / 1e6)
                    t0 = time.time()
    tmp.replace(dest)
    return dest


class _Writer:
    """Buffers features per layer and appends them to the GeoPackage in batches."""

    def __init__(self, path: Path, batch: int = 50000):
        self.path, self.batch = path, batch
        self.buf: dict[str, list] = {}
        self.counts: dict[str, int] = {}

    def add(self, layer: str, wkb_hex: str, props: dict):
        rows = self.buf.setdefault(layer, [])
        rows.append((wkb_hex, props))
        if len(rows) >= self.batch:
            self.flush(layer)

    def flush(self, key: str):
        rows = self.buf.get(key)
        if not rows:
            return
        self.buf[key] = []
        layer = key
        geoms = gpd.GeoSeries.from_wkb([bytes.fromhex(w) for w, _ in rows], crs=4326).to_crs(2180)
        gdf = gpd.GeoDataFrame([p for _, p in rows], geometry=geoms.values, crs=2180)
        if layer == "buildings":  # keep footprint area + centroid only (millions of buildings)
            gdf["area_m2"] = gdf.geometry.area.round(1)
            gdf = gdf.set_geometry(gdf.geometry.centroid)
        elif layer in ("landcover", "admin"):
            gdf = gdf.set_geometry(gdf.geometry.simplify(2 if layer == "landcover" else 5))
        elif layer == "water_areas":  # store shorelines as lines
            gdf = gdf.set_geometry(gdf.geometry.boundary)
            layer = "water"
        elif layer == "candidates":
            gdf = gdf.set_geometry(gdf.geometry.representative_point())
        elif layer == "poi_areas":  # schools, shops ... mapped as areas: one point each, in the pois layer
            gdf = gdf.set_geometry(gdf.geometry.representative_point())
            layer = "pois"
        elif layer == "parks":
            gdf["park_x"], gdf["park_y"] = gdf.geometry.centroid.x, gdf.geometry.centroid.y
        gdf["geometry"] = shapely.force_2d(gdf.geometry.values)
        pyogrio.write_dataframe(gdf, self.path, layer=layer, driver="GPKG",
                                append=layer in self.counts,
                                promote_to_multi=layer not in ("candidates", "places", "buildings", "pois"))
        self.counts[layer] = self.counts.get(layer, 0) + len(gdf)

    def close(self):
        for layer in list(self.buf):
            self.flush(layer)
        return self.counts


def extract(pbf: Path, out: Path, node_cache: Path | None = None) -> dict:
    """Read an OSM extract and write the layers this tool needs to `out` (GeoPackage)."""
    import osmium

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.gpkg")
    if tmp.exists():
        tmp.unlink()
    storage = f"sparse_file_array,{node_cache}" if node_cache else "flex_mem"
    fp = (osmium.FileProcessor(str(pbf)).with_locations(storage).with_areas()
          .with_filter(osmium.filter.KeyFilter(*KEYS)))
    wkb = osmium.geom.WKBFactory()
    w = _Writer(tmp)
    n, t0 = 0, time.time()
    for o in fp:
        n += 1
        if n % 500000 == 0:
            log.info("%d objects processed (%.0f s)", n, time.time() - t0)
        tags = {t.k: t.v for t in o.tags}
        try:
            if o.is_node():
                _node(o, tags, wkb, w)
            elif o.is_way():
                _way(o, tags, wkb, w)
            elif o.is_area():
                _area(o, tags, wkb, w)
        except (osmium.InvalidLocationError, RuntimeError):
            continue
    counts = w.close()
    del fp  # release the node-location cache file (Windows keeps it locked otherwise)
    import gc
    gc.collect()
    if out.exists():
        out.unlink()
    tmp.replace(out)
    log.info("OSM extract written to %s: %s", out, counts)
    return counts


def _cand_props(kind: str, oid: int, tags: dict) -> dict:
    return {"osm_type": kind, "osm_id": oid, "tags": json.dumps({k: tags[k] for k in CAND_TAGS if k in tags}, ensure_ascii=False)}


def _node(o, tags, wkb, w):
    if not o.location.valid():
        return
    if tags.get("place") in PLACES:
        w.add("places", wkb.create_point(o), {"name": tags.get("name", ""), "place": tags["place"],
                                               "alt_names": "|".join(tags[k] for k in ("name:pl", "official_name") if k in tags)})
    if is_relevant_osm(tags):
        w.add("candidates", wkb.create_point(o), _cand_props("node", o.id, tags))
    kind = osm_remote.poi_kind(tags)
    if kind:
        w.add("pois", wkb.create_point(o), {"kind": kind, "name": tags.get("name", "")})


def _way(o, tags, wkb, w):
    hw, pw, ww = tags.get("highway"), tags.get("power"), tags.get("waterway")
    if hw in PUBLIC_ROADS:
        w.add("roads", wkb.create_linestring(o), {"highway": hw})
    elif pw in ("line", "minor_line", "cable"):
        w.add("power", wkb.create_linestring(o), {"power": pw})
    elif ww in ("river", "canal"):
        w.add("water", wkb.create_linestring(o), {"kind": ww})
    elif tags.get("railway") == "rail" and not tags.get("service"):
        w.add("rail", wkb.create_linestring(o), {})


def _area(o, tags, wkb, w):
    kind = "way" if o.from_way() else "relation"
    oid = o.orig_id()
    if "building" in tags:
        w.add("buildings", wkb.create_multipolygon(o), {})
    poi = osm_remote.poi_kind(tags)  # not `kind`: that is way/relation here
    if poi:
        w.add("poi_areas", wkb.create_multipolygon(o), {"kind": poi, "name": tags.get("name", "")})
    if tags.get("leisure") in ("park", "garden"):
        w.add("parks", wkb.create_multipolygon(o), {"name": tags.get("name", "")})
    if tags.get("natural") == "water":
        w.add("water_areas", wkb.create_multipolygon(o), {"kind": "water"})
    for k in ("landuse", "natural"):
        cls = LANDCOVER.get((k, tags.get(k)))
        if cls:
            w.add("landcover", wkb.create_multipolygon(o), {"cls": cls})
            break
    if kind == "relation" and tags.get("boundary") == "administrative" and tags.get("admin_level") in ("2", "4", "6"):
        if tags["admin_level"] != "2" or tags.get("ISO3166-1") == "PL" or tags.get("name") == "Polska":
            w.add("admin", wkb.create_multipolygon(o), {"level": int(tags["admin_level"]), "name": tags.get("name", ""),
                                                         "teryt": tags.get("teryt:terc", "")})
    if is_relevant_osm(tags):
        w.add("candidates", wkb.create_multipolygon(o), _cand_props(kind, oid, tags))


# ===================================================================== backend
class LocalOSM:
    """Same interface as osm.OverpassOSM, answered from the extracted GeoPackage."""

    def __init__(self, gpkg: Path):
        self.path = Path(gpkg)
        self._area = None
        self._cache: dict[str, gpd.GeoDataFrame] = {}

    def _read(self, layer: str, bounds, where: str | None = None) -> gpd.GeoDataFrame:
        try:
            return pyogrio.read_dataframe(self.path, layer=layer, bbox=tuple(bounds), where=where)
        except Exception as e:  # missing layer in a small extract
            log.debug("layer %s: %s", layer, e)
            return gpd.GeoDataFrame(geometry=[], crs=2180)

    def _layer(self, layer: str) -> gpd.GeoDataFrame:
        """Layer clipped to the prepared area (+3 km), loaded once per area."""
        if layer not in self._cache:
            self._cache[layer] = self._read(layer, self._area.buffer(3000).bounds)
        return self._cache[layer]

    def prepare(self, area_pl):
        self._area, self._cache = area_pl, {}

    # --- areas
    def boundary(self, teryt: str):
        df = pyogrio.read_dataframe(self.path, layer="admin", where=f"teryt = '{teryt}' AND level = 6")
        if df.empty:
            raise RuntimeError(f"No powiat {teryt} in {self.path}")
        poly = shapely.make_valid(df.geometry.iloc[0])
        self.prepare(poly)
        return poly, df["name"].iloc[0]

    def powiat_list(self) -> list[tuple[str, str]]:
        df = pyogrio.read_dataframe(self.path, layer="admin", columns=["teryt", "name", "level"],
                                    read_geometry=False, where="level = 6")
        return sorted({(t, n) for t, n in zip(df.teryt, df["name"])
                       if isinstance(t, str) and len(t) == 4 and t[:2] in config.VOIVODESHIPS})

    def poland_outline(self, tolerance_deg: float = 0.01):
        df = pyogrio.read_dataframe(self.path, layer="admin", where="level = 2")
        if df.empty:
            raise RuntimeError("no country outline in extract")
        return shapely.geometry.mapping(geom_to_ll(df.geometry.iloc[0]).simplify(tolerance_deg))

    # --- candidates
    def candidates(self, area_pl) -> list[dict]:
        if self._area is None or not self._area.equals(area_pl):
            self.prepare(area_pl)
        df = self._layer("candidates")
        inside = area_pl.buffer(200)
        # extracts made before the place filter stored settlement nodes named "Dwór" etc. as candidates without
        # their place tag: recognise them as a node at the same spot as a place with the same name
        places = {(round(r.geometry.x), round(r.geometry.y), r.name) for r in self._layer("places").itertuples()}
        out = []
        for row in df.itertuples():
            pt = row.geometry
            if not inside.contains(pt):
                continue
            tags = json.loads(row.tags)
            if row.osm_type == "node" and (round(pt.x), round(pt.y), tags.get("name", "")) in places:
                tags["place"] = "yes"
            if not is_relevant_osm(tags):
                continue
            lon, lat = geom_to_ll(pt).coords[0]
            out.append(osm_remote.candidate_from_tags(row.osm_type, int(row.osm_id), lon, lat, tags, pt.x, pt.y))
        log.info("OSM (local): %d candidate features", len(out))
        return out

    def locate_records(self, records: list[dict], area_pl) -> list[dict]:
        if not records:
            return []
        places = self._layer("places")
        index: dict[str, list] = {}
        for row in places.itertuples():
            for nm in [row.name] + (row.alt_names.split("|") if row.alt_names else []):
                if nm:
                    index.setdefault(fold(nm), []).append((row.geometry.x, row.geometry.y))
        parks = self._layer("parks")
        pxy = np.column_stack([parks["park_x"].values, parks["park_y"].values]) if len(parks) else np.empty((0, 2))
        main = places[places["place"].isin(osm_remote.MAIN_PLACES)] if "place" in places.columns else places
        rxy = np.column_stack([main.geometry.x.values, main.geometry.y.values]) if len(main) else np.empty((0, 2))
        R = config.NID_PLACE_RADIUS_M
        out, missing = [], set()
        for r in records:
            hits = index.get(fold(r["village"]))
            if not hits:
                missing.add(r["village"])
                continue
            vx, vy = hits[0]
            r = dict(r)
            d = np.hypot(pxy[:, 0] - vx, pxy[:, 1] - vy) if len(pxy) else np.array([])
            near = [tuple(map(float, pxy[i])) for i in np.flatnonzero(d <= R)] if len(d) else []
            rivals = [tuple(map(float, p)) for p in rxy[np.hypot(rxy[:, 0] - vx, rxy[:, 1] - vy) <= 2 * R + 1]] if len(rxy) else []
            best = osm_remote.park_for_village((vx, vy), near, rivals, R)
            r["village_xy"] = (float(vx), float(vy))
            r["rivals_xy"] = [p for p in rivals if ((p[0] - vx) ** 2 + (p[1] - vy) ** 2) ** 0.5 > 1]
            if best:
                r["x"], r["y"] = best
                r["location"] = "osm park near village"
            else:
                r["x"], r["y"] = vx, vy
                r["location"] = "village centre"
            if len(hits) > 1:
                r["location"] += " (village name not unique, check)"
            out.append(r)
        if missing:
            log.warning("NID: no OSM place found for %d villages: %s", len(missing), ", ".join(sorted(missing))[:300])
        return out

    # --- measurements
    def context(self, sites: list[dict]) -> dict[str, dict]:
        bld, parks, cover = self._layer("buildings"), self._layer("parks"), self._layer("landcover")
        bxy = np.column_stack([bld.geometry.x, bld.geometry.y]) if len(bld) else np.empty((0, 2))
        barea = bld["area_m2"].values if len(bld) else np.array([])
        park_tree = shapely.STRtree(parks.geometry.values) if len(parks) else None
        cover_tree = shapely.STRtree(cover.geometry.values) if len(cover) else None
        R = config.CONTEXT_RADIUS_M
        out = {}
        for s in sites:
            pt = Point(s["x"], s["y"])
            d = np.hypot(bxy[:, 0] - s["x"], bxy[:, 1] - s["y"]) if len(bxy) else np.array([])
            near = d <= config.MANOR_SEARCH_RADIUS_M
            areas = barea[near] if len(d) else np.array([])
            big_mask = (barea >= config.MANOR_BUILDING_MIN_M2) if len(d) else np.array([], dtype=bool)
            wide = int(((d <= config.MANOR_LOST_RADIUS_M) & big_mask).sum()) if len(d) else 0
            sel = np.nonzero((d <= config.PARCEL_SEARCH_MAX_M) & big_mask)[0] if len(d) else []
            nearby = [[round(float(bxy[k, 0]), 1), round(float(bxy[k, 1]), 1), int(round(float(barea[k])))] for k in sel]
            park = bool(park_tree is not None and len(park_tree.query(pt, predicate="dwithin", distance=R)))
            cls = "unknown"
            if cover_tree is not None:
                idx = cover_tree.query(pt, predicate="dwithin", distance=R)
                if len(idx):
                    dist = shapely.distance(cover.geometry.values[idx], pt)
                    cls = cover["cls"].values[idx[int(np.argmin(dist))]]
            out[s["uid"]] = {
                "n_buildings": int((d <= R).sum()) if len(d) else 0,
                "max_building_m2": int(round(float(areas.max()))) if len(areas) else 0,
                "big_buildings": [int(a) for a in areas if a >= config.MANOR_BUILDING_MIN_M2],
                "big_buildings_wide": wide,
                "nearby_big": nearby,
                "park_nearby": park, "land_cover": cls,
            }
        return out

    def infrastructure(self, sites: list[dict]) -> dict[str, dict]:
        radius = {"power": 2000, "road": 800, "water": 2000}
        layers = {"power": self._layer("power"), "road": self._layer("roads"), "water": self._layer("water")}
        trees = {k: shapely.STRtree(v.geometry.values) if len(v) else None for k, v in layers.items()}
        bld = self._layer("buildings")
        bxy = np.column_stack([bld.geometry.x, bld.geometry.y]) if len(bld) else np.empty((0, 2))
        out = {}
        for s in sites:
            pt = Point(s["x"], s["y"])
            r = {}
            for k, tree in trees.items():
                d = radius[k]
                if tree is not None:
                    idx = tree.query(pt, predicate="dwithin", distance=radius[k])
                    if len(idx):
                        d = float(shapely.distance(layers[k].geometry.values[idx], pt).min())
                r[f"dist_{k}_m"] = round(min(d, radius[k]))
            if len(bxy):
                d = np.hypot(bxy[:, 0] - s["x"], bxy[:, 1] - s["y"])
                r["dist_building_m"] = round(float(min(d.min(), 1500)))
                r["buildings_500m"] = int((d <= 500).sum())
            else:
                r["dist_building_m"], r["buildings_500m"] = 1500, 0
            out[s["uid"]] = r
        return out

    def surroundings(self, lon: float, lat: float) -> dict:
        """Wind turbines, main roads, railways and everyday services around one point (see config)."""
        x, y = ll_to_pl(lon, lat)
        here = Point(x, y)
        R = config.SURROUNDINGS_RADIUS_M
        if "pois" not in self._layers():
            raise RuntimeError("the OSM extract has no pois layer yet: run `gutshaus osm-extract` again")
        found = []
        pois = self._read("pois", here.buffer(max(R.values())).bounds)
        for row in pois.itertuples():
            found.append((row.kind, row.name or "", here.distance(row.geometry)))
        roads = self._read("roads", here.buffer(R["main_road"]).bounds,
                           where="highway IN (" + ",".join(f"'{h}'" for h in config.MAIN_ROADS) + ")")
        found += [("main_road", "", here.distance(g)) for g in roads.geometry]
        found += [("railway", "", here.distance(g)) for g in self._read("rail", here.buffer(R["railway"]).bounds).geometry]
        return osm_remote.summarise_surroundings(here, found)

    def _layers(self) -> set[str]:
        return {name for name, _ in pyogrio.list_layers(self.path)}

    def towns(self, area_pl, pad_m: float = 40000) -> list[tuple]:
        df = self._read("places", area_pl.buffer(pad_m).bounds, where="place IN ('city','town')")
        return [(row.name, row.geometry.x, row.geometry.y) for row in df.itertuples()]
