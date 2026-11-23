"""Public grades and site lookup, shared by the public site and the (separate, private)
dossier generator: dossier ID, the two fixed-weight grades, and finding a site by ID."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import config, criteria

STATUS_DE = {"lost": "Herrenhaus nicht mehr vorhanden", "ruin": "Ruine / Reste vorhanden",
             "standing": "Gebäude steht noch", "unlocated": "Lage nur auf Dorfebene bekannt", "unknown": "unklar"}


# ---------------------------------------------------------------- identifiers
def dossier_id(uid: str) -> str:
    return "GF-" + hashlib.sha1(uid.encode()).hexdigest()[:8].upper()


def grade(score: float | None) -> str:
    s = score or 0
    return next(g for g, lo in config.GRADES if s >= lo)


def public_score(site: dict, profile: str = "new") -> int:
    """Score with the fixed public weights (config.PROFILES), so the site and the dossier always agree.
    Criterion values are recomputed, so older sites.json files get newer criteria too."""
    return criteria.score({**site, **criteria.compute(site)}, config.PROFILES[profile])


def grade_of(site: dict, profile: str = "new") -> str:
    g = grade(public_score(site, profile))
    if profile == "renovation" and site.get("status") == "lost":  # nothing left to renovate
        g = max(g, config.RENOVATION_CAP_LOST)
    if profile == "new" and site.get("status") in ("standing", "ruin"):  # the manor or its walls occupy its place
        g = max(g, config.NEW_CAP_STANDING)
    return g


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
