"""Ingest Tasmanian planning tribunal decisions from AustLII.

Covers BOTH tribunals that hold the planning precedent:
  - TASCAT  (2021-present)  /au/cases/tas/TASCAT/
  - TASRMPAT (pre-2021)     /au/cases/tas/TASRMPAT/   (the former Resource
    Management & Planning Appeal Tribunal, absorbed into TASCAT in 2021)

NETWORK-GATED + RATE-LIMITED. AustLII restricts bulk crawling in its terms of
use and robots.txt. Be a good citizen (and avoid an IP block):
  - requests are throttled (see THROTTLE_SECONDS in __init__) and cached;
  - a descriptive User-Agent with a contact email is sent;
  - for a full historical corpus, email AustLII for bulk/data access rather
    than crawling the whole site.

Pipeline:
  1. Discover case URLs from each tribunal's RSS feed (primary) or year-index
     pages (fallback). Feed URL: www8.austlii.edu.au/cgi-bin/feed/au/cases/tas/{DB}/
  2. Fetch each case; cheaply pre-filter to planning/Resource & Planning matters
     by catchword (TASCAT carries many non-planning streams).
  3. For survivors, extract structured fields. With a Gemini key this is an
     LLM pass (outcome / principle / use_classes / municipality); without one it
     falls back to regex heuristics and leaves `principle` blank for review.
  4. Write data/decisions.json.

Usage:
    python -m ingest.decisions                      # defaults below
    python -m ingest.decisions --year-from 2024 --limit 20   # small test run
    python -m ingest.decisions --db rmpat --year-from 2010   # historical
    GEMINI_API_KEY=... python -m ingest.decisions   # enable the LLM pass

Writes the schema the app's retrieve() consumes:
  decision = {citation, title, municipality, use_classes[], keywords[],
              summary, outcome, principle, provenance}
"""

import re
import html
import json
import argparse
import datetime
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

from . import fetch, write_data, read_data

# AustLII migrated to www8.austlii.edu.au with cgi-bin paths.
# www.austlii.edu.au year-index paths return 410 Gone.
AUSTLII_BASE = "https://www8.austlii.edu.au"
FEED_PATHS = {
    "tascat": "/cgi-bin/feed/au/cases/tas/TASCAT/",
    "rmpat": "/cgi-bin/feed/au/cases/tas/TASRMPAT/",
}
DB_PATHS = {
    "tascat": "/cgi-bin/viewtoc/au/cases/tas/TASCAT/",
    "rmpat": "/cgi-bin/viewtoc/au/cases/tas/TASRMPAT/",
}
DB_CITATION = {"tascat": "TASCAT", "rmpat": "TASRMPAT"}

# Curated leading Resource & Planning precedents (AustLII "most cited" within the
# TASCAT R&P stream). Fetched directly by viewdoc URL — bypasses the search
# endpoint (CAPTCHA-gated) and year-index discovery. Add citations here as the
# leading authorities evolve; `--seed` ingests exactly this list.
SEED_CITATIONS = [
    "[2023] TASCAT 114",   # Owens v Kingborough Council
    "[2023] TASCAT 14",    # W Lashmar v Glamorgan Spring Bay Council
    "[2022] TASCAT 41",    # A Wyminga v Glamorgan Spring Bay Council
    "[2023] TASCAT 217",   # Ryan v Circular Head Council (No 4) — Robbins Island
    "[2022] TASCAT 137",   # G Palmese v Hobart City Council
    "[2021] TASCAT 4",     # McElwaine & Hamilton v West Tamar Council
    "[2022] TASCAT 47",    # Cubitt & Powell v Launceston City Council
    "[2022] TASCAT 128",   # Mt Wellington Cableway Co v Hobart City Council
    "[2023] TASCAT 108",   # Craig Webb Pty Ltd v Launceston City Council
    "[2022] TASCAT 60",    # D & L Plumb v Clarence City Council
    "[2024] TASCAT 108",   # Webb v Kingborough Council
    "[2023] TASCAT 67",    # Beauty Point Trading v West Tamar Council
    "[2023] TASCAT 1",     # Recycal Pty Ltd v EPA Tasmania
    "[2023] TASCAT 158",   # Jacobs v Hobart City Council
    "[2023] TASCAT 27",    # S Cai v Launceston City Council
    "[2023] TASCAT 90",    # Saltwater Lagoon v Glamorgan Spring Bay Council
    "[2022] TASCAT 157",   # Julie Alexander v Hobart City Council
    "[2024] TASCAT 43",    # Smith v Latrobe Council
    "[2022] TASCAT 79",    # Chau Nominees v Hobart City Council
]

_CITATION_RE = re.compile(r"\[(\d{4})\]\s+(TASCAT|TASRMPAT)\s+(\d+)")


def _seed_records(citations):
    """Convert ['[2023] TASCAT 114', ...] into discovery records with viewdoc URLs."""
    out, seen = [], set()
    for cit in citations:
        m = _CITATION_RE.search(cit)
        if not m:
            print(f"[decisions] seed: unparseable citation {cit!r}")
            continue
        year, trib, num = m.group(1), m.group(2), m.group(3)
        db = "tascat" if trib == "TASCAT" else "rmpat"
        key = (db, year, num)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "citation": f"[{year}] {trib} {num}",
            "url": f"{AUSTLII_BASE}/cgi-bin/viewdoc/au/cases/tas/{trib}/{year}/{num}.html",
            "db": db,
        })
    return out

# Catchwords that mark a planning / Resource & Planning matter. TASCAT also hears
# guardianship, health, anti-discrimination etc. — those are filtered out.
PLANNING_TERMS = [
    "planning", "lupaa", "land use planning", "planning scheme", "permit",
    "development application", "subdivision", "zone", "use and development",
    "resource management", "discretionary use", "performance criteria",
    "acceptable solution", "council",
]

OUTCOME_HINTS = {
    "affirmed": "affirmed", "set aside": "set aside", "varied": "varied",
    "dismiss": "dismissed", "permit grant": "permit granted",
    "refus": "refusal", "remit": "remitted",
}

_CASE_LINK_RE = re.compile(r'href="([^"]*?(\d+)\.html)"', re.IGNORECASE)
_ATOM_NS = "http://www.w3.org/2005/Atom"


def _strip_html(s):
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def _gemini():
    """Single Gemini factory (from main.py). Returns None when no key is set."""
    try:
        from main import _gemini_model
        return _gemini_model()
    except Exception as e:
        print(f"[decisions] Gemini unavailable ({e}); using regex fallback.")
        return None


# ── Discovery ─────────────────────────────────────────────────────────────────

def _parse_feed(xml_text, db, year_from, year_to):
    """Parse RSS or Atom feed, return [{citation, url, db}] filtered by year range."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"[decisions] {db}: feed XML parse error ({e})")
        return []

    # RSS 2.0 items
    items = root.findall(".//item")
    # Atom entries
    if not items:
        items = root.findall(f".//{{{_ATOM_NS}}}entry")

    out = []
    for item in items:
        # Get URL — RSS uses <link> text, Atom uses <link href="...">
        link_el = item.find("link")
        if link_el is not None:
            link = (link_el.text or "").strip() or link_el.get("href", "")
        else:
            atom_link = item.find(f"{{{_ATOM_NS}}}link")
            link = atom_link.get("href", "") if atom_link is not None else ""

        if not link:
            guid = item.find("guid")
            link = guid.text.strip() if guid is not None else ""

        if not link:
            continue

        year_m = re.search(r"/(\d{4})/", link)
        if not year_m:
            continue
        year = int(year_m.group(1))
        if year < year_from or year > year_to:
            continue

        num_m = re.search(r"/(\d+)\.html", link)
        if not num_m:
            continue

        out.append({
            "citation": f"[{year}] {DB_CITATION[db]} {num_m.group(1)}",
            "url": link,
            "db": db,
        })
    return out


def discover(db, year_from, year_to):
    """Return [{citation, url, db}] for one tribunal across a year range.

    Uses BOTH the RSS feed (recent cases) and year-index pages (full archive),
    deduplicating by case number so each case appears once.
    """
    seen = set()  # case numbers already added
    out = []

    # 1. RSS feed — catches the most recent cases quickly
    feed_url = AUSTLII_BASE + FEED_PATHS[db]
    try:
        xml_text = fetch(feed_url)
        rss = _parse_feed(xml_text, db, year_from, year_to)
        for r in rss:
            num_m = re.search(r"/(\d+)\.html", r["url"])
            key = num_m.group(1) if num_m else r["url"]
            if key not in seen:
                seen.add(key)
                out.append(r)
        print(f"[decisions] {db} feed: {len(rss)} cases ({year_from}–{year_to})")
    except Exception as e:
        print(f"[decisions] {db}: feed unreachable ({e})")

    # 2. Year-index pages — full archive for each requested year
    for year in range(year_from, year_to + 1):
        index_url = AUSTLII_BASE + f"{DB_PATHS[db]}{year}/"
        try:
            index = fetch(index_url)
        except Exception as e:
            print(f"[decisions] {db} {year}: index unreachable ({e})")
            continue
        new = 0
        for href, num in _CASE_LINK_RE.findall(index):
            if num in seen:
                continue
            seen.add(num)
            out.append({
                "citation": f"[{year}] {DB_CITATION[db]} {num}",
                "url": urljoin(index_url, href),
                "db": db,
            })
            new += 1
        print(f"[decisions] {db} {year}: {new} new cases from year-index")
    return out


# ── Extraction ────────────────────────────────────────────────────────────────

def _is_planning(text):
    low = text[:2500].lower()
    return any(term in low for term in PLANNING_TERMS)


def _heuristic_fields(citation, text):
    title_m = re.search(r"([A-Z][\w .'&-]{2,60} v[. ] [A-Z][\w .'&-]{2,60})", text)
    muni_m = re.search(r"((?:City of |[A-Z][a-z]+[ -]){1,3})(?:City )?Council", text)
    outcome = next((v for k, v in OUTCOME_HINTS.items() if k in text.lower()), "")
    return {
        "citation": citation,
        "title": (title_m.group(1).strip() if title_m else citation),
        "municipality": (muni_m.group(1).strip() if muni_m else "various"),
        "use_classes": [],
        "keywords": sorted(set(re.findall(r"[a-z]{5,}", text[:1200].lower())))[:15],
        "summary": text[:600],
        "outcome": outcome,
        "principle": "",
        "provenance": "LIVE",
    }


_LLM_PROMPT = (
    "You are indexing a Tasmanian planning tribunal decision for a retrieval system. "
    "From the decision text, return ONLY a JSON object with keys: "
    "is_planning (bool — true only if this concerns land use planning / a planning "
    "scheme / a permit appeal under LUPAA or resource management), "
    "title (parties), municipality (e.g. 'Hobart' or 'various'), "
    "outcome (one of: affirmed, set aside, varied, dismissed, permit granted, refusal, remitted), "
    "principle (one sentence — the ratio / planning principle relied on), "
    "use_classes (array of planning use classes in issue, e.g. 'Visitor accommodation'), "
    "keywords (array of <=12 lowercase topical keywords), "
    "summary (<=400 chars). Cite nothing not in the text. Decision text:\n\n"
)


def _llm_fields(model, citation, text):
    resp = model.generate_content(_LLM_PROMPT + text[:14000])
    m = re.search(r"\{.*\}", resp.text.strip(), re.DOTALL)
    data = json.loads(m.group() if m else resp.text)
    if not data.get("is_planning", True):
        return None  # signal: drop non-planning case
    return {
        "citation": citation,
        "title": data.get("title", citation),
        "municipality": data.get("municipality", "various"),
        "use_classes": data.get("use_classes", []),
        "keywords": data.get("keywords", []),
        "summary": data.get("summary", text[:400]),
        "outcome": data.get("outcome", ""),
        "principle": data.get("principle", ""),
        "provenance": "LIVE",
    }


# ── Local-file ingestion (browser-downloaded HTML) ────────────────────────────

def ingest_local(local_dir: str, db: str, model) -> list:
    """Process HTML files saved from a browser. Filenames should include the
    case number (e.g. TASCAT_47.html or just 47.html).

    Usage:
        1. Open https://www8.austlii.edu.au/cgi-bin/viewdb/au/cases/tas/TASCAT/
           in a browser, click each decision, File → Save Page As (HTML only).
        2. Put all saved .html files in one directory.
        3. Run: python -m ingest.decisions --local-dir ./my_cases --db tascat
    """
    import glob
    import os as _os
    out = []
    paths = sorted(glob.glob(_os.path.join(local_dir, "*.html")) +
                   glob.glob(_os.path.join(local_dir, "*.htm")))
    if not paths:
        print(f"[decisions] No .html files found in {local_dir}")
        return out
    print(f"[decisions] local-dir: {len(paths)} files in {local_dir}")
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as f:
            raw = f.read()
        text = _strip_html(raw)
        # Derive a citation from filename
        num_m = re.search(r"(\d+)", _os.path.basename(path))
        num = num_m.group(1) if num_m else _os.path.splitext(_os.path.basename(path))[0]
        year_m = re.search(r"(20\d{2})", _os.path.basename(path))
        year = year_m.group(1) if year_m else str(datetime.date.today().year)
        citation = f"[{year}] {DB_CITATION.get(db, db.upper())} {num}"
        if not _is_planning(text):
            print(f"[decisions] skip (non-planning) {citation}")
            continue
        try:
            rec = _llm_fields(model, citation, text) if model else _heuristic_fields(citation, text)
            if rec is None:
                continue
            print(f"[decisions] kept {citation} — {rec.get('outcome','?')}")
            out.append(rec)
        except Exception as e:
            print(f"[decisions] failed {citation}: {e}")
    return out


# ── Driver ──────────────────────────────────────────────────────────────────

def main():
    now_year = datetime.date.today().year
    ap = argparse.ArgumentParser(description="Ingest TASCAT/TASRMPAT planning decisions from AustLII.")
    ap.add_argument("--db", choices=["tascat", "rmpat", "both"], default="both")
    ap.add_argument("--year-from", type=int, default=2023)
    ap.add_argument("--year-to", type=int, default=now_year)
    ap.add_argument("--limit", type=int, default=200, help="max cases to fetch (across all)")
    ap.add_argument("--no-gemini", action="store_true", help="skip the LLM pass even if a key is set")
    ap.add_argument("--local-dir", help="process locally saved HTML files instead of fetching from AustLII")
    ap.add_argument("--seed", action="store_true",
                    help="ingest the curated leading-precedent list (by viewdoc URL) instead of crawling")
    ap.add_argument("--merge", action="store_true",
                    help="merge into the existing decisions.json (by citation) rather than overwrite")
    args = ap.parse_args()

    model = None if args.no_gemini else _gemini()

    # Local-file mode — bypasses AustLII network access entirely
    if args.local_dir:
        db = "tascat" if args.db == "both" else args.db
        decisions = ingest_local(args.local_dir, db, model)
        if not decisions:
            print("[decisions] Nothing kept from local files.")
            return
        _write(decisions, merge=args.merge)
        return

    # Seed mode — fetch the curated leading precedents directly
    if args.seed:
        found = _seed_records(SEED_CITATIONS)
        print(f"[decisions] seed: {len(found)} curated precedents to fetch")
    else:
        dbs = ["tascat", "rmpat"] if args.db == "both" else [args.db]
        found = []
        for db in dbs:
            found += discover(db, args.year_from, args.year_to)
        found = found[: args.limit]
    if not found:
        print("[decisions] No cases discovered. Confirm network access to AustLII "
              "and the year range. SAMPLE corpus left in place.")
        return

    decisions, skipped = [], 0
    for d in found:
        try:
            text = _strip_html(fetch(d["url"]))
            if not _is_planning(text):
                skipped += 1
                continue
            rec = _llm_fields(model, d["citation"], text) if model else _heuristic_fields(d["citation"], text)
            if rec is None:
                skipped += 1
                continue
            print(f"[decisions] kept {d['citation']} — {rec.get('outcome','?')}")
            decisions.append(rec)
        except Exception as e:
            print(f"[decisions] failed {d['citation']}: {e}")

    if not decisions:
        print(f"[decisions] Nothing kept ({skipped} non-planning skipped); not overwriting corpus.")
        return

    _write(decisions, merge=args.merge, skipped=skipped)


def _write(decisions, *, merge=False, skipped=0):
    """Write decisions.json, optionally merging with the existing corpus by citation."""
    if merge:
        try:
            existing = (read_data("decisions.json") or {}).get("decisions", [])
        except Exception:
            existing = []
        by_cit = {d.get("citation"): d for d in existing if d.get("citation")}
        for d in decisions:
            by_cit[d["citation"]] = d  # new entries win
        merged = list(by_cit.values())
        write_data("decisions.json", {"decisions": merged})
        print(f"[decisions] done: {len(decisions)} fetched, merged into corpus "
              f"({len(merged)} total, {skipped} skipped).")
    else:
        write_data("decisions.json", {"decisions": decisions})
        print(f"[decisions] done: {len(decisions)} planning decisions kept, {skipped} skipped.")


if __name__ == "__main__":
    main()
