"""Who owns a parcel, by type: the registration group (grupa rejestrowa) of the land register (EGiB).

GUGiK's national land-use WMS (Krajowa Integracja Użytków Gruntowych) answers GetFeatureInfo with the parcel's
identifier, registration group, land classes and area. The group is the owner's type (§ 14 of the EGiB regulation,
Dz.U. 2024 poz. 219), never a name: 7 is private persons; see config "Who owns it". Not every powiat publishes it.
"""
from __future__ import annotations

import json
import logging
import re

from shapely.geometry import Polygon

from .. import config
from ..geo import ll_to_pl
from ..http import Http

log = logging.getLogger(__name__)

GROUPS_EN = {1: "State Treasury", 2: "State Treasury, perpetual usufruct", 3: "state legal persons", 4: "gmina",
             5: "gmina, perpetual usufruct", 6: "municipal legal persons", 7: "private persons", 8: "cooperatives",
             9: "churches and religious bodies", 10: "village communities", 11: "powiat",
             12: "powiat, perpetual usufruct", 13: "voivodeship", 14: "voivodeship, perpetual usufruct",
             15: "companies", 16: "other (foundations, associations ...)"}


def parse(page: str, parcel_id: str | None = None) -> dict:
    """{"status": ok|none, "group", "classes", "area_ha"} from a GetFeatureInfo answer (text/html). "none" when the
    answer is about another parcel than `parcel_id` (the point hit a neighbour) or has neither group nor classes."""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"(?s)<style.*?</style>", "", page)))
    pid = re.search(r"Identyfikator dzia\w+\s+(\S+)", text)
    if pid and parcel_id and pid.group(1) != parcel_id:
        return {"status": "none"}
    m = re.search(r"Grupa rejestrowa\s+(\d{1,2})\b", text)
    contour = re.search(r"Oznaczenie konturu\s*(.*?)\s*(?:Data publikacji|$)", text)
    classes = [c.strip() for c in (contour.group(1) if contour else "").split(",") if c.strip()]
    area = re.search(r"Pole pow\. w ewidencji grunt\w+ \(ha\)\s*([\d.,]+)", text)
    if not m and not classes:
        return {"status": "none"}
    return {"status": "ok", "group": int(m.group(1)) if m else None, "classes": classes,
            "area_ha": float(area.group(1).replace(",", ".")) if area else None}


def lookup(http: Http, site: dict) -> dict:
    """The registration group of the site's parcel (site["parcel_ll"], site["parcel_id"]). Never raises:
    {"status": "unavailable"} when the service fails, {"status": "none"} without a parcel or an answer."""
    ring = site.get("parcel_ll") or []
    ring = json.loads(ring) if isinstance(ring, str) else ring
    if len(ring) < 4 or not site.get("parcel_id"):
        return {"status": "none"}
    try:
        c = Polygon([ll_to_pl(lon, lat) for lon, lat in ring]).buffer(0).representative_point()
    except Exception:
        return {"status": "none"}
    d = 30
    params = {"SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetFeatureInfo", "LAYERS": "klasouzytki",
              "QUERY_LAYERS": "klasouzytki", "STYLES": "", "SRS": "EPSG:2180",
              "BBOX": f"{c.x - d:.1f},{c.y - d:.1f},{c.x + d:.1f},{c.y + d:.1f}", "WIDTH": 101, "HEIGHT": 101,
              "X": 50, "Y": 50, "INFO_FORMAT": "text/html", "FEATURE_COUNT": 5}
    try:
        page = http.get(config.EGIB_WMS, params=params, retries=2).decode("utf-8", "replace")
    except Exception as e:
        log.debug("EGiB: %s", e)
        return {"status": "unavailable"}
    return parse(page, site["parcel_id"])


def annotate(http: Http, sites: list[dict], every: int = 250) -> int:
    """site["owner_group"] = registration group (int) or None, for every located site with a parcel; cached by Http.
    Returns the number of sites with a group."""
    for s in sites:
        s["owner_group"] = None
    todo = [s for s in sites if s.get("status") != "unlocated" and s.get("parcel_ll")]
    n = 0
    for i, s in enumerate(todo, 1):
        r = lookup(http, s)
        s["owner_group"] = r.get("group") if r["status"] == "ok" else None
        n += s["owner_group"] is not None
        if i % every == 0:
            log.info("Owner groups %d/%d (%d known)", i, len(todo), n)
    log.info("Owner groups: %d of %d located sites with a parcel", n, len(todo))
    return n
