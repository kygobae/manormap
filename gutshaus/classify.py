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
        "excluded": excluded,
        # A register entry that names a park/complex but not the house itself
        # is a hint that the house may be gone.
        "park_only": bool(config.PARK_PATTERN.search(t)) and not _HOUSE.search(t),
    }


# The house itself as a noun ("dwór", "pałac"), not the adjective ("park dworski").
_HOUSE = re.compile(r"\b(dwor|dworek|palac|palacyk|zamek|gutshaus|herrenhaus|schloss)\b")
# The same in any case ("ruiny dworu", "relikty pałacu"), still without the adjectives
_HOUSE_ANY = re.compile(r"\b(dwor|dworu|dworze|dworem|dworek|dworku|dworki|palac|palacu|palacem|palacyk\w*|"
                        r"zamek|zamku|gutshaus|herrenhaus|schloss)\b")
_GONE = re.compile(r"nieistniej|nie istnieje|rozebran")  # "the former palace that no longer exists"


def fragments(text: str) -> list[tuple[dict | None, str]]:
    """A site's text is its members' texts joined by " || ". Returns (OSM tags or None, words) per member;
    for OSM members the words are only the name/description values, never the tag keys ("ruins=no")."""
    out = []
    for frag in (text or "").split(" || "):
        parts = [p.partition("=") for p in frag.split(" | ")]
        if parts and all(sep for _, sep, _ in parts):
            tags = {k.strip(): v.strip() for k, _, v in parts}
            out.append((tags, " ".join(tags.get(k, "") for k in ("name", "old_name", "description"))))
        else:
            out.append((None, frag))
    return out


def _about_the_house(words: str) -> bool:
    """Not a chapel, barn, church ... of the estate, and not a description of something already gone."""
    t = fold(words)
    return not (config.EXCLUDE_PATTERN.search(t) and not _HOUSE_ANY.search(t)) and not _GONE.search(t)


def ruin_evidence(text: str) -> bool:
    """A member says the manor itself is a ruin: OSM ruin tags (not ruins=no) or ruin words next to the house
    ("ruiny dworu"). Remains of the park or the farm complex ("pozostałości parku dworskiego") do not count."""
    for tags, words in fragments(text):
        if not _about_the_house(words):
            continue
        t = fold(words)
        if tags is not None:
            if tags.get("ruins") == "no":
                continue
            if (tags.get("historic") == "ruins" or tags.get("building") == "ruins"
                    or tags.get("ruins") in ("yes", "manor", "palace", "castle")):
                return True
            if config.RUIN_PATTERN.search(t) and (_HOUSE_ANY.search(t) or tags.get("historic") in
                                                  ("manor", "palace", "castle")):
                return True
        elif config.RUIN_PATTERN.search(t) and _HOUSE_ANY.search(t):
            return True
    return False


def house_mapped_as_building(text: str) -> bool:
    """An OSM member is the house drawn as a building ("Dwór", building=yes): it stands, however small."""
    for tags, words in fragments(text):
        if tags is None or tags.get("building") in (None, "", "no", "ruins") or tags.get("ruins") not in (None, "no"):
            continue
        if not _about_the_house(words):
            continue
        if (tags.get("historic") in ("manor", "palace") or tags.get("building") in ("manor", "palace")
                or tags.get("castle_type") in config.OSM_CASTLE_TYPES or _HOUSE_ANY.search(fold(words))):
            return True
    return False


def register_names_house(text: str) -> bool:
    """A register (non-OSM) member is the house itself ("dwór", "pałac"), not just its park."""
    return any(tags is None and _HOUSE.search(fold(words.split(" | ")[0]))
               for tags, words in fragments(text))


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
