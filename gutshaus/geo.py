"""Coordinate helpers. Internally everything is metric PL-1992 (EPSG:2180)."""
from __future__ import annotations

import unicodedata

from pyproj import Transformer
from shapely.geometry import Point, Polygon
import numpy as np
import shapely

_to2180 = Transformer.from_crs(4326, 2180, always_xy=True)
_to4326 = Transformer.from_crs(2180, 4326, always_xy=True)


def ll_to_pl(lon: float, lat: float) -> tuple[float, float]:
    return _to2180.transform(lon, lat)


def pl_to_ll(x: float, y: float) -> tuple[float, float]:
    return _to4326.transform(x, y)


def _apply(tr, g):
    return shapely.transform(g, lambda c: np.column_stack(tr.transform(c[:, 0], c[:, 1])))


def geom_to_pl(g):
    return _apply(_to2180, g)


def geom_to_ll(g):
    return _apply(_to4326, g)


def way_polygon_pl(coords_ll: list[dict]) -> Polygon | None:
    """Overpass way geometry [{'lat','lon'},...] -> polygon in 2180 (closed ways only)."""
    if len(coords_ll) < 4:
        return None
    pts = [ll_to_pl(c["lon"], c["lat"]) for c in coords_ll]
    if pts[0] != pts[-1]:
        return None
    poly = Polygon(pts)
    if not poly.is_valid:
        poly = poly.buffer(0)
    return poly if not poly.is_empty else None


def fold(s: str) -> str:
    """Lowercase and strip diacritics (ł -> l) for keyword matching."""
    s = (s or "").replace("ł", "l").replace("Ł", "L")
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def point_pl(c: dict) -> Point:
    return Point(c["x"], c["y"])
