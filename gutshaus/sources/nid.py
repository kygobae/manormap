"""NID register of immovable monuments (rejestr zabytków nieruchomych).

Two ways in:
  1. NID open-data API (default): names and villages, no coordinates
  2. --nid-file: any GIS file with geometry (SHP/GPKG/GeoJSON/GML/GeoParquet) or a
     CSV with lon/lat, e.g. downloaded from the NID map portal (mapy.zabytek.gov.pl)
"""
from __future__ import annotations

import io
import json
import logging
import re
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely import affinity

from .. import config
from ..classify import text_flags
from ..http import Http

log = logging.getLogger(__name__)

NAME_HINTS = ("sitename", "name", "nazwa", "text", "spelling", "obiekt", "opis")
ID_HINTS = ("localid", "inspireid", "id", "nr_rej", "rejestr", "gml_id")


# --------------------------------------------------------------------- API
def fetch_api(http: Http, powiat: str) -> list[dict]:
    """All register entries (księga A) for a powiat name, e.g. 'koszaliński'."""
    rows, skip = [], 0
    flt = json.dumps(["powiat", "=", powiat], ensure_ascii=False)
    while True:
        raw = http.get(config.NID_API_URL, params={"format": "json", "filter": flt,
                                                   "take": config.NID_PAGE_SIZE, "skip": skip})
        data = json.loads(raw).get("data", [])
        rows += data
        if len(data) < config.NID_PAGE_SIZE:
            break
        skip += config.NID_PAGE_SIZE
    log.info("NID API: %d register entries in powiat %s", len(rows), powiat)
    return rows


def api_estate_records(rows: list[dict]) -> list[dict]:
    """Keep estate-related entries, normalised; coordinates are added later."""
    out = []
    for r in rows:
        text = " | ".join(str(r.get(k) or "") for k in ("nazwa", "funkcja", "chronologia"))
        f = text_flags(text)
        if not f["estate_kw"] or f["excluded"]:
            continue
        out.append({
            "uid": f"nid:{r.get('inspireId')}",
            "source": "nid",
            "name": f"{r.get('nazwa')}, {r.get('miejscowosc')}",
            "name_de": "",
            "text": f"{text} | {r.get('miejscowosc')} | gm. {r.get('gmina')}",
            "village": r.get("miejscowosc") or "",
            "gmina": r.get("gmina") or "",
            "osm_ref": "",
            "nid_ref": f"{r.get('inspireId')} ({r.get('wykazDokumentow') or ''})",
            "registered": True,
            "approx": True,
        })
    log.info("NID API: %d estate-related entries", len(out))
    return out


# ------------------------------------------------------------------ reading
def read_any_bytes(raw: bytes) -> gpd.GeoDataFrame | None:
    head = raw[:2000].decode("utf-8", "ignore")
    if "ExceptionReport" in head or "ServiceException" in head:
        raise RuntimeError(re.sub(r"\s+", " ", head)[:300])
    return gpd.read_file(io.BytesIO(raw))


def read_file(path: Path) -> gpd.GeoDataFrame:
    path = Path(path)
    if path.suffix.lower() in (".csv", ".txt"):
        df = pd.read_csv(path, sep=None, engine="python")
        cols = {c.lower(): c for c in df.columns}
        lon = next((cols[c] for c in ("lon", "lng", "longitude", "x", "dlugosc") if c in cols), None)
        lat = next((cols[c] for c in ("lat", "latitude", "y", "szerokosc") if c in cols), None)
        if not lon or not lat:
            raise ValueError("CSV needs lon/lat columns")
        return gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df[lon], df[lat]), crs=4326)
    if path.suffix.lower() == ".parquet":
        return gpd.read_parquet(path)  # needs: pip install pyarrow
    return gpd.read_file(path)


def fix_axes(gdf: gpd.GeoDataFrame, area_pl) -> gpd.GeoDataFrame:
    """Some WFS return y,x. Keep whichever orientation lands inside the area."""
    if gdf.crs is None:
        gdf = gdf.set_crs(2180, allow_override=True)
    gdf = gdf.to_crs(2180)
    ok = gdf.geometry.representative_point().within(area_pl.envelope).sum()
    swapped = gdf.geometry.apply(lambda g: affinity.affine_transform(g, [0, 1, 1, 0, 0, 0]))
    ok_sw = swapped.representative_point().within(area_pl.envelope).sum()
    if ok_sw > ok:
        log.info("NID: swapping axis order (%d vs %d inside area)", ok_sw, ok)
        gdf = gdf.set_geometry(swapped)
    return gdf


# --------------------------------------------------------------- normalise
def to_candidates(gdf: gpd.GeoDataFrame, area_pl) -> list[dict]:
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    gdf = fix_axes(gdf, area_pl)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    gdf = gdf[gdf.geometry.representative_point().within(area_pl)]
    str_cols = [c for c in gdf.columns if c != gdf.geometry.name
                and (gdf[c].dtype == object or pd.api.types.is_string_dtype(gdf[c]))]
    name_cols = [c for c in str_cols if any(h in c.lower() for h in NAME_HINTS)] or str_cols
    id_cols = [c for h in ID_HINTS for c in gdf.columns if h in c.lower()]
    out = []
    for idx, row in gdf.iterrows():
        text = " | ".join(str(row[c]) for c in str_cols if pd.notna(row[c]) and str(row[c]).strip())
        names = [str(row[c]) for c in name_cols if pd.notna(row[c]) and str(row[c]).strip()]
        name = max(names, key=len) if names else text[:80]
        flags = text_flags(text)
        if not flags["estate_kw"] or flags["excluded"]:
            continue
        ref = next((str(row[c]) for c in id_cols if pd.notna(row[c])), str(idx))
        pt = row.geometry.representative_point()
        geom = row.geometry if row.geometry.geom_type in ("Polygon", "MultiPolygon") else None
        out.append({
            "uid": f"nid:{ref}",
            "source": "nid",
            "name": name[:200],
            "name_de": "",
            "text": text[:1000],
            "x": pt.x, "y": pt.y,
            "osm_ref": "",
            "nid_ref": ref,
            "registered": True,
            "location": "exact",
            "area_geom": geom,
        })
    log.info("NID: %d estate-related register entries in area", len(out))
    return out
