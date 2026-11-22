"""Keyword classification of candidate names/descriptions."""
from __future__ import annotations

import re

from . import config
from .geo import fold


def text_flags(text: str) -> dict:
    t = fold(text)
    estate = bool(config.ESTATE_PATTERN.search(t))
    excluded = bool(config.EXCLUDE_PATTERN.search(t)) and not estate
    return {
        "estate_kw": estate,
        "park_kw": bool(config.PARK_PATTERN.search(t)),
        "ruin_kw": bool(config.RUIN_PATTERN.search(t)),
        "excluded": excluded,
        # A register entry that names a park/complex but not the house itself
        # is a hint that the house may be gone.
        "park_only": bool(config.PARK_PATTERN.search(t)) and not _HOUSE.search(t),
    }


# The house itself as a noun ("dwór", "pałac"), not the adjective ("park dworski").
_HOUSE = re.compile(r"\b(dwor|dworek|palac|palacyk|zamek|gutshaus|herrenhaus|schloss)\b")


def is_relevant_osm(tags: dict) -> bool:
    if tags.get("historic") in ("manor", "estate", "palace"):
        return True
    if tags.get("building") in ("manor", "palace"):
        return True
    if tags.get("castle_type") in config.OSM_CASTLE_TYPES:
        return True
    if tags.get("historic") == "ruins" and tags.get("ruins") in ("manor", "palace", "castle"):
        return True
    # from here on only the name matches: drop settlements, roads, plants, shops ... that are merely called "Dwór"
    if any(k in tags for k in config.NON_ESTATE_KEYS) or tags.get("building") in config.NON_ESTATE_BUILDINGS:
        return False
    if tags.get("leisure") not in (None, "park", "garden"):
        return False
    text = " ".join(str(tags.get(k, "")) for k in ("name", "old_name", "name:de", "description", "heritage:name"))
    f = text_flags(text)
    return f["estate_kw"] and not f["excluded"]
