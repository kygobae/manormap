"""Turn site measurements into 0..1 criterion values and a weighted score.

Each criterion is defined in config.CRITERIA. A value of None means "unknown";
it counts as 0.5 so unknown sites are neither rewarded nor punished.
"""
from __future__ import annotations

from . import config

# A ruin is the hardest case (review.html score; the public grade gives it an amber house light): usually a listed monument whose walls the conservator decides about,
# so a new house has to work around or into them, and a renovation rebuilds far more than it repairs.
GONE = {"lost": 1.0, "unlocated": 0.6, "unknown": 0.5, "ruin": 0.3, "standing": 0.15}
REMAINS = {"standing": 1.0, "ruin": 0.6, "lost": 0.0}  # unlocated/unknown -> None (unknown)
LANDUSE = {"agri": 1.0, "built": 0.9, "unknown": 0.5, "forest": 0.15}


def ramp(d, full_at, zero_at):
    if d is None:
        return None
    if d <= full_at:
        return 1.0
    if d >= zero_at:
        return 0.0
    return round(1 - (d - full_at) / (zero_at - full_at), 3)


def parcel_value(ha):
    if ha is None:
        return None
    if ha < 0.1:
        return 0.0
    if ha < 0.3:
        return round((ha - 0.1) / 0.2, 3)
    if ha <= 15:
        return 1.0
    if ha <= 50:
        return round(1 - 0.7 * (ha - 15) / 35, 3)
    return 0.3


def compute(site: dict) -> dict:
    """c_<key> values for every criterion. Infrastructure measured at a village
    centre (unlocated sites) says nothing about the manor site -> unknown."""
    located = site.get("status") != "unlocated"
    ramps = {c["key"]: c["ramp"] for c in config.CRITERIA if "ramp" in c}
    dist = lambda k, field: ramp(site.get(field), *ramps[k]) if located else None
    c = {
        "gone": GONE.get(site.get("status"), 0.5),
        "remains": REMAINS.get(site.get("status")),
        "park": 1.0 if site.get("park") else 0.0,
        "power": dist("power", "dist_power_m"),
        "road": dist("road", "dist_road_m"),
        "neighbours": dist("neighbours", "dist_building_m"),
        "landuse": LANDUSE.get(site.get("land_cover"), 0.5) if located else None,
        "protection": site.get("c_protection") if located else None,
        "parcel": parcel_value(site.get("parcel_ha")) if located else None,
        "town": dist("town", "dist_town_m"),
        "water": dist("water", "dist_water_m"),
        "forsale": 1.0 if site.get("kowr_offer") or site.get("sale") else 0.0,
        "register": 1.0 if site.get("registered") else 0.0,
    }
    return {f"c_{k}": v for k, v in c.items()}


def score(values: dict, weights: dict | None = None) -> int:
    weights = weights or {c["key"]: c["weight"] for c in config.CRITERIA}
    total = sum(w for w in weights.values() if w > 0)
    if not total:
        return 0
    s = sum(w * (0.5 if values.get(f"c_{k}") is None else values[f"c_{k}"])
            for k, w in weights.items() if w > 0)
    return round(100 * s / total)


def explain(values: dict) -> str:
    parts = []
    for c in config.CRITERIA:
        v = values.get(f"c_{c['key']}")
        if c["weight"] > 0:
            parts.append(f"{c['key']} {'?' if v is None else f'{v:.2f}'}")
    return "; ".join(parts)
