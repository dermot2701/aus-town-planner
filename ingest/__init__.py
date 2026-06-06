"""Ingestion pipeline for TasPlan Review.

These scripts populate data/scheme_chunks.json and data/decisions.json from the
authoritative sources. They require outbound network access to:
  - tpso.planning.tas.gov.au   (State Planning Provisions + Local Provisions Schedules)
  - austlii.edu.au / tascat.tas.gov.au   (TASCAT Resource & Planning decisions)

Run:
    python -m ingest.scheme
    python -m ingest.decisions

Both write through the same JSON schema the app's retrieve() consumes. Be a good
citizen: requests are throttled and cached under ingest/cache/ (gitignored).
"""

import os
import time
import json
import urllib.request
import http.cookiejar

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
THROTTLE_SECONDS = 2.0

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

_cookie_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cookie_jar))
_warmed = False


def _warm(base_url="https://www8.austlii.edu.au/"):
    """Visit AustLII homepage once to acquire session cookies."""
    global _warmed
    if _warmed:
        return
    try:
        req = urllib.request.Request(base_url, headers=_HEADERS)
        with _opener.open(req, timeout=30):
            pass
        _warmed = True
    except Exception:
        _warmed = True  # don't retry if homepage also blocked


def fetch(url, binary=False):
    """Throttled, cached GET with cookie session. Returns bytes (binary) or str."""
    _warm()
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = "".join(c if c.isalnum() else "_" for c in url)[-180:]
    path = os.path.join(CACHE_DIR, key + (".bin" if binary else ".txt"))
    if os.path.exists(path):
        mode = "rb" if binary else "r"
        with open(path, mode) as f:
            return f.read()
    headers = dict(_HEADERS)
    headers["Referer"] = "https://www8.austlii.edu.au/"
    req = urllib.request.Request(url, headers=headers)
    with _opener.open(req, timeout=60) as resp:
        data = resp.read()
    # Decompress gzip if needed
    if not binary and isinstance(data, bytes):
        import gzip
        try:
            data = gzip.decompress(data)
        except (OSError, gzip.BadGzipFile):
            pass
        data = data.decode("utf-8", errors="replace")
    mode = "wb" if binary else "w"
    with open(path, mode) as f:
        f.write(data)
    time.sleep(THROTTLE_SECONDS)
    return data


def write_data(filename, payload):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, filename), "w") as f:
        json.dump(payload, f, indent=2)
    print(f"wrote data/{filename}")


def read_data(filename):
    """Read a JSON file from the local data dir. Returns None if absent."""
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)
