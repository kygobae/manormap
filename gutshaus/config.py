"""All tunable settings in one place. Edit here, not in the code."""
import re

# --- Endpoints -------------------------------------------------------------
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

ULDK_URL = "https://uldk.gugik.gov.pl/"

# NID open-data API (register of immovable monuments, księga A). Returns names,
# functions and village names but no coordinates; sites are placed at the
# village (OSM place node) or the nearest OSM park.
NID_API_URL = "https://api.zabytek.gov.pl/nidrestapi/api/data/geoportal/otwarteDaneZestawienieZrn"
NID_PAGE_SIZE = 500
NID_PLACE_RADIUS_M = 1500     # search radius around the village for a park to snap to
NID_ATTACH_RADIUS_M = 1000    # attach a village-located register entry to an OSM site this close

# WMS layers shown in review.html (name, url, layers, attribution).
# Layer names marked "verify" are best guesses: if a layer stays blank,
# open <url>?SERVICE=WMS&REQUEST=GetCapabilities and fix the name here.
WMS_LAYERS = [
    {"name": "Topographic map (Geoportal)",
     "url": "https://mapy.geoportal.gov.pl/wss/service/img/guest/TOPO/MapServer/WMSServer",
     "layers": "Raster",
     "attribution": "GUGiK", "default": True},
    {"name": "Messtischblatt 1:25k (1870-1945)",
     "url": "https://pastmaps.cenagis.edu.pl/geoserver/ihpan/wms",
     "layers": "m25k_3857",
     "attribution": "ZAH IH PAN / CENAGIS, CC BY-NC 4.0"},
    {"name": "Karte des Deutschen Reiches 1:100k (~1900)",
     "url": "https://pastmaps.cenagis.edu.pl/geoserver/ihpan/wms",
     "layers": "kdr_3857",
     "attribution": "ZAH IH PAN / CENAGIS, CC BY-NC 4.0"},
    {"name": "Orthophoto (Geoportal)",
     "url": "https://mapy.geoportal.gov.pl/wss/service/PZGIK/ORTO/WMS/StandardResolution",
     "layers": "Raster",
     "attribution": "GUGiK"},
    {"name": "LiDAR shaded relief (Geoportal)",
     "url": "https://mapy.geoportal.gov.pl/wss/service/PZGIK/NMT/GRID1/WMS/ShadedRelief",
     "layers": "Raster",
     "attribution": "GUGiK"},
    {"name": "Administrative boundaries (Geoportal)",
     "url": "https://mapy.geoportal.gov.pl/wss/service/img/guest/Administracyjna/MapServer/WMSServer",
     "layers": "Administracyjna",
     "attribution": "GUGiK", "overlay": True},
    {"name": "Parcels (KIEG)",
     "url": "https://integracja.gugik.gov.pl/cgi-bin/KrajowaIntegracjaEwidencjiGruntow",
     "layers": "dzialki,numery_dzialek",
     "attribution": "GUGiK KIEG", "overlay": True},
    {"name": "Monument register (NID)",
     "url": "https://usluga.zabytek.gov.pl/INSPIRE_IMD/service.svc/get",
     "layers": "Immovable_Monuments",
     "attribution": "NID", "overlay": True},
    {"name": "Utility lines: power, gas, water, sewer (GESUT/KIUT)",
     "url": "https://integracja.gugik.gov.pl/cgi-bin/KrajowaIntegracjaUzbrojeniaTerenu",
     "layers": "przewod_elektroenergetyczny,przewod_gazowy,przewod_wodociagowy,przewod_kanalizacyjny,przewod_cieplowniczy",
     "attribution": "GUGiK KIUT", "overlay": True, "minZoom": 15},
    {"name": "Nature protection (GDOŚ)",
     "url": "https://sdi.gdos.gov.pl/wms",
     "layers": "GDOS:ParkiNarodowe,GDOS:Rezerwaty,GDOS:ParkiKrajobrazowe,GDOS:ObszaryChronionegoKrajobrazu,"
               "GDOS:SpecjalneObszaryOchrony,GDOS:ObszarySpecjalnejOchrony",
     "attribution": "GDOŚ", "overlay": True, "opacity": 0.5},
]

# --- Nature protection (GDOŚ WFS) ------------------------------------------
# layer -> (label, suitability for building a house: 1 = no restriction, 0 = practically impossible)
GDOS_WFS_URL = "https://sdi.gdos.gov.pl/wfs"
GDOS_LAYERS = {
    "GDOS:ParkiNarodowe": ("national park", 0.0),
    "GDOS:Rezerwaty": ("nature reserve", 0.0),
    "GDOS:ParkiKrajobrazowe": ("landscape park", 0.4),
    "GDOS:ZespolyPrzyrodniczoKrajobrazowe": ("nature-landscape complex", 0.3),
    "GDOS:UzytkiEkologiczne": ("ecological site", 0.3),
    "GDOS:SpecjalneObszaryOchrony": ("Natura 2000 (habitats)", 0.6),
    "GDOS:ObszarySpecjalnejOchrony": ("Natura 2000 (birds)", 0.6),
    "GDOS:ObszaryChronionegoKrajobrazu": ("protected landscape area", 0.75),
}

# --- Scoring criteria --------------------------------------------------------
# Each site gets a value 0..1 per criterion; the score is the weighted mean x 100.
# Weights here are defaults: review.html lets you change them live, and a
# gutshaus.json in the working folder can override them (see README).
# `ramp`: (full_at, zero_at) distance in metres, linear in between.
CRITERIA = [
    {"key": "gone", "label": "Manor gone (room to build)", "weight": 3,
     "help": "lost 1 · unlocated 0.6 · unknown 0.5 · ruin 0.3 · standing 0.15"},
    {"key": "remains", "label": "Manor remains (to renovate)", "weight": 0,
     "help": "standing 1 · ruin 0.6 · lost 0 · unlocated/unknown ? (used by the renovation grade)"},
    {"key": "park", "label": "Historic park on site", "weight": 2,
     "help": "park mapped nearby or named in the register"},
    {"key": "power", "label": "Power line nearby", "weight": 2, "ramp": (150, 2000),
     "help": "distance to the nearest OSM power line"},
    {"key": "road", "label": "Public road access", "weight": 2, "ramp": (50, 800),
     "help": "distance to the nearest public road (OSM)"},
    {"key": "neighbours", "label": "Near existing buildings (water/sewer)", "weight": 1.5, "ramp": (200, 1500),
     "help": "nearest other building: a proxy for water, sewer and grid connections"},
    {"key": "landuse", "label": "Land use allows building", "weight": 1.5,
     "help": "farmland 1 · built-up 0.9 · unknown 0.5 · forest 0.15 (forest conversion is hard)"},
    {"key": "protection", "label": "No nature protection", "weight": 2,
     "help": "none 1 · protected landscape 0.75 · Natura 2000 0.6 · landscape park 0.4 · reserve/national park 0"},
    {"key": "parcel", "label": "Parcel size 0.3–15 ha", "weight": 1,
     "help": "very small or very large parcels score lower"},
    {"key": "town", "label": "Town within reach", "weight": 1, "ramp": (5000, 30000),
     "help": "distance to the nearest town or city"},
    {"key": "water", "label": "Lake or river nearby", "weight": 0.5, "ramp": (300, 2000),
     "help": "distance to a lake or river"},
    {"key": "forsale", "label": "Offered for sale by KOWR", "weight": 1,
     "help": "a KOWR heritage property card exists for this site (state sale, price reductions possible)"},
    {"key": "register", "label": "Listed in monument register", "weight": 0.5,
     "help": "evidence the estate existed; note that registered sites need conservator approval"},
]

VOIVODESHIPS = {
    "02": "dolnośląskie", "04": "kujawsko-pomorskie", "06": "lubelskie", "08": "lubuskie",
    "10": "łódzkie", "12": "małopolskie", "14": "mazowieckie", "16": "opolskie",
    "18": "podkarpackie", "20": "podlaskie", "22": "pomorskie", "24": "śląskie",
    "26": "świętokrzyskie", "28": "warmińsko-mazurskie", "30": "wielkopolskie", "32": "zachodniopomorskie",
}
# --all runs voivodeships in this order (largely former German territories first)
VOIVODESHIP_ORDER = ["32", "08", "02", "16", "28", "22", "30", "04", "24",
                     "10", "14", "12", "18", "06", "20", "26"]

# Reference points offered in review.html for "distance from ..." filters/criterion
REFERENCE_POINTS = {
    "Berlin": (52.520, 13.405), "Hamburg": (53.551, 9.993), "Dresden": (51.050, 13.738),
    "Szczecin": (53.429, 14.553), "Poznań": (52.406, 16.925), "Wrocław": (51.108, 17.039),
    "Gdańsk": (54.352, 18.647), "Warszawa": (52.230, 21.011), "Kraków": (50.062, 19.937),
}

# --- Pilot area ------------------------------------------------------------
DEFAULT_POWIAT = "3209"  # powiat koszaliński

# --- Candidate detection ---------------------------------------------------
# Polish estate vocabulary (matched case-insensitively, also without diacritics)
ESTATE_PATTERN = re.compile(
    # "dwór" only at the start of a word: not "podwórko" (courtyard), "Zadworze" (behind the manor: a place name)
    # or "Dworakowski"; and not "dworzec" (station), "Dworzysko", "Dwory" (place names)
    r"\bdw[oó]r(?!zec|ca|co|ak|zysk|y\b)|dworsk|pa[lł]ac|folwar|maj[aą]t|rz[aą]dc[oó]wk|oficyn|"
    r"zesp[oó][lł] (dworsk|pa[lł]acow|folwarcz)|park (pod)?dworsk|park pa[lł]acow|"
    r"gutshaus|herrenhaus|rittergut|schloss",
    re.IGNORECASE,
)
PARK_PATTERN = re.compile(r"park|ogr[oó]d", re.IGNORECASE)
# Ruin words; they only count next to the house ("ruiny dworu"), see classify.ruin_evidence
RUIN_PATTERN = re.compile(r"ruin|relikt|pozosta[lł]o|resztk|fundament", re.IGNORECASE)
# Things we never want even if a keyword matches
EXCLUDE_PATTERN = re.compile(
    r"ko[sś]ci[oó][lł]|cmentarz|kaplic|plebani|szko[lł]|dworzec|stacj|"
    r"m[lł]yn|wiatrak|spichl|stodo[lł]|kamienica|ratusz",
    re.IGNORECASE,
)
# An OSM object that matches only by name is not an estate if it is one of these: settlements named
# "Dwór", "Zadworze", "Kuków-Folwark" (place=*, mostly PRNG imports), roads, stations, shops, plants ...
NON_ESTATE_KEYS = ("place", "highway", "railway", "public_transport", "amenity", "shop", "office", "power",
                   "aeroway", "waterway", "boundary", "man_made", "route")
# ... or a memorial, wayside cross, artwork or viewpoint named after the manor
NON_ESTATE_HISTORIC = {"memorial", "monument", "wayside_cross", "wayside_shrine", "tomb", "milestone",
                       "boundary_stone"}
NON_ESTATE_TOURISM = {"artwork", "viewpoint", "information"}
NON_ESTATE_BUILDINGS = {"industrial", "commercial", "retail", "warehouse", "garage", "garages", "shed", "service",
                        "church", "chapel", "school", "train_station", "transportation", "kiosk", "hangar"}
OSM_HISTORIC_VALUES = ["manor", "castle", "estate", "ruins", "building", "palace"]
OSM_CASTLE_TYPES = ["manor", "palace", "stately", "schloss"]

# --- Spatial thresholds (metres, EPSG:2180) ---------------------------------
MERGE_DISTANCE_M = 250       # candidates closer than this are one site
CONTEXT_RADIUS_M = 150       # radius for buildings/parks/land cover
MANOR_BUILDING_MIN_M2 = 250  # footprint above which a building could be the manor
MANOR_SEARCH_RADIUS_M = 100  # big building must be this close to count as "standing"
# "lost" needs no big building within this radius either; one at 100-200 m makes the status "unknown"
# (site points are often park centres or guesses, and the manor can stand at the edge of its park)
MANOR_LOST_RADIUS_M = 200
# A big building on the site's parcel, or this close outside it, counts as the standing manor (the parcel may have
# been split around the house). Only buildings within PARCEL_SEARCH_MAX_M of the site point are considered, so a
# large farm parcel does not pull in far-away barns.
PARCEL_MARGIN_M = 30
PARCEL_SEARCH_MAX_M = 300
MIN_PARCEL_HA = 0.3
MAX_PARCEL_HA = 30

# --- Network ----------------------------------------------------------------
HTTP_TIMEOUT = (15, 180)   # (connect, read) seconds
USER_AGENT = "gutshaus-finder/0.2 (+https://github.com/kygobae/manormap)"
CONTEXT_BATCH = 40           # candidates per Overpass context query
KOWR_MATCH_RADIUS_M = 500    # a KOWR offer this close to a site counts as "for sale"
# Surroundings of one site (dossier): kind -> search radius in metres. Everyday services, and what may disturb
# (wind turbines, main roads, railways). See osm.poi_kind for the OSM tags of each kind.
SURROUNDINGS_RADIUS_M = {"wind": 3000, "main_road": 2000, "railway": 2000, "station": 15000, "school": 15000,
                         "doctor": 15000, "pharmacy": 15000, "shop": 15000}
MAIN_ROADS = ("motorway", "trunk", "primary")
# KOWR branches, only used if the overview gov.pl/web/kowr/harmonogramy (which links every branch's schedule page)
# is unavailable: then gov.pl/web/kowr/ot-<branch>2 is tried. Wrocław also has heritage cards.
KOWR_BRANCHES = ["bialystok", "bydgoszcz", "czestochowa", "gorzow", "kielce", "koszalin", "krakow", "lodz", "lublin",
                 "olsztyn", "opole", "poznan", "pruszcz-gdanski", "rzeszow", "szczecin", "warszawa", "wroclaw"]
KOWR_PAUSE_S = 1.0           # politeness pause between gov.pl requests
INFRA_BATCH = 10             # candidates per Overpass infrastructure query
OVERPASS_PAUSE_S = 1.0       # politeness pause between Overpass queries
ULDK_PAUSE_S = 0.3


# --- Local overrides ---------------------------------------------------------
def load_overrides(path=None) -> str | None:
    """Apply a JSON file of overrides, e.g.
    {"weights": {"power": 3, "water": 0}, "USER_AGENT": "my-tool (me@example.org)"}.
    Uses the given path, else $GUTSHAUS_CONFIG, else ./gutshaus.json. Keeping the
    file outside the code folder (e.g. in a private repo) keeps personal settings out
    of the public one. Relative RESERVED_FILE paths are read from the file's folder.
    Returns the file used."""
    import json
    import os
    from pathlib import Path
    path = path or os.environ.get("GUTSHAUS_CONFIG")
    p = Path(path) if path else Path("gutshaus.json")
    if not p.exists():
        if path:
            raise FileNotFoundError(p)
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    for key, w in (data.pop("weights", None) or {}).items():
        for c in CRITERIA:
            if c["key"] == key:
                c["weight"] = float(w)
    g = globals()
    for k, v in data.items():
        if k.isupper() and k in g:
            g[k] = v
    if "RESERVED_FILE" in data and not Path(data["RESERVED_FILE"]).is_absolute():
        g["RESERVED_FILE"] = str(p.resolve().parent / data["RESERVED_FILE"])
    return str(p)


# --- Grades -------------------------------------------------------------------
# Grade from the default-weight score (0-100). Shown on the public site and in dossiers.
# Calibrated on the national run of 2026-11 (10,873 sites): A is meant to be rare (about 4 % of sites per
# profile). Unlocated sites score about 56 (mostly unknown values) and so get D. Recheck after big data changes.
GRADES = [("A", 85), ("B", 75), ("C", 62), ("D", 0)]
# Snapshot before any gutshaus.json overrides: public grades never depend on personal weights.
DEFAULT_WEIGHTS = {c["key"]: c["weight"] for c in CRITERIA}
# Two public grades: building new where the manor stood, or renovating what is left.
# For renovation the register entry is a plus: subsidies exist only for registered monuments.
PROFILES = {
    "new": DEFAULT_WEIGHTS,
    "renovation": {"gone": 0, "remains": 5, "register": 2, "park": 1.5, "power": 1.5, "road": 1.5,
                   "neighbours": 0.5, "landuse": 0.5, "protection": 1.5, "parcel": 1, "town": 1,
                   "water": 0.5, "forsale": 1.5},
}
RENOVATION_CAP_LOST = "C"  # best renovation grade when the manor is gone
NEW_CAP_STANDING = "C"     # best new-build grade while the manor stands or is a ruin (no free room in its place)

# Shop: buy buttons, the dossier section and the Cloudflare Functions (webhook, download). Off until the dossier
# is ready to sell; switch on with {"SHOP_ENABLED": true} in gutshaus.json.
SHOP_ENABLED = False
# Cloudflare project (Worker) name the site is deployed to; custom domains come from SITE_URL
DEPLOY_NAME = "manormap"
# Cloudflare Web Analytics site token (dashboard -> Analytics & Logs -> Web Analytics): cookie-free visitor counts.
# Empty: no analytics script at all. The privacy text on legal.html follows this setting.
WEB_ANALYTICS_TOKEN = ""
# True when Cloudflare injects Web Analytics itself (dashboard: Web Analytics -> "Enable", automatic injection):
# no script from us, but the privacy text must still mention it.
WEB_ANALYTICS_AUTO = False
# Legal notice (Impressum) lines for legal.html, e.g. ["Example Ltd", "Owner: Jane Doe", "Street 1, 00-000 City",
# "NIP 0000000000"]. Personal data: set it in your private gutshaus.json, never here.
LEGAL_NOTICE: list = []
DOSSIER_PRICE = "49 €"
# Public site and shop (set in gutshaus.json; placeholders until you have them)
# Brand: ManorMap is the umbrella name (site title in PL/EN, legal page); "Gutshaus Finder"
# stays the German product name (German site title, dossier).
SITE_NAME = "ManorMap"
SITE_NAME_DE = "Gutshaus Finder"
SITE_URL = "https://manormap.eu"
# Lemon Squeezy checkout link of the dossier variant, e.g. https://yourstore.lemonsqueezy.com/checkout/buy/<variant-uuid>
# (the site adds ?checkout[custom][dossier]=GF-XXXXXXXX)
CHECKOUT_URL = ""
CONTACT_EMAIL = ""
# Sites kept off the public site (and out of bulk dossier runs) (one GF-ID or uid per line)
RESERVED_FILE = "reserved.txt"
