import csv

import pytest

from gutshaus import cli
from gutshaus.classify import is_relevant_osm, text_flags
from gutshaus.sources import uldk

from fake_services import FakeHttp


# ------------------------------------------------------------------ units
@pytest.mark.parametrize("text,estate,park_only,excluded", [
    ("zespół dworsko-parkowy", True, True, False),
    ("park dworski", True, True, False),
    ("dwór, ob. dom mieszkalny", True, False, False),
    ("Pałac w Przykładowie", True, False, False),
    ("kościół fil. pw. MB", False, False, True),
    ("Dworzec Wzorowo", False, False, True),
    ("ul. Dworcowa", False, False, False),
    ("spichlerz folwarczny", True, False, False),
    ("Podwórko Nivea", False, False, False),           # courtyard, not a manor
    ("Zadworze", False, False, False),                 # "behind the manor": a place name
])
def test_text_flags(text, estate, park_only, excluded):
    f = text_flags(text)
    assert f["estate_kw"] == estate
    assert f["park_only"] == park_only
    assert f["excluded"] == excluded


def test_osm_relevance():
    assert is_relevant_osm({"historic": "manor"})
    assert is_relevant_osm({"leisure": "park", "name": "Park pałacowy"})
    assert not is_relevant_osm({"leisure": "park", "name": "Park miejski"})
    assert not is_relevant_osm({"railway": "station", "name": "Dworzec"})
    assert not is_relevant_osm({"historic": "castle", "castle_type": "defensive", "name": "Zamek"})
    # only the name matches: settlements, plants, playgrounds are not estates ...
    assert not is_relevant_osm({"place": "hamlet", "name": "Dwór", "source": "PRNG"})
    assert not is_relevant_osm({"place": "village", "name": "Kuków-Folwark"})
    assert not is_relevant_osm({"building": "industrial", "name": "MEW Dwory"})
    assert not is_relevant_osm({"leisure": "playground", "name": "Dwór zabaw"})
    # ... but explicit tags still win, and a name-only dwór stays a candidate
    assert is_relevant_osm({"place": "isolated_dwelling", "historic": "manor", "name": "Dwór"})
    assert is_relevant_osm({"name": "Dwór Kowalskich", "building": "yes"})


def test_lost_needs_no_big_building_within_200_m():
    from shapely.geometry import Point, box
    from gutshaus.pipeline import classify_status
    from gutshaus.sources.osm import summarise_context
    site = {"text": "park dworski", "name": "park dworski", "location": "osm park near village"}
    pt = Point(0, 0)
    manor_150m = ({"building": "yes"}, box(140, -10, 160, 10))          # 400 m², 140 m away
    shed_50m = ({"building": "yes"}, box(45, -2, 55, 2))                # 40 m²
    ctx = summarise_context(pt, [manor_150m, shed_50m])
    assert ctx["big_buildings"] == [] and ctx["big_buildings_wide"] == 1
    assert classify_status(site, ctx) == "unknown"                       # could be the manor at the park edge
    assert classify_status(site, summarise_context(pt, [shed_50m])) == "lost"
    assert classify_status(site, summarise_context(pt, [({"building": "yes"}, box(80, -10, 100, 10)), shed_50m])) == "standing"


def test_manor_on_or_next_to_the_parcel_counts_as_standing():
    """The site point can be far from the house: a big building on the parcel, or just outside it (parcel split
    around the house), is the manor. Far-away buildings on a huge parcel are ignored."""
    from shapely.geometry import Point, box
    from gutshaus.pipeline import classify_status, manor_on_parcel
    from gutshaus.sources.osm import summarise_context
    site = {"text": "dwór", "name": "dwór", "location": "osm park near village"}
    pt = Point(0, 0)
    parcel = box(-100, -100, 300, 100)                                  # point near the west end
    on_parcel = ({"building": "yes"}, box(240, -10, 260, 10))           # 400 m², 240 m away, on the parcel
    beside = ({"building": "yes"}, box(-50, 115, -30, 135))             # 400 m², 15-35 m north of the boundary
    ctx = summarise_context(pt, [on_parcel])
    assert ctx["big_buildings"] == [] and ctx["big_buildings_wide"] == 0
    assert classify_status(site, ctx) == "lost"                         # without the parcel: nothing nearby
    assert manor_on_parcel(ctx, parcel) == "on_parcel" and classify_status(site, ctx, parcel) == "standing"
    ctx = summarise_context(pt, [beside])
    assert manor_on_parcel(ctx, parcel) == "next_to_parcel" and classify_status(site, ctx, parcel) == "standing"
    far = summarise_context(pt, [({"building": "yes"}, box(380, -10, 400, 10))])          # 390 m: too far to matter
    assert manor_on_parcel(far, box(-100, -100, 500, 100)) == "" and classify_status(site, far, box(-100, -100, 500, 100)) == "lost"


def test_uldk_parse():
    raw = "0\nSRID=2180;POLYGON((0 0,100 0,100 100,0 100,0 0))|320999_2.0001.17|zach|kosz|Wzorowo|Przykładowo|17\n"
    p = uldk.parse(raw)
    assert p["parcel_id"] == "320999_2.0001.17" and p["parcel_ha"] == 1.0 and p["parcel_no"] == "17"
    assert uldk.parse("-1 brak wyników") is None


# ------------------------------------------------------------ end to end
def run(tmp_path, monkeypatch, *extra):
    monkeypatch.setattr(cli, "Http", FakeHttp)
    monkeypatch.setattr("gutshaus.config.ULDK_PAUSE_S", 0)
    monkeypatch.setattr("gutshaus.config.OVERPASS_PAUSE_S", 0)
    monkeypatch.chdir(tmp_path.parent)
    args = list(extra) if "--bbox" in extra else ["--powiat", "3209", *extra]
    cli.main(["--out-root", str(tmp_path.parent / "root"), "run", "--out", str(tmp_path), *args])
    with (tmp_path / "candidates.csv").open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def test_end_to_end(tmp_path, monkeypatch):
    rows = run(tmp_path, monkeypatch)
    names = [r["name"] for r in rows]
    # church and station are filtered out
    assert not any("Kości" in n or "Dworzec" in n for n in names)
    site = next(r for r in rows if "Przykład" in r["name"])
    assert site["name"] == "park dworski, Przykładowo"                   # register name wins
    assert site["status"] == "lost"
    assert "nid" in site["sources"] and "osm" in site["sources"]   # NID (via park) + OSM merged
    assert "1001" in site["nid_ref"] and site["registered"] == "True"
    klos = next(r for r in rows if "Kłos" in r["name"])
    assert klos["status"] == "unlocated" and klos["location"] == "village centre"
    assert not any("Nieistniej" in n for n in names)                  # village not in OSM -> dropped
    assert site["parcel_id"] == "320999_2.0001.17"
    assert site["land_cover"] == "forest"
    strz = next(r for r in rows if "Strzeżenic" in r["name"])
    assert strz["status"] == "standing" and strz["name_de"] == "Streitzig"
    assert strz["registered"] == "True"                                 # register "dwór" attached
    ruin = next(r for r in rows if "Ruiny" in r["name"])
    assert ruin["status"] == "ruin"
    assert rows[0]["status"] in ("lost", "ruin")                     # lost sites rank first
    # buildability measurements
    assert 0 < int(site["dist_power_m"]) < 150 and int(site["dist_road_m"]) < 50
    assert site["protection"] == "landscape park" and float(site["c_protection"]) == 0.4
    assert site["nearest_town"] == "Wzorowo" and site["voivodeship"] == "zachodniopomorskie"
    assert float(site["c_power"]) == 1.0 and site["c_landuse"] == "0.15"
    assert klos["c_power"] == "" and klos["c_gone"] == "0.6"           # unlocated: infrastructure unknown
    assert int(rows[0]["score"]) >= int(strz["score"])
    for f in ("review.html", "candidates.gpkg"):
        assert (tmp_path / f).stat().st_size > 1000
    html = (tmp_path / "review.html").read_text(encoding="utf-8")
    assert "__SITES__" not in html and "Przykład" in html
    assert "tile.openstreetmap.org" not in html and "cartocdn" not in html  # both block file:// pages


def test_verdicts_survive_rerun(tmp_path, monkeypatch):
    rows = run(tmp_path, monkeypatch)
    site = next(r for r in rows if "Przykład" in r["name"])
    # simulate the browser export
    with (tmp_path / "verdicts.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["uid", "members", "verdict", "notes", "lat", "lon", "name"])
        w.writerow([site["uid"], site["members"], "high", "Gut on 1890 sheet", site["lat"], site["lon"], ""])
    rows2 = run(tmp_path, monkeypatch)
    site2 = next(r for r in rows2 if "Przykład" in r["name"])
    assert site2["verdict"] == "high" and site2["notes"] == "Gut on 1890 sheet"


def test_nid_file_and_bbox(tmp_path, monkeypatch):
    from fake_services import BBOX, nid_gml
    p = tmp_path / "register.gml"
    p.write_bytes(nid_gml())
    rows = run(tmp_path / "o", monkeypatch, "--bbox", ",".join(map(str, BBOX)), "--nid-file", str(p), "--skip-uldk")
    assert any("Kłos" in r["name"] for r in rows)


def test_verdicts_flag(tmp_path, monkeypatch):
    rows = run(tmp_path, monkeypatch)
    r0 = rows[0]
    v = tmp_path / "keep.csv"
    v.write_text(f"uid,verdict,notes,lat,lon\n{r0['uid']},skip,no park left,{r0['lat']},{r0['lon']}\n", encoding="utf-8")
    rows2 = run(tmp_path / "fresh", monkeypatch, "--verdicts", str(v))
    assert next(r for r in rows2 if r["uid"] == r0["uid"])["verdict"] == "skip"


def test_criteria_and_weights():
    from gutshaus import criteria
    assert criteria.ramp(100, 150, 2000) == 1.0 and criteria.ramp(2000, 150, 2000) == 0.0
    assert criteria.ramp(1075, 150, 2000) == 0.5 and criteria.ramp(None, 1, 2) is None
    assert criteria.parcel_value(0.05) == 0 and criteria.parcel_value(5) == 1 and criteria.parcel_value(100) == 0.3
    v = {"c_a": 1.0, "c_b": 0.0, "c_c": None}
    assert criteria.score(v, {"a": 1, "b": 1}) == 50
    assert criteria.score(v, {"a": 3, "b": 1}) == 75
    assert criteria.score(v, {"a": 1, "c": 1}) == 75           # unknown counts as 0.5
    assert criteria.score(v, {"a": 0, "b": 0}) == 0


def test_overrides(tmp_path, monkeypatch):
    from gutshaus import config
    p = tmp_path / "gutshaus.json"
    p.write_text('{"weights": {"water": 4}, "USER_AGENT": "test-agent"}', encoding="utf-8")
    monkeypatch.setattr(config, "USER_AGENT", config.USER_AGENT)
    monkeypatch.setattr(config, "CRITERIA", [dict(c) for c in config.CRITERIA])
    assert config.load_overrides(p) == str(p)
    assert config.USER_AGENT == "test-agent"
    assert next(c for c in config.CRITERIA if c["key"] == "water")["weight"] == 4


def test_html_has_controls_and_minimap(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch)
    html = (tmp_path / "review.html").read_text(encoding="utf-8")
    import json, re
    data = json.loads(re.search(r"const D = (\{.*?\});\n", html, re.S).group(1))
    assert {c["key"] for c in data["criteria"]} >= {"power", "road", "protection", "gone"}
    assert data["poland"] and data["areas"] and "Berlin" in data["references"]
    site = next(s for s in data["sites"] if "Przykład" in s["name"])
    assert site["parcel_ll"] and site["c_power"] == 1.0
    for ui in ('id="mini"', 'id="weights"', 'id="filters"', "fVoiv", "fPower", "refName", "KrajowaIntegracjaUzbrojeniaTerenu"):
        assert ui in html


def test_run_all_and_build(tmp_path, monkeypatch):
    from gutshaus.sources import kowr
    from fake_services import KOWR_CARDS
    monkeypatch.setattr(kowr, "pdf_text", lambda raw: KOWR_CARDS[raw.decode()])
    monkeypatch.setattr(cli, "Http", FakeHttp)
    monkeypatch.setattr("gutshaus.config.ULDK_PAUSE_S", 0)
    monkeypatch.setattr("gutshaus.config.OVERPASS_PAUSE_S", 0)
    root = tmp_path / "out"
    cli.main(["--out-root", str(root), "run", "--voivodeship", "32"])
    assert (root / "powiats" / "3209" / "sites.json").exists() and (root / "powiats" / "3206" / "sites.json").exists()
    assert not (root / "powiats" / "1465").exists()                      # other voivodeship skipped
    index = (root / "index.html").read_text(encoding="utf-8")
    import json, re
    data = json.loads(re.search(r"const D = (\{.*?\});\n", index, re.S).group(1))
    assert {r["teryt"] for r in data["rows"]} == {"3209", "3206"} and data["national"] and data["total"] == 2
    assert [o["title"] for o in data["kowr"]] == ["Pałac Przykładowo", "Dwór Nigdzie", "Dwór, Stary Kłos"]
    national = root / "poland" / "candidates.csv"
    with national.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == len({r["uid"] for r in rows}) == 4               # same fixture twice -> deduplicated
    assert (root / "poland" / "review.html").stat().st_size > 5000
    assert '"index": "../index.html"' in (root / "poland" / "review.html").read_text(encoding="utf-8")
    assert '"index": "../../index.html"' in (root / "powiats" / "3209" / "review.html").read_text(encoding="utf-8")
    # a second run skips finished powiats
    FakeHttp.calls.clear()
    cli.main(["--out-root", str(root), "run", "--voivodeship", "32"])
    assert not any("uldk" in c[1] for c in FakeHttp.calls)


def test_cropped_geometry_splits_at_gaps():
    from gutshaus.sources.osm import _lines
    g = [{"lon": 16.0, "lat": 54.0}, {"lon": 16.001, "lat": 54.0}, None, {"lon": 16.01, "lat": 54.0}, {"lon": 16.011, "lat": 54.0}]
    assert len(_lines({"geometry": g})) == 2


def test_local_osm_backend(tmp_path, monkeypatch):
    """osm-extract on a tiny .osm file, then a run that never touches Overpass."""
    from fake_services import osm_fixture_xml
    monkeypatch.setattr(cli, "Http", FakeHttp)
    monkeypatch.setattr("gutshaus.config.ULDK_PAUSE_S", 0)
    root = tmp_path / "out"
    pbf = tmp_path / "test.osm"
    pbf.write_text(osm_fixture_xml(), encoding="utf-8")
    cli.main(["--out-root", str(root), "osm-extract", "--pbf", str(pbf)])
    assert (root / "_osm" / "osm.gpkg").exists()
    import pyogrio
    layers = {name for name, _ in pyogrio.list_layers(root / "_osm" / "osm.gpkg")}
    assert {"candidates", "buildings", "roads", "power", "water", "landcover", "parks", "places", "admin"} <= layers

    FakeHttp.calls.clear()
    cli.main(["--out-root", str(root), "powiats"])
    cli.main(["--out-root", str(root), "run", "--powiat", "3209"])
    assert not any(c[0] == "POST" for c in FakeHttp.calls)             # no Overpass at all
    assert (root / "index.html").exists()
    with (root / "powiats" / "3209" / "candidates.csv").open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    names = [r["name"] for r in rows]
    assert not any("Kości" in n for n in names)
    site = next(r for r in rows if "Przykład" in r["name"])
    assert site["status"] == "lost" and site["land_cover"] == "forest"
    assert "nid" in site["sources"] and "osm" in site["sources"]
    assert 0 < int(site["dist_power_m"]) < 150 and int(site["dist_road_m"]) < 60
    assert site["nearest_town"] == "Wzorowo" and site["protection"] == "landscape park"
    strz = next(r for r in rows if "Strzeżenic" in r["name"])
    assert strz["status"] == "standing" and strz["name_de"] == "Streitzig"
    klos = next(r for r in rows if "Kłos" in r["name"])
    assert klos["status"] == "unlocated"
    html = (root / "powiats" / "3209" / "review.html").read_text(encoding="utf-8")
    assert '"poland": {' in html.replace('"poland":{', '"poland": {')


def test_migrates_old_layout(tmp_path, monkeypatch):
    """Folders from older versions (output/<teryt>) move to output/powiats/<teryt>."""
    monkeypatch.setattr(cli, "Http", FakeHttp)
    monkeypatch.setattr("gutshaus.config.ULDK_PAUSE_S", 0)
    monkeypatch.setattr("gutshaus.config.OVERPASS_PAUSE_S", 0)
    root = tmp_path / "out"
    cli.main(["--out-root", str(root), "run", "--powiat", "3209", "--out", str(root / "3209")])
    (root / "3209" / "summary.json").unlink()                            # as written by an old version
    cli.main(["--out-root", str(root), "build"])
    assert not (root / "3209").exists() and (root / "powiats" / "3209" / "sites.json").exists()
    assert (root / "powiats" / "3209" / "summary.json").exists()         # recreated from sites.json
    assert "3209" in (root / "index.html").read_text(encoding="utf-8")


def test_kowr_card_parsing():
    from gutshaus.sources import kowr
    from fake_services import KOWR_CARDS
    c = kowr.parse_card(KOWR_CARDS["11111111-1111-1111-1111-111111111111"])
    assert c["obreb"] == "Przykładowo" and c["gmina"] == "Wzorowo" and c["powiat"] == "koszaliński"
    assert c["parcels"] == ["17", "18"] and c["area_ha"] == 4.8 and c["usable_m2"] == 820.5
    assert c["media"] == {"electricity": "tak", "gas": "nie", "water": "tak", "sewer": "lokalna"}
    assert "2027" in c["tender"] and c["contact_email"] == "zabytki.szczecin@kowr.gov.pl"
    assert c["zoning"].startswith("ZPz")


def test_kowr_offers_flag_sites(tmp_path, monkeypatch):
    from gutshaus.sources import kowr
    from fake_services import KOWR_CARDS
    monkeypatch.setattr(kowr, "pdf_text", lambda raw: KOWR_CARDS[raw.decode()])
    rows = run(tmp_path / "p", monkeypatch)
    site = next(r for r in rows if "Przykład" in r["name"])
    assert site["kowr_offer"] == "Pałac Przykładowo" and site["c_forsale"] == "1.0"
    assert site["kowr_url"].endswith("11111111-1111-1111-1111-111111111111")
    assert all(not r["kowr_offer"] for r in rows if r is not site)
    cache = tmp_path / "root" / "_kowr.json"
    import json
    offers = json.loads(cache.read_text(encoding="utf-8"))
    assert len(offers) == 3 and offers[0]["lat"] and not offers[1].get("lat")    # 2nd card: parcel not found
    assert offers[2]["source"] == "schedule"                                   # manor from a tender schedule
    html = (tmp_path / "p" / "review.html").read_text(encoding="utf-8")
    assert "KOWR heritage offers" in html and "Pałac Przykładowo" in html


def test_kowr_real_cards():
    """All 13 KOWR heritage cards published in November 2026 (contacts redacted)."""
    import json
    from pathlib import Path
    from gutshaus.sources import kowr
    cards = json.loads((Path(__file__).parent / "data" / "kowr_cards_2026-11.json").read_text(encoding="utf-8"))
    assert len(cards) == 13
    titles = [kowr.clean_title(k) for k in cards]
    assert titles[0] == "Dwór Jaczków" and "Pałac Rzędziszowice" in titles
    for title, text in cards.items():
        c = kowr.parse_card(text)
        assert c["voivodeship"] == "dolnośląskie", title
        assert c["powiat"] and c["gmina"] and c["obreb"], title
        assert c["parcels"] and all("/" in p or p.isdigit() for p in c["parcels"]), title
        assert c["area_ha"] and 0.5 < c["area_ha"] < 50, title
        assert "kwartał" in c["tender"], title
        assert c["media"]["electricity"] == "tak", title
    jas = kowr.parse_card(next(v for k, v in cards.items() if "Jastrzębia" in k))
    assert jas["obreb"] == "Jastrzębia" and jas["parcels"] == ["115/2"]   # "obręb i nr działki: Jastrzębia dz. 115/2 AM-2"
    gl = kowr.parse_card(next(v for k, v in cards.items() if "Glinka" in k))
    assert gl["area_ha"] == 9.6221 and gl["usable_m2"] == 980 and "403/9" in gl["parcels"]


# ------------------------------------------------------------ cache + matching
class _Resp:
    def __init__(self, content, status=200):
        self.content, self.status_code, self.url = content, status, "http://x"

    def raise_for_status(self):
        pass


def _http(tmp_path, bodies):
    from gutshaus.http import Http
    h = Http(tmp_path / "cache")
    h.s.request = lambda *a, **kw: _Resp(bodies.pop(0))
    return h


def test_overpass_runtime_error_is_not_cached(tmp_path, monkeypatch):
    import json
    from gutshaus.sources import osm
    monkeypatch.setattr("gutshaus.sources.osm.time.sleep", lambda s: None)
    partial = json.dumps({"elements": [{"id": 1}], "remark": "runtime error: Query timed out"}).encode()
    full = json.dumps({"elements": [{"id": 1}, {"id": 2}]}).encode()
    h = _http(tmp_path, [partial, full])
    assert len(osm.Overpass(h).query("q")["elements"]) == 2      # partial answer rejected, next mirror used
    h.s.request = lambda *a, **kw: pytest.fail("should come from the cache")
    assert len(osm.Overpass(h).query("q")["elements"]) == 2      # and only the full answer was cached


def test_bad_cache_entry_is_dropped(tmp_path, monkeypatch):
    import json
    from gutshaus.sources import osm
    monkeypatch.setattr("gutshaus.sources.osm.time.sleep", lambda s: None)
    h = _http(tmp_path, [json.dumps({"elements": []}).encode()])
    key = h._key("POST", config_url(), None, {"data": "q"})
    key.write_bytes(b'{"elements": [{"id"')                        # half-written by an interrupted run
    assert osm.Overpass(h).query("q") == {"elements": []}
    assert key.read_bytes() == b'{"elements": []}' and not list(key.parent.glob("*.part"))


def config_url():
    from gutshaus import config
    return config.OVERPASS_URLS[0]


def test_kowr_unlocated_offer_needs_same_gmina_or_powiat():
    from gutshaus.sources import kowr
    offer = {"title": "Dwór Nowa Wieś", "url": "u", "obreb": "Nowa Wieś", "gmina": "Wzorowo", "powiat": "koszaliński"}
    here = {"x": 0, "y": 0, "precinct": "Nowa Wieś", "commune": "Wzorowo", "powiat": "koszaliński"}
    same_powiat = {"x": 0, "y": 0, "precinct": "Nowa Wies", "commune": "Mielno", "powiat": "koszaliński"}
    elsewhere = {"x": 0, "y": 0, "precinct": "Nowa Wieś", "commune": "Kłodzko", "powiat": "kłodzki"}
    no_parcel = {"x": 0, "y": 0}
    sites = [here, same_powiat, elsewhere, no_parcel]
    assert kowr.match(sites, [offer]) == 2
    assert here["kowr_offer"] and same_powiat["kowr_offer"]
    assert "kowr_offer" not in elsewhere and "kowr_offer" not in no_parcel

# ------------------------------------------------------------ grades + public site
@pytest.fixture
def built(tmp_path, monkeypatch):
    from gutshaus.sources import kowr
    from fake_services import KOWR_CARDS
    monkeypatch.setattr(kowr, "pdf_text", lambda raw: KOWR_CARDS[raw.decode()])
    monkeypatch.setattr(cli, "Http", FakeHttp)
    monkeypatch.setattr("gutshaus.config.ULDK_PAUSE_S", 0)
    monkeypatch.setattr("gutshaus.config.OVERPASS_PAUSE_S", 0)
    root = tmp_path / "out"
    cli.main(["--out-root", str(root), "run", "--powiat", "3209"])
    cli.main(["--out-root", str(root), "build"])
    return root


def test_grades_ignore_personal_weights(built):
    from gutshaus import config, grades
    s = next(x for x in grades.all_sites(built) if x["status"] == "unlocated")
    did = grades.dossier_id(s["uid"])
    assert grades.find_site(built, did.lower())["uid"] == s["uid"]            # ID lookup, any case
    g = grades.grade_of(s)
    config.CRITERIA[0]["weight"] = 99                                    # personal override must not change grades
    try:
        assert grades.grade_of(s) == g
    finally:
        config.CRITERIA[0]["weight"] = config.DEFAULT_WEIGHTS[config.CRITERIA[0]["key"]]


def test_renovation_grade():
    from gutshaus import grades
    base = {"uid": "x", "name": "x", "lat": 54, "lon": 16, "park": True, "registered": True, "kowr_offer": "x",
            "dist_power_m": 50, "dist_road_m": 20, "dist_building_m": 100, "dist_town_m": 3000, "dist_water_m": 100,
            "land_cover": "agri", "c_protection": 1.0, "parcel_ha": 3}
    lost, ruin = {**base, "status": "lost"}, {**base, "status": "ruin"}
    assert grades.grade_of(lost) == "A" and grades.grade_of(lost, "renovation") == "C"   # nothing to renovate
    assert grades.grade_of(ruin, "renovation") == "A"
    unreg = {**ruin, "registered": False}
    assert grades.public_score(unreg, "renovation") < grades.public_score(ruin, "renovation")  # no subsidies
    standing = {**base, "status": "standing"}
    assert grades.public_score(standing) >= 85 and grades.grade_of(standing) == "C"   # no room while it stands
    assert grades.grade_of(standing, "renovation") == "A"
    assert [grades.grade(x) for x in (85, 84, 75, 74, 62, 61)] == ["A", "B", "B", "C", "C", "D"]


def test_unlocated_sites_get_d(built):
    from gutshaus import grades
    s = next(x for x in grades.all_sites(built) if x["status"] == "unlocated")
    assert grades.grade_of(s) == "D"                                     # position unknown: nothing to sell yet


def test_publish_site(built, monkeypatch):
    import json, re
    from gutshaus import grades
    monkeypatch.setattr("gutshaus.config.SHOP_ENABLED", True)
    monkeypatch.setattr("gutshaus.config.CHECKOUT_URL", "https://shop.lemonsqueezy.com/checkout/buy/abc")
    sites = grades.all_sites(built)
    keep = grades.dossier_id(sites[1]["uid"])
    (built / "reserved.txt").write_text(f"{sites[0]['uid']}  # mine\n", encoding="utf-8")
    cli.main(["--out-root", str(built), "publish", "--reserved", str(built / "reserved.txt")])
    html = (built / "site" / "public" / "index.html").read_text(encoding="utf-8")
    data = json.loads(re.search(r"const D = (\{.*?\});\n", html, re.S).group(1))
    ids = [x["id"] for v in data["villages"] for x in v["sites"]]
    assert keep in ids and grades.dossier_id(sites[0]["uid"]) not in ids and len(ids) == len(sites) - 1
    assert all(x["ren"] in "ABCD" for v in data["villages"] for x in v["sites"]) and all(v["r"] for v in data["villages"])
    for s in sites:                                                       # no parcel data on the public site
        if s.get("parcel_id"):
            assert s["parcel_id"] not in html
    assert data["cfg"]["checkout"].endswith("/abc") and "checkout[custom][dossier]" in html
    assert [o["t"] for o in data["kowr"]] == ["Pałac Przykładowo"]
    assert "zabytki.szczecin" not in html                                 # KOWR staff contacts stay private
    for f in ("functions/webhook.js", "functions/download.js", "wrangler.toml", "public/legal.html"):
        assert (built / "site" / f).exists(), f
    assert not (built / "site" / "wrangler.jsonc").exists()                 # the shop deploys as a Pages project


def test_config_from_env_and_reserved_file(tmp_path, monkeypatch):
    """Personal settings can live in a private folder: GUTSHAUS_CONFIG points at it and
    a relative RESERVED_FILE is read from that folder, not from the working directory."""
    import json
    from gutshaus import config
    ops = tmp_path / "ops"
    ops.mkdir()
    (ops / "gutshaus.json").write_text(json.dumps({"RESERVED_FILE": "reserved.txt", "CONTACT_EMAIL": "x@example.org"}),
                                       encoding="utf-8")
    monkeypatch.setenv("GUTSHAUS_CONFIG", str(ops / "gutshaus.json"))
    monkeypatch.setattr(config, "RESERVED_FILE", "reserved.txt")
    monkeypatch.setattr(config, "CONTACT_EMAIL", "")
    monkeypatch.chdir(tmp_path)
    assert config.load_overrides() == str(ops / "gutshaus.json")
    assert config.RESERVED_FILE == str((ops / "reserved.txt").resolve()) and config.CONTACT_EMAIL == "x@example.org"



def test_output_survives_a_cp1252_console(monkeypatch, capsys):
    """A run's summary names powiats like "złotowski"; with output redirected to a file on Windows (cp1252)
    printing it used to fail the whole powiat."""
    import io
    import sys
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))
    with pytest.raises(SystemExit):                                   # --help prints and exits
        cli.main(["powiats", "--help"])
    sys.stdout.write("powiat złotowski\n")
    sys.stdout.flush()
    assert "złotowski" in raw.getvalue().decode("utf-8")


def test_publish_without_shop(built):
    """Default: the public map only. No buy buttons, no dossier section, no Functions, no sales terms."""
    import json, re
    from gutshaus import config
    assert config.SHOP_ENABLED is False
    cli.main(["--out-root", str(built), "publish", "--reserved", str(built / "none.txt")])
    site = built / "site"
    html = (site / "public" / "index.html").read_text(encoding="utf-8")
    data = json.loads(re.search(r"const D = (\{.*?\});\n", html, re.S).group(1))
    assert data["cfg"]["shop"] is False and data["villages"]
    assert not (site / "functions").exists()
    # static deploy config: only ./public is uploaded, SITE_URL's domain (+ www) as custom domains
    (site / "wrangler.toml").write_text('name = "x"\nid = "REPLACE_WITH_KV_NAMESPACE_ID"\n', encoding="utf-8")
    cli.main(["--out-root", str(built), "publish", "--reserved", str(built / "none.txt")])
    assert not (site / "wrangler.toml").exists()                              # unfilled shop template removed
    raw = (site / "wrangler.jsonc").read_text(encoding="utf-8")
    cfg = json.loads("\n".join(ln for ln in raw.splitlines() if not ln.startswith("//")))
    assert cfg["name"] == config.DEPLOY_NAME and cfg["assets"]["directory"] == "./public"
    host = config.SITE_URL.split("//")[1].strip("/")
    assert [r["pattern"] for r in cfg["routes"]] == [host, "www." + host] and all(r["custom_domain"] for r in cfg["routes"])
    legal = (site / "public" / "legal.html").read_text(encoding="utf-8")
    assert "Widerrufsrecht" not in legal and "Lemon Squeezy" not in legal and "Impressum" in legal and "Datenschutz" in legal
    assert "vor dem Start ausfüllen" in legal                            # placeholder until LEGAL_NOTICE is set
    config.LEGAL_NOTICE = ["Muster GmbH", "Musterstraße 1 <b>", "NIP 123"]
    try:
        cli.main(["--out-root", str(built), "publish", "--reserved", str(built / "none.txt")])
    finally:
        config.LEGAL_NOTICE = []
    legal = (site / "public" / "legal.html").read_text(encoding="utf-8")
    assert "Muster GmbH<br>Musterstraße 1 &lt;b&gt;<br>NIP 123" in legal and "vor dem Start" not in legal



@pytest.mark.parametrize("precinct,name,commune,expected", [
    ("Przykładowo", "park dworski, Przykładowo", "Wzorowo", "Przykładowo"),
    ("04-02", "park pałacowy, Radzymin", "Radzymin", "Radzymin"),          # number precinct: place from the name
    ("15", "Pałac Potockich", "Międzyrzec Podlaski (miasto)", "Międzyrzec Podlaski"),
    ("Nr7", "Dwór", "Skierniewice (miasto)", "Skierniewice"),
    ("Obręb 3", "Dwór", "Wzorowo (gmina wiejska)", "Wzorowo"),
    ("", "park dworski, Dąbrowa", "", "Dąbrowa"),
    ("21 - Szczepanowice", "Dwór", "Wzorowo", "Szczepanowice"),           # numbered precinct
    ("3-Centrum", "Dwór", "Stalowa Wola (miasto)", "Centrum"),
    ("0001 Żagań", "Dwór", "Żagań (miasto)", "Żagań"),
    ("026B Śródmieście", "Dwór", "Wzorowo", "Śródmieście"),
    ("1 M.słubice", "Dwór", "Słubice (miasto)", "Słubice"),
    ("0001 Prabuty - M", "Dwór", "Prabuty", "Prabuty"),
])
def test_public_village_names_are_places(precinct, name, commune, expected):
    from gutshaus.publish import village_of
    assert village_of({"precinct": precinct, "name": name, "commune": commune}) == expected


def test_public_map_has_exact_sites_but_no_parcels(built, monkeypatch):
    """Located sites carry their exact position (it is public in OSM and the register); sites known only by their
    village do not. No parcel numbers. Analytics only with a token, and then the privacy text says so."""
    import json, re
    from gutshaus import config, grades
    cli.main(["--out-root", str(built), "publish", "--reserved", str(built / "none.txt")])
    pub = built / "site" / "public"
    html = (pub / "index.html").read_text(encoding="utf-8")
    # OpenStreetMap by default (the site is served over https), plus orthophoto and Messtischblatt; no Geoportal TOPO
    assert "tile.openstreetmap.org" in html and "TOPO" not in html
    data = json.loads(re.search(r"const D = (\{.*?\});\n", html, re.S).group(1))
    status = {grades.dossier_id(s["uid"]): s["status"] for s in grades.all_sites(built)}
    items = [x for v in data["villages"] for x in v["sites"]]
    assert items and all(("lat" in x) == (status[x["id"]] != "unlocated") for x in items)
    assert any("lat" in x for x in items) and any("lat" not in x for x in items)
    assert "cloudflareinsights" not in html
    assert "keine Cookies und kein Tracking" in (pub / "legal.html").read_text(encoding="utf-8")
    monkeypatch.setattr(config, "WEB_ANALYTICS_TOKEN", "abc123")
    cli.main(["--out-root", str(built), "publish", "--reserved", str(built / "none.txt")])
    html = (pub / "index.html").read_text(encoding="utf-8")
    assert 'data-cf-beacon=\'{"token": "abc123"}\'' in html
    legal = (pub / "legal.html").read_text(encoding="utf-8")
    assert "Cloudflare Web Analytics" in legal and "kein Tracking" not in legal
    # Cloudflare injects the script itself: none from us, but the privacy text still says so
    monkeypatch.setattr(config, "WEB_ANALYTICS_TOKEN", "")
    monkeypatch.setattr(config, "WEB_ANALYTICS_AUTO", True)
    cli.main(["--out-root", str(built), "publish", "--reserved", str(built / "none.txt")])
    assert "cloudflareinsights" not in (pub / "index.html").read_text(encoding="utf-8")
    assert "Cloudflare Web Analytics" in (pub / "legal.html").read_text(encoding="utf-8")


def test_kowr_offer_shown_on_its_site(built, monkeypatch):
    """A KOWR offer matched to a site is linked from that site (shown in its popup, one marker) instead of being a
    separate dot; unmatched offers stay separate markers."""
    import json, re
    from gutshaus.sources import kowr
    from fake_services import KOWR_CARDS
    monkeypatch.setattr(kowr, "pdf_text", lambda raw: KOWR_CARDS[raw.decode()])
    cli.main(["--out-root", str(built), "publish", "--reserved", str(built / "none.txt")])
    html = (built / "site" / "public" / "index.html").read_text(encoding="utf-8")
    data = json.loads(re.search(r"const D = (\{.*?\});\n", html, re.S).group(1))
    linked = [x for v in data["villages"] for x in v["sites"] if x.get("ku")]
    ids = {o["id"] for o in data["kowr"]}
    assert linked and all(x["ku"] in ids and x["kowr"] for x in linked)
    assert 'id="kowrOnly"' in html and "MATCHED.has(o.id || o.url)" in html


def test_kowr_tender_schedules(tmp_path, monkeypatch):
    """Manors from the branches' tender schedules: only sales with a manor or a monument entry; a property that
    also has a card adds its tenders and starting price to the card; schedule-only manors become their own offers."""
    from gutshaus.sources import kowr, kowr_schedules as ks
    from fake_services import KOWR_CARDS
    monkeypatch.setattr(kowr, "pdf_text", lambda raw: KOWR_CARDS[raw.decode()])
    monkeypatch.setattr("gutshaus.config.ULDK_PAUSE_S", 0)
    offers = kowr.fetch(FakeHttp())
    card = next(o for o in offers if o["title"] == "Pałac Przykładowo")
    assert card["price_pln"] == 1250000.0 and card["register"] == "A-1000" and "03.11.2026" in card["tender"]
    sched = [o for o in offers if o.get("source") == "schedule"]
    assert [o["obreb"] for o in sched] == ["Stary Kłos"]                       # the lease row is not an offer
    s = sched[0]
    assert s["title"] == "Dwór, Stary Kłos" and s["price_pln"] == 330000.0 and s["parcels"] == ["5/1"]
    assert s["gmina"] == "Wzorowo" and s["powiat"] == "koszaliński" and s["area_ha"] == 1.2
    assert not s.get("lat")                                                    # the fake ULDK does not know it


@pytest.mark.parametrize("row,expect", [
    # slash layout (Wrocław): price without "zł", 50 % discount, parcels end with ", "
    ("05.11.2026 11:00 SZ Świdnica DOLNOŚLĄSKIE (2)/ świdnicki (19)/ Dobromierz (3_2)/ Szymanów (12)/ 289/8, 289/7, "
     "3,6806 3,4606 281200,00sprzedaż licytacja przetarg nieograniczony ZA-wpisana do rejestru zabytków obniżka 50% 5",
     {"obreb": "Szymanów", "gmina": "Dobromierz", "parcels": ["289/8", "289/7"], "price_pln": 281200.0, "discount_pct": 50}),
    # bare columns (Olsztyn): words before the parcel go to ULDK; the price is not a register number
    ("03.11.2026 10:00 SZ Kętrzyn kętrzyński Barciany Kiemławki Wielkie 1/6 0,0524 nierolna dwór wpisany do rejestru "
     "zabytków 24 400,00 zł sprzedaż nieograniczony", {"powiat": "kętrzyński", "parcels": ["1/6"], "register": "",
                                                       "place_words": ["Barciany", "Kiemławki", "Wielkie"]}),
    # a large farm offer: the manor's own parcels ("na dz. 33/15, 40/5 zespół …") are located first
    ("3.11.2026 10:00 Powiat choszczeński, gmina Drawno obręb Konotop dz. nr 20, 22/2, 33/15, 40/5 sprzedaż ustny "
     "ograniczony nieużytki na dz. 33/15, 40/5 zespół pałacowo-parkowy wpisany do rejestru zabytków pod nr A-88 "
     "51,7914 2 468 000,00 zł", {"obreb": "Konotop", "heritage_parcels": ["33/15", "40/5"], "register": "A-88",
                                 "kind": "zespół pałacowo-parkowy", "price_pln": 2468000.0}),
    # Łódź: dates with hyphens, upper-case "SPRZEDAŻ"
    ("25-11-2026 12:00 KOWR bełchatowski Kleszczów Wola Grzymalina 12/3 SPRZEDAŻ nieograniczony nierolna zabudowana "
     "dwór wpisany do rejestru zabytków pod nr A/123 2,5000 410 000,00", {"date": "25.11.2026", "kind": "dwór",
                                                                       "register": "A/123", "parcels": ["12/3"]}),
    # place names are not manors: "Nowy Dwór Gdański", powiat "nowodworski"
    ("17.11.2026 09:00 SZ Malbork POMORSKIE (22)/ nowodworski (10)/ Nowy Dwór Gdański (2_5)/ Stobna (18)/ 1/51, "
     "sprzedaż licytacja przetarg nieograniczony NN-nierolna niezabudowana 0,1395 74500", None),
])
def test_schedule_rows(row, expect):
    from gutshaus.sources import kowr_schedules as ks
    o = ks.parse_row(row)
    if expect is None:
        assert o is None
    else:
        assert {k: o.get(k) for k in expect} == expect


def test_public_filters(built):
    """The map's filters: grade, condition, features and distance all narrow the result together (AND), and the
    distance filter has the reference cities."""
    import json, re
    from gutshaus import config
    cli.main(["--out-root", str(built), "publish", "--reserved", str(built / "none.txt")])
    html = (built / "site" / "public" / "index.html").read_text(encoding="utf-8")
    for el in ('id="kowrOnly"', 'id="parkOnly"', 'id="regOnly"', 'id="statuses"', 'id="ref"', 'id="km"', 'id="reset"'):
        assert el in html, el
    assert "shown.has(sg(s)) && F.status.has(s.status) && (!F.kowr || s.ku)" in html      # AND, not "KOWR overrides grade"
    data = json.loads(re.search(r"const D = (\{.*?\});\n", html, re.S).group(1))
    assert data["cfg"]["refs"]["Berlin"] == list(config.REFERENCE_POINTS["Berlin"])



def test_kowr_branch_pages_from_overview(monkeypatch):
    """Branch schedule pages are read from KOWR's overview (their names vary), with the configured names as fallback."""
    from gutshaus.sources import kowr_schedules as ks
    FakeHttp.calls = []
    assert ks.branch_pages(FakeHttp()) == [("OT Koszalin", "https://www.gov.pl/web/kowr/ot-koszalin1")]
    pdfs = ks.schedule_pdfs(FakeHttp())
    assert pdfs and pdfs[0]["branch"] == "OT Koszalin"
    assert any("ot-koszalin1" in c[1] for c in FakeHttp.calls)

    class NoOverview(FakeHttp):
        def get(self, url, params=None, **kw):
            if "harmonogramy" in url:
                raise RuntimeError("down")
            return super().get(url, params, **kw)
    assert ks.branch_pages(NoOverview()) == [("szczecin", "https://www.gov.pl/web/kowr/ot-szczecin2")]
