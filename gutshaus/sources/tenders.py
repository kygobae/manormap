"""Public tenders for manors that a gmina, powiat, voivodeship or the State Treasury owns.

Public owners sell real estate by public tender, announced in their Biuletyn Informacji Publicznej (BIP). There is no
national register of these notices, but the land register tells us who owns each manor (sources/egib.py), so only
those offices need watching (`gutshaus tenders`):

1. offices(): the owners to watch: the gmina for groups 4/5, the starostwo (powiat) for State Treasury land and
   powiat land (1, 2, 11, 12), the marshal's office for voivodeship land (13, 14), each with the sites it owns.
2. discover(): each office's website (Wikidata, by TERYT code), the BIP linked from it, and the BIP's pages about
   property sales ("przetargi", "sprzedaż nieruchomości", "wykazy" ...). Kept in <out-root>/_tenders/offices.json;
   config.TENDER_PAGES_FILE (private, next to gutshaus.json) sets the pages by hand where this fails.
3. check(): reads those pages fresh and the notices on them (cached; PDF and DOCX attachments too): first those whose
   link names one of the office's villages or a manor, then the newest few. A notice naming a manor's parcel number
   after "działka" and its precinct (obręb) is a "parcel" match: it goes on the map as a sale (as_sales). One that
   only names the village and a manor word ("pałac", "dwór", "zespół dworski" ...) is a "village" match: listed for
   review, never published. A notice about a tender's result ("informacja o wyniku") is never a sale.
Writes <out-root>/_tenders/found.json.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html as htmllib
import io
import json
import logging
import re
import threading
import time
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .. import config
from .. import http as _http  # noqa: F401  (system certificates: many BIPs send incomplete chains)
from ..geo import fold

log = logging.getLogger(__name__)

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
OFFICE_GROUPS = {4: "gmina", 5: "gmina", 1: "powiat", 2: "powiat", 11: "powiat", 12: "powiat",
                 13: "voivodeship", 14: "voivodeship"}
# a page about selling property, by its link text (folded)
# a page about selling property, by its link text (folded). Strong: a sale word next to "nieruchomości", a list of
# properties for sale, property management, the gmina's assets. Weak (only when there is nothing strong): plain
# "przetargi" (often procurement), "nieruchomości", "ogłoszenia o przetargach".
SECTION_STRONG = re.compile(r"(przetarg|sprzedaz|zbyci|zbywan|obrot|rokowan)\w*[^|]{0,40}nieruchomo|"
                            r"nieruchomo\w*[^|]{0,40}(sprzedaz|przetarg|zbyci|zbywan|do zbycia|wykaz)|"
                            r"wykaz\w* nieruchomo|gospodark\w* nieruchomo|gospodarka mieniem|"
                            r"mieni\w* (gminy|gminne|komunaln|wojewodztw|powiatu|skarbu)|przetargi na (sprzedaz|zbycie)")
SECTION_WEAK = re.compile(r"^(przetargi|nieruchomosci|ogloszenia o przetargach|przetargi i ogloszenia|"
                          r"sprzedaz nieruchomosci|ogloszenia i przetargi)$")
SECTION_BAD = re.compile(r"zamowien|naborze|\bprac[ay]\b|stanowisk|wybor|petycj|deklaracj|dostepnosc|rodo|"
                         r"rejestr cen")
# a notice about letting, not selling: never a sale
LEASE = re.compile(r"dzierzaw|najem|najmu|uzyczen")
SALE = re.compile(r"sprzedaz|zbyci|zbywan|zbyc\b")
# a notice on such a page
NOTICE = re.compile(r"przetarg|zbyci|sprzedaz|wykaz|ogloszen|licytac|rokowan|nieruchomo")
ATTACHMENT = re.compile(r"\.(pdf|docx)(\?|$)|/(download|attachment|plik|pobierz|zalacznik|file)", re.I)
RESULT = re.compile(r"informacj\w* o wynik|wynik\w* przetarg|wynikach przetarg")
MANOR = re.compile(r"\b(palac\w*|dwor|dworu|dworek|dworku|dworkiem|zespol\w* (palac|dwor|parkow)\w*|"
                   r"park\w* (dworsk|palacow|podworsk)\w*|oficyn\w*|zamek|zamku)\b")
MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6, "lipca": 7, "sierpnia": 8,
          "wrzesnia": 9, "pazdziernika": 10, "listopada": 11, "grudnia": 12}
MAX_NOTICES = 60          # links looked at per section page, newest first as BIPs list them
NEWEST = 5                # notices read per office although their link names none of its villages
MAX_ATTACHMENTS = 3       # per notice
MAX_BYTES = 8 << 20
WORKERS = 8               # offices read at the same time (one request at a time per host)
RETRY_DAYS = 7            # an office without sale pages is looked at again after this many days

import http.client  # noqa: E402
http.client._MAXHEADERS = 1000   # some municipal servers send hundreds of headers (cookies)


# ---------------------------------------------------------------- who to watch
def office_key(site: dict) -> str | None:
    """'gmina:0219073', 'powiat:0219' or 'voivodeship:02' for a site owned by a public body; None otherwise.
    The gmina code comes from the parcel id; a town or rural part (types 4, 5) belongs to its urban-rural gmina (3)."""
    kind = OFFICE_GROUPS.get(site.get("owner_group"))
    pid = site.get("parcel_id") or ""
    if not kind or not re.match(r"\d{6}_\d", pid):
        return None
    if kind == "gmina":
        t = pid[7]
        return f"gmina:{pid[:6]}{'3' if t in '45' else t}"
    return f"powiat:{pid[:4]}" if kind == "powiat" else f"voivodeship:{pid[:2]}"


def offices(sites: list[dict]) -> dict[str, dict]:
    """{key: {"kind", "terc", "sites": [uid, ...]}} for every public owner of a located manor."""
    out: dict[str, dict] = {}
    for s in sites:
        k = office_key(s)
        if k:
            kind, terc = k.split(":")
            out.setdefault(k, {"kind": kind, "terc": terc, "sites": []})["sites"].append(s["uid"])
    return out


# ---------------------------------------------------------------- fetching
class Fetcher:
    """GET with the project's User-Agent, one request at a time per host with a pause, and an on-disk cache for
    notices (they do not change); section pages are read fresh (cache=False). Returns (final url, bytes); raises on
    failure. Safe to share between threads."""

    def __init__(self, cache_dir: Path | None, pause_s: float | None = None):
        import requests
        self.s = requests.Session()
        self.s.headers["User-Agent"] = config.USER_AGENT
        self.cache = Path(cache_dir) if cache_dir else None
        if self.cache:
            self.cache.mkdir(parents=True, exist_ok=True)
        self.pause = config.TENDER_PAUSE_S if pause_s is None else pause_s
        self.last: dict[str, float] = {}
        self.locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

    def __call__(self, url: str, cache: bool = True) -> tuple[str, bytes]:
        key = self.cache / hashlib.sha1(url.encode()).hexdigest() if self.cache and cache else None
        if key and key.exists():
            d = json.loads(key.read_text(encoding="utf-8"))
            return d["url"], bytes.fromhex(d["body"])
        host = urlparse(url).netloc
        with self.locks[host]:
            wait = self.last.get(host, 0) + self.pause - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.s.get(url, timeout=(10, 40), stream=True)
                body = r.raw.read(MAX_BYTES + 1, decode_content=True)
            finally:
                self.last[host] = time.monotonic()
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} from {url}")
        if len(body) > MAX_BYTES:
            raise RuntimeError(f"too large: {url}")
        if key:
            key.write_text(json.dumps({"url": r.url, "body": body.hex()}), encoding="utf-8")
        return r.url, body


def decode(body: bytes) -> str:
    """HTML bytes as text: the declared charset, else UTF-8, else Windows-1250 (common on Polish BIPs)."""
    m = re.search(rb'charset=["\']?([\w-]+)', body[:3000], re.I)
    for enc in ([m.group(1).decode("ascii", "ignore")] if m else []) + ["utf-8", "cp1250"]:
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", "replace")


_A = re.compile(r'<a\b[^>]*?href\s*=\s*["\']([^"\'#]+)[^>]*>(.*?)</a>', re.S | re.I)


def links(base: str, html: str) -> list[tuple[str, str]]:
    """[(absolute url, folded link text)] of a page, in page order, without mailto/javascript links."""
    out = []
    for href, text in _A.findall(html):
        href = htmllib.unescape(href.strip())
        if href.lower().startswith(("mailto:", "javascript:", "tel:")):
            continue
        t = fold(htmllib.unescape(re.sub(r"<[^>]+>", " ", text)))
        out.append((urljoin(base, href), re.sub(r"\s+", " ", t).strip()))
    return out


def page_text(url: str, body: bytes) -> str:
    """Readable text of an HTML page, a PDF or a DOCX."""
    if body[:5] == b"%PDF-":
        try:
            from pypdf import PdfReader
            return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(body)).pages[:30])
        except Exception as e:
            log.debug("PDF %s: %s", url, e)
            return ""
    if body[:2] == b"PK":
        try:
            xml = zipfile.ZipFile(io.BytesIO(body)).read("word/document.xml").decode("utf-8", "replace")
            return htmllib.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"</w:p>", "\n", xml)))
        except Exception:
            return ""
    html = re.sub(r"(?is)<(script|style|nav|header|footer)\b.*?</\1>", " ", decode(body))
    return htmllib.unescape(re.sub(r"<[^>]+>", " ", html))


# ---------------------------------------------------------------- discovery
def websites(http) -> dict[str, list[str]]:
    """TERYT (TERC) code -> official websites, from Wikidata (P1653, P856). One query, cached by Http."""
    q = "SELECT ?terc ?site WHERE { ?g wdt:P1653 ?terc . ?g wdt:P856 ?site . }"
    raw = http.get(WIKIDATA_SPARQL, params={"query": q, "format": "json"})
    out: dict[str, list[str]] = {}
    for b in json.loads(raw)["results"]["bindings"]:
        out.setdefault(b["terc"]["value"], []).append(b["site"]["value"])
    return out


def _is_bip(url: str, text: str = "") -> bool:
    host = urlparse(url).netloc.lower()
    return host.startswith("bip") or ".bip." in host or "-bip." in host or host.endswith(".bip.gov.pl") \
        or text in ("bip", "biuletyn informacji publicznej") or text.startswith("bip ")


def sections(page_links: list[tuple[str, str]]) -> list[str]:
    """Links to pages about selling property: the strong ones (SECTION_STRONG), else the weak ones. At most 3."""
    scored = []
    for url, t in page_links:
        if not t or SECTION_BAD.search(t):
            continue
        if SECTION_STRONG.search(t):
            scored.append((-2, url))
        elif SECTION_WEAK.match(t):
            scored.append((-1, url))
    if any(s == -2 for s, _ in scored):
        scored = [x for x in scored if x[0] == -2]
    out = []
    for _, url in sorted(scored, key=lambda x: x[0]):
        if url not in out:
            out.append(url)
    return out[:3]


def _get_links(fetch, url: str) -> tuple[str, list]:
    base, body = fetch(url, cache=False)
    return base, links(base, decode(body))


def discover_office(fetch, sites_for: list[str]) -> dict:
    """{"web", "bip", "pages": [section urls], "how"} for one office from its websites; never raises."""
    out = {"web": "", "bip": "", "pages": [], "how": "website unreachable"}
    for web in sites_for:
        for candidate in dict.fromkeys([web.replace("http://", "https://", 1), web]):   # https first
            try:
                base, L = _get_links(fetch, candidate)
            except Exception:
                continue
            out.update(web=base, how="no BIP found")
            bips = [base] if _is_bip(base) else list(dict.fromkeys(u for u, t in L if _is_bip(u, t)))
            if not bips:                                  # try bip.<domain>
                bips = [f"https://bip.{urlparse(base).netloc.lower().removeprefix('www.')}/"]
            for bip in bips[:2]:
                try:
                    bbase, BL = (base, L) if bip == base else _get_links(fetch, bip)
                except Exception:
                    continue
                out.update(bip=bbase, how="no sale pages found")
                pages = sections(BL)
                for u, t in ([] if pages else BL):        # one level down: "ogłoszenia", "komunikaty"
                    if not re.search(r"ogloszeni|komunikat|obwieszcz", t) or SECTION_BAD.search(t):
                        continue
                    try:
                        pages = sections(_get_links(fetch, u)[1])
                    except Exception:
                        continue
                    if pages:
                        break
                if pages:
                    return {"web": base, "bip": bbase, "pages": pages, "how": "auto"}
            break
    return out


def discover(fetch, http, offices_: dict[str, dict], known: dict | None = None, redo: bool = False,
             today: dt.date | None = None) -> dict[str, dict]:
    """Section pages for every office, kept in `known`: offices without pages are tried again after RETRY_DAYS,
    every office with redo=True; pages set by hand (how "manual") are kept."""
    known = dict(known or {})
    today = today or dt.date.today()
    web = websites(http)

    def stale(v):
        return not v.get("pages") and v.get("how") != "manual" and \
            (today - dt.date.fromisoformat(v.get("checked") or "2000-01-01")).days >= RETRY_DAYS
    todo = [k for k in offices_ if k not in known or stale(known[k]) or (redo and known[k].get("how") != "manual")]

    def one(k):
        terc = offices_[k]["terc"]
        if not web.get(terc):
            v = {"terc": terc, "web": "", "bip": "", "pages": [], "how": "no website on Wikidata"}
        else:
            v = {"terc": terc, **discover_office(fetch, web[terc])}
        return k, {**v, "checked": today.isoformat()}
    with ThreadPoolExecutor(WORKERS) as pool:
        for i, (k, v) in enumerate(pool.map(one, todo), 1):
            known[k] = v
            if i % 50 == 0:
                log.info("Tenders: discovered %d/%d offices", i, len(todo))
    log.info("Tenders: %d of %d offices with sale pages", sum(bool(known[k]["pages"]) for k in offices_), len(offices_))
    return known


# ---------------------------------------------------------------- reading notices
def notice_links(section_url: str, html: str) -> list[tuple[str, str]]:
    """Notices on a section page: links whose text or address speaks of a tender, a sale or a list of properties,
    on the same site or attachments; at most MAX_NOTICES, in page order."""
    host = urlparse(section_url).netloc
    out, seen = [], {section_url}
    for url, t in links(section_url, html):
        if url in seen or SECTION_BAD.search(t):
            continue
        if (NOTICE.search(t) or NOTICE.search(fold(url))) and (urlparse(url).netloc == host or ATTACHMENT.search(url)):
            seen.add(url)
            out.append((url, t))
    return out[:MAX_NOTICES]


def read_notice(fetch, url: str) -> tuple[str, str]:
    """(title, text) of a notice page with its PDF / DOCX attachments."""
    base, body = fetch(url)
    text = page_text(base, body)
    title = ""
    if body[:5] != b"%PDF-" and body[:2] != b"PK":
        html = decode(body)
        m = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", html) or re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
        title = re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))).strip() if m else ""
        for a in [u for u, _ in links(base, html) if ATTACHMENT.search(u)][:MAX_ATTACHMENTS]:
            try:
                text += "\n" + page_text(*fetch(a))
            except Exception as e:
                log.debug("attachment %s: %s", a, e)
    return title, text


def _parcel_pattern(no: str):
    """The parcel number shortly after "działka" / "dz." / "nr": "działka nr 136", "dz. ewid. nr 1250/1"."""
    n = re.escape(no.strip())
    return re.compile(rf"(dzialk\w*|dz\.|nr ewid\w*|numer\w*|nr)\W{{1,4}}(?:[^.\n]{{0,60}}?\W)?{n}(?![\d/])")


def _place(v: str) -> str:
    return fold(re.sub(r"^\d+[a-zA-Z]?\s*[-–.]?\s*", "", v or "")).strip()


def match_text(text: str, site: dict) -> str:
    """'parcel' (the precinct or village named and the parcel number after 'działka'), 'village' (the village and a
    manor word) or ''."""
    t = fold(text)
    places = [p for p in {_place(site.get("precinct")), _place(site.get("village"))} if len(p) >= 3]
    if not any(re.search(rf"\b{re.escape(p)}\b", t) for p in places):
        return ""
    no = fold(site.get("parcel_no") or "")
    if no and _parcel_pattern(no).search(t):
        return "parcel"
    return "village" if MANOR.search(t) else ""


def tender_date(text: str) -> str:
    """The tender's date ("przetarg odbędzie się w dniu 15 grudnia 2026 r." / "dnia 15.12.2026"), ISO, or ""."""
    for m in re.finditer(r"(odbedzie sie|w dniu|dnia)\s+(\d{1,2})[.\s/-]+(\d{1,2}|[a-z]+)[.\s/-]+(\d{4})", fold(text)):
        d, mo, y = m.group(2), m.group(3), m.group(4)
        mo = int(mo) if mo.isdigit() else MONTHS.get(mo)
        try:
            return dt.date(int(y), int(mo), int(d)).isoformat()
        except (TypeError, ValueError):
            continue
    return ""


def asking_price(text: str) -> int | None:
    """The opening price ("cena wywoławcza ... 750 000,00 zł") in złoty, or None."""
    m = re.search(r"cen\w* wywolawcz\w*[^0-9]{0,60}?(\d[\d\s. ]*(?:,\d{2})?)\s*zl", fold(text))
    if not m:
        return None
    v = re.sub(r"[\s. ]", "", m.group(1)).split(",")[0]
    return int(v) if v.isdigit() and int(v) >= 1000 else None


def check(fetch, known: dict[str, dict], offices_: dict[str, dict], sites: list[dict],
          today: dt.date | None = None) -> list[dict]:
    """Read every office's sale pages and notices; one entry per (notice, site) that matches:
    {"id", "office", "site" (dossier ID), "uid", "url", "title", "match", "result", "date", "price_pln", "seen"}."""
    from ..grades import dossier_id
    today = today or dt.date.today()
    by_uid = {s["uid"]: s for s in sites}

    def one(k):
        pages = (known.get(k) or {}).get("pages") or []
        mine = [by_uid[u] for u in offices_[k]["sites"] if u in by_uid]
        places = {p for s in mine for p in (_place(s.get("precinct")), _place(s.get("village"))) if len(p) >= 3}
        notices = []
        for p in pages:
            try:
                base, body = fetch(p, cache=False)
            except Exception as e:
                log.debug("Tenders: %s unreachable (%s)", p, e)
                continue
            notices += [n for n in notice_links(base, decode(body)) if n not in notices]
        named = [n for n in notices if MANOR.search(n[1]) or any(pl in n[1] for pl in places)]
        out, read = [], 0
        for url, link_text in named + [n for n in notices if n not in named][:NEWEST]:
            try:
                title, text = read_notice(fetch, url)
            except Exception as e:
                log.debug("Tenders: %s: %s", url, e)
                continue
            read += 1
            head = fold(f"{title} {link_text}")
            if LEASE.search(head) and not SALE.search(head):     # a lease, not a sale
                continue
            for s in mine:
                how = match_text(text, s)
                if how:
                    out.append({"id": "tender-" + hashlib.sha1(f"{url}|{s['uid']}".encode()).hexdigest()[:10],
                                "office": k, "site": dossier_id(s["uid"]), "uid": s["uid"], "url": url,
                                "title": (title or link_text)[:200], "match": how,
                                "result": bool(RESULT.search(fold(f"{title} {link_text} {text[:600]}"))),
                                "date": tender_date(text), "price_pln": asking_price(text),
                                "seen": today.isoformat()})
        return out, read
    found, read = [], 0
    with ThreadPoolExecutor(WORKERS) as pool:
        for out, n in pool.map(one, list(offices_)):
            found += out
            read += n
    log.info("Tenders: %d notices read from %d offices, %d matches (%d by parcel)", read,
             sum(bool((known.get(k) or {}).get("pages")) for k in offices_), len(found),
             sum(f["match"] == "parcel" for f in found))
    return found


# ---------------------------------------------------------------- results
def load_found(root: Path) -> list[dict]:
    p = Path(root) / "_tenders" / "found.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def merge_found(old: list[dict], new: list[dict]) -> list[dict]:
    """Keep when a match was first seen ("first"); matches whose notice is no longer listed drop out."""
    first = {f["id"]: f.get("first") or f.get("seen") for f in old}
    return [{**f, "first": first.get(f["id"]) or f["seen"]} for f in new]


def as_sales(found: list[dict]) -> list[dict]:
    """Parcel matches that announce a sale (not a result) as gutshaus.sales entries, one per site: shown until the
    tender date + SALE_SHOW_DAYS, or TENDER_SHOW_DAYS after first seen when the notice gives no date."""
    out, seen = [], set()
    for f in sorted(found, key=lambda f: f.get("date") or "", reverse=True):
        if f["match"] != "parcel" or f.get("result") or f["uid"] in seen:
            continue
        seen.add(f["uid"])
        e = {"id": f["id"], "source": "tender", "site": f["site"], "url": f["url"], "title": f["title"],
             "date": f.get("date") or "", "price_pln": f.get("price_pln")}
        if not e["date"]:
            first = dt.date.fromisoformat((f.get("first") or f["seen"])[:10])
            e["until"] = (first + dt.timedelta(days=config.TENDER_SHOW_DAYS)).isoformat()
        out.append(e)
    return out
