"""Public grades and site lookup, shared by the public site and the (separate, private)
dossier generator: dossier ID, the path (renovation or rebuilding), five traffic lights, one grade,
and finding a site by ID. Rules and thresholds: config "Grades: five traffic lights"."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import config, criteria

STATUS_DE = {"lost": "Herrenhaus nicht mehr vorhanden", "ruin": "Ruine / Reste vorhanden",
             "standing": "Gebäude steht noch", "unlocated": "Lage nur auf Dorfebene bekannt", "unknown": "unklar"}

GREEN, AMBER, RED = "green", "amber", "red"
GRADE_ORDER = "ABCD?"
# what is left decides the path: renovate what stands, rebuild where nothing stands; ruins and unclear cases both
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

    out["house"] = {"level": {"standing": GREEN, "lost": GREEN, "ruin": AMBER, "unknown": AMBER}.get(st),
                    "why": [st or "unknown"]}

    ha, (lo, hi) = site.get("parcel_ha") if located else None, config.PARCEL_OK_HA
    why = ["park" if site.get("park") else "nopark"]
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
        if site.get("land_cover") == "forest":
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


def grade_of(site: dict, lights_: dict | None = None) -> str:
    """A all five green · B four green, no red · C three green, at most one red · D otherwise ·
    D in a national park or nature reserve · '?' when only the village is known."""
    if site.get("status") == "unlocated":
        return "?"
    L = lights_ or lights(site)
    if L["rules"]["level"] == RED:
        return "D"
    levels = [x["level"] for x in L.values()]
    g, r = levels.count(GREEN), levels.count(RED)
    return "A" if g == 5 else "B" if g == 4 and not r else "C" if g >= 3 and r <= 1 else "D"


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
