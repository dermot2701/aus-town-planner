"""Ingest the Tasmanian Planning Scheme — State Planning Provisions (SPP).

The SPP is the statewide rulebook: zone purposes, use tables, codes, and the
acceptable-solution / performance-criterion standards. It is published as a
single PDF by the State Planning Office. This script downloads it, extracts the
text, chunks it by clause heading, and writes data/scheme_chunks.json — the same
schema the app's retrieve() consumes.

The per-municipality Local Provisions Schedules (LPS) live in the dynamic TPSO
viewer (tpso.planning.tas.gov.au) and are not ingested here — see
docs/INGESTION.md. The SPP carries the substantive provisions a review grounds
on; the LPS layer mainly maps zones to parcels (the planner supplies the zone).

Text extraction prefers `pdftotext` (poppler) if present, else falls back to
pdfminer.six (`pip install pdfminer.six`).

Usage:
    python -m ingest.scheme
    python -m ingest.scheme --pdf-url <url>     # override the SPP source
    python -m ingest.scheme --pdf-file spp.pdf  # use a locally downloaded PDF

Writes:
  chunk = {clause_id, instrument, scope, kind, zone_or_code, title, text,
           keywords[], use_classes[], provenance}
"""

import os
import re
import datetime
import argparse
import subprocess

from . import fetch, write_data, CACHE_DIR

# Current effective SPP PDF (State Planning Office). Override with --pdf-url when
# a new consolidated version is published.
SPP_PDF_URL = (
    "https://www.stateplanning.tas.gov.au/__data/assets/pdf_file/0007/556441/"
    "Tasmanian-Planning-Scheme-State-Planning-Provisions-effective-25-December-2024.pdf"
)

# Clause heading: 8.0 / 8.2 / 8.4.1 / C10.0 / C10.6.1, then a short title.
CLAUSE_RE = re.compile(r"(?m)^\s*(C?\d+(?:\.\d+){1,3})\s+(.{3,90}?)\s*$")

# Running headers/footers to strip from clause bodies.
_NOISE = re.compile(
    r"(?im)^\s*(page\s+\d+|tasmanian planning scheme.*|state planning provisions.*)\s*$"
)

_STOP = {"the", "and", "for", "any", "are", "not", "with", "this", "that", "from",
         "must", "may", "have", "which", "use", "development", "planning", "scheme"}


def _extract_text(pdf_bytes):
    """PDF bytes → text. Try pdftotext, fall back to pdfminer.six."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, "spp.pdf")
    with open(path, "wb") as f:
        f.write(pdf_bytes)
    try:
        out = subprocess.run(["pdftotext", "-layout", path, "-"],
                             capture_output=True, check=True)
        return out.stdout.decode("utf-8", errors="replace")
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    try:
        from pdfminer.high_level import extract_text
        return extract_text(path)
    except ImportError:
        raise SystemExit(
            "[scheme] Need a PDF text extractor. Install poppler (pdftotext) "
            "or run: pip install pdfminer.six")


def _clean_body(body):
    body = _NOISE.sub("", body)
    return re.sub(r"\s+", " ", body).strip()


def _kind(clause_id, title):
    t = title.lower()
    if "use table" in t:
        return "use_table"
    if re.search(r"\.\d+\.\d+$", clause_id):
        return "standard"          # e.g. 8.4.1 — acceptable solution / performance criterion
    if clause_id.startswith("C"):
        return "code"
    if t.endswith("zone"):
        return "zone_purpose"
    return "provision"


def chunk_spp(text):
    """Split SPP text into deduplicated clause chunks (drops the TOC stubs)."""
    text = re.sub(r"[ \t]+", " ", text)
    matches = list(CLAUSE_RE.finditer(text))
    best = {}
    for i, m in enumerate(matches):
        cid = m.group(1).strip()
        title = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = _clean_body(text[start:end])
        # Drop table-of-contents stubs: too short, or mostly page numbers.
        digit_ratio = sum(c.isdigit() for c in body) / max(len(body), 1)
        if len(body) < 40 or digit_ratio > 0.4:
            continue
        # Keep the longest body seen for a clause id (real content beats the TOC).
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
            "clause_id": f"SPP {cid}",
            "instrument": "SPP",
            "scope": "statewide",
            "kind": kind,
            "zone_or_code": title,
            "title": title,
            "text": body[:1500],
            "keywords": keywords,
            "use_classes": use_classes,
            "provenance": "LIVE",
        })
    return chunks


def main():
    ap = argparse.ArgumentParser(description="Ingest the Tasmanian SPP from the published PDF.")
    ap.add_argument("--pdf-url", default=SPP_PDF_URL, help="SPP PDF URL")
    ap.add_argument("--pdf-file", help="use a locally downloaded SPP PDF instead of fetching")
    args = ap.parse_args()

    if args.pdf_file:
        with open(args.pdf_file, "rb") as f:
            pdf = f.read()
        source = args.pdf_file
    else:
        print(f"[scheme] fetching SPP PDF: {args.pdf_url}")
        pdf = fetch(args.pdf_url, binary=True)
        source = args.pdf_url

    text = _extract_text(pdf)
    chunks = chunk_spp(text)
    if not chunks:
        print("[scheme] No clauses extracted; not overwriting existing corpus.")
        return

    kinds = {}
    for c in chunks:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1

    write_data("scheme_chunks.json", {"chunks": chunks})
    write_data("scheme_manifest.json", {
        "provenance": "LIVE",
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "note": "State Planning Provisions ingested from the published PDF. "
                "Local Provisions Schedules (per-municipality) not yet ingested.",
        "sources": [{"name": "State Planning Provisions (SPP)", "url": source, "fetched": True}],
        "municipalities_covered": ["statewide (SPP)"],
        "chunk_count": len(chunks),
        "instruments": ["SPP"],
    })
    print(f"[scheme] done: {len(chunks)} SPP clauses — {kinds}")


if __name__ == "__main__":
    main()
