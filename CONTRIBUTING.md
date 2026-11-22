# Contributing

Issues and pull requests are welcome, in English, Polish or German.

- **Setup:** `pip install -e ".[test]"`, then `pytest -q`. The tests run offline against fake services (`tests/fake_services.py`), so they never touch the real APIs.
- **Data sources:** if a service changes (a WMS layer is renamed, an API moves), `gutshaus check` shows which one. Endpoints and layer names live in `gutshaus/config.py`.
- **New criteria:** add the measurement in `sources/` or `pipeline.py`, the 0..1 conversion in `criteria.py`, and an entry in `config.CRITERIA`. The review page picks it up automatically. Add a test.
- **Be polite to free services:** keep the pauses in `config.py` (`OVERPASS_PAUSE_S`, `ULDK_PAUSE_S`), and don't parallelise requests to Overpass or ULDK.
- **Personal data:** never commit outputs, verdicts or anything that identifies landowners.
