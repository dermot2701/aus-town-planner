# Ingestion Pipeline

Three network-gated scripts build the corpus. Each writes through the same JSON
schema the app's `retrieve()` consumes. Run as modules from the repo root.

> **Be a good citizen.** Requests are throttled (`THROTTLE_SECONDS = 2.0`) and
> cached under `ingest/cache/` (gitignored). AustLII restricts bulk crawling.

| Script | Source | Output | Purpose |
|--------|--------|--------|---------|
| `ingest/scheme.py` | SPP PDF (`stateplanning.tas.gov.au`) | `scheme_chunks.json`, `scheme_manifest.json` | State Planning Provisions (statewide) |
| `ingest/lps.py` | TPSO viewer API (`tpso.planning.tas.gov.au`) | `scheme_chunks.json` (merge), `scheme_manifest.json` | Local Provisions Schedules (per-municipality) |
| `ingest/decisions.py` | AustLII (`www8.austlii.edu.au`) | `decisions.json` | TASCAT/TASRMPAT decision summaries |
| `ingest/embed.py` | AustLII + Gemini embeddings | `decision_chunks.json` | Semantic index (RAG layer) |

> **Order matters for the scheme.** `ingest/scheme.py` writes `scheme_chunks.json`
> from scratch (SPP only); `ingest/lps.py` then **merges** each council's LPS into
> that same file. Always run `scheme` first, then `lps`, then upload the combined
> file to GCS.

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

A curated list (**55 cases**, 2021–2026) of the most-cited TASCAT R&P authorities
(e.g. Owens v Kingborough, the Robbins Island wind-farm series, Mt Wellington
Cableway, Fragrance Tas-Hobart) plus recent council appeals across most
municipalities. They're fetched by direct `viewdoc` URL — this **bypasses
AustLII's CAPTCHA-gated search endpoint**. Every citation's case-name pairing is
taken from AustLII's own most-cited / search index, never recalled from memory.
Edit the list in `ingest/decisions.py` as the leading authorities evolve — only
land-use planning matters belong here (a strata-title and a party-v-party case
were excluded even though they appeared in the source lists).

> **Never invent a citation.** A seed entry is fetched by citation → URL, so a
> wrong number silently ingests the *wrong* decision and mislabels it. Only add
> citations confirmed against AustLII's own index — otherwise widen with the
> year-crawl (`--db rmpat --year-from …`), which discovers real citations.

### Quick start — ingest decisions with the key from Secret Manager

In production the Gemini key lives in **Secret Manager** as the secret
`GEMINI_API_KEY` (the same secret Cloud Run mounts). Pull it into the env var
for the ingest run rather than pasting it — run this from the Mac mini (or any
machine with `gcloud` auth and network access to AustLII):

```bash
cd ~/path/to/aus-town-planner
git checkout main && git pull

# Pull the Gemini key straight out of Secret Manager into the env var
export GEMINI_API_KEY="$(gcloud secrets versions access latest \
  --secret=GEMINI_API_KEY --project=aus-town-planner)"

# Sanity check it loaded before spending time ingesting
[ -n "$GEMINI_API_KEY" ] && echo "Key loaded (${#GEMINI_API_KEY} chars)" \
  || { echo "Could not read GEMINI_API_KEY from Secret Manager"; exit 1; }

# Ingest the curated leading cases (merges with existing corpus, dedup by citation)
./venv/bin/python -m ingest.decisions --seed --merge

# Push the result to production (deploys do NOT update GCS data)
gcloud storage cp data/decisions.json gs://aus-town-planner-data/decisions.json \
  --project=aus-town-planner
```

Then reload `/decisions` — new cases appear immediately, no redeploy needed.
For depth, follow with a year-crawl using the same exported key, e.g.
`./venv/bin/python -m ingest.decisions --db both --year-from 2010 --merge`.

> Drop the `--project` flags if your `gcloud config` default project is already
> `aus-town-planner`. If the secret lives in a different project, set
> `--project` to that one. Confirm the secret name with
> `gcloud secrets list --project=aus-town-planner --filter="name:GEMINI"`.

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

## Local Provisions Schedules — `ingest/lps.py`

Ingests each council's **Local Provisions Schedule (LPS)** — particular purpose
zones, specific area plans, site-specific qualifications, and code overlays
(e.g. the local heritage list) — from the TPSO viewer. The viewer is a
JavaScript SPA, but it is backed by a small, unauthenticated JSON/HTML API:

```
/planning-scheme-viewer/{schemeId}/init
    → {"title": "Tasmanian Planning Scheme - Glenorchy",
       "openSectionId": 2096,                 # the LPS root section
       "planningSchemeTypeName": "Local Provisions Schedule"}
/planning-scheme-viewer/{schemeId}/document-tree?effectiveForDate=YYYY-MM-DD
    → nested {name, sectionId, children, orderNum}
/planning-scheme-viewer/{schemeId}/section/{sectionId}/html?includeChildren=true&effectiveForDate=…
    → rendered HTML of that section and all descendants (the clause text)
```

So per council it's: `init` → `openSectionId` → fetch that section's HTML with
`includeChildren=true` → flatten HTML to text → chunk by clause heading
(`GLE-S1.0`, `GLE-P2.1`, `GLE-C6.1.258`, …). Each chunk is written with
`scope` = the council name (lowercased) so the app's `retrieve()` pulls it in
for that municipality alongside the statewide SPP. Output schema is identical to
`ingest/scheme.py`.

```bash
python -m ingest.lps --discover            # probe scheme ids, print id→council map
python -m ingest.lps                       # discover + ingest every LPS (merges into SPP)
python -m ingest.lps --scheme-id 15        # one council (15 = Glenorchy), repeatable
python -m ingest.lps --id-range 1-60       # widen/narrow the discovery probe
python -m ingest.lps --replace             # overwrite scheme_chunks.json (drops SPP — rarely wanted)
```

It **merges** by default: existing chunks for the councils being re-ingested are
replaced, the statewide SPP and any other councils are kept. Scheme ids are
discovered by probing `/{id}/init` and keeping those whose
`planningSchemeTypeName` is `"Local Provisions Schedule"` (scheme id 30 is the
SPP-only view and is skipped). Glenorchy alone yields ~690 LPS clauses.

## Corpus status & the SAMPLE banner

The repo's committed corpus is illustrative **SAMPLE** data (`provenance:
"SAMPLE"`, citations suffixed `(SAMPLE)`, banner driven by
`app_config.corpus_status`). Ingestion replaces it with `provenance: "LIVE"`
records and flips `corpus_status` to `LIVE`; the banner is gated on that flag
(see `templates/base.html`, `admin.html`, `home.html`). **Production (GCS) is
LIVE:** the real SPP and TASCAT decisions are loaded. Per the data rule, the
LIVE corpus lives only in GCS — it is never committed to the repo.

## Deploying re-ingested data

**Deploys never touch GCS data.** Re-ingest locally, then upload. For the
scheme, run `scheme` then `lps` so the combined file carries both layers:

```bash
python -m ingest.scheme                                  # SPP → scheme_chunks.json
python -m ingest.lps                                     # merge every LPS into it
gcloud storage cp data/scheme_chunks.json   gs://aus-town-planner-data/scheme_chunks.json   --project=aus-town-planner
gcloud storage cp data/scheme_manifest.json gs://aus-town-planner-data/scheme_manifest.json --project=aus-town-planner
gcloud storage cp data/decisions.json       gs://aus-town-planner-data/decisions.json       --project=aus-town-planner
gcloud storage cp data/decision_chunks.json gs://aus-town-planner-data/decision_chunks.json --project=aus-town-planner
```

Files must land at the **bucket root**, not under a `data/` prefix — see
[`ARCHITECTURE.md`](ARCHITECTURE.md#data-access--the-one-rule).

## Curated supplement (always-available clauses)

Table-based standards — density, parking rates, multiple-dwelling and
private-open-space provisions, use tables — are easily lost when the SPP PDF is
extracted and chunked (they are number-heavy and `pdftotext` mangles tables).
`chunk_spp()` now keeps longer number-heavy blocks (only short, mostly-numeric
lines are treated as table-of-contents stubs), so a re-run captures more — but
verify via `/scheme` afterwards.

For provisions that still must be guaranteed present, `_SUPPLEMENT_CHUNKS` in
`main.py` is a **code-bundled** list of curated clauses merged into retrieval by
`_load_scheme_chunks()` (and shown in `/scheme`, counted in admin). Because it
ships with the code, it is always available **independent of GCS** — no upload
needed, unlike `scheme_chunks.json`. The supplement wins on a `clause_id`+`scope`
clash with the ingested corpus.

Rules for the supplement (it is exempt from `load_json` only because it is code,
not data — treat its content with the same rigour as the corpus):

- Paste **verbatim** official scheme text. Never paraphrase or invent — the
  grounding rule still applies; planners rely on these as citations.
- Use the real `clause_id` (e.g. `SPP 9.4.x`) and set `provenance: "LIVE"`.
- Keep it small: it is a backstop for known gaps, not a second corpus. Prefer a
  proper re-ingest where the chunker can capture the clause from source.
