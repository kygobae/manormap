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
    assert not is_relevant_osm({"historic": "memorial", "name": "Por. Jerzy Dworakowski"})
    assert not is_relevant_osm({"name": "Por. Jerzy Dworakowski"})                 # "dwór" inside a surname
    assert not is_relevant_osm({"man_made": "water_tower", "name": "Zbiornik wody \"Wysoki Dwór\""})
    assert not is_relevant_osm({"tourism": "viewpoint", "name": "widok na pałac Belwederski"})
    assert not is_relevant_osm({"name": "BP Dworzysko", "building": "yes"})
    assert not is_relevant_osm({"name": "Dwory Pętla"})
    assert not is_relevant_osm({"name": "Młyn Wodny w Gzowicach-Folwarku", "building": "yes"})
    assert is_relevant_osm({"name": "Pałac, obecnie szkoła", "building": "yes"})    # a school in a palace stays
    assert is_relevant_osm({"name": "Nowy Dwór", "building": "yes"})
    assert is_relevant_osm({"tourism": "hotel", "name": "Pałac Mierzęcin"})
    assert is_relevant_osm({"historic": "monument", "building": "yes", "name": "Dawny dwór"})
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
    site = {"text": "park dworski", "name": "park dworski", "location": "osm park near village"}
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


@pytest.mark.parametrize("text,ruin", [
    ("name=Ruiny dworu | historic=ruins", True),
    ("name=Pałac | historic=manor | building=yes | ruins=yes", True),
    ("ruina pałacu | pałac | XVIII w. | Włostów | gm. Lipnik", True),
    ("name=Resztki pałacu | historic=memorial", True),
    ("name=Zamek Ujazdowski | historic=castle | building=castle | castle_type=stately | ruins=no", False),
    ("pozostałości parku dworskiego | park | koniec XV w. | Olchowa", False),   # the park's remains, not the house
    ("relikty folwarku | zespół - folwark | XIX w. | Wiejce", False),
    ("name=Stodoła folwarczna z XIX wieku | historic=ruins | heritage=2", False),  # a barn
    ("ruina kaplicy dworskiej | kaplica | XVIII w. | Morawica", False),
    ("name=Domek Ogrodnika | description=Fragment zabudowań nieistniejącego Pałacu | building=yes | ruins=yes",
     False),                                                                    # the palace itself is gone
    ("name=Park pałacowy | leisure=park || name=Ruiny pałacu | historic=ruins", True),  # any member counts
])
def test_ruin_evidence(text, ruin):
    from gutshaus.classify import ruin_evidence
    assert ruin_evidence(text) == ruin


def test_small_mapped_manor_stands_and_guessed_places_are_not_lost():
    from shapely.geometry import Point, box
    from gutshaus.pipeline import classify_status
    from gutshaus.sources.osm import summarise_context
    empty = summarise_context(Point(0, 0), [({"building": "yes"}, box(45, -2, 55, 2))])   # only a shed
    dworek = {"text": "name=Dworek w Smardzowicach | historic=house | building=yes || dwór | dwór | 1930 r.",
              "name": "dwór", "location": "exact"}
    assert classify_status(dworek, empty) == "standing"           # under 250 m², but mapped as the house
    tower = {"text": "name=Wieża w Stołpiu | historic=castle | building=yes | castle_type=manor | ruins=no",
             "name": "Wieża w Stołpiu", "location": "exact"}
    assert classify_status(tower, empty) == "standing"
    guessed = {"text": "dwór | dwór | 1. poł. XIX w. | Kolonia Niedzielsko | gm. Wieluń", "name": "dwór",
               "location": "osm park near village"}
    assert classify_status(guessed, empty) == "unknown"            # the register lists the house: our guess is off
    assert classify_status({**guessed, "location": "exact"}, empty) == "lost"
    park = {"text": "park dworski | park | XIX w. | Smolno", "name": "park dworski",
            "location": "osm park near village"}
    assert classify_status(park, empty) == "lost"                  # only the park is listed: the house is likely gone


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


def _check_surroundings(got):
    from fake_services import SURROUNDINGS_EXPECTED
    for kind, (count, nearest) in SURROUNDINGS_EXPECTED.items():
        assert got[kind]["count"] == count, kind
        assert (got[kind]["nearest_m"] is None) == (nearest is None), kind
        if nearest:
            assert abs(got[kind]["nearest_m"] - nearest) <= 25, (kind, got[kind])
    assert got["school"]["name"] == "Szkoła Podstawowa" and got["shop"]["name"] == "Market Wzorowo"


def test_surroundings_from_both_backends(tmp_path, monkeypatch):
    """Wind turbines, main roads, railways and services around a site: same answer from Overpass and the extract."""
    from fake_services import SITE_A, osm_fixture_xml
    from gutshaus.sources import osm, osm_local
    _check_surroundings(osm.OverpassOSM(FakeHttp()).surroundings(*SITE_A))
    monkeypatch.setattr(cli, "Http", FakeHttp)
    pbf = tmp_path / "test.osm"
    pbf.write_text(osm_fixture_xml(), encoding="utf-8")
    cli.main(["--out-root", str(tmp_path), "osm-extract", "--pbf", str(pbf)])
    _check_surroundings(osm_local.LocalOSM(tmp_path / "_osm" / "osm.gpkg").surroundings(*SITE_A))


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
    assert c["zoning"].startswith("ZPz") and c["kw"] == ["KO1K/00012345/6"]


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
    assert offers[0]["kw"] == ["KO1K/00012345/6"]                              # land register number from the card
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
    # parcel lists over several lines: all of them, not only the first line (and no "AM-2" map sheet)
    ryn = kowr.parse_card(next(v for k, v in cards.items() if "Rynarcice" in k))
    assert len(ryn["parcels"]) == 13 and ryn["parcels"][-1] == "203/30"
    mal = kowr.parse_card(next(v for k, v in cards.items() if "Małkowice" in k))
    assert mal["parcels"][-1] == "1/72" and len(mal["parcels"]) == 9


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


NID_INFO = """<BODY><TABLE class="mainTb"><TR><TD> Immovable_Monuments</TD></TR><TR><TD><TABLE>
<TR><TD class="featureTh"> INSPIREID </TD><TD> </TD><TD class="featureTd"> PL.1.9.ZIPOZ.NID_N_02_ZZ.1 </TD></TR>
<TR><TD class="featureTh"> LEGALFOUNDATIONDOCUMENT </TD><TD> </TD><TD class="featureTd"> 890/WŁ z 1982-06-25;
A/4048/890/WŁ z 2010-10-27 </TD></TR>
<TR><TD class="featureTh"> SITENAME </TD><TD> </TD><TD class="featureTd"> ogrody </TD></TR></TABLE></TD>
<TD><TABLE><TR><TD class="featureTd"> PL.1.9.ZIPOZ.NID_N_02_BK.2 </TD></TR><TR><TD class="featureTd"> A/1 z 2010 </TD></TR>
<TR><TD class="featureTd"> oficyna </TD></TR></TABLE></TD></TR></TABLE></BODY>"""


def test_register_entries_from_nid_map():
    """NID's map draws monuments as outlines: one GetFeatureInfo per drawn shape on or next to the parcels."""
    import io
    from PIL import Image, ImageDraw
    from gutshaus.geo import pl_to_ll
    from gutshaus.sources import nid_map
    assert [(f["id"][-4:], f["name"], f["docs"]) for f in nid_map.parse_info(NID_INFO)] == [
        ("ZZ.1", "ogrody", "890/WŁ z 1982-06-25; A/4048/890/WŁ z 2010-10-27"), ("BK.2", "oficyna", "A/1 z 2010")]

    class Http:
        calls = []
        def get(self, url, params=None, **kw):
            self.calls.append(params["request"])
            if params["request"] == "GetMap":
                im = Image.new("RGBA", (params["width"], params["height"]), (0, 0, 0, 0))
                cx, cy = params["width"] // 2, params["height"] // 2              # on the parcel
                ImageDraw.Draw(im).rectangle([cx - 15, cy - 15, cx + 15, cy + 15], fill=(128, 128, 128, 255), outline=(0, 0, 0, 255))
                buf = io.BytesIO()
                im.save(buf, "PNG")
                return buf.getvalue()
            return NID_INFO.encode()
    x0, y0 = 400000.0, 400000.0
    ring = [list(pl_to_ll(x0 + dx, y0 + dy)) for dx, dy in ((0, 0), (100, 0), (100, 100), (0, 100), (0, 0))]
    http = Http()
    found = nid_map.monuments_in(http, [ring], near_m=50)
    assert {m["name"] for m in found} == {"ogrody", "oficyna"} and all(m["on"] for m in found)
    assert http.calls == ["GetMap", "GetFeatureInfo"]                  # one shape, one question


def test_kowr_register_entries_and_parcels():
    """Entries NID draws at an offer move there; sites match only on the offered parcels; a member on the offer
    gives the site its position."""
    from gutshaus import pipeline
    from gutshaus.geo import pl_to_ll
    from gutshaus.sources import kowr
    x0, y0 = 400000.0, 400000.0
    ring = [list(pl_to_ll(x0 + dx, y0 + dy)) for dx, dy in ((0, 0), (100, 0), (100, 100), (0, 100), (0, 0))]
    offer = {"title": "Pałac X", "url": "u", "uid": "kowr:u", "x": x0 + 50, "y": y0 + 50, "parcels_ll": [ring],
             "monuments": [{"id": "PL.9", "name": "oficyna", "docs": "A/1", "on": True, "x": x0 + 60, "y": y0 + 40},
                           {"id": "PL.8", "name": "klasztor", "docs": "A/2", "on": False, "x": x0 + 160, "y": y0 + 50}]}
    entry = {"uid": "nid:PL.9", "source": "nid", "x": 0.0, "y": 0.0, "location": "village centre", "village_xy": [0, 0]}
    house_off = {"uid": "osm:1", "source": "osm", "x": x0 + 170, "y": y0 + 50, "text": "historic=manor | building=yes"}
    house_on = {"uid": "osm:2", "source": "osm", "x": x0 + 30, "y": y0 + 30, "text": "historic=manor | building=yes"}
    cands = [entry, house_off, house_on]
    assert kowr.pin_candidates(cands, [offer]) == 1
    assert (entry["x"], entry["location"], "village_xy" in entry) == (x0 + 60, "exact", False)
    assert entry["on_offer"] and house_on["on_offer"] and not house_off.get("on_offer")
    assert pipeline.point_rank(house_on) < pipeline.point_rank(house_off)
    near = {"x": x0 + 170, "y": y0 + 50}                         # 70 m outside the parcels: not for sale
    on = {"x": x0 + 99, "y": y0 + 50, "registered": False}
    assert kowr.match([near, on], [offer]) == 1 and not near.get("kowr_offer")
    assert on["kowr_offer"] == "Pałac X" and on["registered"] and len(on["kowr_monuments"]) == 2
    lone = kowr.site_from_offer(offer)
    assert lone["registered"] and lone["nid_ref"] == "PL.9 (A/1)"


def test_kowr_offer_locates_village_only_site():
    """A register entry known only by its village moves onto the offer's parcel; an offer nobody explains is added."""
    from shapely.geometry import box
    from gutshaus.sources import kowr
    offer = {"title": "Pałac Wzorowo", "url": "u1", "uid": "kowr:u1", "place": "Wzorowo", "obreb": "Wzorowo",
             "gmina": "Góra", "x": 1000.0, "y": 0.0, "kind": "dwór z XVIII w.", "register": "A/1"}
    village = {"uid": "nid:1", "name": "dwór, Wzorowo", "village": "Wzorowo", "gmina": "Góra - obszar wiejski",
               "location": "village centre", "village_xy": [0, 0], "x": 0.0, "y": 0.0}
    other_gmina = {**village, "uid": "nid:2", "gmina": "Kłodzko"}
    assert kowr.place_sites([other_gmina], [offer]) == 1 and other_gmina["x"] == 0      # not this village: new site
    sites = [village]
    assert kowr.place_sites(sites, [offer]) == 1 and len(sites) == 1
    assert (village["x"], village["location"], "village_xy" in village) == (1000.0, "kowr parcel", False)
    assert kowr.place_sites(sites, [offer]) == 0                    # now located: nothing more to do
    lone = []
    assert kowr.place_sites(lone, [offer]) == 1 and lone[0]["source"] == "kowr" and lone[0]["registered"]
    assert lone[0]["name"] == "Pałac Wzorowo" and lone[0]["lat"] is not None
    assert kowr.place_sites([], [offer], area=box(-10, -10, 10, 10)) == 0                 # outside the area
    sched = {"row": "Ciepłowody 4,0797 sprzedaż ZA-wpisana do rejestru zabytków obniżka 50%", "register": ""}
    assert kowr.is_registered(sched) and not kowr.is_registered({"row": "grunty rolne", "register": ""})


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


def test_traffic_light_grade():
    from gutshaus import grades
    base = {"uid": "x", "name": "x", "lat": 54, "lon": 16, "park": True, "registered": True, "kowr_offer": "x",
            "dist_power_m": 50, "dist_road_m": 20, "dist_building_m": 100, "dist_town_m": 3000, "dist_water_m": 100,
            "land_cover": "agri", "protection": "none", "c_protection": 1.0, "parcel_ha": 3}
    standing, lost = {**base, "status": "standing"}, {**base, "status": "lost"}
    ruin, unknown = {**base, "status": "ruin"}, {**base, "status": "unknown"}
    # what is left sets the path; both main paths can reach A
    assert (grades.path(standing), grades.path(lost), grades.path(ruin)) == ("renovation", "new", "either")
    assert grades.grade_of(standing) == "A" and grades.grade_of(lost) == "A"
    L = grades.lights(standing)
    assert list(L) == ["house", "plot", "access", "rules", "location"] and all(x["level"] == "green" for x in L.values())
    assert "register" in L["rules"]["why"] and "water" in L["location"]["why"]
    # ruins and unclear cases: house amber, at best B
    assert grades.lights(ruin)["house"]["level"] == "amber" and grades.grade_of(ruin) == "B"
    assert grades.grade_of(unknown) == "B"
    # nature reserve / national park: knock-out
    assert grades.grade_of({**standing, "protection": "national park"}) == "D"
    # forest on the map or a landscape park: amber, not red
    forest = {**standing, "land_cover": "forest"}
    assert grades.lights(forest)["rules"]["level"] == "amber" and grades.grade_of(forest) == "B"
    # no park and a far town: two lights off green
    far = {**standing, "park": False, "dist_town_m": 20000}
    assert grades.lights(far)["location"]["level"] == "red" and grades.grade_of(far) == "C"
    assert grades.grade_of({**far, "dist_power_m": 5000, "dist_road_m": 2000, "dist_building_m": 3000}) == "D"
    assert [grades.grade_rank(g) for g in "AB?"] == [0, 1, 4]


def test_commune_label():
    from gutshaus.publish import commune_label
    assert commune_label("2013BIAŁOBRZEGI OBSZAR WIEJSKI") == "Białobrzegi"
    assert commune_label("011_KUTNO") == "Kutno" and commune_label("Będzino") == "Będzino"


def test_forest_database():
    """Forest stands from the BDL decide the forest light; OSM's land cover only when BDL was not asked."""
    import json
    from gutshaus import grades
    from gutshaus.sources import bdl

    class Http:
        def __init__(self, cover): self.cover = cover
        def get(self, url, params=None, **kw):
            ring = json.loads(params["geometry"])["rings"][0]
            xs, ys = [p[0] for p in ring], [p[1] for p in ring]
            x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
            x1 = x0 + (x1 - x0) * self.cover                # the stand covers the western part of the parcel
            return json.dumps({"results": [{"layerId": 6, "geometry": {"rings": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]},
                                            "attributes": {"species_cd_d": "BK", "species_age": "150", "a_year": "2011",
                                                           "site_type_cd": "LŚW", "owner_cat_name": "prywatne "}}]}).encode()

    class Down:
        def get(self, *a, **kw): raise RuntimeError("offline")
    d = 0.002
    site = {"uid": "x", "name": "x", "status": "lost", "lat": 54.0, "lon": 16.0, "park": True, "land_cover": "agri",
            "parcel_ll": [[16 - d, 54 - d], [16 + d, 54 - d], [16 + d, 54 + d], [16 - d, 54 + d], [16 - d, 54 - d]],
            "dist_power_m": 50, "dist_road_m": 20, "dist_building_m": 100, "dist_town_m": 3000, "protection": "none",
            "parcel_ha": 3}
    f = bdl.stands(Http(1.0), site)
    assert f["status"] == "found" and f["pct"] == 100 and f["at_site"] and f["stands"][0]["species"] == "BK"
    assert f["stands"][0]["age"] == 150 and f["stands"][0]["habitat"] == "LŚW" and f["stands"][0]["owner_pl"] == "prywatne"
    small = bdl.stands(Http(0.2), site)                    # a strip at the edge: not the manor's spot, 20 %
    assert small["status"] == "found" and not small["at_site"] and not bdl.is_forest(small) and bdl.is_forest(f)
    assert bdl.stands(Down(), site) == {"status": "unavailable"}
    for s, http in ((site, Http(1.0)), ({**site, "land_cover": "forest"}, Http(0.2))):
        bdl.annotate(http, [s])
    assert "forest_db" in grades.lights(site)["rules"]["why"] and grades.lights(site)["rules"]["level"] == "amber"
    park_trees = {**site, "land_cover": "forest"}          # OSM wood, BDL: only a strip -> not forest
    bdl.annotate(Http(0.2), [park_trees])
    assert grades.lights(park_trees)["rules"]["level"] == "green" and grades.grade_of(park_trees) == "A"
    unasked = {k: v for k, v in park_trees.items() if not k.startswith("forest_")}
    assert "forest" in grades.lights(unasked)["rules"]["why"]                   # OSM fallback


def test_unlocated_sites_have_no_grade(built):
    from gutshaus import grades
    s = next(x for x in grades.all_sites(built) if x["status"] == "unlocated")
    assert grades.grade_of(s) == "?"                                     # position unknown: no grade yet
    L = grades.lights(s)
    assert L["access"]["level"] is None and L["rules"]["level"] is None and L["location"]["level"] is None


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
    outlines = json.loads((built / "site" / "public" / "parcels.json").read_text(encoding="utf-8"))
    assert grades.dossier_id(sites[0]["uid"]) not in outlines                # a reserved site's parcel stays private
    items = [x for v in data["villages"] for x in v["sites"]]
    assert all(x["grade"] in "ABCD?" and x["path"] in ("renovation", "new", "either") and len(x["L"]) == 5 for x in items)
    assert all(v["g"] == min((x["grade"] for x in v["sites"]), key=grades.grade_rank) for v in data["villages"])
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


def test_public_map_has_exact_sites_and_outlines_but_no_parcel_numbers(built, monkeypatch):
    """Located sites carry their exact position and parcel outline (both public in OSM, the register and Geoportal);
    sites known only by their village do not. No parcel numbers. Analytics only with a token, and then the privacy
    text says so."""
    import json, re
    from gutshaus import config, grades
    cli.main(["--out-root", str(built), "publish", "--reserved", str(built / "none.txt")])
    pub = built / "site" / "public"
    html = (pub / "index.html").read_text(encoding="utf-8")
    # OpenStreetMap by default (the site is served over https), plus orthophoto and Messtischblatt; no Geoportal TOPO
    assert "tile.openstreetmap.org" in html and "TOPO" not in html
    # the display font is served by the site itself: no requests to Google Fonts (GDPR)
    assert (pub / "fonts" / "fraunces-latin-ext-wght-normal.woff2").stat().st_size > 10000
    assert "fonts.googleapis" not in html and "fonts.gstatic" not in html
    # Leaflet is served by the site too (no unpkg); its CSS images come along
    assert "unpkg.com" not in html and 'src="vendor/leaflet/leaflet.js"' in html
    assert (pub / "vendor" / "leaflet" / "leaflet.js").stat().st_size > 100000
    assert (pub / "vendor" / "leaflet" / "images" / "layers.png").exists()
    data = json.loads(re.search(r"const D = (\{.*?\});\n", html, re.S).group(1))
    status = {grades.dossier_id(s["uid"]): s["status"] for s in grades.all_sites(built)}
    items = [x for v in data["villages"] for x in v["sites"]]
    assert items and all(("lat" in x) == (status[x["id"]] != "unlocated") for x in items)
    assert any("lat" in x for x in items) and any("lat" not in x for x in items)
    outlines = json.loads((pub / "parcels.json").read_text(encoding="utf-8"))
    located = {x["id"] for x in items if "lat" in x}
    assert outlines and set(outlines) <= located
    assert all(len(r) >= 4 and r[0] == r[-1] and 49 < r[0][0] < 55 and 14 < r[0][1] < 25 for r in outlines.values())
    parcel_ids = [s["parcel_id"] for s in grades.all_sites(built) if s.get("parcel_id")]
    assert parcel_ids and not any(p in html or p in json.dumps(outlines) for p in parcel_ids)
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
    assert all(o["past"] is False for o in data["kowr"])                   # fixture tenders lie in 2027 and 2099
    # the whole tender area is drawn: outlines of the offer's parcels, [lat, lon] rings in Poland
    located = [o for o in data["kowr"] if o.get("lat")]
    assert located and all(o["pl"] and o["pn"] >= 1 and 49 < o["pl"][0][0][0] < 55 for o in located)
    assert "drawOfferAreas" in html
    # every site card can be shared as manormap.eu/#GF-…, and such a link opens the site
    assert "data-share" in html and "openShared" in html and "/^GF-[0-9A-F]{8}$/i" in html
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
    assert card["price_pln"] == 1250000.0 and card["register"] == "A-1000" and "03.11.2099" in card["tender"]
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
    assert "shown.has(sg(s)) && onPath(s) && F.status.has(s.status) && (!F.kowr || s.ku)" in html  # AND, not "KOWR overrides grade"
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


@pytest.mark.parametrize("text,end", [
    ("IV kwartał 2025 r", (2025, 12, 31)),
    ("I-II kwartał 2026 r", (2026, 6, 30)),
    ("III kwartał 2026 r", (2026, 9, 30)),
    ("I kwartał 2027 r.", (2027, 3, 31)),
    ("II półrocze 2026", (2026, 12, 31)),
    ("03.11.2026 10:00 (ustny nieograniczony); 24.11.2026 10:00", (2026, 11, 24)),
    ("2027 r.", (2027, 12, 31)),
    ("do uzgodnienia", None),
    ("", None),
])
def test_kowr_tender_end(text, end):
    from gutshaus.sources import kowr
    assert kowr.tender_end(text) == end


def test_stale_kowr_offers():
    """A card's tender period in the past is flagged, not shown as coming; schedule offers whose tenders are all
    past are dropped (the fallback used to keep the last past tender)."""
    from gutshaus.sources import kowr, kowr_schedules as ks
    today = (2026, 11, 23)
    assert kowr.tender_passed("IV kwartał 2025 r", today) and kowr.tender_passed("III kwartał 2026 r", today)
    assert not kowr.tender_passed("IV kwartał 2026 r", today) and not kowr.tender_passed("brak", today)
    row = dict(obreb="Szymanów", parcels=["289/8"], mode="", price_pln=281200.0, kind="pałac")
    past = ks.merge([{**row, "date": "05.11.2026", "time": "11:00"}], today)
    both = ks.merge([{**row, "date": "05.11.2026", "time": "11:00"}, {**row, "date": "03.12.2026", "time": "11:00"}], today)
    assert past == [] and [t["date"] for t in both[0]["tenders"]] == ["03.12.2026"]


def test_register_entry_not_in_the_neighbours_park():
    """Sulisławice's register entry used to land in Wiśniowa's manor park (the nearest park, but nearer Wiśniowa)."""
    from gutshaus.sources.osm import park_for_village
    sulislawice, wisniowa = (0, 0), (300, 600)
    park_wisniowa, park_own = (250, 500), (-400, -900)
    assert park_for_village(sulislawice, [park_wisniowa], [sulislawice, wisniowa], 1500) is None
    assert park_for_village(sulislawice, [park_wisniowa, park_own], [sulislawice, wisniowa], 1500) == park_own
    assert park_for_village(wisniowa, [park_wisniowa], [sulislawice, wisniowa], 1500) == park_wisniowa


def test_site_sits_on_the_house_not_the_park_centre():
    """A park and the palace in it become one site; the site point (and so its parcel) is the palace's."""
    from gutshaus.pipeline import merge
    park = {"uid": "osm:way/1", "source": "osm", "name": "Zespół pałacowo-parkowy", "text": "name=Zespół pałacowo-parkowy | leisure=park",
            "x": 1000.0, "y": 1000.0, "registered": False, "location": "exact"}
    palace = {"uid": "osm:way/2", "source": "osm", "name": "Pałac Wiśniowa", "text": "name=Pałac Wiśniowa | historic=castle | building=residential",
              "x": 925.0, "y": 1010.0, "registered": True, "location": "exact"}
    guess = {"uid": "nid:3", "source": "nid", "name": "park pałacowy, Sulisławice", "text": "park pałacowy | park | Sulisławice",
             "x": 1000.0, "y": 1000.0, "registered": True, "location": "osm park near village"}
    (s,) = merge([park, palace, guess])
    assert (s["x"], s["y"]) == (925.0, 1010.0) and s["uid"] == "osm:way/1"       # dossier ID stays the same
    (s,) = merge([park])
    assert (s["x"], s["y"]) == (1000.0, 1000.0)                                    # no house: the park as before


def test_village_entry_joins_only_a_site_in_its_own_village():
    """Sulisławice's register entry (village level) does not join the Wiśniowa palace 736 m away, which lies
    nearer Wiśniowa; it does join a site in Sulisławice."""
    from gutshaus.pipeline import merge
    palace = {"uid": "osm:way/2", "source": "osm", "name": "Pałac Wiśniowa", "text": "name=Pałac Wiśniowa | historic=castle | building=yes",
              "x": 736.0, "y": 0.0, "registered": True, "location": "exact"}
    entry = {"uid": "nid:3", "source": "nid", "name": "park pałacowy, Sulisławice", "text": "park pałacowy | park | Sulisławice",
             "x": 0.0, "y": 0.0, "registered": True, "location": "village centre", "village_xy": (0.0, 0.0), "rivals_xy": [(900.0, 100.0)]}
    sites = merge([palace, entry])
    assert len(sites) == 2 and next(s for s in sites if s["uid"] == "osm:way/2")["name"] == "Pałac Wiśniowa"
    own = {**palace, "uid": "osm:way/4", "name": "Dwór Sulisławice", "x": 200.0}
    assert len(merge([own, entry])) == 1


def test_share_pages_give_link_previews(built, monkeypatch):
    """manormap.eu/s/<ID>: one page per published site with its own preview (title, grades, image) that forwards
    to the map; none for reserved sites; the Share button hands out these links."""
    import json, re
    from gutshaus import config, grades
    monkeypatch.setattr(config, "SITE_URL", "https://example.org")
    sites = grades.all_sites(built)
    (built / "reserved.txt").write_text(sites[0]["uid"] + "\n", encoding="utf-8")
    cli.main(["--out-root", str(built), "publish", "--reserved", str(built / "reserved.txt")])
    pub = built / "site" / "public"
    html = (pub / "index.html").read_text(encoding="utf-8")
    data = json.loads(re.search(r"const D = (\{.*?\});\n", html, re.S).group(1))
    ids = [x["id"] for v in data["villages"] for x in v["sites"]]
    pages = {f.stem for f in (pub / "s").glob("GF-*.html")}
    assert pages == set(ids) and grades.dossier_id(sites[0]["uid"]) not in pages
    v = next(v for v in data["villages"] if v["sites"])
    item = v["sites"][0]
    page = (pub / "s" / f"{item['id']}.html").read_text(encoding="utf-8")
    assert f'<meta property="og:url" content="https://example.org/s/{item["id"]}">' in page
    assert 'content="https://example.org/og.png"' in page and f"Note {item['grade']}" in page
    assert f"Ocena {item['grade']}" in page and v["v"] in page
    assert f'url=/#{item["id"]}' in page and f'location.replace("/#{item["id"]}")' in page
    assert (pub / "og.png").stat().st_size > 20000
    assert '<meta property="og:image" content="https://example.org/og.png">' in html and "__SITE_URL__" not in html
    assert 'new URL("s/" + id' in html
    cfg = json.loads("\n".join(ln for ln in (built / "site" / "wrangler.jsonc").read_text(encoding="utf-8").splitlines()
                               if not ln.startswith("//")))
    assert cfg["assets"]["html_handling"] == "auto-trailing-slash"


def test_status_overrides_from_the_ground(built, monkeypatch):
    """STATUS_OVERRIDES corrects a site's state (an OSM 'Ruiny …' where only foundations remain); the grades follow
    and the note reaches sites.json for the dossier. Foundations alone are no ruin evidence."""
    import json
    from gutshaus import config, grades
    from gutshaus.classify import ruin_evidence
    assert not ruin_evidence("pozostały fundamenty dworu | dwór") or ruin_evidence("ruiny dworu | dwór")
    assert not ruin_evidence("fundamenty dworu | dwór | XIX w.")
    ruin = next(s for s in grades.all_sites(built) if s["status"] == "ruin")
    did = grades.dossier_id(ruin["uid"])
    monkeypatch.setattr(config, "STATUS_OVERRIDES", {did: {"status": "lost", "note": "nur Fundamente erhalten"}})
    cli.main(["--out-root", str(built), "build"])
    s = next(x for x in json.loads((built / "poland" / "sites.json").read_text(encoding="utf-8"))["sites"]
             if x["uid"] == ruin["uid"])
    assert s["status"] == "lost" and s["status_data"] == "ruin" and s["status_note"] == "nur Fundamente erhalten"
    assert grades.path(s) == "new"                                                 # nothing left: rebuilding


def test_house_marked_for_standing_sites():
    """The building a "standing" state rests on is stored (and published) so the map can show it."""
    import numpy as np
    from gutshaus import pipeline
    from gutshaus.geo import pl_to_ll
    from gutshaus.publish import site_item
    x, y = 400000.0, 400000.0
    lon, lat = pl_to_ll(x, y)
    ring = [list(pl_to_ll(x + dx, y + dy)) for dx, dy in ((-150, -150), (150, -150), (150, 150), (-150, 150), (-150, -150))]
    site = lambda **kw: {"status": "standing", "lat": lat, "lon": lon, "parcel_ll": ring, **kw}
    mark = lambda s, pts, areas: (pipeline.mark_houses([s], np.array(pts, dtype=float), np.array(areas, dtype=float)), s.get("house"))
    # the largest big building within 100 m, else the largest on the parcel, else one just outside it
    assert mark(site(), [[x + 60, y + 60], [x + 75, y]], [300, 900])[1][2:] == [900, "near"]
    assert mark(site(), [[x + 140, y]], [500])[1][3] == "on"
    assert mark(site(), [[x + 170, y]], [500])[1][3] == "next"
    assert mark(site(), [[x + 250, y]], [500]) == (0, None)                  # 100 m outside the parcel
    assert mark(site(status="lost"), [[x + 10, y]], [500]) == (0, None)
    assert mark(site(status="unknown"), [[x + 150, y], [x + 190, y]], [400, 800])[1][2:] == [800, "wide"]
    s = site()
    mark(s, [[x + 5, y]], [300])
    assert site_item("GF-1", {**s, "uid": "u"})["h"][2:] == [300, "near"]
    # the land area: a KOWR offer's parcels together, else the site's parcel
    assert site_item("GF-1", {**s, "uid": "u", "parcel_ha": 1.234})["ha"] == 1.23
    assert site_item("GF-1", {**s, "uid": "u", "parcel_ha": 1.2, "offer_ha": 4.5})["ha"] == 4.5
    assert "ha" not in site_item("GF-1", {**s, "uid": "u"})


def test_buildings_on_the_parcel_and_the_house_parcel():
    """A lost manor needs a building on its parcel (somewhere to live while rebuilding) for a green house light;
    a KOWR offer counts all its parcels; a standing manor takes the parcel its house stands on."""
    import numpy as np
    from shapely.geometry import box
    from gutshaus import grades, pipeline
    from gutshaus.geo import pl_to_ll
    x, y = 400000.0, 400000.0
    lon, lat = pl_to_ll(x, y)
    ring = [list(pl_to_ll(x + dx, y + dy)) for dx, dy in ((-50, -50), (50, -50), (50, 50), (-50, 50), (-50, -50))]
    base = {"status": "lost", "lat": lat, "lon": lon, "parcel_ll": ring, "parcel_ha": 1.0, "park": True,
            "dist_power_m": 50, "dist_road_m": 20, "dist_building_m": 100, "dist_town_m": 3000, "protection": "none"}
    read = lambda pts, areas: (lambda bbox: (np.array(pts, dtype=float), np.array(areas, dtype=float)))
    with_house, empty = dict(base), dict(base)
    pipeline.count_buildings([with_house], read([[x + 10, y], [x + 80, y]], [120, 400]))      # one on, one off the parcel
    pipeline.count_buildings([empty], read([[x + 10, y]], [30]))                              # too small to live in
    assert with_house["buildings"] == {"n": 1, "max_m2": 120, "m2": 120} and empty["buildings"]["n"] == 0
    assert grades.lights(with_house)["house"] == {"level": "green", "why": ["lost", "bldg_on"]}
    assert grades.lights(empty)["house"] == {"level": "amber", "why": ["lost", "bldg_none"]}
    assert grades.lights(base)["house"]["level"] == "green"                                   # not counted: as before
    # a KOWR offer: all its parcels count, for buildings and for the area
    sale = {**base, "kowr_uid": "k", "offer_ha": 40.0}
    pipeline.count_buildings([sale], read([[x + 80, y]], [400]), {"k": box(x - 100, y - 100, x + 100, y + 100)})
    assert sale["buildings"]["n"] == 1 and "parcel_large" in grades.lights(sale)["plot"]["why"]
    # standing: the house stands next to the (park) parcel -> the site takes the house's parcel
    from shapely.geometry import Polygon
    house_parcel = Polygon([(x + 60, y - 20), (x + 120, y - 20), (x + 120, y + 20), (x + 60, y + 20)])
    standing = {**base, "status": "standing", "parcel_id": "park/1", "house": [*pl_to_ll(x + 80, y)[::-1], 300, "next"]}
    calls = []
    parcel_at = lambda px, py: calls.append((px, py)) or {"parcel_id": "house/2", "parcel_ha": 0.24, "parcel_geom": house_parcel}
    assert pipeline.house_parcels([standing], parcel_at) == 1
    assert standing["parcel_id"] == "house/2" and standing["park_parcel"] == {"id": "park/1", "ha": 1.0}
    plot = grades.lights(standing)["plot"]
    assert plot["level"] == "amber" and "park_apart" in plot["why"]          # the park may belong to someone else
    assert len(standing["parcel_ll"]) == 5 and "identifyParcel=house/2" in standing["link_geoportal"]
    assert standing["house"][3] == "on"
    inside = {**base, "status": "standing", "parcel_id": "p/3", "house": [lat, lon, 300, "near"]}
    assert pipeline.house_parcels([inside], parcel_at) == 0 and len(calls) == 1            # already on its parcel


def test_publish_with_feedback(built, monkeypatch):
    """With a D1 database and a Turnstile key the site gets a Worker for /api/feedback; without, it stays static."""
    import json, re
    from gutshaus import config
    site = built / "site"
    read_cfg = lambda: json.loads("\n".join(ln for ln in (site / "wrangler.jsonc").read_text(encoding="utf-8").splitlines()
                                            if not ln.startswith("//")))
    data = lambda: json.loads(re.search(r"const D = (\{.*?\});\n", (site / "public" / "index.html").read_text(encoding="utf-8"), re.S).group(1))
    cli.main(["--out-root", str(built), "publish", "--reserved", str(built / "none.txt")])
    assert "main" not in read_cfg() and not (site / "worker.js").exists() and data()["cfg"]["feedback"] == ""
    assert "Rückmeldungen über das Formular" not in (site / "public" / "legal.html").read_text(encoding="utf-8")
    monkeypatch.setattr(config, "FEEDBACK_D1_ID", "db-1")
    monkeypatch.setattr(config, "TURNSTILE_SITE_KEY", "0xKEY")
    cli.main(["--out-root", str(built), "publish", "--reserved", str(built / "none.txt")])
    cfg = read_cfg()
    assert cfg["main"] == "worker.js" and cfg["assets"]["run_worker_first"] == ["/api/*"] and cfg["assets"]["binding"] == "ASSETS"
    assert cfg["d1_databases"] == [{"binding": "DB", "database_name": "manormap-feedback", "database_id": "db-1"}]
    host = config.SITE_URL.split("//")[1].strip("/")
    assert cfg["vars"]["TURNSTILE_HOSTNAMES"] == f"{host},www.{host}"
    worker = (site / "worker.js").read_text(encoding="utf-8")
    assert "/api/feedback" in worker and "siteverify" in worker and "TURNSTILE_SECRET" in worker
    assert 'check.action !== ACTION' in worker and "hosts.includes(check.hostname)" in worker
    assert data()["cfg"]["feedback"] == "0xKEY"
    legal = (site / "public" / "legal.html").read_text(encoding="utf-8")
    assert "Rückmeldungen über das Formular" in legal and "Ich bin Eigentümer" in legal and "Nachweis" in legal


def test_town_register_entries_do_not_decide_the_house():
    """Register entries known only by their town attach to the drawn house but may be other buildings there:
    they neither make it a ruin (Wałbrzych) nor name it (Czempiń). Monastery outbuildings and town houses
    are not estates (Krzeszów)."""
    from shapely.geometry import Point
    from gutshaus.classify import text_flags
    from gutshaus.pipeline import classify_status, merge
    from gutshaus.sources.osm import summarise_context
    osm = {"uid": "osm:way/1", "source": "osm", "name": "Pałac Czettritzów", "location": "exact", "x": 0, "y": 0,
           "text": "name=Pałac Czettritzów | historic=castle | building=university | castle_type=palace",
           "registered": False}
    nid = lambda uid, name: {"uid": uid, "source": "nid", "name": name, "location": "village", "x": 300, "y": 0,
                             "text": f"{name.split(',')[0]} | zamek | XIV w. | Wałbrzych", "registered": True}
    [site] = merge([osm, nid("nid:1", "ruiny zamku Nowy Dwór, Wałbrzych"), nid("nid:2", "pałac Tielsona, Wałbrzych"),
                    nid("nid:4", "kamienica z oficyną, Wałbrzych")])
    assert site["name"] == "Pałac Czettritzów"
    assert classify_status(site, summarise_context(Point(0, 0), [])) == "standing"
    [site] = merge([osm, nid("nid:2", "pałac, Czempiń"), nid("nid:4", "kamienica z oficyną, Czempiń")])
    assert site["name"] == "pałac, Czempiń"                     # one house in the register: its wording
    # a register entry on the house itself still counts
    exact_ruin = {**nid("nid:3", "ruiny pałacu, Wałbrzych"), "location": "exact", "x": 10}
    [site] = merge([osm, exact_ruin])
    assert classify_status(site, summarise_context(Point(0, 0), [])) == "ruin" and site["name"] == "ruiny pałacu, Wałbrzych"
    assert text_flags("oficyna przyklasztorna | oficyna gospodarcza | Krzeszów")["excluded"]
    assert text_flags("kamienica z oficyną | budynek mieszkalny | Czempiń")["excluded"]
    assert not text_flags("kamienica, tzw. pałac Köhna | Włocławek")["excluded"]


@pytest.mark.parametrize("tags, use", [
    ("building=university | historic=castle | castle_type=palace", "school"),
    ("building=yes | historic=manor | tourism=hotel", "hotel"),
    ("building=apartments | historic=manor", "flats"),
    ("building=yes | historic=manor | office=company", "office"),
    ("building=yes | historic=manor", ""),
    ("building=hotel | name=Stajnia", ""),                       # not the house
    ("name=Dom Pomocy Społecznej w Jarogniewicach | building=yes | historic=manor", "care"),
    ("name=Instytut Chemii Bioorganicznej PAN - Pałac w Turwi | building=yes", "school"),
])
def test_house_in_use(tags, use):
    from gutshaus.classify import house_use
    from gutshaus.grades import lights
    text = "name=Pałac " + " | " + tags if "name=" not in tags else tags
    assert house_use(text) == use
    L = lights({"status": "standing", "text": text, "lat": 52, "lon": 17})
    assert L["house"]["level"] == ("amber" if use else "green") and (("use_" + use) in L["house"]["why"]) == bool(use)
