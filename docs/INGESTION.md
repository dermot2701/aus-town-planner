# Ingestion Pipeline

Three network-gated scripts build the corpus. Each writes through the same JSON
schema the app's `retrieve()` consumes. Run as modules from the repo root.

> **Be a good citizen.** Requests are throttled (`THROTTLE_SECONDS = 2.0`) and
> cached under `ingest/cache/` (gitignored). AustLII restricts bulk crawling.

| Script | Source | Output | Purpose |
|--------|--------|--------|---------|
| `ingest/scheme.py` | `tpso.planning.tas.gov.au` | `scheme_chunks.json`, `scheme_manifest.json` | SPP + LPS clauses |
| `ingest/decisions.py` | AustLII (`www8.austlii.edu.au`) | `decisions.json` | TASCAT/TASRMPAT decision summaries |
| `ingest/embed.py` | AustLII + Gemini embeddings | `decision_chunks.json` | Semantic index (RAG layer) |

## Shared HTTP layer — `ingest/__init__.py`

- `fetch(url, binary=False)` — throttled, cached GET with a browser User-Agent,
  a cookie jar, a pre-warm visit to the AustLII homepage (`_warm()`), a `Referer`
  header, and gzip handling. These defeat AustLII's bot blocks for the
  `feed` / `viewtoc` / `viewdoc` endpoints.
- `write_data(filename, payload)` / `read_data(filename)` — local `data/` dir I/O
  for the ingest scripts (the app itself uses `load_json` / `save_json`).

## Decisions — `ingest/decisions.py`

Covers **TASCAT** (2021–present) and **TASRMPAT** (pre-2021, the former Resource
Management & Planning Appeal Tribunal).

**Discovery** (`discover()`) uses two AustLII sources and dedupes by case number:
1. **RSS feed** — `/cgi-bin/feed/au/cases/tas/{DB}/` — the ~29 most recent cases.
2. **Year-index** — `/cgi-bin/viewtoc/au/cases/tas/{DB}/{year}/` — the full
   per-year archive. (The plain `/au/cases/...` path 500s on `www8`; the
   `cgi-bin/viewtoc` path is the working one.)

**Filtering & extraction**
- `_is_planning()` keeps only Resource & Planning matters (TASCAT also hears
  guardianship, health, medical-board, anti-discrimination streams).
- With a Gemini key, `_llm_fields()` extracts `{title, municipality, outcome,
  principle, use_classes, keywords, summary}`. Without one, `_heuristic_fields()`
  uses regex and leaves `principle` blank.

**Modes**
```bash
# Full archive from 2022, merged into the existing corpus
python -m ingest.decisions --year-from 2022 --limit 400 --merge

# Curated leading precedents only (fetched directly by viewdoc URL)
python -m ingest.decisions --seed

# Browser-saved HTML files (when AustLII blocks automated fetches)
python -m ingest.decisions --local-dir ~/Downloads/tascat --db tascat
```

| Flag | Effect |
|------|--------|
| `--db {tascat,rmpat,both}` | which tribunal(s) |
| `--year-from / --year-to` | year range for discovery |
| `--limit N` | cap cases fetched |
| `--seed` | ingest `SEED_CITATIONS` (leading precedents) by direct viewdoc URL |
| `--merge` | merge into existing `decisions.json` by citation rather than overwrite |
| `--local-dir DIR` | process locally saved HTML, no network |
| `--no-gemini` | skip the LLM pass even if a key is set |

### Leading precedents — `SEED_CITATIONS`

A curated list of the most-cited TASCAT R&P authorities (e.g. Owens v Kingborough,
the Robbins Island wind-farm appeal, Mt Wellington Cableway). They're fetched by
direct `viewdoc` URL — this **bypasses AustLII's CAPTCHA-gated search endpoint**.
Edit the list in `ingest/decisions.py` as the leading authorities evolve.

## Semantic index — `ingest/embed.py`

Builds the RAG layer (`decision_chunks.json`). Requires `GEMINI_API_KEY`.

```bash
GEMINI_API_KEY=... python -m ingest.embed            # seed precedents (default)
GEMINI_API_KEY=... python -m ingest.embed --all      # every case in decisions.json
GEMINI_API_KEY=... python -m ingest.embed --limit 5  # small test run
```

Per case: fetch full `viewdoc` text → `chunk_text()` into ~3500-char
(~1000-token) windows with 300-char overlap → embed each chunk
(`gemini-embedding-001`, `RETRIEVAL_DOCUMENT`) → write
`{chunk_id, citation, title, municipality, text, embedding[768]}`.

This **complements** `decisions.json` (kept for the `/decisions` browse page and
keyword fallback) — it does not replace it.

## Scheme — `ingest/scheme.py`

Ingests the **State Planning Provisions (SPP)** — the statewide rulebook (zone
purposes, use tables, codes, and acceptable-solution / performance-criterion
standards) — from the published PDF. Downloads it, extracts text (prefers
`pdftotext`, falls back to `pdfminer.six`), and chunks by clause heading
(`8.2 Use Table`, `10.4.1`, `C10.6.1`, …), deduplicating against the PDF's
table of contents.

```bash
python -m ingest.scheme                      # current effective SPP
python -m ingest.scheme --pdf-url <url>      # a newer consolidated SPP
python -m ingest.scheme --pdf-file spp.pdf   # a locally downloaded PDF
```

Yields ~500 clauses (≈400 standards, 23 use tables, 27 codes), all
`provenance: "LIVE"`, `scope: "statewide"`.

> **LPS not yet ingested.** The per-municipality **Local Provisions Schedules**
> live in the dynamic TPSO viewer (`tpso.planning.tas.gov.au/tpso/external/
> planning-scheme-viewer/{id}/section/{n}`), a JavaScript SPA backed by an
> undocumented JSON API. Wiring that up needs the API endpoint (capture it from
> the browser DevTools → Network tab). The SPP carries the substantive
> provisions a review grounds on; the LPS layer mainly maps zones to parcels,
> which the planner supplies as the proposal's zone.

## Corpus status & the SAMPLE banner

The committed corpus is illustrative **SAMPLE** data (`provenance: "SAMPLE"`,
citations suffixed `(SAMPLE)`, persistent UI banner driven by
`app_config.corpus_status`). Ingestion overwrites it with `provenance: "LIVE"`
records. The banner clears once both scheme and decisions are LIVE.

## Deploying re-ingested data

**Deploys never touch GCS data.** After ingesting locally, upload:

```bash
gcloud storage cp data/decisions.json       gs://aus-town-planner-data/decisions.json       --project=aus-town-planner
gcloud storage cp data/decision_chunks.json gs://aus-town-planner-data/decision_chunks.json --project=aus-town-planner
gcloud storage cp data/scheme_chunks.json   gs://aus-town-planner-data/scheme_chunks.json   --project=aus-town-planner
```

Files must land at the **bucket root**, not under a `data/` prefix — see
[`ARCHITECTURE.md`](ARCHITECTURE.md#data-access--the-one-rule).
