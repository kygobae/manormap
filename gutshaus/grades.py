"""Public grades and site lookup, shared by the public site and the (separate, private)
dossier generator: dossier ID, the path (renovation or rebuilding), five traffic lights, one grade,
and finding a site by ID. Rules and thresholds: config "Grades: five traffic lights"."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import config, criteria
from .classify import house_use, site_use

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
    """{key: {"grade": A|B|C|D|None, "level": green|amber|red|None, "why": [reason codes]}} for config.LIGHT_KEYS:
    one school grade per part (config "Grades"); level is the grade as a traffic light (A green, B/C amber, D red).
    Reason codes are language-neutral; the site and the dossier turn them into text."""
    st = site.get("status")
    located = st != "unlocated"
    c = criteria.compute(site)
    out = {}

    # house, from a buyer's view: standing best; in use (hotel, school ...) rarely for sale; a ruin, an unclear state
    # or a lost manor with another building to live in (site["buildings"], `gutshaus build`) fair; nothing left poor
    b = site.get("buildings") if located else None
    why = [st or "unknown"]
    g = {"standing": "A", "ruin": "C", "unknown": "C", "lost": "D"}.get(st)
    if b is not None and st != "standing":
        why.append("bldg_on" if b.get("n") else "bldg_none")
    if st == "lost" and b and b.get("n"):
        g = "C"
    use = (site["house_use"] if "house_use" in site else house_use(site.get("text") or "")) if st == "standing" else ""
    if use:
        why.append("use_" + use)
        g = "B"
    out["house"] = _part(g, why)

    # park & plot: a historic park on a parcel of PARCEL_OK_HA is best; a big garden without a park is still good
    ha = (site.get("offer_ha") or site.get("parcel_ha")) if located else None   # a KOWR offer: all its parcels
    lo, hi = config.PARCEL_OK_HA
    why = ["park" if site.get("park") else "nopark"]
    if site.get("park_parcel"):
        why.append("park_apart")                   # the house stands on another parcel than the park: owners may differ
    if ha is not None and ha < lo:
        why.append("parcel_small")
    elif ha is not None and ha > hi:
        why.append("parcel_large")
    if not located:
        g = None
    elif ha is not None and ha < lo:
        g = "C" if site.get("park") else "D"
    elif site.get("park"):
        g = "A" if len(why) == 1 else "B"
    else:
        g = "B" if ha is not None and ha >= config.GARDEN_GOOD_HA else "C"
    out["plot"] = _part(g, why)

    # utilities & access: public road, neighbours (water, sewage) and, for isolated sites only, a power line
    parts = {k: c[f"c_{k}"] for k in ("power", "road", "neighbours") if c[f"c_{k}"] is not None}
    village = (parts.get("neighbours") or 0) >= 0.5       # houses around: the village has power (low voltage unmapped)
    if village or st == "standing":                       # ... and a standing house is connected
        parts.pop("power", None)
    mean = sum(parts.values()) / len(parts) if parts else None
    g = None if mean is None or not located else "A" if mean >= config.ACCESS_GREEN else "B" if mean >= 0.6 \
        else "C" if mean >= config.ACCESS_AMBER else "D"
    out["access"] = _part(g, (["power_village"] if village else [])
                          + [f"{k}_{'near' if v >= 0.5 else 'far'}" for k, v in parts.items()])

    # protection & planning: a reserve or national park rules building out (and caps the grade at D); forest needs a
    # change of use; a protected landscape restricts new building (not a renovation)
    if not located:
        out["rules"] = _part(None, [])
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
        g = "D" if "reserve" in why else "C" if "forest_db" in why else \
            "B" if "forest" in why or ("protected" in why and st != "standing") else "A"
        if site.get("registered"):
            why.append("register")                 # the conservator decides, both ways; subsidies possible
        out["rules"] = _part(g, why or ["free"])

    out["location"] = location(site) if located else _part(None, [])
    if located and (c["c_water"] or 0) >= 0.5:
        out["location"]["why"].append("water")
    return {k: out[k] for k in config.LIGHT_KEYS}          # by weight, house first


def _part(grade, why: list) -> dict:
    return {"grade": grade, "level": {"A": GREEN, "B": AMBER, "C": AMBER, "D": RED}.get(grade), "why": why}


def location(site: dict) -> dict:
    """Everyday life: supermarket, school and doctor nearby, and a city within reach (config SERVICES_NEAR_M,
    CITY_*_M); without that data (no `gutshaus build` yet) the nearest town."""
    sv = site.get("services")
    if not sv:
        d = site.get("dist_town_m")
        if d is None:
            return _part(None, [])
        code = "town_near" if d <= config.TOWN_GREEN_M else "town_mid" if d <= config.TOWN_AMBER_M else "town_far"
        return _part({"town_near": "A", "town_mid": "B"}.get(code, "C"), [code])
    why, miss = [], 0
    for k, limit in config.SERVICES_NEAR_M.items():
        near = sv.get(k) is not None and sv[k] <= limit
        why.append(f"{k}_{'near' if near else 'far'}")
        miss += not near
    cd = (sv.get("city") or [None, None])[1]
    city = "city_near" if cd is not None and cd <= config.CITY_GREEN_M else \
        "city_mid" if cd is not None and cd <= config.CITY_AMBER_M else "city_far"
    why.append(city)
    miss += {"city_near": 0, "city_mid": 1}.get(city, 2)
    return _part("ABCD"[min(miss, 3)], why)


POINTS = {"A": 4, "B": 3, "C": 2, "D": 1}


def score_of(L: dict):
    """Weighted average of the parts (A 4 · B 3 · C 2 · D 1, weights config.GRADE_WEIGHTS); parts without a grade
    are left out and the other weights scaled up."""
    w = [(config.GRADE_WEIGHTS[k], POINTS[x["grade"]]) for k, x in L.items() if x.get("grade")]
    total = sum(a for a, _ in w)
    return sum(a * p for a, p in w) / total if total else None


def grade_of(site: dict, lights_: dict | None = None) -> str:
    """One grade like a school report: the weighted average of the five parts, rounded with config.GRADE_CUTS
    (A from 3.8, B from 3.2, C from 2.5, else D). A national park or nature reserve caps it at D. '?' when only the village is known."""
    if site.get("status") == "unlocated":
        return "?"
    L = lights_ or lights(site)
    sc = score_of(L)
    if sc is None:
        return "?"
    if L["rules"]["grade"] == "D":
        return "D"
    return next((g for g, cut in config.GRADE_CUTS.items() if sc >= cut), "D")


def grade_rank(g: str) -> int:
    return GRADE_ORDER.index(g) if g in GRADE_ORDER else len(GRADE_ORDER)


# ---------------------------------------------------------------- who can sell it
def unlisted_reason(site: dict) -> str:
    """Why a site is left off the public map and the dossier batch, "" when it is shown (config "Who owns it"):
    "unlocated" when only the village is known, "owner:<group>" for a parcel owned by anyone but private persons,
    "use:<use>" for a school, offices,
    a foundation ... when the owner is unknown. A site for sale (KOWR, gutshaus.sales) is always shown."""
    if site.get("kowr_offer") or site.get("sale"):     # for sale (KOWR, an auction, a tender, the owner): shown
        return ""
    if site.get("status") == "unlocated":       # only the village is known: no parcel, no owner, nothing to visit
        return "unlocated"
    g = site.get("owner_group")
    if g in config.OWNER_PRIVATE:
        return ""
    if g is not None and g not in config.OWNER_UNCLEAR:
        return f"owner:{g}"
    use = site_use(site)
    return f"use:{use}" if use in config.UNLISTED_USES else ""


def listed(site: dict) -> bool:
    """In private hands as far as we know, or offered by KOWR: on the public map and in `--all` dossier batches."""
    return not unlisted_reason(site)


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
