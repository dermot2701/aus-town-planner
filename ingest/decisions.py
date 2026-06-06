"""Ingest TASCAT Resource & Planning decisions from AustLII.

NETWORK-GATED. Requires outbound access to austlii.edu.au. AustLII has usage
terms — requests are throttled and cached. Selectors below target AustLII's
standard decision listing/markup and may need adjustment.

Pipeline:
  1. Fetch the TASCAT decisions listing on AustLII (Resource & Planning stream).
  2. For each decision, fetch the document, extract citation, parties, catchwords,
     outcome, and a short summary.
  3. Build a keyword index and write data/decisions.json.

Writes the schema the app's retrieve() consumes:
  decision = {citation, title, municipality, use_classes[], keywords[],
              summary, outcome, principle, provenance}
"""

import re
import html

from . import fetch, write_data

# AustLII database path for TASCAT. Confirm the exact stream/year path on the live site.
AUSTLII_BASE = "https://www.austlii.edu.au"
TASCAT_INDEX = AUSTLII_BASE + "/cgi-bin/viewdb/au/cases/tas/TASCAT/"

CITATION_RE = re.compile(r"\[(\d{4})\]\s*TASCAT\s*(\d+)")
OUTCOME_HINTS = {
    "affirmed": "affirmed", "set aside": "set aside", "varied": "varied",
    "dismiss": "dismissed", "granted": "permit granted", "refus": "refusal",
}


def _strip_html(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s)).strip()


def discover_decisions():
    """Return [{citation, url}] from the TASCAT index page(s)."""
    out = []
    try:
        index = fetch(TASCAT_INDEX)
        for href, label in re.findall(r'href="([^"]+\.html)"[^>]*>([^<]*TASCAT[^<]*)<', index):
            m = CITATION_RE.search(label)
            if not m:
                continue
            url = href if href.startswith("http") else AUSTLII_BASE + href
            out.append({"citation": f"[{m.group(1)}] TASCAT {m.group(2)}", "url": url})
    except Exception as e:
        print(f"[decisions] could not reach AustLII TASCAT index: {e}")
    return out


def parse_decision(citation, url):
    raw = fetch(url)
    text = _strip_html(raw)
    title_m = re.search(r"([A-Z][\w .'-]+ v [A-Z][\w .'-]+)", text)
    outcome = next((v for k, v in OUTCOME_HINTS.items() if k in text.lower()), "")
    # Catchwords / first substantive paragraph as a summary surrogate.
    summary = text[:600]
    muni_m = re.search(r"([A-Z][a-z]+(?:[ -][A-Z][a-z]+)*)\s+Council", text)
    return {
        "citation": citation,
        "title": title_m.group(1) if title_m else citation,
        "municipality": (muni_m.group(1) if muni_m else "various"),
        "use_classes": [],
        "keywords": sorted(set(re.findall(r"[a-z]{5,}", text[:800].lower())))[:15],
        "summary": summary,
        "outcome": outcome,
        "principle": "",  # populate via manual review or an LLM summarisation pass
        "provenance": "LIVE",
    }


def main(limit=200):
    found = discover_decisions()
    if not found:
        print("[decisions] No decisions discovered. Confirm network access to AustLII "
              "and the TASCAT index path, then re-run. SAMPLE corpus left in place.")
        return

    decisions = []
    for d in found[:limit]:
        try:
            print(f"[decisions] {d['citation']}")
            decisions.append(parse_decision(d["citation"], d["url"]))
        except Exception as e:
            print(f"[decisions] failed {d['citation']}: {e}")

    if not decisions:
        print("[decisions] Nothing parsed; not overwriting existing corpus.")
        return

    write_data("decisions.json", {"decisions": decisions})
    print(f"[decisions] done: {len(decisions)} decisions.")


if __name__ == "__main__":
    main()
