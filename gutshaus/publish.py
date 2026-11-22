"""Public website: village-level map with grades and a buy button per site.

No parcel numbers, exact positions or scores leave this machine: each village is
shown at a rounded position with its sites' grades and status. KOWR offers are
public anyway and are shown exactly. Sites listed in reserved.txt are left out.
"""
from __future__ import annotations

import html as htmllib
import json
import logging
import re
import shutil
from collections import defaultdict
from pathlib import Path

from . import config
from .grades import STATUS_DE, dossier_id, grade_of

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
DEPLOY = HERE / "deploy"


def village_of(s: dict) -> str:
    v = s.get("precinct")
    if not v:  # "park dworski, Dąbrowa" -> "Dąbrowa"
        v = s["name"].rsplit(",", 1)[-1].strip() if "," in s["name"] else s.get("commune") or s["name"]
    return v


def load_reserved(path: Path | None) -> set[str]:
    if not path or not Path(path).exists():
        return set()
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return {ln.split("#")[0].strip().upper() if ln.strip().upper().startswith("GF-") else ln.split("#")[0].strip()
            for ln in lines if ln.split("#")[0].strip()}


def villages(sites: list[dict], reserved: set[str] = frozenset(), round_to: int = 2) -> list[dict]:
    groups = defaultdict(list)
    for s in sites:
        did = dossier_id(s["uid"])
        if did in reserved or s["uid"] in reserved or set(s.get("members", [])) & reserved:
            continue
        groups[(village_of(s), s.get("commune") or "", s.get("powiat") or "")].append((did, s))
    out = []
    for (village, commune, powiat), members in groups.items():
        lat = sum(s["lat"] for _, s in members) / len(members)
        lon = sum(s["lon"] for _, s in members) / len(members)
        items = sorted(({"id": did, "grade": grade_of(s), "ren": grade_of(s, "renovation"),
                         "status": s.get("status", "unknown"),
                         "park": bool(s.get("park")), "registered": bool(s.get("registered")),
                         "kowr": bool(s.get("kowr_offer"))} for did, s in members), key=lambda d: d["grade"])
        name_de = next((s["name_de"] for _, s in members if s.get("name_de")), "")
        out.append({"v": village, "de": name_de, "gm": commune, "pw": powiat,
                    "vv": members[0][1].get("voivodeship", ""),
                    "lat": round(lat, round_to), "lon": round(lon, round_to),
                    "g": items[0]["grade"], "r": min(i["ren"] for i in items), "sites": items})
    out.sort(key=lambda v: (v["g"], v["v"]))
    return out


def offers(sites: list[dict], kowr_json: Path | None) -> list[dict]:
    if not kowr_json or not Path(kowr_json).exists():
        return []
    raw = json.loads(Path(kowr_json).read_text(encoding="utf-8"))
    raw = raw.get("offers", raw) if isinstance(raw, dict) else raw
    return [{"t": o.get("title"), "url": o.get("url"), "lat": o.get("lat"), "lon": o.get("lon"),
             "ha": o.get("area_ha"), "tender": o.get("tender"), "place": o.get("place")}
            for o in raw if o.get("lat") is not None]


def publish(sites: list[dict], out: Path, reserved: set[str] = frozenset(), kowr_json: Path | None = None,
            sample_pdf: Path | None = None) -> dict:
    """Write <out>/public (static site) and copy the Cloudflare Pages Functions next to it."""
    out = Path(out)
    pub = out / "public"
    if pub.exists():
        shutil.rmtree(pub)
    pub.mkdir(parents=True)
    vs = villages(sites, reserved)
    data = {"villages": vs, "kowr": offers(sites, kowr_json),
            "cfg": {"name": config.SITE_NAME, "name_de": config.SITE_NAME_DE, "url": config.SITE_URL, "price": config.DOSSIER_PRICE,
                    "checkout": config.CHECKOUT_URL, "email": config.CONTACT_EMAIL,
                    "status_de": STATUS_DE, "sample": bool(sample_pdf) and config.SHOP_ENABLED,
                    "shop": bool(config.SHOP_ENABLED)}}
    html = (HERE / "site_template.html").read_text(encoding="utf-8")
    js = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    (pub / "index.html").write_text(html.replace("/*__DATA__*/null", js), encoding="utf-8")
    legal = (HERE / "legal_template.html").read_text(encoding="utf-8")
    if not config.SHOP_ENABLED:  # no sales yet: drop purchase, withdrawal and order-data sections
        legal = re.sub(r"<!--SHOP-->.*?<!--/SHOP-->\n?", "", legal, flags=re.S)
    if config.LEGAL_NOTICE:
        notice = "<br>".join(htmllib.escape(str(line)) for line in config.LEGAL_NOTICE)
        legal = re.sub(r'<p class="todo">.*?</p>', f"<p>{notice}</p>", legal, count=1, flags=re.S)
    else:
        log.warning("LEGAL_NOTICE is empty: legal.html still shows the placeholder. Set it before going live.")
    (pub / "legal.html").write_text(legal.replace("{{NAME}}", config.SITE_NAME).replace("{{EMAIL}}", config.CONTACT_EMAIL or "–"),
                                    encoding="utf-8")
    (pub / "robots.txt").write_text("User-agent: *\nAllow: /\n", encoding="utf-8")
    if sample_pdf and config.SHOP_ENABLED:
        shutil.copy(sample_pdf, pub / "sample.pdf")
    # Cloudflare Pages Functions (webhook + download): only with the shop; without it the site is purely static
    if (out / "functions").exists():
        shutil.rmtree(out / "functions")
    if config.SHOP_ENABLED:
        shutil.copytree(DEPLOY / "functions", out / "functions")
    if not (out / "wrangler.toml").exists():  # keep the user's KV id once filled in
        shutil.copy(DEPLOY / "wrangler.toml", out / "wrangler.toml")
    return {"villages": len(vs), "sites": sum(len(v["sites"]) for v in vs)}
