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


_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4}
_QUARTER = re.compile(r"\b(IV|I{1,3})\s*(?:[-–]\s*(IV|I{1,3}))?\s*kwarta\w*\s+(\d{4})", re.I)
_HALF = re.compile(r"\b(I{1,2})\s*(?:[-–]\s*(I{1,2}))?\s*p[oó][lł]rocz\w*\s+(\d{4})", re.I)
_DATE = re.compile(r"\b(\d{1,2})[.\-](\d{1,2})[.\-](\d{4})\b")
_YEAR = re.compile(r"\b(20\d{2})\s*r?\b")


def tender_end(text: str | None) -> tuple[int, int, int] | None:
    """The last day a tender text refers to, as (year, month, day): "IV kwartał 2025 r" -> (2025, 12, 31),
    "I-II kwartał 2026" -> (2026, 6, 30), "03.11.2026 10:00; 24.11.2026 10:00" -> (2026, 11, 24),
    "II półrocze 2026" -> (2026, 12, 31), "2027 r." -> (2027, 12, 31). None if it names no time."""
    t = text or ""
    ends = []
    for m in _DATE.finditer(t):
        ends.append((int(m.group(3)), int(m.group(2)), int(m.group(1))))
    for m in _QUARTER.finditer(t):
        q = _ROMAN[(m.group(2) or m.group(1)).upper()]
        ends.append((int(m.group(3)), 3 * q, (31, 30, 30, 31)[q - 1]))
    for m in _HALF.finditer(t):
        h = _ROMAN[(m.group(2) or m.group(1)).upper()]
        ends.append((int(m.group(3)), 6 * h, 30 if h == 1 else 31))
    if not ends:
        ends += [(int(y), 12, 31) for y in _YEAR.findall(t)]
    return max(ends) if ends else None


def tender_passed(text: str | None, today: tuple[int, int, int] | None = None) -> bool:
    """True when every time the tender text names lies in the past (so the date shown would mislead)."""
    end = tender_end(text)
    return end is not None and end < (today or tuple(time.localtime()[:3]))


def parse_card(text: str) -> dict:
    t = clean_text(text)
    head = re.search(r"^\s*([A-ZĄĆĘŁŃÓŚŹŻ][A-ZĄĆĘŁŃÓŚŹŻ\- ]{2,}),\s*gm(?:ina|\.)\s*([^\n]+)", t, re.M)
    obreb = _one(r"obręb\s*:\s*([^\n]+)", t)
    # the parcel list can run over several lines ("203/4, 203/5, …,\n203/9, …"): read it up to the next heading
    parcels_raw = (_section(t, r"(?<!Powierzchnia )(?<!Pow\. )działk[ai]\s*(?:nr|numer)?\.?\s*:")
                   or _one(r"(?<!Powierzchnia )(?<!Pow\. )działk[ai]\s*(?:nr|numer)?\.?\s*:\s*([^\n]+)", t))
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


def locate(http: Http, o: dict) -> None:
    """Position an offer at its first findable parcel (ULDK by obręb name + number, checked against gmina/powiat).
    Schedules with bare columns give a run of words instead of gmina and obręb: try its tails as the obręb name."""
    p = None
    names = [o["obreb"]] if o.get("obreb") else []
    words = o.get("place_words") or []
    names += [" ".join(words[i:]) for i in range(len(words))]      # "Barciany Kiemławki Wielkie" -> tails
    nrs = list(dict.fromkeys((o.get("heritage_parcels") or []) + (o.get("parcels") or [])))  # the manor's parcels first
    for name in names:
        for nr in nrs[:3]:
            p = parcel_by_name(http, name, nr, o.get("gmina", ""), o.get("powiat", ""))
            if p and (not words or fold(o.get("powiat", "")) in fold(p["county"])):
                o["obreb"] = o.get("obreb") or name
                o["gmina"] = o.get("gmina") or p["commune"]
                break
            p = None
        if p:
            break
    if p and p.get("parcel_geom") is not None:
        c = p["parcel_geom"].centroid
        o["x"], o["y"] = c.x, c.y
        o["lon"], o["lat"] = pl_to_ll(c.x, c.y)
        o["parcel_id"] = p["parcel_id"]
        o["located"] = "parcel"
        outline_parcels(http, o, p)
    else:
        o["located"] = ""


def outline_parcels(http: Http, o: dict, first: dict, max_parcels: int = 20) -> None:
    """All parcels of the offer (a tender often sells the manor with its farmland): their outlines as
    [[lon, lat], ...] rings (about 1 m precision) in o["parcels_ll"], their total area in o["parcels_ha"].
    The parcels are looked up in the same gmina as the one already found, so a common obręb name does not
    pull in another village's parcels."""
    geoms = {first["parcel_id"]: first["parcel_geom"]}
    for nr in list(dict.fromkeys(o.get("parcels") or []))[:max_parcels]:
        if nr == first.get("parcel_no"):
            continue
        q = parcel_by_name(http, o["obreb"], nr, first.get("commune", ""), first.get("county", ""))
        if q and q.get("parcel_geom") is not None and fold(q.get("commune", "")) == fold(first.get("commune", "")):
            geoms[q["parcel_id"]] = q["parcel_geom"]
    rings = []
    for g in geoms.values():
        for poly in getattr(g, "geoms", [g]):
            ring = poly.simplify(1.0).exterior.coords
            rings.append([[round(lon, 5), round(lat, 5)] for lon, lat in (pl_to_ll(x, y) for x, y in ring)])
    o["parcels_ll"] = rings
    o["parcels_found"] = len(geoms)
    o["parcels_ha"] = round(sum(g.area for g in geoms.values()) / 10000, 2)


def fetch(http: Http) -> list[dict]:
    """All current KOWR heritage offers: the Wrocław branch's property cards plus manors in all branches' monthly
    tender schedules. A schedule entry for a property that also has a card adds its tenders and price to the card."""
    from . import kowr_schedules
    offers = []
    for card in list_cards(http):
        try:
            text = pdf_text(http.get(card["url"]))
        except Exception as e:
            log.warning("KOWR card %s unreadable: %s", card["url"], e)
            continue
        o = parse_card(text)
        o.update({"title": card["title"], "url": card["url"], "source": "card"})
        o["uid"] = "kowr:" + card["url"].rsplit("/", 1)[-1]
        locate(http, o)
        offers.append(o)
    log.info("KOWR: %d cards parsed, %d located", len(offers), sum(1 for o in offers if o.get("lat")))
    try:
        scheduled = kowr_schedules.fetch(http, pdf_text, lambda o: locate(http, o))
    except Exception as e:
        log.warning("KOWR schedules unavailable (%s); cards only", e)
        scheduled = []
    by_obreb = {fold(o["obreb"]): o for o in offers if o.get("obreb")}
    for s in scheduled:
        card = by_obreb.get(fold(s.get("obreb") or ""))
        if card and set(s.get("parcels", [])) & set(card.get("parcels", [])):
            card.update({k: s[k] for k in ("tenders", "price_pln", "register") if s.get(k)})
            card["tender"] = s["tender"] or card.get("tender", "")
        else:
            offers.append(s)
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
            s["kowr_uid"] = hit.get("uid") or hit["url"]   # schedules: several offers share one PDF
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
