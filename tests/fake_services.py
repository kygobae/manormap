"""Offline stand-ins for Overpass, NID WFS and ULDK, built around a made-up village, Przykładowo."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import geopandas as gpd
from shapely.geometry import Point, box

from gutshaus.geo import ll_to_pl, pl_to_ll

# Approximate locations (fixture only)
SITE_A = (15.9300, 54.1600)
STRZEZENICE = (15.9900, 54.1300)
RUINA = (15.8800, 54.1900)
LOST_PARK = (16.0200, 54.2000)
CHURCH = (15.9310, 54.1650)
BBOX = (15.80, 54.08, 16.10, 54.25)


def _node(i, lonlat, tags):
    return {"type": "node", "id": i, "lon": lonlat[0], "lat": lonlat[1], "tags": tags}


def _square_ll(lonlat, half_m):
    x, y = ll_to_pl(*lonlat)
    sq = box(x - half_m, y - half_m, x + half_m, y + half_m)
    return [{"lon": pl_to_ll(px, py)[0], "lat": pl_to_ll(px, py)[1]} for px, py in sq.exterior.coords]


def overpass_boundary():
    ring = [{"lon": BBOX[0], "lat": BBOX[1]}, {"lon": BBOX[2], "lat": BBOX[1]},
            {"lon": BBOX[2], "lat": BBOX[3]}, {"lon": BBOX[0], "lat": BBOX[3]},
            {"lon": BBOX[0], "lat": BBOX[1]}]
    return {"elements": [{"type": "relation", "id": 1, "tags": {"name": "powiat koszaliński"},
                          "members": [{"type": "way", "role": "outer", "geometry": ring}]}]}


def overpass_candidates():
    return {"elements": [
        {"type": "way", "id": 10, "center": {"lon": SITE_A[0] + 0.0005, "lat": SITE_A[1]},
         "tags": {"leisure": "park", "name": "Park dworski w Przykładowie", "heritage": "2"}},
        _node(11, STRZEZENICE, {"historic": "manor", "name": "Dwór w Strzeżenicach", "name:de": "Streitzig"}),
        _node(12, RUINA, {"historic": "ruins", "ruins": "manor", "name": "Ruiny pałacu"}),
        _node(13, CHURCH, {"historic": "church", "name": "Kościół w Przykładowie", "heritage": "2"}),
        _node(14, (15.95, 54.12), {"railway": "station", "name": "Dworzec Wzorowo"}),
        # settlements and things merely called "Dwór": never sites
        _node(15, (15.96, 54.14), {"place": "hamlet", "name": "Dwór", "source": "PRNG"}),
        _node(16, (15.97, 54.15), {"place": "hamlet", "name": "Zadworze"}),
        _node(17, (15.91, 54.13), {"leisure": "playground", "name": "Podwórko"}),
        {"type": "way", "id": 18, "center": {"lon": 15.90, "lat": 54.20}, "tags": {"building": "industrial", "name": "MEW Dwory"}},
    ]}


def overpass_context():
    els = []
    # Przykładowo: only small buildings, a park and forest -> "lost"
    els.append({"type": "way", "id": 100, "tags": {"building": "yes"}, "geometry": _square_ll((SITE_A[0] + 0.001, SITE_A[1]), 6)})
    els.append({"type": "way", "id": 101, "tags": {"leisure": "park"}, "geometry": _square_ll(SITE_A, 120)})
    els.append({"type": "way", "id": 102, "tags": {"landuse": "forest"}, "geometry": _square_ll(SITE_A, 130)})
    # Strzeżenice: a 30x20 = 600 m² house -> "standing"
    els.append({"type": "way", "id": 110, "tags": {"building": "house"}, "geometry": _square_ll(STRZEZENICE, 13)})
    # Lost park: farmland only
    els.append({"type": "way", "id": 120, "tags": {"landuse": "farmland"}, "geometry": _square_ll(LOST_PARK, 140)})
    return {"elements": els}


def _off(lonlat, dx_m, dy_m):
    x, y = ll_to_pl(*lonlat)
    return pl_to_ll(x + dx_m, y + dy_m)


PLACES = {"Przykładowo": _off(SITE_A, 600, 0), "Kłos": LOST_PARK, "Strzeżenice": _off(STRZEZENICE, 300, 0)}

NID_API_ROWS = [
    {"inspireId": "PL.1.9.ZIPOZ.NID_N_32_ZZ.30986", "nazwa": "park dworski", "funkcja": "ogród",
     "chronologia": "2. poł. XIX w.", "wykazDokumentow": "1001 z 1978-06-12, A-1000 z 2020-07-08",
     "powiat": "koszaliński", "gmina": "Wzorowo", "miejscowosc": "Przykładowo"},
    {"inspireId": "PL.1.9.ZIPOZ.NID_N_32_ZZ.1", "nazwa": "kościół fil.", "funkcja": "kościół",
     "powiat": "koszaliński", "gmina": "Wzorowo", "miejscowosc": "Przykładowo"},
    {"inspireId": "PL.1.9.ZIPOZ.NID_N_32_ZZ.2", "nazwa": "park dworski", "funkcja": "ogród",
     "powiat": "koszaliński", "gmina": "Wzorowo", "miejscowosc": "Kłos"},
    {"inspireId": "PL.1.9.ZIPOZ.NID_N_32_ZZ.3", "nazwa": "dwór", "funkcja": "dwór",
     "powiat": "koszaliński", "gmina": "Wzorowo", "miejscowosc": "Strzeżenice"},
    {"inspireId": "PL.1.9.ZIPOZ.NID_N_32_ZZ.4", "nazwa": "pałac", "funkcja": "pałac",
     "powiat": "koszaliński", "gmina": "Mielno", "miejscowosc": "Nieistniejące"},
]


def nid_gml() -> bytes:
    rows = [
        {"localId": "PL.1.9.ZIPOZ.NID_N_32_BK.123", "siteName": "zespół dworsko-parkowy, Przykładowo",
         "geometry": Point(ll_to_pl(*SITE_A)).buffer(150)},
        {"localId": "PL.1.9.ZIPOZ.NID_N_32_BK.456", "siteName": "park dworski, Kłos",
         "geometry": Point(ll_to_pl(*LOST_PARK)).buffer(100)},
        {"localId": "PL.1.9.ZIPOZ.NID_N_32_BK.789", "siteName": "kościół fil. pw. MB, Przykładowo",
         "geometry": Point(ll_to_pl(*CHURCH)).buffer(15)},
    ]
    gdf = gpd.GeoDataFrame(rows, crs=2180)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "nid.gml"
        gdf.to_file(p, driver="GML")
        return p.read_bytes()


def uldk_response(xy: str) -> bytes:
    x, y, _ = xy.split(",")
    x, y = float(x), float(y)
    poly = box(x - 100, y - 100, x + 100, y + 100)  # 4 ha
    return (f"0\nSRID=2180;{poly.wkt}|320999_2.0001.17|zachodniopomorskie|koszaliński|"
            f"Wzorowo|Przykładowo|17\n").encode()


def _line_ll(points_ll):
    return [{"lon": lo, "lat": la} for lo, la in points_ll]


def overpass_infra():
    """Power line 100 m east of Przykładowo, a road 30 m north, a lake 2.5 km away (out of range)."""
    sx, sy = ll_to_pl(*SITE_A)
    def ll(dx, dy):
        return pl_to_ll(sx + dx, sy + dy)
    return {"elements": [
        {"type": "way", "id": 500, "tags": {"power": "line"}, "geometry": _line_ll([ll(100, -500), ll(100, 500)])},
        {"type": "way", "id": 501, "tags": {"highway": "unclassified"}, "geometry": _line_ll([ll(-400, 30), ll(400, 30)])},
        {"type": "relation", "id": 502, "tags": {"natural": "water"},
         "members": [{"type": "way", "role": "outer", "geometry": _line_ll([ll(2500, 0), ll(2600, 0), ll(2600, 100), ll(2500, 0)])}]},
    ]}


def overpass_building_centres():
    sx, sy = ll_to_pl(*SITE_A)
    pts = [pl_to_ll(sx + 180, sy), pl_to_ll(sx + 300, sy + 50), pl_to_ll(sx + 900, sy)]
    return {"elements": [{"type": "way", "id": 600 + i, "center": {"lon": lo, "lat": la}} for i, (lo, la) in enumerate(pts)]}


def gdos_geojson(layer: str) -> dict:
    """A landscape park covering Przykładowo."""
    if layer != "GDOS:ParkiKrajobrazowe":
        return {"type": "FeatureCollection", "features": []}
    lo, la = SITE_A
    ring = [[lo - 0.02, la - 0.02], [lo + 0.02, la - 0.02], [lo + 0.02, la + 0.02], [lo - 0.02, la + 0.02], [lo - 0.02, la - 0.02]]
    return {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"nazwa": "Park Testowy"},
                                                        "geometry": {"type": "Polygon", "coordinates": [ring]}}]}


TOWNS = {"Koszalin": (16.18, 54.19), "Wzorowo": (15.95, 54.10)}
POWIATS = [("3209", "powiat koszaliński"), ("3206", "powiat gryfiński"), ("1465", "Warszawa")]


KOWR_HTML = """<html><body><h3>Materiały</h3>
<a href="/attachment/11111111-1111-1111-1111-111111111111" class="file-download">Pałac Przykładowo<span>Pałac_Przykładowo_Karta_Nieruchomości.pdf 0.55MB</span></a>
<a href="https://www.gov.pl/attachment/22222222-2222-2222-2222-222222222222">Dwór Nigdzie<span>Dwór_Nigdzie_Karta_Nieruchomości.pdf 0.3MB</span></a>
</body></html>"""

KOWR_CARDS = {
    "11111111-1111-1111-1111-111111111111": """Oddział Terenowy w Szczecinie
* Niniejsza oferta, nie jest ofertą handlową zgodnie z art. 66 Kodeksu Cywilnego
PRZYKŁADOWO, gmina Wzorowo
Powierzchnia
Powierzchnia działki: 4,8000 ha
pow. użytkowa pałacu: 820,5 m2
Lokalizacja
województwo: zachodniopomorskie
powiat: koszaliński
gmina: Wzorowo
obręb: Przykładowo
działka nr: 17, 18
Rodzaj nieruchomości
Nieruchomość gruntowa zabudowana ruiną pałacu, park.
Dominujące przeznaczenie MPZP / SUiKZP / plan ogólny / dec. WZiZT
ZPz - obszary zabytkowych parków
Przewidywany termin procedury przetargowej
I kwartał 2027 r.
Media na nieruchomości lub w pobliżu
elektryczność: tak
gaz: nie
woda: tak
kanalizacja: lokalna
Opis nieruchomości
Ruina pałacu z XIX wieku w zabytkowym parku.
Kontakt
Krajowy Ośrodek Wsparcia Rolnictwa
tel. 91 81 44 200
E-mail: zabytki.szczecin@kowr.gov.pl
""",
    "22222222-2222-2222-2222-222222222222": """Oddział Terenowy we Wrocławiu
NIGDZIE, gmina Nigdzie
Lokalizacja
województwo: dolnośląskie
powiat: trzebnicki
gmina: Nigdzie
obręb: Nigdzie
działka nr: 1/2
""",
}


class FakeHttp:
    calls: list = []

    def __init__(self, *a, **kw):
        pass

    def get(self, url, params=None, **kw):
        FakeHttp.calls.append(("GET", url, params))
        params = params or {}
        if "uldk" in url:
            if params.get("request") == "GetParcelByIdOrNr":
                if params["id"].startswith("Przykładowo"):
                    x, y = ll_to_pl(*SITE_A)
                    return uldk_response(f"{x},{y},2180")
                return b"-1 brak wynikow"
            return uldk_response(params["xy"])
        if "gov.pl/web/kowr" in url:
            return KOWR_HTML.encode()
        if "gov.pl/attachment/" in url:
            return url.rsplit("/", 1)[-1].encode()  # tests patch kowr.pdf_text to map id -> card text
        if "gdos" in url:
            return json.dumps(gdos_geojson(params["TYPENAMES"])).encode()
        if "api.zabytek" in url:
            assert "koszaliński" in params["filter"]
            rows = NID_API_ROWS if params.get("skip", 0) == 0 else []
            return json.dumps({"data": rows}).encode()
        raise RuntimeError(f"unexpected GET {url}")

    def post(self, url, data=None, **kw):
        q = data["data"]
        FakeHttp.calls.append(("POST", url, q[:40]))
        if '"power"~' in q:  # before the boundary check: also contains relation[...] + out geom
            return json.dumps(overpass_infra()).encode()
        if "relation(49715)" in q:
            return json.dumps(overpass_boundary()).encode()
        if '["teryt:terc"](' in q:
            return json.dumps({"elements": [{"type": "relation", "id": i, "tags": {"teryt:terc": c, "name": n}}
                                            for i, (c, n) in enumerate(POWIATS)]}).encode()
        if "relation[" in q and "out geom" in q:
            return json.dumps(overpass_boundary()).encode()
        if 'node["place"~"^(city|town)$"]' in q:
            return json.dumps({"elements": [{"type": "node", "id": 700 + i, "lon": ll[0], "lat": ll[1], "tags": {"place": "town", "name": n}}
                                            for i, (n, ll) in enumerate(TOWNS.items())]}).encode()
        if 'way["building"](around:1500' in q:
            return json.dumps(overpass_building_centres()).encode()
        if "out center tags" in q:
            return json.dumps(overpass_candidates()).encode()
        if 'node["place"' in q:
            assert "out tags" not in q, "place nodes need coordinates"
            return json.dumps({"elements": [{"type": "node", "id": 900 + i, "lon": ll[0], "lat": ll[1],
                                             "tags": {"place": "village", "name": n}}
                                            for i, (n, ll) in enumerate(PLACES.items())]}).encode()
        if "around:" in q and "out center;" in q:  # parks near villages
            return json.dumps({"elements": [{"type": "way", "id": 10, "center": {"lon": SITE_A[0] + 0.0005, "lat": SITE_A[1]}}]}).encode()
        if "around:" in q:
            return json.dumps(overpass_context()).encode()
        raise RuntimeError("unexpected overpass query")


# ------------------------------------------------------------ local OSM fixture
def osm_fixture_xml() -> str:
    """A tiny .osm file with the same world as the Overpass fakes."""
    nodes, ways, rels = [], [], []
    nid = [0]

    def node(lon, lat, tags=None):
        nid[0] += 1
        nodes.append((nid[0], lon, lat, tags or {}))
        return nid[0]

    def ring_ll(lonlat, half_m):
        return [{"lon": c["lon"], "lat": c["lat"]} for c in _square_ll(lonlat, half_m)][:-1]

    def way(coords, tags=None, closed=False):
        ids = [node(c["lon"], c["lat"]) for c in coords]
        if closed:
            ids.append(ids[0])
        ways.append((1000 + len(ways), ids, tags or {}))
        return ways[-1][0]

    def rel(members, tags):
        rels.append((5000 + len(rels), members, tags))

    sx, sy = ll_to_pl(*SITE_A)
    def ll(dx, dy):
        lo, la = pl_to_ll(sx + dx, sy + dy)
        return {"lon": lo, "lat": la}

    # admin: powiat 3209 (bbox square) and Poland (bigger square)
    sq = [{"lon": BBOX[0], "lat": BBOX[1]}, {"lon": BBOX[2], "lat": BBOX[1]}, {"lon": BBOX[2], "lat": BBOX[3]}, {"lon": BBOX[0], "lat": BBOX[3]}]
    rel([("way", way(sq, closed=True), "outer")], {"type": "boundary", "boundary": "administrative", "admin_level": "6",
                                                  "name": "powiat koszaliński", "teryt:terc": "3209"})
    big = [{"lon": 14.1, "lat": 49.0}, {"lon": 24.2, "lat": 49.0}, {"lon": 24.2, "lat": 54.9}, {"lon": 14.1, "lat": 54.9}]
    rel([("way", way(big, closed=True), "outer")], {"type": "boundary", "boundary": "administrative", "admin_level": "2",
                                                   "name": "Polska", "ISO3166-1": "PL"})
    # candidates
    way(ring_ll((SITE_A[0] + 0.0005, SITE_A[1]), 120), {"leisure": "park", "name": "Park dworski w Przykładowie", "heritage": "2"}, closed=True)
    node(*STRZEZENICE, {"historic": "manor", "name": "Dwór w Strzeżenicach", "name:de": "Streitzig"})
    node(*RUINA, {"historic": "ruins", "ruins": "manor", "name": "Ruiny pałacu"})
    node(*CHURCH, {"historic": "church", "name": "Kościół w Przykładowie", "heritage": "2"})
    node(15.96, 54.14, {"place": "hamlet", "name": "Dwór", "source": "PRNG"})       # settlement, not a site
    # context: small building + forest at Przykładowo, 600 m² house at Strzeżenice, farmland at Kłos
    way(ring_ll((SITE_A[0] + 0.001, SITE_A[1]), 6), {"building": "yes"}, closed=True)
    way(ring_ll(SITE_A, 130), {"landuse": "forest"}, closed=True)
    way(ring_ll(STRZEZENICE, 13), {"building": "house"}, closed=True)
    way(ring_ll(LOST_PARK, 140), {"landuse": "farmland"}, closed=True)
    for i, d in enumerate([(180, 0), (300, 50), (900, 0)]):
        way(ring_ll(pl_to_ll(sx + d[0], sy + d[1]), 5), {"building": "yes"}, closed=True)
    # infrastructure near Przykładowo
    way([ll(100, -500), ll(100, 500)], {"power": "line"})
    way([ll(-400, 30), ll(400, 30)], {"highway": "unclassified"})
    way([ll(2500, -300), ll(2500, 300)], {"waterway": "river"})
    # places
    for name, lonlat in PLACES.items():
        node(*lonlat, {"place": "village", "name": name})
    for name, lonlat in TOWNS.items():
        node(*lonlat, {"place": "town", "name": name})

    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<osm version="0.6" generator="test">']
    esc = lambda v: str(v).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
    for i, lon, lat, tags in nodes:
        t = "".join(f'<tag k="{k}" v="{esc(v)}"/>' for k, v in tags.items())
        out.append(f'<node id="{i}" version="1" lat="{lat:.7f}" lon="{lon:.7f}">{t}</node>')
    for i, ids, tags in ways:
        nd = "".join(f'<nd ref="{n}"/>' for n in ids)
        t = "".join(f'<tag k="{k}" v="{esc(v)}"/>' for k, v in tags.items())
        out.append(f'<way id="{i}" version="1">{nd}{t}</way>')
    for i, members, tags in rels:
        m = "".join(f'<member type="{ty}" ref="{ref}" role="{role}"/>' for ty, ref, role in members)
        t = "".join(f'<tag k="{k}" v="{esc(v)}"/>' for k, v in tags.items())
        out.append(f'<relation id="{i}" version="1">{m}{t}</relation>')
    out.append("</osm>")
    return "\n".join(out)
