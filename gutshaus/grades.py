"""Public grades and site lookup, shared by the public site and the (separate, private)
dossier generator: dossier ID, the path (renovation or rebuilding), five traffic lights, one grade,
and finding a site by ID. Rules and thresholds: config "Grades: five traffic lights"."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import config, criteria
from .classify import house_use

STATUS_DE = {"lost": "Herrenhaus nicht mehr vorhanden", "ruin": "Ruine / Reste vorhanden",
             "standing": "Gebäude steht noch", "unlocated": "Lage nur auf Dorfebene bekannt", "unknown": "unklar"}

GREEN, AMBER, RED = "green", "amber", "red"
GRADE_ORDER = "ABCD?"
# what is left suggests the project (the dossier's texts); the public grade does not depend on it
PATHS = {"standing": "renovation", "lost": "new", "ruin": "either", "unknown": "either", "unlocated": "either"}


# ---------------------------------------------------------------- identifiers
def dossier_id(uid: str) -> str:
    return "GF-" + hashlib.sha1(uid.encode()).hexdigest()[:8].upper()


# ---------------------------------------------------------------- grade
def path(site: dict) -> str:
    """'renovation', 'new' (rebuilding) or 'either'."""
    return PATHS.get(site.get("status"), "either")


def lights(site: dict) -> dict[str, dict]:
    """{key: {"level": green|amber|red|None, "why": [reason codes]}} for config.LIGHT_KEYS.
    Reason codes are language-neutral; the site and the dossier turn them into text."""
    st = site.get("status")
    located = st != "unlocated"
    c = criteria.compute(site)
    out = {}

    # the house, from a buyer's view: a standing house is best; a ruin, or a lost manor with another building on the
    # parcel (somewhere to live, site["buildings"] from `gutshaus build`) is a weakness; nothing to live in is red
    b = site.get("buildings") if located else None
    house_why = [st or "unknown"]
    level = {"standing": GREEN, "lost": RED, "ruin": AMBER, "unknown": AMBER}.get(st)
    if b is not None and st != "standing":
        house_why.append("bldg_on" if b.get("n") else "bldg_none")
    if st == "lost" and b and b.get("n"):
        level = AMBER
    use = (site["house_use"] if "house_use" in site else house_use(site.get("text") or "")) if st == "standing" else ""
    if use:                                        # a hotel, school, offices ...: rarely for sale, little to renovate
        house_why.append("use_" + use)
        level = AMBER
    out["house"] = {"level": level, "why": house_why}

    # a KOWR offer sells all its parcels together: their total area counts
    ha = (site.get("offer_ha") or site.get("parcel_ha")) if located else None
    lo, hi = config.PARCEL_OK_HA
    why = ["park" if site.get("park") else "nopark"]
    if site.get("park_parcel"):
        why.append("park_apart")                   # the house stands on another parcel than the park: owners may differ
    if ha is not None and ha < lo:
        why.append("parcel_small")
    elif ha is not None and ha > hi:
        why.append("parcel_large")
    out["plot"] = {"level": GREEN if why == ["park"] else AMBER, "why": why}

    parts = {k: c[f"c_{k}"] for k in ("power", "road", "neighbours") if c[f"c_{k}"] is not None}
    mean = sum(parts.values()) / len(parts) if parts else None
    out["access"] = {"level": None if mean is None else GREEN if mean >= config.ACCESS_GREEN
                     else AMBER if mean >= config.ACCESS_AMBER else RED,
                     "why": [f"{k}_{'near' if v >= 0.5 else 'far'}" for k, v in parts.items()]}

    if not located:
        out["rules"] = {"level": None, "why": []}
    else:
        prot = site.get("protection") or ("nature reserve" if site.get("c_protection") == 0 else "none")
        why = []
        if prot in config.KNOCKOUT_PROTECTION:
            why.append("reserve")
        elif prot != "none":
            why.append("protected")
        fs = site.get("forest_status")
        if fs in ("found", "none"):                  # the forest database knows (see sources/bdl.py)
            if fs == "found" and (site.get("forest_at_site") or (site.get("forest_pct") or 0) >= config.FOREST_SHARE_PCT):
                why.append("forest_db")
        elif site.get("land_cover") == "forest":     # only OSM's land cover: park trees often look like forest
            why.append("forest")
        level = RED if "reserve" in why else AMBER if why else GREEN
        if site.get("registered"):
            why.append("register")                 # the conservator decides, both ways; subsidies possible
        out["rules"] = {"level": level, "why": why or ["free"]}

    d = site.get("dist_town_m") if located else None
    why = [] if d is None else ["town_near" if d <= config.TOWN_GREEN_M else "town_mid" if d <= config.TOWN_AMBER_M
                                else "town_far"]
    if located and (c["c_water"] or 0) >= 0.5:
        why.append("water")
    out["location"] = {"level": None if d is None else {"town_near": GREEN, "town_mid": AMBER}.get(why[0], RED),
                       "why": why}
    return out


def counts(site: dict, L: dict) -> dict:
    """The lights that count for the grade. A standing house is already connected and may be renovated in a
    protected landscape: for it, utilities & access and a protected area (not forest, not a reserve) do not count."""
    if site.get("status") != "standing":
        return L
    out = {k: x for k, x in L.items() if k != "access"}
    r = L["rules"]
    if r["level"] == AMBER and not {"forest", "forest_db"} & set(r["why"]):
        out["rules"] = {**r, "level": GREEN}
    return out


def weaknesses(L: dict) -> int:
    """Amber lights count one, red two (the house's red and a blocker decide the grade on their own)."""
    return sum({AMBER: 1, RED: 2}.get(x["level"], 0) for x in L.values())


def grade_of(site: dict, lights_: dict | None = None) -> str:
    """How good a starting point the place is for a home of your own at a manor (config "Grades"):
    D when no house is left or a rule stands in the way (national park, nature reserve) · A everything green ·
    B one weakness · C two or three, or a ruin · D four or more · '?' when only the village is known."""
    if site.get("status") == "unlocated":
        return "?"
    L = counts(site, lights_ or lights(site))
    if L["rules"]["level"] == RED or L["house"]["level"] == RED:
        return "D"
    n = weaknesses(L)
    if n == 0 and all(x["level"] == GREEN for x in L.values()):
        return "A"
    if n <= 1 and site.get("status") != "ruin":
        return "B"
    return "C" if n <= 3 else "D"


def grade_rank(g: str) -> int:
    return GRADE_ORDER.index(g) if g in GRADE_ORDER else len(GRADE_ORDER)


# ---------------------------------------------------------------- lookup
def all_sites(root: Path) -> list[dict]:
    """The national list from `gutshaus build` (deduplicated, KOWR-matched), else every powiat."""
    nat = Path(root) / "poland" / "sites.json"
    files = [nat] if nat.exists() else sorted(Path(root).glob("powiats/*/sites.json"))
    out, seen = [], set()
    for f in files:
        for s in json.loads(f.read_text(encoding="utf-8"))["sites"]:
            if s["uid"] not in seen:
                seen.update([s["uid"], *s.get("members", [])])
                out.append(s)
    return out


def find_site(root: Path, key: str) -> dict | None:
    """Site by uid, member uid or dossier ID."""
    key = key.strip().upper() if key.strip().upper().startswith("GF-") else key.strip()
    for s in all_sites(root):
        if key in (s["uid"], dossier_id(s["uid"])) or key in s.get("members", []):
            return s
    return None
