"""Manors in KOWR's monthly tender schedules (harmonogram przetargów).

Only KOWR's Wrocław branch publishes heritage property cards (see kowr.py). The other branches list everything they
sell or lease in monthly tender schedules (PDF) on their gov.pl pages, each with its own table layout. The pages are
found via KOWR's overview `/web/kowr/harmonogramy` (their names vary: ot-koszalin1, ot-bialystok3, ...).
Instead of one parser per layout we:

1. split a schedule's text into rows at each tender date ("03.11.2026 10:00"),
2. keep sale rows that mention a manor, palace, estate park or a monument register entry,
3. read location, parcels, area, starting price and register number with patterns that work across layouts
   (labelled "Powiat … gmina … obręb … dz. nr …" or bare columns, which ULDK then disambiguates),
4. merge the rows of one property (several tender dates, corrected schedule versions) into one offer.

Unlike the heritage cards, schedules give the starting price (cena wywoławcza).
"""
from __future__ import annotations

import html as htmllib
import logging
import re
import time

from .. import config
from ..geo import fold
from ..http import Http

log = logging.getLogger(__name__)

INDEX_URL = "https://www.gov.pl/web/kowr/harmonogramy"      # links every branch's schedule page
BRANCH_URL = "https://www.gov.pl/web/kowr/ot-{branch}2"      # fallback name pattern
SUBPAGE_RE = re.compile(r'href="(/web/kowr/harmonogram-przetargow-[^"#?]+)"')
ATTACH_RE = re.compile(r'href="(?P<url>(?:https://www\.gov\.pl)?/attachment/[0-9a-f-]{36})"[^>]*>(?P<title>.*?)</a>', re.S)
SCHEDULE_TITLE = re.compile(r"(?i)harm|przetarg")       # "Harmonogram …", "harm_rawski_2026-11" (Łódź)

ROW_START = re.compile(r"\b(\d{1,2}[.\-]\d{2}[.\-]\d{4})\s*(?:r\.?)?,?\s*(?:godz\.?\s*)?(\d{1,2}[:.]\d{2})")  # "05-11-2026" (Łódź)
# A manor, not a place name: "dwór"/"dworski"/"folwark" in lower case as words of their own ("Nowy Dwór Gdański",
# powiat "nowodworski", "Kuków-Folwark" are places); "zabytkowy" only with a register entry or a building type.
HERITAGE = re.compile(r"(?i:rejestr\w*\s+zabytk|wpisan\w*\s+do\s+rejestru|zabytkow\w*\s+(?:dw|pa[lł]|park|zesp|obiekt|budyn))"
                      r"|\b(?i:pa[lł]ac)\w*|\bdw[oó]r\b|\bdworek\b|\bdworsk\w*|\bfolwar\w*|\b(?i:park)\s+(?:dworski|pa[lł]acowy)")
HEADER = re.compile(r"(?i)data\s+i\s+godzina\s+rozpocz|miejsce\s+przeprowa")
KIND = re.compile(r"(?i)(zesp[oó][lł]\s+(?:dworsko|pa[lł]acowo|folwarczn)[\w\-\s]*?(?=\s+(?:wpisan|w\s|z\s|o\s|pod\b|\d)|$)"
                  r"|park\s+(?:dworski|pa[lł]acowy)|pa[lł]ac\b|dw[oó]r\b|dworek|oficyna)")
PRICE = re.compile(r"(\d{1,3}(?:[  ]\d{3})*,\d{2})\s*z[lł]")
AREA = re.compile(r"(?<![\d,])(\d{1,4},\d{4})(?!\d)")                # hectares with four decimals
# "pod nr A-88", "nr 123/45", or a bare "A-1950": a register number needs "nr" or a letter prefix, so the
# price after "rejestru zabytków 24 400,00 zł" is not taken for one
REGISTER = re.compile(r"(?i:rejestr\w*\s+zabytk\w*)\s*(?:(?i:pod)\s+)?(?:(?i:nr)\.?\s*([A-Z]{0,3}[\-/]?\s?\d[\w/\-.]*)|([A-Z]{1,3}[\-/]\d[\w/\-.]*))")  # "A-88", "A/123"
MODE = re.compile(r"(?i)(ustny|pisemny)?\s*(nieograniczony|ograniczony)")
POWIAT_ADJ = re.compile(r"\b([a-ząćęłńóśźż]+(?:ski|cki|zki))\b")     # bare-column layouts: "kętrzyński"
# slash layout (Wrocław, Malbork): "DOLNOŚLĄSKIE (2)/ świdnicki (19)/ Dobromierz (3_2)/ Szymanów (12)/ 289/8, 289/7,"
SLASH = re.compile(r"[A-ZĄĆĘŁŃÓŚŹŻ\-]+\s*\(\d+\)\s*/\s*([a-ząćęłńóśźż\-]+)\s*\(\d+\)\s*/\s*([^/()]+?)\s*\([\d_]+\)\s*/"
                   r"\s*([^/()]+?)\s*\(\d+\)\s*/\s*((?:\d{1,5}(?:/\d{1,4})?,(?=\s)\s*)+)")  # "289/8, " but not "3,6806"
PRICE_BARE = re.compile(r"(?<![\d,])(\d+,\d{2})\s*(?=sprzeda)")         # "281200,00sprzedaż" (no "zł", no spaces)
DISCOUNT = re.compile(r"(?i)obni[zż]k\w*\s*(\d{1,2})\s*%")


def _clean(t: str) -> str:
    t = t.replace("​", "").replace("\xa0", " ").replace("\r", "")
    t = re.sub(r"(\w)-\n(\w)", r"\1-\2", t)                          # "pałacowo-\nparkowy"
    return t


def rows(text: str) -> list[str]:
    t = _clean(text)
    starts = [m.start() for m in ROW_START.finditer(t)]
    return [t[a:b] for a, b in zip(starts, starts[1:] + [len(t)])]


def _price(s: str) -> float | None:
    m = PRICE.search(s) or PRICE_BARE.search(s)
    return float(re.sub(r"[  ]", "", m.group(1)).replace(",", ".")) if m else None


def parse_row(row: str) -> dict | None:
    """A heritage sale row -> offer fields; None for leases and rows without a manor or monument."""
    flat = re.sub(r"\s+", " ", row)
    flat = re.sub(r"\*\s*\(.*$", "", flat)          # footnote legends ("*(zabudowa mieszkaniowa, … zabytkowa …)")
    flat = re.sub(r"(?:\bPOWIAT [A-ZĄĆĘŁŃÓŚŹŻ]+\s*)+.*$", "", flat)   # page footers listing powiats
    if HEADER.search(flat) or not re.search(r"(?i)sprzeda", flat):   # table headers; leases are not offers to buy
        return None
    if not HERITAGE.search(flat):
        return None
    d = ROW_START.search(flat)
    # keywords case-insensitive ((?i:...)), names case-sensitive: a name is a run of capitalised words
    name = r"([A-ZĄĆĘŁŃÓŚŹŻ][\wąćęłńóśźż\-]*(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ][\wąćęłńóśźż\-]*)*)"
    lab = {k: (re.search(p, flat).group(1).strip(" ,") if re.search(p, flat) else "")
           for k, p in (("powiat", r"\b(?i:powiat)\s+([a-ząćęłńóśźż\-]+)"),
                        ("gmina", r"\b(?i:gmin[ay])\s+" + name),
                        ("obreb", r"\b(?i:obr[eę]b(?:ie)?)\s+(?:(?i:ewid)\.?\s*)?" + name))}
    parcels_txt = ""
    m = re.search(r"(?i)dz(?:iałk\w*|\.)\s*(?:nr|numer)?\.?:?\s*(.*?)(?=\s+sprzeda|\s+o\s+pow|\s+pow\.|$)", flat)
    if m:
        parcels_txt = m.group(1)
    kind = KIND.search(flat)
    reg = REGISTER.search(flat)
    mode = MODE.search(flat)
    area = AREA.search(flat)
    out = {
        "date": d.group(1).replace("-", ".") if d else "", "time": d.group(2).replace(".", ":") if d else "",
        "powiat": lab["powiat"], "gmina": lab["gmina"], "obreb": lab["obreb"],
        "parcels": re.findall(r"\b\d{1,5}(?:/\d{1,4})?\b", re.sub(r"\bAM-?\s*\d+\b", " ", parcels_txt)),
        "kind": re.sub(r"\s+", " ", kind.group(1)).strip() if kind else "",
        "register": (reg.group(1) or reg.group(2)).strip(" .,") if reg else "",
        "mode": " ".join(x for x in (mode.group(1), mode.group(2)) if x).lower() if mode else "",
        "area_ha": float(area.group(1).replace(",", ".")) if area else None,
        "price_pln": _price(flat),
        "discount_pct": int(DISCOUNT.search(flat).group(1)) if DISCOUNT.search(flat) else None,
        # "… nieużytki na dz. 33/15, 40/5 zespół pałacowo-parkowy …": the parcels the manor stands on, located first
        # (a large farm offer's first parcel can be a field a kilometre away)
        "heritage_parcels": re.findall(r"\d{1,5}(?:/\d{1,4})?", m2.group(1)) if (m2 := re.search(
            r"(?i)\bna\s+dz(?:iałk\w*|\.)?\s*(?:nr\.?\s*)?((?:\d{1,5}(?:/\d{1,4})?(?:,\s*|\s+i\s+|\s+))+)(?=\S*\s*"
            r"(?:zesp|pa[lł]ac|dw[oó]r|park|budyn|obiekt))", flat)) else [],
        "kw": sorted(set(re.findall(r"\b[A-Z]{2}\d[A-Z]/\d{8}/\d\b", flat))),   # land register number(s)
        "row": flat[:600],
    }
    s = SLASH.search(flat)
    if s and not out["obreb"]:
        out.update({"powiat": s.group(1), "gmina": s.group(2).strip(), "obreb": s.group(3).strip(),
                    "parcels": re.findall(r"\d{1,5}(?:/\d{1,4})?", s.group(4))})
    if not out["obreb"]:   # bare columns: "<unit> <powiat adj> <gmina…> <obręb…> <parcel>"; ULDK decides the split
        m = POWIAT_ADJ.search(flat[d.end():] if d else flat)
        if m:
            out["powiat"] = out["powiat"] or m.group(1)
            rest = flat[(d.end() if d else 0) + m.end():]
            words = re.match(r"\s*((?:[A-ZĄĆĘŁŃÓŚŹŻ][\wąćęłńóśźż\-]*\s+){1,6})(\d{1,5}(?:/\d{1,4})?)", rest)
            if words:
                out["place_words"] = words.group(1).split()
                out["parcels"] = out["parcels"] or [words.group(2)]
    return out


def title_of(o: dict) -> str:
    kind = (o.get("kind") or "obiekt zabytkowy").strip()
    place = o.get("obreb") or (o.get("place_words") or [""])[-1]
    return f"{kind[:1].upper()}{kind[1:]}, {place}".strip(", ")


def merge(offers: list[dict], today: tuple[int, int, int] | None = None) -> list[dict]:
    """One offer per property: rows with the same obręb and first parcel are tenders of the same property.
    Only future tenders count; a property whose tenders are all past is left out (sold, or it comes back in a
    newer schedule)."""
    today = today or tuple(time.localtime()[:3])
    by_key: dict[tuple, dict] = {}
    for o in offers:
        key = (fold(o.get("obreb") or " ".join(o.get("place_words", []))), (o.get("parcels") or [""])[0])
        t = {"date": o["date"], "time": o["time"], "mode": o["mode"], "price_pln": o["price_pln"]}
        if key in by_key:
            m = by_key[key]
            if t not in m["tenders"]:
                m["tenders"].append(t)
        else:
            by_key[key] = {**o, "tenders": [t]}
    out = []
    for o in by_key.values():
        day = lambda t: tuple(int(x) for x in reversed(t["date"].split(".")))
        o["tenders"].sort(key=lambda t: day(t) + (t["time"],))
        o["tenders"] = [t for t in o["tenders"] if day(t) >= today]   # offers are reloaded daily: drop past tenders
        if not o["tenders"]:
            continue
        o["tender"] = "; ".join(f"{t['date']} {t['time']}" + (f" ({t['mode']})" if t["mode"] else "") for t in o["tenders"])
        prices = [t["price_pln"] for t in o["tenders"] if t["price_pln"]]
        o["price_pln"] = min(prices) if prices else None        # the lowest current starting price
        out.append(o)
    return out


# ------------------------------------------------------------------ fetching
def _attachments(page: str) -> list[tuple[str, str]]:
    out = []
    for m in ATTACH_RE.finditer(page):
        url = m.group("url")
        title = re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", m.group("title")))).replace("​", "").strip()
        out.append((url if url.startswith("http") else "https://www.gov.pl" + url, title))
    return out


def branch_pages(http: Http) -> list[tuple[str, str]]:
    """[(branch, url)] from KOWR's overview of all branch schedule pages. Their names differ ("ot-koszalin1",
    "ot-bialystok3", "ot-gorzow-wlkp---zielona-gora"), so they are read from there rather than guessed; if the
    overview is unavailable, fall back to config.KOWR_BRANCHES with the common "ot-<branch>2" name."""
    try:
        page = http.get(INDEX_URL, cache=False).decode("utf-8", "ignore")
        found = []
        for path, text in re.findall(r'href="(/web/kowr/ot-[^"#?]+)"[^>]*>(.*?)</a>', page, re.S):
            name = re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", text))).strip() or path.rsplit("/", 1)[-1]
            if ("https://www.gov.pl" + path) not in {u for _, u in found}:
                found.append((name, "https://www.gov.pl" + path))
        if found:
            return found
        log.warning("KOWR schedules: no branch links on %s; using the configured branches", INDEX_URL)
    except Exception as e:
        log.warning("KOWR schedules: overview %s unavailable (%s); using the configured branches", INDEX_URL, e)
    return [(b, BRANCH_URL.format(branch=b)) for b in config.KOWR_BRANCHES]


def schedule_pdfs(http: Http, branches=None, per_page: int = 4) -> list[dict]:
    """[{branch, title, url}] of the newest schedules on each branch page (and on monthly subpages, as in Poznań).
    `branches`: config-style branch names to read instead of the overview (e.g. for a quick look at one branch)."""
    found, empty = [], []
    pages = [(b, BRANCH_URL.format(branch=b)) for b in branches] if branches else branch_pages(http)
    for b, url in pages:
        try:
            page = http.get(url, cache=False).decode("utf-8", "ignore")
        except Exception as e:
            log.warning("KOWR %s: branch page unavailable (%s)", b, e)
            continue
        pdfs = [(u, t) for u, t in _attachments(page) if SCHEDULE_TITLE.search(t)][:per_page]
        for sub in SUBPAGE_RE.findall(page)[-2:]:                    # newest months are listed last
            time.sleep(config.KOWR_PAUSE_S)
            try:
                sp = http.get("https://www.gov.pl" + sub, cache=False).decode("utf-8", "ignore")
                pdfs += [(u, t) for u, t in _attachments(sp) if u not in {x[0] for x in pdfs}][:per_page]
            except Exception as e:
                log.warning("KOWR %s: %s unavailable (%s)", b, sub, e)
        found += [{"branch": b, "title": t, "url": u} for u, t in pdfs]
        if not pdfs:
            empty.append(b)
        time.sleep(config.KOWR_PAUSE_S)
    log.info("KOWR schedules: %d PDFs from %d of %d branches", len(found), len({f["branch"] for f in found}), len(pages))
    if empty:
        log.warning("KOWR schedules: no schedule found for %s (page layout changed?)", ", ".join(empty))
    return found


def fetch(http: Http, pdf_text, locate) -> list[dict]:
    """Heritage sale offers from all branch schedules. `pdf_text(bytes) -> str` and `locate(offer) -> None`
    (sets x/y/lat/lon/parcel_id, see kowr.locate) are passed in by kowr.fetch."""
    rows_found = []
    for s in schedule_pdfs(http):
        try:
            text = pdf_text(http.get(s["url"]))
        except Exception as e:
            log.warning("KOWR schedule %s unreadable: %s", s["url"], e)
            continue
        for r in rows(text):
            o = parse_row(r)
            if o:
                rows_found.append({**o, "branch": s["branch"], "url": s["url"], "schedule": s["title"]})
        time.sleep(config.KOWR_PAUSE_S)
    offers = merge(rows_found)
    for o in offers:
        o["title"] = title_of(o)
        o["source"] = "schedule"
        o["uid"] = "kowr-schedule:" + fold(o["title"]).replace(" ", "-") + ":" + "-".join(o.get("parcels", [])[:2])
        locate(o)
    log.info("KOWR schedules: %d heritage sale offers (%d located)", len(offers), sum(1 for o in offers if o.get("lat")))
    return offers
