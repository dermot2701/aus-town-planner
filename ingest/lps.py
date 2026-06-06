"""Ingest per-municipality Local Provisions Schedules (LPS) from the TPSO viewer.

The State Planning Provisions (ingest/scheme.py) are the statewide rulebook. Each
of Tasmania's councils then has a Local Provisions Schedule that applies the SPP
locally — particular purpose zones, specific area plans, site-specific
qualifications, and code overlays. The LPS lives in the dynamic TPSO viewer, which
is a JavaScript SPA backed by a small JSON/HTML API:

    /planning-scheme-viewer/{schemeId}/init
        -> {"title": "Tasmanian Planning Scheme - Glenorchy",
            "openSectionId": 2096,                 # the LPS root section
            "planningSchemeTypeName": "Local Provisions Schedule"}
    /planning-scheme-viewer/{schemeId}/document-tree?effectiveForDate=YYYY-MM-DD
        -> nested {name, sectionId, children, orderNum}
    /planning-scheme-viewer/{schemeId}/section/{sectionId}/html?includeChildren=true&effectiveForDate=...
        -> rendered HTML of that section and all descendants (the clause text)

So per council it's: init -> openSectionId -> fetch that section's HTML with
includeChildren -> parse clauses. Each chunk is written with scope = the council
name (lowercased) so the app's retrieve() pulls it in for that municipality
alongside the statewide SPP. Output schema matches ingest/scheme.py exactly.

This MERGES into data/scheme_chunks.json by default: it keeps the statewide SPP
chunks (and any other councils' LPS) and replaces only the chunks for the councils
it ingests this run. Run ingest/scheme.py first so the SPP chunks exist.

Usage:
    python -m ingest.lps --discover                 # probe ids, print id->council map
    python -m ingest.lps                            # discover + ingest every LPS
    python -m ingest.lps --scheme-id 15 --scheme-id 19   # specific councils only
    python -m ingest.lps --replace                  # overwrite scheme_chunks.json (SPP is dropped!)

Writes (merging): data/scheme_chunks.json, data/scheme_manifest.json
"""

import os
import re
import json
import time
import gzip
import datetime
import argparse
import urllib.request
import urllib.error
from html.parser import HTMLParser

from . import CACHE_DIR, DATA_DIR, THROTTLE_SECONDS, write_data, read_data

TPSO_BASE = "https://tpso.planning.tas.gov.au/tpso/external/planning-scheme-viewer"

# LPS clause/zone headings carry a council prefix, e.g. GLE-S1.0, KIN-P2.1, HOB-C3.0.
CLAUSE_RE = re.compile(r"(?m)^\s*([A-Z]{2,4}-[A-Z]?\d+(?:\.\d+){0,3})\s+(.{3,100}?)\s*$")

_NOISE = re.compile(
    r"(?im)^\s*(page\s+\d+|tasmanian planning scheme.*|local provisions schedule\s*)\s*$"
)

_STOP = {"the", "and", "for", "any", "are", "not", "with", "this", "that", "from",
         "must", "may", "have", "which", "use", "development", "planning", "scheme",
         "zone", "area", "plan", "provisions", "local", "schedule"}

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/html, */*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://tpso.planning.tas.gov.au/tpso/external/tasmanian-planning-scheme",
}


def _today():
    return datetime.date.today().isoformat()


# The TPSO server intermittently returns its SPA error shell ("This website
# requires Javascript to run") instead of the API payload. It's short and never
# valid content, so we detect it, never cache it, and retry on it.
_ERROR_MARKERS = ("This website requires Javascript to run", "<title>Error")


def _is_error_page(text):
    return len(text) < 400 or any(m in text for m in _ERROR_MARKERS)


def _fetch(url):
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    try:
        data = gzip.decompress(data)
    except (OSError, gzip.BadGzipFile):
        pass
    return data.decode("utf-8", errors="replace")


def _get(url, as_json=False, retries=4):
    """Throttled, cached GET to the TPSO API. Returns parsed JSON or text.

    Retries on the transient error shell and only caches good responses, so a
    flaky fetch never gets pinned in the cache. Pass retries=0 for discovery
    probes, where an error shell legitimately means "no such scheme".
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = "".join(c if c.isalnum() else "_" for c in url)[-180:]
    path = os.path.join(CACHE_DIR, key + ".txt")
    if os.path.exists(path):
        with open(path) as f:
            cached = f.read()
        if not _is_error_page(cached):            # ignore a previously-cached error
            return json.loads(cached) if as_json else cached

    delay = 2.0
    text = _fetch(url)
    attempt = 0
    while _is_error_page(text) and attempt < retries:
        time.sleep(delay)
        delay = min(delay * 2, 16.0)
        attempt += 1
        text = _fetch(url)

    if not _is_error_page(text):                  # cache good responses only
        with open(path, "w") as f:
            f.write(text)
    time.sleep(THROTTLE_SECONDS)
    return json.loads(text) if as_json else text


class _TextExtractor(HTMLParser):
    """Flatten rendered HTML to text, inserting newlines at block boundaries so
    clause headings land on their own line for the regex."""
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
             "table", "section", "header"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _html_to_text(html):
    p = _TextExtractor()
    p.feed(html)
    text = "".join(p.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]*\n+", "\n", text)
    return text


def _clean_body(body):
    body = _NOISE.sub("", body)
    return re.sub(r"\s+", " ", body).strip()


def _kind(clause_id, title):
    t = title.lower()
    if "use table" in t:
        return "use_table"
    if "specific area plan" in t or re.search(r"-S\d", clause_id):
        return "specific_area_plan"
    if "particular purpose" in t or re.search(r"-P\d", clause_id):
        return "particular_purpose_zone"
    if re.search(r"-C\d", clause_id):
        return "code_overlay"
    if re.search(r"\.\d+\.\d+$", clause_id):
        return "standard"
    return "provision"


def chunk_lps(text, municipality):
    """Split LPS section text into deduplicated clause chunks."""
    scope = municipality.lower()
    matches = list(CLAUSE_RE.finditer(text))
    best = {}
    for i, m in enumerate(matches):
        cid = m.group(1).strip()
        title = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = _clean_body(text[start:end])
        digit_ratio = sum(c.isdigit() for c in body) / max(len(body), 1)
        if len(body) < 40 or digit_ratio > 0.4:
            continue
        if cid not in best or len(body) > len(best[cid][1]):
            best[cid] = (title, body)

    chunks = []
    for cid, (title, body) in sorted(best.items()):
        kind = _kind(cid, title)
        use_classes = []
        if kind == "use_table":
            for label in ("No Permit Required", "Permitted", "Discretionary", "Prohibited"):
                if label in body:
                    use_classes.append(label)
        words = re.findall(r"[a-z]{4,}", (title + " " + body[:300]).lower())
        keywords = sorted({w for w in words if w not in _STOP})[:12]
        chunks.append({
            "clause_id": cid,
            "instrument": "LPS",
            "scope": scope,
            "kind": kind,
            "zone_or_code": title,
            "title": title,
            "text": body[:1500],
            "keywords": keywords,
            "use_classes": use_classes,
            "provenance": "LIVE",
        })
    return chunks


def _municipality(title):
    """'Tasmanian Planning Scheme - Glenorchy' -> 'Glenorchy'."""
    return title.split("-")[-1].strip() if "-" in title else title.strip()


# Stop discovery after this many consecutive ids yield no LPS scheme, so a probe
# doesn't grind through dozens of non-existent ids (the scheme ids are contiguous).
_DISCOVER_MISS_LIMIT = 8


def discover(id_range):
    """Probe /init across id_range; return [{id, municipality, root}] for LPS schemes.

    Probes fail fast (retries=0): an error shell here means "no such scheme".
    Stops after _DISCOVER_MISS_LIMIT consecutive misses.
    """
    found = []
    misses = 0
    for sid in id_range:
        try:
            init = _get(f"{TPSO_BASE}/{sid}/init", as_json=True, retries=0)
        except (urllib.error.HTTPError, json.JSONDecodeError, ValueError):
            misses += 1
            if misses >= _DISCOVER_MISS_LIMIT:
                break
            continue
        except Exception as e:
            print(f"[lps] /{sid}/init error: {e}")
            misses += 1
            if misses >= _DISCOVER_MISS_LIMIT:
                break
            continue
        if init.get("planningSchemeTypeName") != "Local Provisions Schedule":
            misses += 1
            if misses >= _DISCOVER_MISS_LIMIT:
                break
            continue
        root = init.get("openSectionId")
        if not root:
            misses += 1
            continue
        misses = 0
        muni = _municipality(init.get("title", f"scheme {sid}"))
        found.append({"id": sid, "municipality": muni, "root": root})
        print(f"[lps]   {sid} -> {muni} (LPS root section {root})")
    return found


def ingest_scheme(sid, municipality, root, effective):
    url = (f"{TPSO_BASE}/{sid}/section/{root}/html"
           f"?effectiveForDate={effective}&includeChildren=true")
    html = _get(url, retries=4)
    text = _html_to_text(html)
    chunks = chunk_lps(text, municipality)
    if not chunks:
        print(f"[lps] {municipality}: 0 clauses — fetch returned no parseable content "
              f"(transient server error?). Re-run --scheme-id {sid} to retry.")
    else:
        print(f"[lps] {municipality}: {len(chunks)} LPS clauses")
    return chunks


def _merge(existing_chunks, new_chunks, councils):
    """Drop existing chunks for the councils we re-ingested, keep the rest, add new."""
    scopes = {c.lower() for c in councils}
    kept = [c for c in existing_chunks if (c.get("scope") or "").lower() not in scopes]
    return kept + new_chunks


def main():
    ap = argparse.ArgumentParser(description="Ingest per-municipality LPS from the TPSO viewer.")
    ap.add_argument("--scheme-id", type=int, action="append", default=[],
                    help="ingest a specific scheme id (repeatable); skips discovery")
    ap.add_argument("--id-range", default="1-60",
                    help="id range to probe during discovery, e.g. 1-60")
    ap.add_argument("--discover", action="store_true",
                    help="only probe and print the id->council map, don't ingest")
    ap.add_argument("--effective", default=_today(), help="effectiveForDate (YYYY-MM-DD)")
    ap.add_argument("--limit", type=int, default=100, help="max councils to ingest")
    ap.add_argument("--replace", action="store_true",
                    help="overwrite scheme_chunks.json instead of merging (drops SPP!)")
    args = ap.parse_args()

    if args.scheme_id:
        schemes = []
        for sid in args.scheme_id:
            init = _get(f"{TPSO_BASE}/{sid}/init", as_json=True)
            if init.get("planningSchemeTypeName") != "Local Provisions Schedule":
                print(f"[lps] scheme {sid} is '{init.get('planningSchemeTypeName')}', not an LPS — skipping")
                continue
            schemes.append({"id": sid, "municipality": _municipality(init.get("title", "")),
                            "root": init.get("openSectionId")})
    else:
        lo, hi = (args.id_range.split("-") + [args.id_range])[:2]
        schemes = discover(range(int(lo), int(hi) + 1))

    if args.discover:
        print(f"[lps] discovered {len(schemes)} Local Provisions Schedules")
        return

    schemes = schemes[: args.limit]
    if not schemes:
        print("[lps] no LPS schemes to ingest.")
        return

    new_chunks, councils = [], []
    for s in schemes:
        try:
            cs = ingest_scheme(s["id"], s["municipality"], s["root"], args.effective)
        except Exception as e:
            print(f"[lps] {s['municipality']} (scheme {s['id']}) failed: {e}")
            continue
        if cs:
            new_chunks.extend(cs)
            councils.append(s["municipality"])

    if not new_chunks:
        print("[lps] No LPS clauses extracted; leaving scheme_chunks.json unchanged.")
        return

    if args.replace:
        chunks = new_chunks
    else:
        existing = (read_data("scheme_chunks.json") or {}).get("chunks", [])
        chunks = _merge(existing, new_chunks, councils)

    write_data("scheme_chunks.json", {"chunks": chunks})

    # Refresh the manifest to record the LPS coverage alongside the SPP.
    manifest = read_data("scheme_manifest.json") or {"instruments": ["SPP"], "sources": []}
    covered = sorted(set(manifest.get("municipalities_covered", [])) | set(councils))
    if "statewide (SPP)" not in covered:
        covered = ["statewide (SPP)"] + [c for c in covered if c != "statewide (SPP)"]
    manifest.update({
        "provenance": "LIVE",
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "municipalities_covered": covered,
        "instruments": sorted(set(manifest.get("instruments", [])) | {"SPP", "LPS"}),
        "chunk_count": len(chunks),
    })
    manifest.setdefault("sources", []).append({
        "name": f"Local Provisions Schedules: {', '.join(councils)}",
        "url": f"{TPSO_BASE}/{{schemeId}}/section/{{root}}/html",
        "fetched": True,
    })
    write_data("scheme_manifest.json", manifest)

    kinds = {}
    for c in new_chunks:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    print(f"[lps] done: {len(new_chunks)} LPS clauses from {len(councils)} councils "
          f"({', '.join(councils)}) — {kinds}")
    print(f"[lps] scheme_chunks.json now holds {len(chunks)} chunks (SPP + LPS).")


if __name__ == "__main__":
    main()
