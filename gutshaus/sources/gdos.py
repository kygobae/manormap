"""Nature protection areas from the GDOŚ WFS (national parks, reserves, Natura 2000 ...)."""
from __future__ import annotations

import io
import logging

import geopandas as gpd
from shapely import affinity

from .. import config
from ..geo import geom_to_ll
from ..http import Http

log = logging.getLogger(__name__)


def fetch(http: Http, area_pl) -> list[dict]:
    """Protected areas intersecting the search area, in EPSG:2180."""
    w, s, e, n = geom_to_ll(area_pl.envelope).bounds
    out = []
    for layer, (label, suit) in config.GDOS_LAYERS.items():
        params = {"SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature", "TYPENAMES": layer,
                  "OUTPUTFORMAT": "application/json", "SRSNAME": "EPSG:4326", "COUNT": 2000,
                  "BBOX": f"{s:.5f},{w:.5f},{n:.5f},{e:.5f},urn:ogc:def:crs:EPSG::4326"}
        try:
            raw = http.get(config.GDOS_WFS_URL, params=params, retries=2)
            gdf = gpd.read_file(io.BytesIO(raw))
        except Exception as ex:
            log.warning("GDOŚ %s failed: %s", layer, ex)
            continue
        if gdf.empty:
            continue
        gdf = _fix_axes(gdf.set_crs(4326, allow_override=True), (w, s, e, n)).to_crs(2180)
        name_col = next((c for c in ("nazwa", "name") if c in gdf.columns), None)
        for _, row in gdf.iterrows():
            g = row.geometry
            if g is None or g.is_empty or not g.intersects(area_pl):
                continue
            out.append({"kind": label, "suitability": suit,
                        "name": str(row[name_col]) if name_col else label, "geom": g})
    log.info("GDOŚ: %d protected areas touch the search area", len(out))
    return out


def _fix_axes(gdf, bounds):
    """GeoServer may answer lat/lon; keep the orientation that overlaps the bbox."""
    w, s, e, n = bounds
    def hits(series):
        c = series.representative_point()
        return int(((c.x > w - 2) & (c.x < e + 2) & (c.y > s - 2) & (c.y < n + 2)).sum())
    swapped = gdf.geometry.apply(lambda g: affinity.affine_transform(g, [0, 1, 1, 0, 0, 0]))
    if hits(swapped) > hits(gdf.geometry):
        gdf = gdf.set_geometry(swapped)
    return gdf


def assess(point, areas: list[dict]) -> dict:
    """Most restrictive protection covering a point."""
    hit = [a for a in areas if a["geom"].covers(point)]
    if not hit:
        return {"protection": "none", "protection_names": "", "c_protection": 1.0}
    worst = min(hit, key=lambda a: a["suitability"])
    return {"protection": worst["kind"],
            "protection_names": "; ".join(sorted({f"{a['kind']}: {a['name']}" for a in hit}))[:300],
            "c_protection": worst["suitability"]}
