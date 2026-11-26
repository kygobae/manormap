"""Manors for sale besides KOWR: bailiff auctions, gmina / starostwo tenders, owners' own listings, anything found
by hand. A sale keeps its site on the public map whoever owns it (grades.listed), like a KOWR offer.

One JSON list in config.SALES_FILE (kept with the private config, never in this repo). Each entry:

    {"id": "bailiff-2026-001",          unique and stable: the map links to it
     "source": "bailiff" | "tender" | "owner" | "manual",
     "site": "GF-1A2B3C4D",             the site it sells (dossier ID or uid), or
     "lat": 54.1, "lon": 16.2,          where it is: the located site within config.SALE_MATCH_M takes it;
                                        with no site there it is shown on its own
     "title": "Pałac w X – licytacja",  "url": "https://…",  "price_pln": 1200000,  "area_ha": 2.1,
     "date": "2026-12-15",              the auction or tender (shown; a past date says so)
     "until": "2026-12-31",             last day on the map (default: date + config.SALE_SHOW_DAYS; without a
                                        date: until removed)
     "contact": "…",                    owners' listings only: exactly what the owner agreed to publish
     "added": "2026-11-26", "note": "…"}   private: never published

Entries without an id, a source, or a site or position are skipped with a warning.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
from pathlib import Path

from . import config
from .geo import ll_to_pl

log = logging.getLogger(__name__)

SOURCES = ("bailiff", "tender", "owner", "manual")


def _day(v) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def last_day(e: dict) -> dt.date | None:
    """The last day a sale is shown: "until", else its date + SALE_SHOW_DAYS, else None (no end)."""
    if _day(e.get("until")):
        return _day(e["until"])
    d = _day(e.get("date"))
    return d + dt.timedelta(days=config.SALE_SHOW_DAYS) if d else None


def load(path: Path | str | None = None, today: dt.date | None = None) -> list[dict]:
    """The current sales from config.SALES_FILE (or `path`): valid entries whose last day has not passed."""
    p = Path(path or config.SALES_FILE)
    if not p.exists():
        return []
    raw = json.loads(p.read_text(encoding="utf-8"))
    today = today or dt.date.today()
    out, seen = [], set()
    for e in raw.get("sales", raw) if isinstance(raw, dict) else raw:
        if not e.get("id") or e.get("source") not in SOURCES or e["id"] in seen:
            log.warning("Sales: entry skipped (needs a unique id and a source of %s): %s", SOURCES, str(e)[:120])
            continue
        if not e.get("site") and (e.get("lat") is None or e.get("lon") is None):
            log.warning("Sales: %s has neither a site nor a position", e["id"])
            continue
        end = last_day(e)
        if end and end < today:
            continue
        seen.add(e["id"])
        out.append(e)
    return out


def match(sites: list[dict], sales: list[dict]) -> int:
    """site["sale"] = {uid, src, title, url} for the site each sale names (dossier ID, uid or member uid) or the
    located site nearest its position within config.SALE_MATCH_M. A KOWR offer already on a site stays its offer.
    Returns the number of sales matched to a site; each sale gets e["_site"] (uid) when it has one."""
    from .grades import dossier_id
    for s in sites:
        s.pop("sale", None)
    by_key = {}
    for s in sites:
        for k in (s["uid"], dossier_id(s["uid"]), *s.get("members", [])):
            by_key.setdefault(k, s)
    located = [(s, *ll_to_pl(s["lon"], s["lat"])) for s in sites
               if s.get("status") != "unlocated" and s.get("lat") is not None]
    n = 0
    for e in sales:
        e.pop("_site", None)
        key = str(e.get("site") or "").strip()
        s = by_key.get(key.upper() if key.upper().startswith("GF-") else key) if key else None
        if s is None and e.get("lat") is not None:
            x, y = ll_to_pl(e["lon"], e["lat"])
            best = min(located, key=lambda r: math.hypot(r[1] - x, r[2] - y), default=None)
            if best and math.hypot(best[1] - x, best[2] - y) <= config.SALE_MATCH_M:
                s = best[0]
        if s is None:
            if key:
                log.warning("Sales: %s names site %s, which is not in the data", e["id"], key)
            continue
        e["_site"] = s["uid"]
        if s.get("kowr_offer") or s.get("sale"):
            continue
        s["sale"] = {"uid": e["id"], "src": e["source"], "title": e.get("title") or "", "url": e.get("url") or ""}
        n += 1
    return n


def offers(sales: list[dict], sites: list[dict], today: dt.date | None = None) -> list[dict]:
    """The sales as offers for the public map, in the shape of publish.offers (KOWR). A sale on a site is shown at the
    site; one without a site at its own position. The private note never leaves this machine."""
    today = today or dt.date.today()
    at = {s["uid"]: s for s in sites}
    out = []
    for e in sales:
        s = at.get(e.get("_site"))
        lat = e.get("lat") if e.get("lat") is not None else (s or {}).get("lat")
        lon = e.get("lon") if e.get("lon") is not None else (s or {}).get("lon")
        if lat is None or lon is None:
            continue
        d = _day(e.get("date"))
        out.append({"id": e["id"], "t": e.get("title") or "", "url": e.get("url") or "", "lat": lat, "lon": lon,
                    "ha": e.get("area_ha"), "tender": e.get("date") or "", "past": bool(d and d < today),
                    "price": e.get("price_pln"), "src": e["source"],
                    "contact": e.get("contact") if e["source"] == "owner" else "",
                    "pl": [], "pn": None, "pf": None, "pha": None, "gone": None})
    return out
