"""Heritage properties offered for sale by KOWR (Krajowy Ośrodek Wsparcia Rolnictwa).

KOWR publishes one property card (PDF) per manor or palace it is selling on
gov.pl/web/kowr/nieruchomosci-zabytkowe. Each card has the same layout:
location (voivodeship/powiat/gmina/obręb/parcel numbers), plot area, zoning,
planned tender date, utilities, description and contact. We read the cards,
locate the first parcel via ULDK and match offers to nearby sites.

The separate KOWR offers portal (nieruchomoscikowr.gov.pl) is protected
against automated access and is deliberately not used.
"""
from __future__ import annotations

import html as htmllib
import io
import json
import logging
import re
import time
from pathlib import Path

from shapely.geometry import Point

from .. import config
from ..geo import fold, pl_to_ll
from ..http import Http
from . import uldk

log = logging.getLogger(__name__)

LIST_URL = "https://www.gov.pl/web/kowr/nieruchomosci-zabytkowe"
ATTACH_RE = re.compile(r'href="(?P<url>(?:https://www\.gov\.pl)?/attachment/[0-9a-f-]{36})"[^>]*>(?P<title>.*?)</a>', re.S)


# ------------------------------------------------------------------ listing
def list_cards(http: Http) -> list[dict]:
    """[{title, url}] of all property cards on the KOWR heritage page."""
    page = http.get(LIST_URL, cache=False).decode("utf-8", "ignore")
    cards, seen = [], set()
    for m in ATTACH_RE.finditer(page):
        url = m.group("url")
        url = url if url.startswith("http") else "https://www.gov.pl" + url
        title = clean_title(htmllib.unescape(re.sub(r"<[^>]+>", " ", m.group("title"))))
        if url not in seen:
            seen.add(url)
            cards.append({"title": title or url.rsplit("/", 1)[-1], "url": url})
    log.info("KOWR: %d heritage property cards listed", len(cards))
    return cards


def pdf_text(raw: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(raw))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


# ------------------------------------------------------------------ parsing
def clean_title(title: str) -> str:
    title = title.replace("\u200b", " ").replace("\xa0", " ")
    title = re.sub(r"(Karta[_\s]*Nieruchomo\S*|\.pdf|\d+(\.\d+)?\s*MB)", " ", title, flags=re.I)
    title = re.sub(r"[_\s]+", " ", title).strip(" -")
    w = title.split()
    if len(w) % 2 == 0 and w and w[: len(w) // 2] == w[len(w) // 2:]:  # "Pałac X Pałac_X_Karta…pdf"
        title = " ".join(w[: len(w) // 2])
    return title


def clean_text(text: str) -> str:
    t = text.replace("\u200b", "").replace("\xa0", " ").replace("\r", "")
    t = re.sub(r"^\s*\*\s*Niniejsza oferta.*$", "", t, flags=re.M)      # disclaimer on every page
    t = re.sub(r"^\s*Strona \d+ z \d+\s*$", "", t, flags=re.M)
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t)


def _one(pattern: str, t: str) -> str:
    m = re.search(pattern, t, re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip(" ,;.") if m else ""


def _num(s: str) -> float | None:
    s = re.sub(r"[^\d,\.]", "", s or "").replace(",", ".")
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _section(t: str, header: str) -> str:
    """Text after a header until the next known header."""
    m = re.search(header, t, re.I)
    if not m:
        return ""
    rest = t[m.end():]
    ends = [mm.start() for h in HEADERS for mm in [re.search(h, rest, re.I)] if mm]
    chunk = rest[: min(ends)] if ends else rest[:1500]
    return re.sub(r"\s+", " ", chunk).strip(" :;")


HEADERS = [r"\bPowierzchnia\s+(?:Powierzchnia|Pow\.)", r"\bLokalizacja\b", r"\bRodzaj nieruchomo\w*",
           r"Dominujące przeznaczenie", r"Przewidywany termin", r"Media na nieruchomo", r"Opis nieruchomo",
           r"Uwarunkowania otoczenia", r"Odległość do najbliższego", r"Aktualne ograniczenia", r"\bKontakt\b"]
PARCEL_RE = re.compile(r"\b\d{1,5}(?:/\d{1,4})?\b")


def _parcels(s: str) -> list[str]:
    s = re.sub(r"\bAM-?\s*\d+\b", " ", s)          # map sheet numbers ("AM-1") are not parcels
    s = re.sub(r"\b(?:dz|nr)\.?", " ", s, flags=re.I)
    return PARCEL_RE.findall(s)


def parse_card(text: str) -> dict:
    t = clean_text(text)
    head = re.search(r"^\s*([A-ZĄĆĘŁŃÓŚŹŻ][A-ZĄĆĘŁŃÓŚŹŻ\- ]{2,}),\s*gm(?:ina|\.)\s*([^\n]+)", t, re.M)
    obreb = _one(r"obręb\s*:\s*([^\n]+)", t)
    parcels_raw = _one(r"(?<!Powierzchnia )(?<!Pow\. )działk[ai]\s*(?:nr|numer)?\.?\s*:\s*([^\n]+)", t)
    combo = _one(r"obręb i nr działk\w*\s*:\s*([^\n]+)", t)       # "Jastrzębia dz. 115/2 AM-2"
    if combo:
        obreb = obreb or re.split(r"\s+(?:dz\.|nr|\d)", combo, maxsplit=1)[0].strip(" ,")
        parcels_raw = parcels_raw or combo[len(obreb):]
    area = _one(r"(?:Powierzchnia działki|Powierzchnia nieruchomości|Pow\. nieruchomości|Powierzchnia ogółem)"
                r"\s*[:\-–]?\s*([\d \.,]+)\s*ha", t)
    usable = _one(r"pow(?:ierzchnia)?\.?\s*użytkowa[^:\-–\d]*[:\-–]?\s*([\d \.,]+)\s*m", t)
    media = {}
    for key, label in (("electricity", "elektryczność"), ("gas", "gaz"), ("water", "woda"), ("sewer", "kanalizacja")):
        media[key] = _one(label + r"\s*:\s*([^\s,;]+)", t).lower()
    email = re.search(r"[\w.\-]+@kowr\.gov\.pl", t)
    phone = re.search(r"(?:tel\.?|kom\.?)\s*([\d ()\-]{9,})", t)
    return {
        "branch": _one(r"Oddział Terenowy\s+(?:w|we)\s+([^\n]+)", t),
        "place": head.group(1).title() if head else obreb,
        "voivodeship": _one(r"województwo\s*:\s*([^\n]+)", t),
        "powiat": _one(r"\bpowiat\s*:\s*([^\n]+)", t),
        "gmina": _one(r"\bgmina\s*:\s*([^\n]+)", t) or (head.group(2).strip() if head else ""),
        "obreb": obreb,
        "parcels": _parcels(parcels_raw),
        "area_ha": _num(area),
        "usable_m2": _num(usable),
        "kind": _section(t, r"Rodzaj nieruchomo\w*")[:200],
        "zoning": _section(t, r"Dominujące przeznaczenie[^\n]*")[:300],
        "tender": _one(r"Przewidywany termin procedury\s*przetargowej\s*([^\n]+)", t)
                  or _section(t, r"Przewidywany termin[^\n]*")[:80],
        "media": media,
        "description": _section(t, r"Opis nieruchomo\w*")[:600],
        "restrictions": _section(t, r"Aktualne ograniczenia[^\n]*")[:200],
        "contact_email": email.group(0) if email else "",
        "contact_phone": re.sub(r"\s+", " ", phone.group(1)).strip() if phone else "",
    }


# ----------------------------------------------------------------- locating
def parcel_by_name(http: Http, obreb: str, number: str, gmina: str = "", powiat: str = "") -> dict | None:
    """ULDK search by 'obręb name + parcel number'; picks the match in the right gmina/powiat."""
    params = {"request": "GetParcelByIdOrNr", "id": f"{obreb} {number}",
              "result": uldk.FIELDS, "srid": "2180"}
    try:
        raw = http.get(config.ULDK_URL, params=params, retries=2).decode("utf-8", "ignore")
    except Exception as e:
        log.warning("ULDK search failed for %s %s: %s", obreb, number, e)
        return None
    finally:
        time.sleep(config.ULDK_PAUSE_S)
    lines = [ln for ln in raw.strip().splitlines() if ln.strip()]
    if not lines or lines[0].strip().startswith("-1"):
        return None
    best = None
    for ln in lines[1:]:
        p = uldk.parse("0\n" + ln)
        if not p:
            continue
        score = (fold(gmina) in fold(p["commune"])) * 2 + (fold(powiat) in fold(p["county"]))
        if best is None or score > best[0]:
            best = (score, p)
    return best[1] if best else None


def fetch(http: Http) -> list[dict]:
    """All current KOWR heritage offers, parsed and located."""
    offers = []
    for card in list_cards(http):
        try:
            text = pdf_text(http.get(card["url"]))
        except Exception as e:
            log.warning("KOWR card %s unreadable: %s", card["url"], e)
            continue
        o = parse_card(text)
        o.update({"title": card["title"], "url": card["url"]})
        o["uid"] = "kowr:" + card["url"].rsplit("/", 1)[-1]
        p = None
        for nr in o["parcels"][:3]:
            p = parcel_by_name(http, o["obreb"], nr, o["gmina"], o["powiat"])
            if p:
                break
        if p and p.get("parcel_geom") is not None:
            c = p["parcel_geom"].centroid
            o["x"], o["y"] = c.x, c.y
            o["lon"], o["lat"] = pl_to_ll(c.x, c.y)
            o["parcel_id"] = p["parcel_id"]
            o["located"] = "parcel"
        else:
            o["located"] = ""
        offers.append(o)
    log.info("KOWR: %d offers parsed, %d located", len(offers), sum(1 for o in offers if o.get("lat")))
    return offers


def load_or_fetch(http: Http, cache: Path, max_age_h: float = 24, refresh: bool = False) -> list[dict]:
    """Offers cached in <out-root>/_kowr.json, refreshed daily."""
    if cache.exists() and not refresh and time.time() - cache.stat().st_mtime < max_age_h * 3600:
        return json.loads(cache.read_text(encoding="utf-8"))
    try:
        offers = fetch(http)
    except Exception as e:
        log.warning("KOWR offers unavailable (%s)", e)
        return json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else []
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(offers, ensure_ascii=False, default=str), encoding="utf-8")
    return offers


# ----------------------------------------------------------------- matching
def match(sites: list[dict], offers: list[dict], radius_m: float | None = None) -> int:
    """Flag sites within radius of a KOWR offer (or in the same obręb when the offer
    could not be located). Returns the number of flagged sites."""
    from ..geo import ll_to_pl
    r = radius_m or config.KOWR_MATCH_RADIUS_M
    n = 0
    for s in sites:
        sx, sy = (s["x"], s["y"]) if "x" in s else ll_to_pl(s["lon"], s["lat"])
        hit = None
        for o in offers:
            if o.get("x") is not None and Point(sx, sy).distance(Point(o["x"], o["y"])) <= r:
                hit = o
                break
            if o.get("x") is None and same_precinct(o, s):
                hit = o
                break
        if hit:
            s["kowr_offer"] = hit.get("title") or hit.get("place")
            s["kowr_url"] = hit["url"]
            s["kowr_tender"] = hit.get("tender", "")
            n += 1
    return n


def same_precinct(offer: dict, site: dict) -> bool:
    """Same obręb name AND same gmina or powiat: obręb names like "Nowa Wieś"
    repeat all over Poland, so the name alone is not enough."""
    if not offer.get("obreb") or fold(offer["obreb"]) != fold(site.get("precinct") or ""):
        return False

    def agrees(a, b):
        a, b = fold(a or "").strip(), fold(b or "").strip()
        return bool(a and b) and (a in b or b in a)

    return (agrees(offer.get("gmina"), site.get("commune"))
            or agrees(offer.get("powiat"), site.get("county") or site.get("powiat")))
