"""ULDK (GUGiK) parcel lookup by coordinate."""
from __future__ import annotations

import logging
import time

from shapely import wkt

from .. import config
from ..http import Http

log = logging.getLogger(__name__)

FIELDS = "geom_wkt,teryt,voivodeship,county,commune,region,parcel"


def parcel_at(http: Http, x: float, y: float) -> dict | None:
    params = {"request": "GetParcelByXY", "xy": f"{x:.2f},{y:.2f},2180",
              "result": FIELDS, "srid": "2180"}
    try:
        raw = http.get(config.ULDK_URL, params=params, retries=2).decode("utf-8", "ignore")
    except Exception as e:
        log.warning("ULDK failed at %.0f,%.0f: %s", x, y, e)
        return None
    finally:
        time.sleep(config.ULDK_PAUSE_S)
    return parse(raw)


def parse(raw: str) -> dict | None:
    lines = [l for l in raw.strip().splitlines() if l.strip()]
    if len(lines) < 2 or lines[0].strip().startswith("-1"):
        return None
    parts = lines[1].split("|")
    if len(parts) < 7:
        return None
    g = parts[0]
    if g.upper().startswith("SRID="):
        g = g.split(";", 1)[1]
    try:
        geom = wkt.loads(g)
    except Exception:
        geom = None
    return {
        "parcel_id": parts[1],
        "voivodeship": parts[2], "county": parts[3], "commune": parts[4],
        "precinct": parts[5], "parcel_no": parts[6],
        "parcel_ha": round(geom.area / 10000, 2) if geom is not None else None,
        "parcel_geom": geom,
    }
