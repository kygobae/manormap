"""Public website: village-level map with one grade and five traffic lights per site (buy button when the shop is on).

No parcel numbers, owners or scores leave this machine. Located sites are shown at their exact position with
their parcel outline (parcels.json, loaded when zoomed in): both are public anyway (OSM, the monument register,
Geoportal). Sites known only by their village are shown at the village. KOWR offers are shown exactly.
Sites listed in reserved.txt are left out entirely.
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
from .grades import STATUS_DE, dossier_id, grade_of, grade_rank, lights, path
from .sources import kowr

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


def commune_label(c: str) -> str:
    """Cadastre (ULDK) commune names can carry a code and be upper case: '2013BIAŁOBRZEGI OBSZAR WIEJSKI' -> 'Białobrzegi'."""
    c = re.sub(r"^[\d_\s]+", "", c or "")
    c = re.sub(r"(?i)\s*[-(]?\s*(obszar wiejski|miasto|gmina wiejska|gmina miejska)\)?$", "", c).strip()
    return c.title() if c.isupper() else c


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
        items = sorted((site_item(did, s) for did, s in members), key=lambda d: grade_rank(d["grade"]))
        name_de = next((s["name_de"] for _, s in members if s.get("name_de")), "")
        out.append({"v": village, "de": name_de, "gm": commune_label(commune), "pw": powiat,
                    "vv": members[0][1].get("voivodeship", ""),
                    "lat": round(lat, round_to), "lon": round(lon, round_to),
                    "g": items[0]["grade"], "sites": items})
    out.sort(key=lambda v: (grade_rank(v["g"]), v["v"]))
    return out


def site_item(did: str, s: dict) -> dict:
    """One site on the public map. Located sites carry their exact position (as in OSM and the register, which are
    public); sites known only by their village do not. Never parcel numbers, owners or scores."""
    L = lights(s)
    item = {"id": did, "grade": grade_of(s, L), "path": path(s), "status": s.get("status", "unknown"),
            # the five lights as one letter each (g/a/r, - unknown) and their reason codes (no distances)
            "L": "".join((x["level"] or "-")[0] for x in L.values()),
            "w": [w for k, x in L.items() if k != "house" for w in x["why"]],
            "park": bool(s.get("park")), "registered": bool(s.get("registered")), "kowr": bool(s.get("kowr_offer"))}
    if s.get("status") != "unlocated" and s.get("lat") is not None:
        item["lat"], item["lon"] = round(s["lat"], 5), round(s["lon"], 5)
    if s.get("kowr_url"):
        item["ku"] = s.get("kowr_uid") or s["kowr_url"]  # the matched KOWR offer (D.kowr[].url): shown in this site's popup, not separately
    return item


def parcel_outlines(villages_: list[dict], sites: list[dict]) -> dict[str, list]:
    """Dossier ID -> parcel outline [[lat, lon], ...] (about 1 m precision) for the located sites on the map."""
    from shapely.geometry import Polygon
    from .geo import ll_to_pl, pl_to_ll
    by_id = {dossier_id(s["uid"]): s for s in sites}
    out = {}
    for v in villages_:
        for item in v["sites"]:
            ring = (by_id.get(item["id"]) or {}).get("parcel_ll") or []
            if "lat" not in item or len(ring) < 4:
                continue
            poly = Polygon([ll_to_pl(lon, lat) for lon, lat in ring]).simplify(1.0)
            if poly.is_empty or poly.geom_type != "Polygon":
                continue
            out[item["id"]] = [[round(lat, 5), round(lon, 5)]
                               for lon, lat in (pl_to_ll(x, y) for x, y in poly.exterior.coords)]
    return out


# ---------------------------------------------------------------- link previews
SHARE_STATUS = {"de": {"lost": "Herrenhaus verschwunden", "ruin": "Ruine", "standing": "Gebäude steht",
                       "unlocated": "Lage im Dorf unklar", "unknown": "Zustand unklar"},
                "pl": {"lost": "dwór nie istnieje", "ruin": "ruina", "standing": "budynek istnieje",
                       "unlocated": "lokalizacja we wsi niepewna", "unknown": "stan nieustalony"}}


SHARE_PATH = {"de": {"renovation": "Sanierung", "new": "Wiederaufbau", "either": "Sanierung oder Wiederaufbau"},
              "pl": {"renovation": "remont", "new": "odbudowa", "either": "remont lub odbudowa"}}


def share_page(v: dict, item: dict) -> str:
    """/s/<dossier ID>: a tiny page whose title, description and image show up as the preview when the link is
    shared (WhatsApp, Signal, Facebook, mail); people are sent straight on to the map with the site's card open.
    Link previews only see the page the server sends, never a #fragment, hence one page per site."""
    base = (config.SITE_URL or "").rstrip("/")
    sid = item["id"]
    place = v["v"] + (f" ({v['de']})" if v.get("de") else "")
    title = f"{place} – {config.SITE_NAME}"
    de = [f"Note {item['grade']} · {SHARE_PATH['de'][item['path']]}", SHARE_STATUS["de"].get(item["status"], "")]
    pl = [f"Ocena {item['grade']} · {SHARE_PATH['pl'][item['path']]}", SHARE_STATUS["pl"].get(item["status"], "")]
    extra_de = [x for x, on in (("Park", item.get("park")), ("Denkmal", item.get("registered")),
                                ("zum Verkauf (KOWR)", item.get("kowr"))) if on]
    extra_pl = [x for x, on in (("park", item.get("park")), ("zabytek", item.get("registered")),
                                ("na sprzedaż (KOWR)", item.get("kowr"))) if on]
    where = f"gm. {v['gm']}, pow. {v['pw']}" if v.get("gm") else ""
    desc = " · ".join(filter(None, de + extra_de)) + " | " + " · ".join(filter(None, pl + extra_pl)) + (f" — {where}" if where else "")
    e = lambda x: htmllib.escape(x, quote=True)
    target = f"/#{sid}"
    return f"""<!DOCTYPE html>
<html lang="pl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<meta name="description" content="{e(desc)}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="{e(config.SITE_NAME)}">
<meta property="og:title" content="{e(title)}">
<meta property="og:description" content="{e(desc)}">
<meta property="og:url" content="{e(base + '/s/' + sid)}">
<meta property="og:image" content="{e(base + '/og.png')}">
<meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">
<meta property="og:image:alt" content="{e(config.SITE_NAME)}: Gutshäuser in Polen · Dwory i pałace w Polsce">
<meta name="twitter:card" content="summary_large_image">
<meta http-equiv="refresh" content="0; url={e(target)}">
<script>location.replace({json.dumps(target)});</script>
</head><body style="font-family:system-ui,sans-serif;padding:24px">
<p><a href="{e(target)}">{e(place)} – {e(config.SITE_NAME)}</a></p>
</body></html>
"""


def write_share_pages(vs: list[dict], pub: Path) -> int:
    """One /s/<ID>.html per published site (reserved sites are not in `vs`, so they get none)."""
    d = pub / "s"
    d.mkdir(parents=True, exist_ok=True)
    n = 0
    for v in vs:
        for item in v["sites"]:
            (d / f"{item['id']}.html").write_text(share_page(v, item), encoding="utf-8")
            n += 1
    return n


def offers(sites: list[dict], kowr_json: Path | None) -> list[dict]:
    if not kowr_json or not Path(kowr_json).exists():
        return []
    raw = json.loads(Path(kowr_json).read_text(encoding="utf-8"))
    raw = raw.get("offers", raw) if isinstance(raw, dict) else raw
    # a card's planned tender period can lie in the past while the card is still online: say so instead of
    # showing the old date as if it were coming
    return [{"id": o.get("uid") or o.get("url"), "t": o.get("title"), "url": o.get("url"), "lat": o.get("lat"),
             "lon": o.get("lon"), "ha": o.get("area_ha"), "tender": o.get("tender"), "place": o.get("place"),
             "price": o.get("price_pln"), "disc": o.get("discount_pct"), "reg": o.get("register"),
             "src": o.get("source") or "card", "past": kowr.tender_passed(o.get("tender")),
             # the whole tender area (often the manor with its farmland): KOWR publishes these parcels itself
             "pl": [[[lat, lon] for lon, lat in ring] for ring in o.get("parcels_ll") or []],
             "pn": len(o.get("parcels") or []) or o.get("parcels_found"), "pf": o.get("parcels_found"),
             "pha": o.get("parcels_ha")}
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
            "cfg": {"name": config.SITE_NAME, "url": config.SITE_URL, "price": config.DOSSIER_PRICE,
                    "checkout": config.CHECKOUT_URL, "email": config.CONTACT_EMAIL,
                    "status_de": STATUS_DE, "sample": bool(sample_pdf) and config.SHOP_ENABLED,
                    "shop": bool(config.SHOP_ENABLED), "town": [config.TOWN_GREEN_M // 1000, config.TOWN_AMBER_M // 1000], "refs": config.REFERENCE_POINTS}}
    html = (HERE / "site_template.html").read_text(encoding="utf-8")
    js = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = html.replace("/*__DATA__*/null", js).replace("__SITE_URL__", (config.SITE_URL or "").rstrip("/"))
    if config.WEB_ANALYTICS_TOKEN:  # Cloudflare Web Analytics: no cookies, no personal data, no consent banner needed
        beacon = ('<script defer src="https://static.cloudflareinsights.com/beacon.min.js" '
                  f"data-cf-beacon='{json.dumps({'token': str(config.WEB_ANALYTICS_TOKEN)})}'></script>")
        page = page.replace("</body>", beacon + "\n</body>", 1)
    (pub / "index.html").write_text(page, encoding="utf-8")
    (pub / "parcels.json").write_text(json.dumps(parcel_outlines(vs, sites), separators=(",", ":")), encoding="utf-8")
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
    # the display face and Leaflet (1.9.4, BSD-2-Clause) are served from the site itself: no requests to font or
    # script CDNs (GDPR); only the map tiles come from elsewhere
    shutil.copytree(HERE / "fonts", pub / "fonts")
    shutil.copytree(HERE / "vendor", pub / "vendor")
    shutil.copy(HERE / "static" / "og.png", pub / "og.png")
    write_share_pages(vs, pub)
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
           # html_handling: /s/GF-1A2B3C4D serves s/GF-1A2B3C4D.html (the link-preview page of a site)
           "assets": {"directory": "./public", "not_found_handling": "404-page", "html_handling": "auto-trailing-slash"}}
    if routes:
        cfg["routes"] = routes
    path.write_text(f"// {GENERATED}: the static site (SHOP_ENABLED off). Deploy: npx wrangler deploy\n"
                    + json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
