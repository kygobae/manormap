# ManorMap

Finds former manor-house sites in Poland (dwory, pałace, Gutshäuser of the pre-1945 estates) and ranks them by how suitable they are for **building a new house where the old manor stood**. Ranking criteria: the manor is gone but its park survives, a power line and a public road are close, neighbouring buildings suggest water and sewer connections, the land is not forest, there is no strict nature protection, and the parcel is a sensible size.

Everything comes from open data: OpenStreetMap, the NID monument register, the ULDK cadastre, GDOŚ nature protection areas, and Geoportal and historical map services. The result is a single `review.html` you open in a browser. It has live weight sliders, filters, a Poland minimap, historical maps (Messtischblatt 1:25k), orthophoto, LiDAR relief and utility-line overlays.

## Install

Requires Python 3.10+.

```bash
git clone https://github.com/kygobae/manormap
cd manormap
pip install -e ".[test]"      # or first: conda create -n gutshaus python=3.12 && conda activate gutshaus
pytest -q                     # offline tests
gutshaus check                # are the data services reachable from your machine?
gutshaus osm-extract          # recommended, once: downloads Poland from Geofabrik (~2 GB) and extracts what we need
```

`osm-extract` writes `output/_osm/osm.gpkg` (buildings, roads, power lines, water, land cover, parks, places, boundaries, candidates). From then on, runs read OSM data from this file instead of the public Overpass API. That is much faster, works offline, and keeps load off the free Overpass servers, whose usage policy discourages bulk national queries. To refresh it, delete `output/_osm/poland-latest.osm.pbf` and run it again. Without the extract, or with `--overpass`, the tool queries Overpass, which is fine for a powiat or two.

## Use

```bash
gutshaus run --powiat 3209              # one powiat (TERYT code; `gutshaus powiats` lists them)
gutshaus run --voivodeship 32,08        # whole voivodeships
gutshaus run --all                      # all of Poland: resumable, re-run to continue or retry
gutshaus build                          # combine finished powiats into output/poland/ and refresh output/index.html
```

Open **`output/index.html`**, the start page. It links to the national map and to every powiat (grouped by voivodeship, with site counts and the best sites), and you can search it.

```
output/
  index.html            start page
  poland/               national map, CSV and GeoPackage (gutshaus build)
  powiats/<teryt>/      one folder per powiat
  _osm/                 local OSM extract (gutshaus osm-extract)
```

Each powiat folder contains:

| File | Use |
|---|---|
| `review.html` | The review map: open it in a browser (works from disk; map tiles need internet). |
| `candidates.csv` | All sites with measurements, criterion values and default score. |
| `candidates.gpkg` | For QGIS: layers `sites`, `parcels`, `search_area`. |
| `sites.json` | Raw results; `gutshaus build` combines these. |

With the local OSM extract, a whole-country run spends most of its time on parcel lookups (ULDK, one per site) and the register/protection APIs: expect a few hours. Through Overpass it takes days. Caching makes re-runs quick, and finished powiats are skipped.

## Properties for sale (KOWR)

KOWR, the state agricultural property agency, sells manors and palace/park complexes it owns. It publishes one property card per object on [gov.pl](https://www.gov.pl/web/kowr/nieruchomosci-zabytkowe). Each run reads these cards (cached for a day in `output/_kowr.json`) and locates them by parcel via ULDK. The results appear in three places:
- a blue **€ layer** on every map, with a popup showing area, zoning, tender date, utilities, contact and a link to the PDF card;
- a **"for sale" tag**, filter and criterion on sites within 500 m of an offer;
- a list on the start page.

Registered monuments can be sold at up to 50 % off if the buyer commits to renovating them. The separate KOWR offers portal (nieruchomoscikowr.gov.pl) blocks automated access and is not used. `--skip-kowr` turns this source off.

## How sites are found

1. **Candidates:** OSM features tagged manor/palace/estate/ruins or with estate names (dwór, pałac, park dworski, folwark…), plus estate entries from the NID monument register. The register API has no coordinates, so each entry is placed at the nearest OSM park within 1.5 km of its village, or else at the village centre (status *unlocated*).
2. **Lost or standing:** a building of 250 m² or more within 100 m means *standing*; none means *lost*. Names with "ruina/relikty" mean *ruin*.
3. **Measurements:** distance to the nearest OSM power line, public road, other building, lake or river and town; land use (OSM); nature protection (GDOŚ WFS); and the parcel's ID, outline and area (ULDK).
4. **Score:** each criterion becomes a value from 0 to 1, and the score is their weighted average. You can change the weights live in `review.html`, or set defaults in `gutshaus.json` (copy `gutshaus.example.json`).
   **Keep personal files out of the code folder:** put `gutshaus.json` (and `reserved.txt`) in a separate, private
   folder and point to it once with `setx GUTSHAUS_CONFIG C:\path\to\private\gutshaus.json` (Windows) or
   `export GUTSHAUS_CONFIG=...`. A relative `"RESERVED_FILE": "reserved.txt"` in it is read from that folder.

| Criterion | Default weight | 1.0 means | 0 means |
|---|---|---|---|
| Manor gone | 3 | lost (ruin 0.9, unlocated 0.6, standing 0.15) | |
| Historic park | 2 | park on site | no park |
| Power line | 2 | ≤ 150 m | ≥ 2 km |
| Public road | 2 | ≤ 50 m | ≥ 800 m |
| Existing buildings (water/sewer proxy) | 1.5 | ≤ 200 m | ≥ 1.5 km |
| Land use | 1.5 | farmland (built-up 0.9) | forest 0.15 |
| Nature protection | 2 | none | national park / reserve (landscape park 0.4, Natura 2000 0.6, protected landscape 0.75) |
| Parcel size | 1 | 0.3–15 ha | < 0.1 ha |
| Town | 1 | ≤ 5 km | ≥ 30 km |
| Lake or river | 0.5 | ≤ 300 m | ≥ 2 km |
| Offered for sale by KOWR | 1 | a KOWR heritage card lies within 500 m | none |
| Monument register | 0.5 | listed | not listed |
| Distance to a reference point | 0 (off) | at the point | at your chosen max km |

Unknown values count as 0.5. For *unlocated* sites, infrastructure is treated as unknown, because it was measured at the village centre.

## The review map

- **Sites tab:** ranked list with your current weights. Click a site for the popup: per-criterion bars, all measurements, links to Geoportal, Google satellite and OSM, a high/possible/skip verdict and notes.
- **Weights tab:** a slider per criterion, plus an optional "distance from" criterion with a city or a custom point you click on the map.
- **Filters tab:** status, minimum score, voivodeship/powiat, maximum distance to the reference point, only the current map view, land use, nature protection, power line and road distance, parcel size, park, register entry and verdict.
- **Layers:** Geoportal topographic map, Messtischblatt 1:25k (1870–1945), Karte des Deutschen Reiches 1:100k, orthophoto, LiDAR shaded relief; overlays for parcels, the monument register, utility lines (GESUT, zoom 15+), nature protection and administrative boundaries.
- **Minimap:** Poland with the current view and the shown sites. Click it to jump.
- **Saving:** weights, filters and verdicts are kept in your browser. **Verdicts** exports `verdicts.csv`; put it next to `candidates.csv` or pass `--verdicts` and the next run keeps them. **CSV** exports the sites currently shown.

## Limits: check before you act

- **Distances are a first screen only.** OSM is reasonably complete for medium/high-voltage power lines and roads, but not for low-voltage lines, water or sewer. Use the GESUT utility overlay and ask the operators (warunki przyłączenia).
- **Planning law decides whether you can build.** Since the 2023 reform, a WZ is issued only in an *obszar uzupełnienia zabudowy* of the gmina's general plan (plan ogólny), unless a local plan (MPZP) covers the parcel. Check both. Neither is read automatically yet.
- **Land class:** OSM land use is not the cadastral class (Ls, R, Ps…). Converting forest land is difficult. Check the Geoportal link.
- **Registered monuments** need conservator (WKZ) approval for any work.
- **Buying land:** agricultural land and forest come with legal restrictions (KOWR pre-emption, State Forests). Get legal advice.

## Data sources and licences

| Source | Used for | Licence or terms |
|---|---|---|
| OpenStreetMap (Geofabrik extract or Overpass) | candidates, buildings, roads, power, water, towns, boundaries | ODbL; respect the Overpass usage policy |
| NID open-data API | monument register entries | NID open data |
| ULDK (GUGiK) | parcel ID, outline, area | open (Polish state geodata) |
| GDOŚ WFS/WMS | nature protection areas | open (Polish state data) |
| Geoportal WMS (GUGiK) | topographic map, orthophoto, LiDAR relief, parcels, utilities (KIUT/GESUT), boundaries | open (Polish state geodata) |
| KOWR heritage property cards (gov.pl/web/kowr/nieruchomosci-zabytkowe) | manors and palaces the state is selling: location, parcels, area, zoning, tender date, utilities | public information (gov.pl) |
| pastmaps.cenagis.edu.pl (IH PAN / CENAGIS) | Messtischblatt, Karte des Deutschen Reiches | CC BY-NC 4.0: non-commercial use |

The code is MIT-licensed (see `LICENSE`). Results you generate contain OSM data (ODbL) and must be attributed accordingly.

## Responsible use

Many of these sites are private property or protected monuments. The tool shows public cadastral and register data. It does not identify owners, and it shouldn't be used to harass anyone. Contact owners through proper channels, respect heritage protection, and don't enter private land.

## Public site and dossiers (optional)

You can publish a free, village-level map and sell a site dossier (PDF) per site. Fixed cost: **€0**
(Cloudflare free plan, Lemon Squeezy takes 5 % + 50¢ per sale and handles EU VAT as merchant of record).
The dossier generator itself is not part of this repository: the site only needs one PDF per site, named by its
dossier ID (`GF-` + 8 hex characters, see `gutshaus/grades.py`), in the R2 bucket.

```
gutshaus build                                   # national list -> output/poland/sites.json
gutshaus publish --sample sample.pdf             # -> output/site/public + functions
```

- **Shop switch:** by default the site is the free map only (no buy buttons, no dossier section, no Functions, no
  sales terms on the legal page): a static site. Set `"SHOP_ENABLED": true` in `gutshaus.json` when dossiers are on sale.
- **Public site** (`output/site/public/index.html`, DE/PL/EN): villages at a rounded position (~1 km) with grades A–D or ?,
  five traffic lights and status per site (buy button when the shop is on). No parcel numbers, exact positions or scores. KOWR offers are shown exactly
  (they are public). Sites listed in `reserved.txt` (uid or GF-ID per line, git-ignored) are left out.
- **One grade per site from five traffic lights** (`gutshaus/grades.py`, thresholds in `config.py`; personal
  `gutshaus.json` weights never change them): **house** (standing or gone fits a path; ruin or unclear is amber),
  **park & plot**, **utilities & access** (power, road, neighbours), **protection & planning** (national park or nature
  reserve is red and means D; other protection or forest on the map is amber) and **location** (nearest town).
  A = all five green, B = four green and no red, C = three green and at most one red, D = otherwise; `?` when only the
  village is known. What is left sets the path (standing: renovation, gone: rebuilding, ruin or unclear: either), and
  the map filters by path. The card shows the five lights with their reasons, without distances.

### Deploy the free map (no shop)

With `SHOP_ENABLED` off, `gutshaus publish` writes a static site plus `output/site/wrangler.jsonc` for Cloudflare
(free plan; Pages is now part of Workers, so the site is a Worker with static assets). The custom domains come from
`SITE_URL` (the domain and its `www.` variant); the project name is `DEPLOY_NAME`.

1. Put the domain's DNS on Cloudflare (add the domain in the dashboard, then switch the nameservers at your registrar).
2. Once: `npx wrangler login`.
3. Every update:
   ```
   gutshaus build && gutshaus publish
   cd output/site
   npx wrangler deploy --dry-run    # should list only index.html, legal.html, robots.txt
   npx wrangler deploy
   ```

Run wrangler only inside `output/site`, never in the repository root: without a config there, wrangler's autoconfig
offers to upload the whole `output` folder (OSM extract, your verdicts, caches).

### Setup (once, for selling dossiers)

1. **Lemon Squeezy** (free account, store verification takes a few days): create a product "Standort-Dossier",
   single payment of 49 €. In the product's confirmation settings set the button link to
   `https://<your-domain>/download?o=[order_identifier]` (also used in the receipt e-mail).
   Under Settings → Webhooks add `https://<your-domain>/webhook` with the events `order_created` and
   `order_refunded`, and choose a signing secret. Put the product's checkout link in `gutshaus.json`:
   `{"CHECKOUT_URL": "https://<store>.lemonsqueezy.com/checkout/buy/<id>", "SITE_URL": "https://<your-domain>",
   "CONTACT_EMAIL": "you@example.org"}`.
2. **Cloudflare** (free plan; R2 asks for a payment method but stays free up to 10 GB):
   ```
   npm install -g wrangler && wrangler login
   wrangler r2 bucket create gutshaus-dossiers
   wrangler kv namespace create ORDERS          # copy the id into output/site/wrangler.toml
   ```
3. **Upload dossiers** to the bucket under `dossiers/`, e.g. with rclone (R2 S3 API token):
   `rclone copy output/dossiers r2:gutshaus-dossiers/dossiers`, or one file:
   `wrangler r2 object put gutshaus-dossiers/dossiers/GF-1A2B3C4D.pdf --file output/dossiers/GF-1A2B3C4D.pdf --remote`.
4. **Deploy** from `output/site`: `wrangler pages deploy`, then once
   `wrangler pages secret put LS_SIGNING_SECRET` (the webhook secret from step 1).
   Own domain (free): Cloudflare dashboard → Workers & Pages → the project → Custom domains → add it
   (easiest with the domain's DNS on Cloudflare), and set `SITE_URL` to it in `gutshaus.json`.
5. Fill in the legal notice in `output/site/public/legal.html` (name, address, NIP) before going live.

How a sale works: the buy button opens the Lemon Squeezy checkout with the dossier ID as custom data → the webhook
(`functions/webhook.js`) verifies the signature and stores order → dossier in KV → the buyer's button/receipt link
(`functions/download.js`) streams the PDF from R2. If a PDF is missing, the buyer sees a "within one working day"
note and the order is logged in KV under `missing:<GF-ID>`.

## Future extensions

- **KOWR tender schedules: done, with gaps.** `sources/kowr_schedules.py` reads the monthly tender schedules on the
  branches' gov.pl pages (`ot-<branch>2`), keeps sales that mention a manor, palace, estate park or a monument
  entry, and adds them to the KOWR offers with their starting price (cena wywoławcza) and any discount. It reads the
  labelled, the slash and the bare-column layouts, for all 17 branches (found via `/web/kowr/harmonogramy`).
  Gaps: Warszawa's schedule table is an image inside the PDF (would need OCR), and new layouts may need another
  pattern; a branch without a readable schedule is logged.

## Contributing

See `CONTRIBUTING.md`. Issues and pull requests are welcome, in English, Polish or German.
