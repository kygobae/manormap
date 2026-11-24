# ManorMap: notes for AI coding assistants

Open-source tool that finds former manor-house sites (dwory, pałace, Gutshäuser) in Poland and ranks them by how suitable they are for building a new house: manor gone, park, utilities, road access, nature protection, land use and parcel size.

- OSM has two interchangeable backends with the same methods: `osm.OverpassOSM` (public API) and `osm_local.LocalOSM` (GeoPackage from `gutshaus osm-extract`, built with pyosmium). Change both together.
- KOWR heritage offers (`sources/kowr.py`): PDF cards from gov.pl/web/kowr/nieruchomosci-zabytkowe, parsed with pypdf and located via ULDK GetParcelByIdOrNr. Never scrape nieruchomoscikowr.gov.pl: it is bot-protected.
- Pipeline: `sources/` (osm/osm_local, nid, uldk, gdos, kowr) -> `pipeline.merge/assess` -> `criteria.compute/score` -> `outputs` (csv, gpkg, sites.json, review.html).
- The dossier PDF generator is closed-source and lives outside this repo. `gutshaus/grades.py` (dossier IDs, the two
  public grades, `all_sites`/`find_site`) is the interface it imports: keep it stable. Don't add PDF generation here.
- Thresholds, keywords, endpoints, criteria and default weights live only in `gutshaus/config.py`. Users override weights in `gutshaus.json`, which is git-ignored.
- `review.html` is a single self-contained file opened from disk (`file://`). Use only base maps that allow that: WMS services such as Geoportal. OSM and CARTO tile servers block it.
- Tests must run offline. `tests/fake_services.py` fakes Overpass, ULDK, the NID API and the GDOŚ WFS. Many sandboxes cannot reach the Polish services.
- The NID register API has no coordinates (NID has no WFS for the register). Historical maps come from pastmaps.cenagis.edu.pl, whose SSL chain is incomplete, hence `truststore`.
- Never commit `output/`, `reviews/*` or `gutshaus.json`: they hold personal data.
- Work on a branch and open a PR. CI runs pytest on Ubuntu and Windows.
- Personal or machine-specific notes belong in `CLAUDE.local.md` (git-ignored).
