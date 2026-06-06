"""Build the semantic index for TASCAT precedents: decision_chunks.json.

Fetches the full text of leading Resource & Planning decisions, chunks each
into overlapping windows, embeds every chunk with Gemini's free embedding
model, and writes data/decision_chunks.json. The app's retrieve_passages()
reads this to ground reviews on verbatim holding text (not just summaries).

This is the RAG layer. It complements decisions.json (the summary corpus used
for the /decisions browse page and as a keyword fallback) — it does not replace
it. Re-run whenever the seed list changes; deploys do NOT touch GCS data, so
upload the result manually afterwards.

Usage:
    GEMINI_API_KEY=... python -m ingest.embed              # seed precedents
    GEMINI_API_KEY=... python -m ingest.embed --all        # every case in decisions.json
    GEMINI_API_KEY=... python -m ingest.embed --limit 5    # small test run

Then upload to production:
    gcloud storage cp data/decision_chunks.json gs://aus-town-planner-data/decision_chunks.json
"""

import re
import argparse

from . import fetch, write_data, read_data
from .decisions import (
    SEED_CITATIONS, _seed_records, _strip_html, _heuristic_fields, _CITATION_RE,
    AUSTLII_BASE,
)

CHUNK_CHARS = 3500      # ~1000 tokens
OVERLAP_CHARS = 300


EMBED_THROTTLE = 0.4      # seconds between embedding calls (free-tier RPM headroom)
EMBED_RETRIES = 4         # retries on transient/rate-limit errors


def _embed(text):
    """Embed one chunk as a document, with throttle + backoff on rate limits.
    Returns vector or None after exhausting retries."""
    import time
    from main import _embed_text
    delay = 2.0
    for attempt in range(EMBED_RETRIES + 1):
        try:
            vec = _embed_text(text, task_type="RETRIEVAL_DOCUMENT", raise_on_error=True)
            time.sleep(EMBED_THROTTLE)
            return vec
        except Exception as e:
            msg = str(e)
            transient = "429" in msg or "RESOURCE_EXHAUSTED" in msg or "500" in msg or "503" in msg
            if attempt < EMBED_RETRIES and transient:
                print(f"[embed]   rate-limited, retrying in {delay:.0f}s...")
                time.sleep(delay)
                delay *= 2
                continue
            print(f"[embed]   embed error: {msg[:160]}")
            return None


def _list_embedding_models():
    """Print the models this key can use for embedContent (diagnostic on 404)."""
    import urllib.request
    from main import GEMINI_API_KEY
    try:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models"
               f"?key={GEMINI_API_KEY}&pageSize=200")
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read())
        names = [m["name"] for m in data.get("models", [])
                 if "embedContent" in m.get("supportedGenerationMethods", [])]
        if names:
            print("[embed] models available for embedContent on this key:")
            for n in names:
                print(f"          {n}")
            print("[embed] set EMBED_MODEL in main.py to one of the above (without 'models/').")
        else:
            print("[embed] no embedContent-capable models returned for this key.")
    except Exception as e:
        print(f"[embed] could not list models: {e}")


def _preflight():
    """Do one embedding call up front; raise with the real cause if it fails.
    Catches a missing key, an invalid key, or a wrong model before we churn
    through hundreds of chunks."""
    from main import _embed_text, EMBED_MODEL, GEMINI_API_KEY
    if not GEMINI_API_KEY:
        raise SystemExit(
            "[embed] GEMINI_API_KEY is not set. Run with:\n"
            "    GEMINI_API_KEY=your-key ./venv/bin/python -m ingest.embed")
    try:
        vec = _embed_text("preflight check", task_type="RETRIEVAL_DOCUMENT", raise_on_error=True)
    except Exception as e:
        print(f"[embed] embedding API call failed ({EMBED_MODEL}): {e}")
        _list_embedding_models()
        raise SystemExit(1)
    print(f"[embed] preflight OK — {EMBED_MODEL} returned a {len(vec)}-dim vector")


def chunk_text(text):
    """Split into overlapping ~CHUNK_CHARS windows on whitespace boundaries."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    chunks, start = [], 0
    while start < len(text):
        end = min(start + CHUNK_CHARS, len(text))
        # Don't cut mid-word: back up to the last space if we're not at the end.
        if end < len(text):
            sp = text.rfind(" ", start, end)
            if sp > start:
                end = sp
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = end - OVERLAP_CHARS
    return [c for c in chunks if c]


def _records_from_decisions(limit):
    """Build seed-style records from the existing summary corpus (--all mode)."""
    data = read_data("decisions.json") or {}
    out = []
    for d in data.get("decisions", [])[:limit]:
        m = _CITATION_RE.search(d.get("citation", ""))
        if not m:
            continue
        year, trib, num = m.group(1), m.group(2), m.group(3)
        out.append({
            "citation": f"[{year}] {trib} {num}",
            "url": f"{AUSTLII_BASE}/cgi-bin/viewdoc/au/cases/tas/{trib}/{year}/{num}.html",
            "db": "tascat" if trib == "TASCAT" else "rmpat",
            "_title": d.get("title"),
            "_municipality": d.get("municipality"),
        })
    return out


def main():
    ap = argparse.ArgumentParser(description="Build the semantic index (decision_chunks.json).")
    ap.add_argument("--all", action="store_true",
                    help="embed every case in decisions.json instead of just the seed list")
    ap.add_argument("--limit", type=int, default=100, help="max cases to embed")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore any existing index and re-embed every case from scratch")
    args = ap.parse_args()

    _preflight()

    records = (_records_from_decisions(args.limit) if args.all
               else _seed_records(SEED_CITATIONS)[: args.limit])

    # Resume: keep chunks already embedded so a killed run picks up where it left off.
    existing = [] if args.fresh else (read_data("decision_chunks.json") or {}).get("chunks", [])
    done = {c["citation"] for c in existing}
    chunks = list(existing)
    if done:
        print(f"[embed] resuming — {len(done)} cases already indexed ({len(existing)} chunks). "
              f"Use --fresh to rebuild from scratch.")
    todo = [r for r in records if r["citation"] not in done]
    print(f"[embed] {len(records)} cases in scope, {len(todo)} to embed")

    for rec in todo:
        cit = rec["citation"]
        try:
            text = _strip_html(fetch(rec["url"]))
        except Exception as e:
            print(f"[embed] fetch failed {cit}: {e}")
            continue
        # Title/municipality: reuse what we have, else derive heuristically.
        meta = _heuristic_fields(cit, text)
        title = rec.get("_title") or meta["title"]
        muni = rec.get("_municipality") or meta["municipality"]

        pieces = chunk_text(text)
        case_chunks = []
        for i, piece in enumerate(pieces):
            vec = _embed(piece)
            if not vec:
                print(f"[embed] embedding failed {cit} chunk {i} (API error after retries)")
                continue
            case_chunks.append({
                "chunk_id": f"{cit} #{i}",
                "citation": cit,
                "title": title,
                "municipality": muni,
                "text": piece,
                "embedding": vec,
            })
        chunks.extend(case_chunks)
        print(f"[embed] {cit}: {len(case_chunks)}/{len(pieces)} chunks embedded")
        # Checkpoint after every case so an interrupted run loses nothing.
        if case_chunks:
            write_data("decision_chunks.json", {"chunks": chunks})

    if not chunks:
        print("[embed] No chunks embedded. Set GEMINI_API_KEY and confirm network access. "
              "Existing index left in place.")
        return

    n_cases = len({c["citation"] for c in chunks})
    print(f"[embed] done: {len(chunks)} chunks from {n_cases} cases → decision_chunks.json")


if __name__ == "__main__":
    main()
