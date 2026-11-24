"""Register entries (rejestr zabytków) that lie on given parcels, from NID's INSPIRE map service.

The NID register API has no coordinates, but its map service draws every immovable monument as an outline
(building, park, whole ensemble). This module renders that layer over an area once (GetMap), finds the drawn
shapes (regions enclosed by outlines) and asks GetFeatureInfo once per shape inside the parcels. Used for the
few KOWR offers: their parcels are known exactly, the register entries in the same village often are not.
"""
from __future__ import annotations

import io
import logging
import re

from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from .. import config
from ..geo import ll_to_pl, pl_to_ll
from ..http import Http

log = logging.getLogger(__name__)

LAYER = "Immovable_Monuments"
M_PER_PX = 1.5              # rendering resolution; outlines of small buildings stay closed
MAX_PX = 1200
MAX_QUERIES = 80            # per area: one GetFeatureInfo per drawn shape
MIN_SHAPE_PX = 6            # specks of anti-aliasing are not shapes


def _bbox_ll(x0, y0, x1, y1) -> str:
    lo0, la0 = pl_to_ll(x0, y0)
    lo1, la1 = pl_to_ll(x1, y1)
    return f"{lo0},{la0},{lo1},{la1}"


def parse_info(html: str) -> list[dict]:
    """GetFeatureInfo HTML -> [{id, name, docs, date, kind}]. The first feature's table has the field names,
    the others only the values, in the same order."""
    heads = [h.strip() for h in re.findall(r'class="featureTh">\s*(.*?)\s*</TD>', html, re.S | re.I)]
    vals = [re.sub(r"\s+", " ", v).strip() for v in re.findall(r'class="featureTd">(.*?)</TD>', html, re.S | re.I)]
    if not heads:
        return []
    out = []
    for i in range(0, len(vals) - len(heads) + 1, len(heads)):
        f = dict(zip(heads, vals[i:i + len(heads)]))
        if f.get("INSPIREID"):
            out.append({"id": f["INSPIREID"], "name": f.get("SITENAME", ""), "docs": f.get("LEGALFOUNDATIONDOCUMENT", ""),
                        "date": f.get("LEGALFOUNDATIONDATE", ""), "kind": f.get("NATIONALMONUMENTSRECORD", "")})
    return out


def _shapes(png: bytes):
    """Regions of drawn fill (not outline, not empty) as lists of (col, row): 4-connected flood fill."""
    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    w, h = im.size
    px = im.load()
    fill = bytearray(w * h)
    for r in range(h):
        for c in range(w):
            R, G, B, A = px[c, r]
            fill[r * w + c] = 1 if A > 128 and max(R, G, B) > 60 else 0     # grey fill; black outlines and empty = 0
    seen = bytearray(w * h)
    for start in range(w * h):
        if not fill[start] or seen[start]:
            continue
        stack, region = [start], []
        seen[start] = 1
        while stack:
            i = stack.pop()
            region.append(i)
            r, c = divmod(i, w)
            for j in ((i - 1) if c else -1, (i + 1) if c < w - 1 else -1, i - w, i + w):
                if 0 <= j < w * h and fill[j] and not seen[j]:
                    seen[j] = 1
                    stack.append(j)
        if len(region) >= MIN_SHAPE_PX:
            yield [(i % w, i // w) for i in region]


def monuments_in(http: Http, rings_ll: list, near_m: float = 0, on_m: float = 5) -> list[dict]:
    """Register entries drawn on the parcels (rings of [lon, lat]) or within near_m of them. Each has "on" (its
    drawn shape reaches within on_m of the parcels), the centre of its drawn shape ("x", "y" in EPSG:2180,
    "lon", "lat") and the drawn area in m² near the parcels ("m2"). Empty when the service is unavailable."""
    polys = [Polygon([ll_to_pl(lon, lat) for lon, lat in r]).buffer(0) for r in rings_ll if len(r) >= 4]
    if not polys:
        return []
    parcels = unary_union(polys)
    on_area, near_area = parcels.buffer(on_m), parcels.buffer(max(near_m, on_m))
    x0, y0, x1, y1 = near_area.buffer(30).bounds
    scale = max(M_PER_PX, (x1 - x0) / MAX_PX, (y1 - y0) / MAX_PX)
    w, h = int((x1 - x0) / scale) + 1, int((y1 - y0) / scale) + 1
    common = {"service": "WMS", "version": "1.1.1", "layers": LAYER, "styles": "", "srs": "EPSG:4326",
              "bbox": _bbox_ll(x0, y0, x0 + w * scale, y0 + h * scale), "width": w, "height": h}
    try:
        png = http.get(config.NID_WMS, params={**common, "request": "GetMap", "format": "image/png", "transparent": "true"})
        shapes = list(_shapes(png))
    except Exception as e:
        log.warning("NID map unavailable (%s)", e)
        return []
    to_xy = lambda c, r: (x0 + (c + .5) * scale, y0 + h * scale - (r + .5) * scale)   # rows count from the top
    touches = lambda region, a: any(a.contains(Point(to_xy(*p))) for p in region[:: max(1, len(region) // 300)])
    found: dict[str, dict] = {}
    queries = 0
    for region in sorted(shapes, key=len):                   # small shapes (buildings) first
        if not touches(region, near_area):
            continue
        if queries >= MAX_QUERIES:
            log.warning("NID map: more than %d shapes, the rest skipped", MAX_QUERIES)
            break
        queries += 1
        cx, cy = sum(c for c, _ in region) / len(region), sum(r for _, r in region) / len(region)
        c, r = min(region, key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2)    # a pixel inside the shape
        try:
            html = http.get(config.NID_WMS, params={**common, "request": "GetFeatureInfo", "query_layers": LAYER,
                                                    "x": c, "y": r, "info_format": "text/html", "feature_count": 20})
        except Exception as e:
            log.debug("NID map info: %s", e)
            continue
        on = touches(region, on_area)
        for f in parse_info(html.decode("utf-8", "replace")):
            # a monument is every shape it is returned for (a park around a building is cut by its outline)
            e = found.setdefault(f["id"], {**f, "on": False, "_px": []})
            e["_px"] += region
            e["on"] = e["on"] or on
    out = []
    for e in found.values():
        px = e.pop("_px")
        mx, my = to_xy(sum(c for c, _ in px) / len(px), sum(r for _, r in px) / len(px))
        lon, lat = pl_to_ll(mx, my)
        out.append({**e, "x": round(mx, 1), "y": round(my, 1), "lon": round(lon, 6), "lat": round(lat, 6),
                    "m2": round(len(px) * scale * scale)})
    return sorted(out, key=lambda m: (not m["on"], m["name"]))
