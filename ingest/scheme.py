"""Ingest the Tasmanian Planning Scheme: SPP + per-municipality LPS.

NETWORK-GATED. Requires outbound access to tpso.planning.tas.gov.au. Selectors
below are a starting point and will likely need adjustment against the live site
structure — they are intentionally conservative and log what they find.

Pipeline:
  1. Resolve the SPP and each LPS document (PDF) from TPSO.
  2. Download PDFs (throttled, cached), extract text with `pdftotext`.
  3. Chunk by clause heading (e.g. "10.4.1 A1").
  4. Write data/scheme_chunks.json + data/scheme_manifest.json.

This writes the same schema the app's retrieve() consumes:
  chunk = {clause_id, instrument, scope, kind, zone_or_code, title, text,
           performance_criterion?, use_classes[], keywords[], provenance}
"""

import os
import re
import subprocess
import datetime

from . import fetch, write_data, CACHE_DIR

TPSO_BASE = "https://www.tpso.planning.tas.gov.au"

# Clause heading like "10.4.1 A1" or "C13.6 P1" or "10.2 Use Table"
CLAUSE_RE = re.compile(r"^(C?\d+(?:\.\d+)*\s*(?:A\d+|P\d+)?)\s+(.{3,120})$", re.MULTILINE)


def pdf_to_text(pdf_bytes, tag):
    """Run pdftotext on bytes via a temp file in the cache dir."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    pdf_path = os.path.join(CACHE_DIR, f"{tag}.pdf")
    with open(pdf_path, "wb") as f:
        f.write(pdf_bytes)
    out = subprocess.run(
        ["pdftotext", "-layout", pdf_path, "-"],
        capture_output=True, check=True,
    )
    return out.stdout.decode("utf-8", errors="replace")


def chunk_clauses(text, *, instrument, scope, provenance="LIVE"):
    """Split extracted text into clause chunks keyed by the heading regex."""
    chunks = []
    matches = list(CLAUSE_RE.finditer(text))
    for i, m in enumerate(matches):
        clause_id = m.group(1).strip()
        title = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        kind = "standard" if re.search(r"A\d+|P\d+", clause_id) else (
            "use_table" if "use table" in title.lower() else "code" if clause_id.startswith("C") else "zone_purpose")
        chunks.append({
            "clause_id": f"{instrument} {clause_id}",
            "instrument": instrument,
            "scope": scope,
            "kind": kind,
            "zone_or_code": title,
            "title": title,
            "text": body[:1500],
            "keywords": sorted(set(re.findall(r"[a-z]{4,}", (title + " " + body[:200]).lower())))[:12],
            "use_classes": [],
            "provenance": provenance,
        })
    return chunks


def discover_documents():
    """Return [{instrument, scope, url}] for the SPP + each LPS PDF.

    TODO: parse the TPSO document index. Until the live structure is confirmed,
    this returns an empty list so a misconfigured run is a no-op rather than a
    crash. Populate by inspecting the TPSO 'approved planning scheme' pages.
    """
    docs = []
    try:
        index = fetch(f"{TPSO_BASE}/")
        # Heuristic: collect links to .pdf documents on the index page.
        for href in re.findall(r'href="([^"]+\.pdf)"', index, re.IGNORECASE):
            url = href if href.startswith("http") else TPSO_BASE + href
            instrument = "SPP" if "spp" in url.lower() or "state-planning" in url.lower() else "LPS"
            scope = "statewide" if instrument == "SPP" else _guess_municipality(url)
            docs.append({"instrument": instrument, "scope": scope, "url": url})
    except Exception as e:
        print(f"[scheme] could not reach TPSO index: {e}")
    return docs


def _guess_municipality(url):
    m = re.search(r"/([a-z-]+)-(?:lps|local-provisions)", url.lower())
    return m.group(1).replace("-", " ").title() if m else "unknown"


def main():
    docs = discover_documents()
    if not docs:
        print("[scheme] No documents discovered. Confirm network access to TPSO and "
              "the document index structure, then re-run. SAMPLE corpus left in place.")
        return

    all_chunks = []
    fetched = []
    for d in docs:
        try:
            print(f"[scheme] fetching {d['instrument']} ({d['scope']}): {d['url']}")
            pdf = fetch(d["url"], binary=True)
            tag = "".join(c if c.isalnum() else "_" for c in d["url"])[-80:]
            text = pdf_to_text(pdf, tag)
            all_chunks.extend(chunk_clauses(text, instrument=d["instrument"], scope=d["scope"]))
            fetched.append({"name": f"{d['instrument']} {d['scope']}", "url": d["url"], "fetched": True})
        except Exception as e:
            print(f"[scheme] failed {d['url']}: {e}")
            fetched.append({"name": d["url"], "url": d["url"], "fetched": False})

    if not all_chunks:
        print("[scheme] No clauses extracted; not overwriting existing corpus.")
        return

    write_data("scheme_chunks.json", {"chunks": all_chunks})
    write_data("scheme_manifest.json", {
        "provenance": "LIVE",
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "note": "Ingested from TPSO.",
        "sources": fetched,
        "municipalities_covered": sorted({c["scope"] for c in all_chunks}),
        "chunk_count": len(all_chunks),
        "instruments": sorted({c["instrument"] for c in all_chunks}),
    })
    print(f"[scheme] done: {len(all_chunks)} chunks from {len(docs)} documents.")


if __name__ == "__main__":
    main()
