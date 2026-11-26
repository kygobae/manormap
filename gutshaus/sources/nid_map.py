"""Register entries (rejestr zabytków) that lie on given parcels, from NID's INSPIRE map service.

The NID register API has no coordinates, but its map service draws every immovable monument as an outline
(building, park, whole ensemble). This module renders that layer over an area once (GetMap), finds the drawn
shapes (regions enclosed by outlines) and asks GetFeatureInfo once per shape inside the parcels. Used for the
few KOWR offers: their parcels are known exactly, the register entries in the same village often are not; and
(pin_village_entries) to find where NID draws every register entry known only by its village.
"""
from __future__ import annotations

import io
import logging
import re
import time

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


# ----------------------------------------------------------------- entries known only by their village
def _render(http: Http, x0: float, y0: float, x1: float, y1: float):
    """GetMap over the box: (request params shared with GetFeatureInfo, shapes, pixel -> EPSG:2180, m per px)."""
    scale = max(M_PER_PX, (x1 - x0) / MAX_PX, (y1 - y0) / MAX_PX)
    w, h = int((x1 - x0) / scale) + 1, int((y1 - y0) / scale) + 1
    common = {"service": "WMS", "version": "1.1.1", "layers": LAYER, "styles": "", "srs": "EPSG:4326",
              "bbox": _bbox_ll(x0, y0, x0 + w * scale, y0 + h * scale), "width": w, "height": h}
    png = _get(http, {**common, "request": "GetMap", "format": "image/png", "transparent": "true"})
    to_xy = lambda c, r: (x0 + (c + .5) * scale, y0 + h * scale - (r + .5) * scale)   # rows count from the top
    return common, list(_shapes(png)), to_xy, scale


def _get(http: Http, params: dict) -> bytes:
    """A request to NID's map service, with a pause after each one that was not cached."""
    t0 = time.monotonic()
    try:
        return http.get(config.NID_WMS, params=params, retries=2)
    finally:
        if time.monotonic() - t0 > 0.05:
            time.sleep(config.NID_PAUSE_S)


def drawn_positions(http: Http, x: float, y: float, wanted: set[str], half_m: float) -> dict[str, tuple]:
    """Where NID's map draws the register entries `wanted` (INSPIRE ids) within half_m of (x, y), EPSG:2180:
    {id: (x, y, m2)} at the centre of each entry's drawn shapes. Larger shapes are asked first (ensembles, parks,
    manors before small houses); it stops once every wanted entry is found or after MAX_QUERIES shapes.
    Raises when the service fails."""
    common, shapes, to_xy, scale = _render(http, x - half_m, y - half_m, x + half_m, y + half_m)
    px: dict[str, list] = {}
    for queries, region in enumerate(sorted(shapes, key=len, reverse=True)):
        if queries >= MAX_QUERIES or wanted <= set(px):
            break
        cx, cy = sum(c for c, _ in region) / len(region), sum(r for _, r in region) / len(region)
        c, r = min(region, key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2)
        html = _get(http, {**common, "request": "GetFeatureInfo", "query_layers": LAYER, "x": c, "y": r,
                           "info_format": "text/html", "feature_count": 20})
        for f in parse_info(html.decode("utf-8", "replace")):
            if f["id"] in wanted:
                px.setdefault(f["id"], []).extend(region)
    out = {}
    for i, region in px.items():
        mx, my = to_xy(sum(c for c, _ in region) / len(region), sum(r for _, r in region) / len(region))
        out[i] = (round(mx, 1), round(my, 1), round(len(region) * scale * scale))
    return out


MAX_FAILURES = 10


def pin_village_entries(http: Http, cands: list[dict]) -> int:
    """Before merging: a register entry the API knows only by its village (location "village centre" or a park
    guessed near it) moves to where NID's map draws it, one map per village (config.NID_PLACE_RADIUS_M around the
    village's place). Entries NID does not draw there stay where they were. Returns the number of entries moved."""
    villages: dict[tuple, list[dict]] = {}
    for c in cands:
        if c.get("source") == "nid" and c.get("location", "exact") != "exact" and c.get("village_xy"):
            villages.setdefault(tuple(round(v) for v in c["village_xy"]), []).append(c)
    n = failed = 0
    for i, ((vx, vy), group) in enumerate(villages.items(), 1):
        wanted = {c["uid"].removeprefix("nid:") for c in group}
        try:
            pos = drawn_positions(http, vx, vy, wanted, config.NID_PLACE_RADIUS_M)
            failed = 0
        except Exception as e:
            failed += 1
            log.debug("NID map: %s", e)
            if failed >= MAX_FAILURES:
                log.warning("NID map unreachable (%d failures in a row): %d villages left as they are",
                            failed, len(villages) - i)
                break
            continue
        for c in group:
            p = pos.get(c["uid"].removeprefix("nid:"))
            if p:
                c.update({"x": p[0], "y": p[1], "location": "exact", "approx": False, "located_by": "nid map"})
                c.pop("village_xy", None)
                c.pop("rivals_xy", None)
                n += 1
        if i % 100 == 0:
            log.info("NID map: %d/%d villages, %d entries placed", i, len(villages), n)
    if villages:
        log.info("NID map: %d of %d village-located register entries placed where NID draws them", n,
                 sum(len(g) for g in villages.values()))
    return n

