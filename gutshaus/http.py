"""HTTP with retries and a simple on-disk cache, so re-runs are fast and polite."""
from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

import requests

from . import config

try:  # use the operating system's certificate store (fixes incomplete chains on some PL servers)
    import truststore
    truststore.inject_into_ssl()
except Exception:  # pragma: no cover
    pass

log = logging.getLogger(__name__)


def write_atomic(path: Path, content: bytes) -> None:
    """Write to a temp file, then rename: an interrupted run never leaves a
    half-written cache entry behind."""
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(content)
    tmp.replace(path)


class Http:
    def __init__(self, cache_dir: Path | None, refresh: bool = False):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.refresh = refresh
        self.s = requests.Session()
        self.s.headers["User-Agent"] = config.USER_AGENT

    def _key(self, method: str, url: str, params, data) -> Path | None:
        if not self.cache_dir:
            return None
        if url.endswith("/interpreter"):  # any Overpass mirror: same answer, same cache entry
            url = "overpass"
        h = hashlib.sha1(repr((method, url, sorted((params or {}).items()), data)).encode()).hexdigest()
        return self.cache_dir / h

    def request(self, method: str, url: str, *, params=None, data=None,
                retries: int = 3, cache: bool = True, validate=None) -> bytes:
        """validate(content) may raise to reject a response: it is then neither
        cached nor returned, and a cached copy that fails it is ignored."""
        key = self._key(method, url, params, data) if cache else None
        if key and key.exists() and not self.refresh:
            content = key.read_bytes()
            try:
                if validate:
                    validate(content)
                return content
            except Exception as e:
                log.warning("Ignoring bad cache entry %s (%s)", key.name, str(e)[:80])
                key.unlink(missing_ok=True)
        last = None
        for attempt in range(retries):
            try:
                r = self.s.request(method, url, params=params, data=data, timeout=config.HTTP_TIMEOUT)
                if r.status_code in (429, 502, 503, 504):
                    raise requests.HTTPError(f"{r.status_code} from {url}")
                if 400 <= r.status_code < 500:  # client errors won't fix themselves
                    raise RuntimeError(f"HTTP {r.status_code} from {r.url}")
                r.raise_for_status()
                if validate:
                    validate(r.content)  # raises: not cached
                if key:
                    write_atomic(key, r.content)
                return r.content
            except requests.RequestException as e:
                last = e
                if attempt + 1 < retries:
                    wait = 5 * (attempt + 1)
                    log.warning("Request failed (%s), retrying in %ss", e, wait)
                    time.sleep(wait)
        raise RuntimeError(f"Giving up on {url}: {last}")

    def get(self, url, params=None, **kw) -> bytes:
        return self.request("GET", url, params=params, **kw)

    def post(self, url, data=None, **kw) -> bytes:
        return self.request("POST", url, data=data, **kw)
