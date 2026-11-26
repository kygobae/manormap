"""ManorMap command line.

  gutshaus osm-extract                download + extract the Poland OSM file once (recommended)
  gutshaus check                      test the data services from this machine
  gutshaus run --powiat 3209          one powiat (TERYT code)
  gutshaus run --all                  every powiat in Poland (resumable)
  gutshaus run --voivodeship 32,08    only these voivodeships
  gutshaus build                      combine all finished powiats into output/poland
  gutshaus powiats                    list powiat codes
  gutshaus publish                    public website for selling dossiers
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from . import config, criteria, outputs, pipeline
from .geo import geom_to_ll
from .http import Http
from .sources import gdos, kowr, nid, nid_map, osm, osm_local, uldk

log = logging.getLogger("gutshaus")


# ------------------------------------------------------------------ one area
def run_area(args, out: Path, http: Http, src, teryt: str | None = None,
             bbox: tuple | None = None) -> list[dict]:
    """src: osm.OverpassOSM or osm_local.LocalOSM."""
    out.mkdir(parents=True, exist_ok=True)
    if bbox:
        area, title = osm.bbox_polygon(bbox), "bbox " + ",".join(map(str, bbox))
    else:
        area, name = src.boundary(teryt)
        title = f"{name} ({teryt})"
    src.prepare(area)
    log.info("Search area: %s, %.0f km²", title, area.area / 1e6)

    # candidates
    cands = src.candidates(area)
    if args.nid_file:
        cands += nid.to_candidates(nid.read_file(args.nid_file), area)
    elif not args.no_nid:
        powiat_name = args.nid_powiat or (None if bbox else title.split(" (")[0].replace("powiat ", ""))
        if powiat_name:
            try:
                recs = nid.api_estate_records(nid.fetch_api(http, powiat_name))
                cands += src.locate_records(recs, area)
                if not getattr(args, "no_nid_map", False):   # where NID's map draws the village-only entries
                    nid_map.pin_village_entries(http, cands)
            except Exception as e:
                log.warning("NID API failed (%s); continuing with OSM only", e)
        else:
            log.warning("With --bbox, pass --nid-powiat <name> to include the monument register")
    if not cands:
        log.warning("No candidates in %s", title)

    # merge, measure, assess
    offers = load_kowr(args, http)
    kowr.pin_candidates(cands, offers)               # register entries NID draws on / next to an offer
    sites = pipeline.merge(cands)
    kowr.place_sites(sites, offers, area)          # KOWR's parcels locate village-only entries (or add a site)
    if args.limit:
        sites = sites[: args.limit]
    measure = bool(sites) and not args.skip_infra
    ctx = src.context(sites) if sites else {}
    infra = src.infrastructure(sites) if measure else {}
    protected = gdos.fetch(http, area) if measure else None
    town_list = src.towns(area) if measure else None
    assessed = []
    for i, s in enumerate(sites, 1):
        parcel = None if args.skip_uldk else uldk.parcel_at(http, s["x"], s["y"])
        if i % 25 == 0:
            log.info("Parcels %d/%d", i, len(sites))
        a = pipeline.assess(s, ctx.get(s["uid"]), parcel, infra.get(s["uid"]), protected, town_list)
        a.update(pipeline.links(a))
        a["powiat"] = title.split(" (")[0].replace("powiat ", "")
        a["voivodeship"] = config.VOIVODESHIPS.get((teryt or a.get("parcel_id") or "")[:2], a.get("voivodeship", ""))
        assessed.append(a)
    if offers and kowr.match(assessed, offers):
        for a in assessed:  # re-score with the "offered for sale" criterion
            if a.get("kowr_offer"):
                a.update(criteria.compute(a))
                a["score"], a["why"] = criteria.score(a), criteria.explain(a)
    outputs.rank(assessed)

    # outputs
    ser = [outputs.serialise(s) for s in assessed]
    outputs.apply_verdicts(ser, outputs.load_verdicts(out, Path(args.verdicts) if args.verdicts else None))
    area_ll = geom_to_ll(area)
    meta = {"title": title, "teryt": teryt, "area": area_ll.simplify(0.0005).__geo_interface__,
            "finished": time.strftime("%Y-%m-%d %H:%M")}
    outputs.save_sites(assessed, meta, out / "sites.json")
    outputs.save_summary(ser, meta, out / "summary.json")
    outputs.write_csv(ser, out / "candidates.csv")
    outputs.write_gpkg(ser, area_ll, out / "candidates.gpkg")
    root = Path(args.out_root)
    index_href = "../../index.html" if out.resolve().parent == (root / "powiats").resolve() else None
    local = [o for o in offers if o.get("x") is not None and area.buffer(5000).contains(osm.Point(o["x"], o["y"]))]
    outputs.write_html(ser, title, out / "review.html", [area_ll], load_poland(src, root), index_href, local)
    counts = {k: sum(1 for s in ser if s["status"] == k) for k in ("lost", "ruin", "standing", "unlocated", "unknown")}
    print(f"{len(ser)} sites in {title}: " + ", ".join(f"{v} {k}" for k, v in counts.items()), flush=True)
    return ser


def mark_houses(args, root: Path, sites: list[dict]) -> None:
    """Big buildings from the local OSM extract (skipped without it: the public API is too slow for all of Poland)."""
    gpkg = Path(getattr(args, "osm", None) or root / "_osm" / "osm.gpkg")
    if not gpkg.exists():
        log.info("No local OSM extract: buildings of standing sites not marked")
        return
    import numpy as np
    import pyogrio
    b = pyogrio.read_dataframe(gpkg, layer="buildings", where=f"area_m2 >= {config.MANOR_BUILDING_MIN_M2}")
    n = pipeline.mark_houses(sites, np.column_stack([b.geometry.x, b.geometry.y]), b["area_m2"].values)
    log.info("Buildings marked for %d standing / unknown sites", n)


def services(args, root: Path, sites: list[dict]) -> None:
    """Nearest supermarket, school, doctor, station and city for each located site (local OSM extract)."""
    gpkg = Path(getattr(args, "osm", None) or root / "_osm" / "osm.gpkg")
    if not gpkg.exists() or "pois" not in {n for n, _ in __import__("pyogrio").list_layers(gpkg)}:
        log.info("No POIs in the local OSM extract: location from the nearest town only")
        return
    import numpy as np
    import pandas as pd
    import pyogrio
    pois = pd.concat([pyogrio.read_dataframe(gpkg, layer=layer, columns=["kind", "name"])
                      for layer in ("pois", "poi_areas")], ignore_index=True)
    places = {k: (np.column_stack([g.geometry.x, g.geometry.y]), g["name"].values)
              for k, g in pois.groupby("kind") if k in ("shop", "school", "doctor", "station")}
    city = pyogrio.read_dataframe(gpkg, layer="places", where="place = 'city'")
    places["city"] = (np.column_stack([city.geometry.x, city.geometry.y]), city["name"].values)
    n = pipeline.nearest_services(sites, places)
    log.info("Services measured for %d sites", n)


def current_uses(args, root: Path, sites: list[dict]) -> None:
    """What each house is used for today: OSM, the register's "ob. …", and schools or clinics at the house
    (local OSM extract; without it the first two)."""
    gpkg = Path(getattr(args, "osm", None) or root / "_osm" / "osm.gpkg")
    pois = None
    import pyogrio
    if gpkg.exists() and "pois" in {n for n, _ in pyogrio.list_layers(gpkg)}:
        import numpy as np
        import pandas as pd
        kinds = ",".join(f"'{k}'" for k in config.USE_POI_KINDS)
        df = pd.concat([pyogrio.read_dataframe(gpkg, layer=layer, columns=["kind", "name"], where=f"kind IN ({kinds})")
                        for layer in ("pois", "poi_areas")], ignore_index=True)
        pois = {k: (np.column_stack([g.geometry.x, g.geometry.y]), g["name"].values) for k, g in df.groupby("kind")}
    n = pipeline.current_uses(sites, pois)
    log.info("Current use known for %d sites (%s)", sum(n.values()), ", ".join(f"{k} {v}" for k, v in n.items()))


def count_buildings(args, root: Path, sites: list[dict], offers: list[dict]) -> None:
    """Buildings on each parcel from the local OSM extract (skipped without it)."""
    gpkg = Path(getattr(args, "osm", None) or root / "_osm" / "osm.gpkg")
    if not gpkg.exists():
        return
    import numpy as np
    import pyogrio

    def read(bbox):
        b = pyogrio.read_dataframe(gpkg, layer="buildings", bbox=tuple(bbox),
                                   where=f"area_m2 >= {config.BUILDING_ON_PARCEL_M2}")
        return (np.column_stack([b.geometry.x, b.geometry.y]) if len(b) else []), (b["area_m2"].values if len(b) else [])
    areas = {o.get("uid") or o["url"]: a for o in offers or [] if (a := kowr.offer_area(o)) is not None}
    n = pipeline.count_buildings(sites, read, areas)
    log.info("Buildings counted on %d parcels", n)


def load_kowr(args, http: Http) -> list[dict]:
    """KOWR heritage offers, cached for a day in <out-root>/_kowr.json."""
    if getattr(args, "skip_kowr", False):
        return []
    return kowr.load_or_fetch(http, Path(args.out_root) / "_kowr.json", refresh=getattr(args, "refresh", False))


def make_osm(args, http: Http):
    """Local extract if available (fast), otherwise the public Overpass API."""
    gpkg = Path(args.osm) if args.osm else Path(args.out_root) / "_osm" / "osm.gpkg"
    if not args.overpass and gpkg.exists():
        log.info("OSM data: local extract %s", gpkg)
        return osm_local.LocalOSM(gpkg)
    if args.osm and not gpkg.exists():
        raise SystemExit(f"{gpkg} not found: run `gutshaus osm-extract` first")
    log.info("OSM data: public Overpass API (run `gutshaus osm-extract` once for faster, offline runs)")
    return osm.OverpassOSM(http)


def load_poland(src, root: Path) -> dict | None:
    """Poland outline for the minimap, cached in <out-root>/_poland.json."""
    p = Path(root) / "_poland.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    try:
        geo = src.poland_outline()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(geo), encoding="utf-8")
        return geo
    except Exception as e:
        log.warning("Poland outline unavailable for the minimap: %s", e)
        return None


# ------------------------------------------------------------------ commands
def cmd_run(args):
    root = Path(args.out_root)
    outputs.migrate_layout(root)
    if args.all or args.voivodeship:
        return run_many(args, root)
    if args.bbox:
        bbox = tuple(float(v) for v in args.bbox.split(","))
        out = Path(args.out or root / "bbox")
        http = Http(out / ".cache", refresh=args.refresh)
        run_area(args, out, http, make_osm(args, http), bbox=bbox)
    else:
        out = Path(args.out or root / "powiats" / args.powiat)
        http = Http(out / ".cache", refresh=args.refresh)
        run_area(args, out, http, make_osm(args, http), teryt=args.powiat)
        if not args.out:
            outputs.write_index(root)
    print(f"Open {out / 'review.html'} (map), {out / 'candidates.csv'} (sheet) or {out / 'candidates.gpkg'} (QGIS).")


def run_many(args, root: Path):
    http = Http(root / ".cache", refresh=False)
    src = make_osm(args, http)
    pw = src.powiat_list()
    wanted = set(args.voivodeship.split(",")) if args.voivodeship else None
    order = {v: i for i, v in enumerate(config.VOIVODESHIP_ORDER)}
    todo = sorted([p for p in pw if not wanted or p[0][:2] in wanted],
                  key=lambda p: (order.get(p[0][:2], 99), p[0]))
    log.info("%d powiats selected", len(todo))
    failed = []
    for n, (code, name) in enumerate(todo, 1):
        out = root / "powiats" / code
        if (out / "sites.json").exists() and not args.refresh:
            continue
        log.info("=== [%d/%d] %s %s ===", n, len(todo), code, name)
        try:
            phttp = Http(out / ".cache", refresh=args.refresh)
            run_area(args, out, phttp, src if isinstance(src, osm_local.LocalOSM) else make_osm(args, phttp), teryt=code)
            outputs.write_index(root, total=len(todo))
        except Exception as e:
            log.error("Powiat %s failed: %s", code, e)
            failed.append(code)
    (root / "failed.txt").write_text("\n".join(failed), encoding="utf-8")
    if failed:
        log.warning("%d powiats failed (see %s); run the same command again to retry them.",
                    len(failed), root / "failed.txt")
    cmd_build(args)
    outputs.write_index(root, total=len(todo), failed=failed)


def cmd_build(args):
    """Combine every <out-root>/<teryt>/sites.json into <out-root>/poland."""
    root = Path(args.out_root)
    outputs.migrate_layout(root)
    files = sorted((root / "powiats").glob("*/sites.json"))
    if not files:
        print("Nothing to build yet: run some powiats first.")
        return
    sites, seen, previous = [], set(), []
    for f in files:
        _, ss = outputs.load_sites(f)
        previous += outputs.load_verdicts(f.parent)
        for s in ss:
            if s["uid"] in seen:  # sites near powiat borders can appear twice
                continue
            seen.update([s["uid"], *s.get("members", [])])
            sites.append(s)
    http = Http(root / ".cache")
    offers = load_kowr(args, http)
    for s in sites:
        for k in ("kowr_offer", "kowr_url", "kowr_tender", "kowr_uid", "kowr_kw", "kowr_monuments", "offer_ha",
                  "kowr_gone"):
            s.pop(k, None)
    if offers:
        kowr.match(sites, offers)
    from . import sales as sales_mod                     # auctions, tenders, owners' listings (config.SALES_FILE)
    from .sources import tenders
    sales = sales_mod.load(extra=tenders.as_sales(tenders.load_found(root)))   # + public owners' tenders
    if sales:
        log.info("Sales: %d current, %d on a site", len(sales), sales_mod.match(sites, sales))
    gone = kowr.recently_gone(kowr.history_path(root / "_kowr.json"), offers) if not getattr(args, "skip_kowr", False) else []
    if gone:                                             # dropped off KOWR's lists lately: probably sold
        log.info("KOWR: %d offers gone in the last %d days, %d sites marked", len(gone), config.KOWR_GONE_DAYS,
                 kowr.match(sites, gone, gone=True))
    n_fixed = apply_status_overrides(sites, config.STATUS_OVERRIDES)
    mark_houses(args, root, sites)                       # which building a "standing" state rests on (map marker)
    from .sources import uldk                            # ... and its parcel is the site's parcel (cached lookups)
    n = pipeline.house_parcels(sites, lambda x, y: uldk.parcel_at(http, x, y))
    log.info("%d standing sites moved to the parcel their house stands on", n)
    count_buildings(args, root, sites, offers)           # buildings on the parcel: somewhere to live while rebuilding
    services(args, root, sites)                          # supermarket, school, doctor, city: the location light
    current_uses(args, root, sites)                      # a school, offices, a foundation ...: not for a private buyer
    if not getattr(args, "no_owner", False):             # who owns the parcel, by type (land register, cached)
        from .sources import egib
        egib.annotate(http, sites)
    if not getattr(args, "no_forest", False):            # forest stands from the forest database (cached)
        from .sources import bdl
        bdl.annotate(http, sites)
    if offers or n_fixed or sales:
        for s in sites:
            s.update(criteria.compute(s))
            s["score"], s["why"] = criteria.score(s), criteria.explain(s)
    from .grades import unlisted_reason
    for s in sites:                                      # not in private hands: left off the public map
        s["unlisted"] = unlisted_reason(s)
    log.info("%d sites not in private hands (left off the public map)", sum(bool(s["unlisted"]) for s in sites))
    outputs.rank(sites)
    out = root / "poland"
    out.mkdir(parents=True, exist_ok=True)
    previous += outputs.load_verdicts(out, Path(args.verdicts) if getattr(args, "verdicts", None) else None)
    outputs.apply_verdicts(sites, previous)
    outputs.write_csv(sites, out / "candidates.csv")
    outputs.save_sites(sites, {"title": "Poland", "powiats": len(files)}, out / "sites.json")
    outputs.write_gpkg(sites, None, out / "candidates.gpkg")
    outputs.write_html(sites, f"Poland ({len(files)} powiats)", out / "review.html", [],
                       load_poland(make_osm(args, http), root), "../index.html", offers)
    outputs.write_index(root)
    print(f"{len(sites)} sites from {len(files)} powiats -> {out / 'review.html'}", flush=True)
    print(f"Start page: {root / 'index.html'}", flush=True)


def apply_status_overrides(sites: list[dict], overrides: dict) -> int:
    """config.STATUS_OVERRIDES: the state of a site as known on the ground (dossier ID or uid -> status, note)."""
    from .grades import dossier_id
    if not overrides:
        return 0
    n = 0
    for s in sites:
        o = overrides.get(dossier_id(s["uid"])) or overrides.get(s["uid"])
        if not o:
            continue
        o = {"status": o} if isinstance(o, str) else o
        if o.get("status"):
            s["status_data"] = s.get("status_data") or s["status"]      # what the data said
            s["status"] = o["status"]
        s["status_note"] = o.get("note", "")
        n += 1
    if n:
        log.info("Status corrected for %d site(s) from STATUS_OVERRIDES", n)
    return n


def cmd_powiats(args):
    http = Http(Path(args.out_root) / ".cache")
    for code, name in make_osm(args, http).powiat_list():
        print(code, name, config.VOIVODESHIPS.get(code[:2], ""))


def cmd_osm_extract(args):
    """Download the Geofabrik Poland extract (if needed) and extract the layers we use."""
    d = Path(args.out_root) / "_osm"
    pbf = Path(args.pbf) if args.pbf else d / "poland-latest.osm.pbf"
    if not pbf.exists():
        log.info("Downloading %s (about 2 GB) to %s", osm_local.GEOFABRIK_URL, pbf)
        osm_local.download(pbf)
    cache = d / "nodes.cache"
    t0 = time.time()
    counts = osm_local.extract(pbf, d / "osm.gpkg", node_cache=cache if pbf.stat().st_size > 50e6 else None)
    try:
        cache.unlink(missing_ok=True)
    except OSError as e:
        log.warning("Could not delete the temporary %s (%s); you can delete it by hand.", cache, e)
    print(f"Done in {(time.time() - t0) / 60:.0f} min: {d / 'osm.gpkg'} {counts}")
    print("Runs now use this file automatically. Refresh it now and then: delete the .pbf and run osm-extract again.")


def cmd_check(args):
    """Test every external service and WMS layer name from this machine."""
    import xml.etree.ElementTree as ET

    from shapely.geometry import box

    from .geo import ll_to_pl
    http = Http(None)
    ok = True

    def report(name, good, detail=""):
        nonlocal ok
        ok &= good
        print(f"{'OK  ' if good else 'FAIL'} {name} {detail}", flush=True)

    try:
        d = osm.Overpass(http).query('[out:json];node(1);out;')
        report("Overpass", "elements" in d)
    except Exception as e:
        report("Overpass", False, str(e)[:120])
    try:
        p = uldk.parcel_at(http, *ll_to_pl(16.18, 54.19))
        report("ULDK", p is not None, p["parcel_id"] if p else "no parcel returned")
    except Exception as e:
        report("ULDK", False, str(e)[:120])
    try:
        rows = nid.fetch_api(http, "koszaliński")
        report("NID register API", bool(rows), f"{len(rows)} entries in powiat koszaliński")
    except Exception as e:
        report("NID register API", False, str(e)[:120])
    try:
        (x1, y1), (x2, y2) = ll_to_pl(15.9, 54.0), ll_to_pl(16.4, 54.3)
        areas = gdos.fetch(http, box(x1, y1, x2, y2))
        report("GDOŚ protected areas WFS", bool(areas), f"{len(areas)} areas around Koszalin")
    except Exception as e:
        report("GDOŚ protected areas WFS", False, str(e)[:120])
    for w in config.WMS_LAYERS:
        try:
            raw = http.get(w["url"], params={"SERVICE": "WMS", "REQUEST": "GetCapabilities"}, retries=1)
            names = [el.text for el in ET.fromstring(raw).iter() if el.tag.endswith("Name") and el.text]
            missing = [x for x in w["layers"].split(",") if x not in names]
            report(f"WMS {w['name']}", not missing,
                   f"missing {missing}; available: {names[:15]}" if missing else "")
        except Exception as e:
            report(f"WMS {w['name']}", False, str(e)[:120])
    print("\nAll good." if ok else "\nFix FAIL lines in gutshaus/config.py (layer names) or see README.")


def cmd_tenders(args):
    """Watch the BIPs of the public bodies that own manors for tenders (sources/tenders.py)."""
    import csv
    from .grades import all_sites
    from .sources import tenders
    root = Path(args.out_root)
    sites = all_sites(root)
    if not sites:
        raise SystemExit("No results yet: run `gutshaus run` and `gutshaus build` first.")
    offs = tenders.offices(sites)
    d = root / "_tenders"
    d.mkdir(parents=True, exist_ok=True)
    kp = d / "offices.json"
    known = json.loads(kp.read_text(encoding="utf-8")) if kp.exists() else {}
    fetch = tenders.Fetcher(d / "cache")
    known = tenders.discover(fetch, Http(root / ".cache"), offs, known, redo=args.rediscover)
    manual = Path(config.TENDER_PAGES_FILE)
    for k, pages in (json.loads(manual.read_text(encoding="utf-8")) if manual.exists() else {}).items():
        known[k] = {**known.get(k, {"terc": k.split(":")[-1], "web": "", "bip": ""}), "pages": pages, "how": "manual"}
    kp.write_text(json.dumps(known, ensure_ascii=False, indent=1), encoding="utf-8")
    with (d / "missing.csv").open("w", newline="", encoding="utf-8-sig") as f:   # offices to set by hand
        w = csv.writer(f)
        w.writerow(["office", "manors", "website", "bip", "why"])
        for k, o in sorted(offs.items(), key=lambda x: -len(x[1]["sites"])):
            v = known.get(k) or {}
            if not v.get("pages"):
                w.writerow([k, len(o["sites"]), v.get("web", ""), v.get("bip", ""), v.get("how", "")])
    n_pages = sum(bool((known.get(k) or {}).get("pages")) for k in offs)
    print(f"{len(offs)} public owners of manors, {n_pages} with sale pages ({d / 'missing.csv'} lists the rest)")
    if args.discover_only:
        return
    found = tenders.merge_found(tenders.load_found(root), tenders.check(fetch, known, offs, sites))
    (d / "found.json").write_text(json.dumps(found, ensure_ascii=False, indent=1), encoding="utf-8")
    for label, keep in (("On the map (parcel named)", lambda f: f["match"] == "parcel" and not f["result"]),
                        ("Results of past tenders (not shown)", lambda f: f["result"]),
                        ("To review (village and a manor word only)", lambda f: f["match"] == "village" and not f["result"])):
        rows = [f for f in found if keep(f)]
        print(f"\n{label}: {len(rows)}")
        for f in rows:
            print(f"  {f['site']}  {f.get('date') or '-':10}  {f['title'][:70]}\n      {f['url']}")
    print(f"\nRun `gutshaus publish` (and deploy) to show them. Details: {d / 'found.json'}")


def cmd_publish(args):
    from . import grades, publish
    root = Path(args.out_root)
    sites = grades.all_sites(root)
    if not sites:
        raise SystemExit("No results yet: run `gutshaus run` (and `gutshaus build`) first.")
    reserved = publish.load_reserved(Path(args.reserved or config.RESERVED_FILE))
    out = Path(args.out or root / "site")
    from . import sales
    from .sources import tenders
    current = sales.load(extra=tenders.as_sales(tenders.load_found(root)))   # matched again: new sales show at once
    if current:
        log.info("Sales: %d current, %d on a site", len(current), sales.match(sites, current))
    n = publish.publish(sites, out, reserved, root / "_kowr.json", Path(args.sample) if args.sample else None,
                        sales=current)
    print(f"{n['villages']} villages, {n['sites']} sites ({len(reserved)} reserved) -> {out / 'public' / 'index.html'}")
    if config.SHOP_ENABLED and not config.CHECKOUT_URL:
        print("Note: SHOP_ENABLED is on but CHECKOUT_URL is not set, so buy buttons fall back to e-mail.")


def main(argv=None):
    # Polish names in output: when stdout/stderr go to a file, Windows defaults to cp1252, which cannot encode "ł"
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="gutshaus", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="JSON overrides (default: ./gutshaus.json if present)")
    p.add_argument("--out-root", default="output", help="root output folder (default: output)")
    p.add_argument("--osm", help="OSM GeoPackage from osm-extract (default: <out-root>/_osm/osm.gpkg if present)")
    p.add_argument("--overpass", action="store_true", help="use the public Overpass API even if a local extract exists")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="find candidate sites")
    where = r.add_mutually_exclusive_group()
    where.add_argument("--powiat", default=config.DEFAULT_POWIAT, help="4-digit TERYT, e.g. 3209 = koszaliński")
    where.add_argument("--bbox", help="min_lon,min_lat,max_lon,max_lat")
    where.add_argument("--all", action="store_true", help="every powiat in Poland (skips finished ones)")
    where.add_argument("--voivodeship", help="comma-separated voivodeship codes, e.g. 32,08")
    r.add_argument("--nid-file", help="register file with geometry (SHP/GPKG/GeoJSON/GML/GeoParquet/CSV)")
    r.add_argument("--no-nid", action="store_true", help="OSM only")
    r.add_argument("--nid-powiat", help="powiat name for the register API (default: from the boundary)")
    r.add_argument("--skip-uldk", action="store_true", help="no parcel lookup (faster)")
    r.add_argument("--skip-infra", action="store_true", help="no utility/road/protection measurements (faster)")
    r.add_argument("--skip-kowr", action="store_true", help="don't load KOWR heritage offers")
    r.add_argument("--no-nid-map", action="store_true",
                   help="don't look up where NID's map draws register entries known only by their village")
    r.add_argument("--limit", type=int, help="only the first N sites per area (testing)")
    r.add_argument("--out", help="output folder for a single area (default: <out-root>/powiats/<powiat>)")
    r.add_argument("--verdicts", help="earlier verdicts CSV to carry over")
    r.add_argument("--refresh", action="store_true", help="ignore the download cache / redo finished powiats")

    b = sub.add_parser("build", help="combine finished powiats into <out-root>/poland and refresh index.html")
    b.add_argument("--no-forest", action="store_true", help="skip the forest database (BDL) lookups")
    b.add_argument("--no-owner", action="store_true", help="skip the owner-type lookups (land register groups)")
    b.add_argument("--verdicts", help="earlier verdicts CSV to carry over")
    b.add_argument("--skip-kowr", action="store_true", help="don't load KOWR heritage offers")
    b.add_argument("--refresh", action="store_true", help="reload KOWR offers now")
    x = sub.add_parser("osm-extract", help="download + extract the Poland OSM file (once; ~2 GB, ~1 h)")
    x.add_argument("--pbf", help="use this .osm.pbf instead of downloading Geofabrik's poland-latest")
    pb = sub.add_parser("publish", help="write the public village-level website (<out-root>/site)")
    pb.add_argument("--reserved", help="IDs to leave out (default: RESERVED_FILE in the config, reserved.txt)")
    pb.add_argument("--sample", help="PDF to offer as sample dossier")
    pb.add_argument("--out", help="folder (default: <out-root>/site)")
    tn = sub.add_parser("tenders", help="look for tenders of the public bodies that own manors (weekly)")
    tn.add_argument("--discover-only", action="store_true", help="only find each office's sale pages")
    tn.add_argument("--rediscover", action="store_true", help="find the sale pages again for every office")
    sub.add_parser("check", help="test that all data services are reachable from this machine")
    sub.add_parser("powiats", help="list powiat TERYT codes")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S", force=True)
    logging.getLogger("pyogrio").setLevel(logging.WARNING)
    used = config.load_overrides(args.config)
    if used:
        log.info("Using overrides from %s", used)
    {"run": cmd_run, "build": cmd_build, "check": cmd_check, "powiats": cmd_powiats,
     "osm-extract": cmd_osm_extract, "publish": cmd_publish, "tenders": cmd_tenders}[args.cmd](args)


if __name__ == "__main__":
    main()
