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

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
USER_AGENT = "TasPlanReview/0.1 (planning research; contact: set ADMIN_EMAIL)"
THROTTLE_SECONDS = 2.0


def fetch(url, binary=False):
    """Throttled, cached GET. Returns bytes (binary) or str."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = "".join(c if c.isalnum() else "_" for c in url)[-180:]
    path = os.path.join(CACHE_DIR, key + (".bin" if binary else ".txt"))
    if os.path.exists(path):
        mode = "rb" if binary else "r"
        with open(path, mode) as f:
            return f.read()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    if not binary:
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
