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


def _is_place_name(v: str) -> bool:
    """Town precincts are often just numbers ("04-02", "15", "Nr7", "Obręb 3"): not a place name."""
    return bool(re.search(r"[^\W\d_]{3,}", v or "")) and not re.match(r"(?i)^(nr|obr[eę]b)\b", v.strip())


def village_of(s: dict) -> str:
    # numbered precincts: "21 - Szczepanowice", "0001 Żagań", "026B Śródmieście", "1 M.słubice" (M. = miasto), "Prabuty - M"
    v = re.sub(r"^\d+[A-Za-z]?(\s*[-–.]\s*|\s+)", "", (s.get("precinct") or "").strip())
    v = re.sub(r"\s*-\s*M$", "", re.sub(r"^M\.\s*", "", v))
    v = v[:1].upper() + v[1:]
    if not _is_place_name(v):  # "park dworski, Dąbrowa" -> "Dąbrowa", else the municipality
        v = s["name"].rsplit(",", 1)[-1].strip() if "," in s["name"] else ""
        if not _is_place_name(v):
            v = re.sub(r"\s*\((miasto|gmina [a-z-]+|obszar wiejski)\)", "", s.get("commune") or "").strip() or s["name"]
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
        items = sorted((site_item(did, s) for did, s in members), key=lambda d: d["grade"])
        name_de = next((s["name_de"] for _, s in members if s.get("name_de")), "")
        out.append({"v": village, "de": name_de, "gm": commune, "pw": powiat,
                    "vv": members[0][1].get("voivodeship", ""),
                    "lat": round(lat, round_to), "lon": round(lon, round_to),
                    "g": items[0]["grade"], "r": min(i["ren"] for i in items), "sites": items})
    out.sort(key=lambda v: (v["g"], v["v"]))
    return out


def site_item(did: str, s: dict) -> dict:
    """One site on the public map. Located sites carry their exact position (as in OSM and the register, which are
    public); sites known only by their village do not. Never parcel numbers, owners or scores."""
    item = {"id": did, "grade": grade_of(s), "ren": grade_of(s, "renovation"), "status": s.get("status", "unknown"),
            "park": bool(s.get("park")), "registered": bool(s.get("registered")), "kowr": bool(s.get("kowr_offer"))}
    if s.get("status") != "unlocated" and s.get("lat") is not None:
        item["lat"], item["lon"] = round(s["lat"], 5), round(s["lon"], 5)
    if s.get("kowr_url"):
        item["ku"] = s.get("kowr_uid") or s["kowr_url"]  # the matched KOWR offer (D.kowr[].url): shown in this site's popup, not separately
    return item


def offers(sites: list[dict], kowr_json: Path | None) -> list[dict]:
    if not kowr_json or not Path(kowr_json).exists():
        return []
    raw = json.loads(Path(kowr_json).read_text(encoding="utf-8"))
    raw = raw.get("offers", raw) if isinstance(raw, dict) else raw
    return [{"id": o.get("uid") or o.get("url"), "t": o.get("title"), "url": o.get("url"), "lat": o.get("lat"),
             "lon": o.get("lon"), "ha": o.get("area_ha"), "tender": o.get("tender"), "place": o.get("place"),
             "price": o.get("price_pln"), "disc": o.get("discount_pct"), "reg": o.get("register"),
             "src": o.get("source") or "card"}
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
    page = html.replace("/*__DATA__*/null", js)
    if config.WEB_ANALYTICS_TOKEN:  # Cloudflare Web Analytics: no cookies, no personal data, no consent banner needed
        beacon = ('<script defer src="https://static.cloudflareinsights.com/beacon.min.js" '
                  f"data-cf-beacon='{json.dumps({'token': str(config.WEB_ANALYTICS_TOKEN)})}'></script>")
        page = page.replace("</body>", beacon + "\n</body>", 1)
    (pub / "index.html").write_text(page, encoding="utf-8")
    legal = (HERE / "legal_template.html").read_text(encoding="utf-8")
    if not config.SHOP_ENABLED:  # no sales yet: drop purchase, withdrawal and order-data sections
        legal = re.sub(r"<!--SHOP-->.*?<!--/SHOP-->\n?", "", legal, flags=re.S)
    if config.WEB_ANALYTICS_TOKEN or config.WEB_ANALYTICS_AUTO:
        legal = legal.replace("Diese Website setzt keine Cookies und kein Tracking ein.",
                              "Diese Website setzt keine Cookies ein. Für eine anonyme Besucherstatistik nutzen wir Cloudflare "
                              "Web Analytics: Es speichert keine IP-Adressen und erstellt keine Nutzerprofile; erfasst werden nur "
                              "aufgerufene Seite, Herkunftsseite, Land und Gerätetyp.")
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
    static_cfg = out / "wrangler.jsonc"
    if config.SHOP_ENABLED:
        shutil.copytree(DEPLOY / "functions", out / "functions")
        if static_cfg.exists() and GENERATED in static_cfg.read_text(encoding="utf-8"):
            static_cfg.unlink()  # the shop deploys as a Pages project (wrangler.toml) with Functions
        if not (out / "wrangler.toml").exists():  # keep the user's KV id once filled in
            shutil.copy(DEPLOY / "wrangler.toml", out / "wrangler.toml")
    else:
        write_static_config(static_cfg)
        toml = out / "wrangler.toml"
        if toml.exists():
            if "REPLACE_WITH_KV_NAMESPACE_ID" in toml.read_text(encoding="utf-8"):
                toml.unlink()  # unfilled shop template from an earlier run: it would confuse wrangler
            else:
                log.warning("%s holds a filled-in shop config; move it away to deploy the static site", toml)
    return {"villages": len(vs), "sites": sum(len(v["sites"]) for v in vs)}


GENERATED = "Generated by `gutshaus publish`"


def write_static_config(path: Path) -> None:
    """wrangler.jsonc for the static site (no shop): Cloudflare Workers static assets (Pages is part of Workers
    now), uploading only ./public, with SITE_URL's domain and its www. variant as custom domains.
    Deploy with `npx wrangler deploy` inside <out-root>/site, never in the repo root: there wrangler's
    autoconfig would offer to upload the whole output folder."""
    host = re.sub(r"^https?://", "", config.SITE_URL or "").split("/")[0].lower()
    routes = []
    if host and not host.endswith((".workers.dev", ".pages.dev")):
        routes = [{"pattern": h, "custom_domain": True} for h in (host, "www." + host.removeprefix("www."))]
    cfg = {"name": config.DEPLOY_NAME, "compatibility_date": "2026-11-01",
           "assets": {"directory": "./public", "not_found_handling": "404-page"}}
    if routes:
        cfg["routes"] = routes
    path.write_text(f"// {GENERATED}: the static site (SHOP_ENABLED off). Deploy: npx wrangler deploy\n"
                    + json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
