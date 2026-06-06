# TasPlan Review — Documentation

Developer documentation for the Tasmanian planning assessment assistant.

| Doc | Read it for… |
|-----|--------------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Stack, data-access rule, routes, AI surfaces, env vars, data file schemas |
| [REVIEW_ENGINE.md](REVIEW_ENGINE.md) | How a proposal becomes a grounded assessment — retrieval, semantic RAG, the Gemini + heuristic paths, output schema |
| [INGESTION.md](INGESTION.md) | Building the corpus — scheme clauses, TASCAT decisions, the semantic index |
| [SETUP.md](SETUP.md) | Local dev and Cloud Run deployment |

## The one-paragraph version

A single-file Flask app (`main.py`) assesses a development proposal against the
Tasmanian Planning Scheme and TASCAT precedent. It retrieves municipality-scoped
clauses and relevant decisions — by keyword overlap and, for leading precedents,
by **semantic similarity over embedded full text** — then either prompts Gemini
(grounded strictly on that context) or falls back to a deterministic heuristic.
Every assessment cites only ingested material and carries the load-bearing
caveat: *"Analytical aid only; not a statutory determination or legal advice."*

## Non-negotiables

- **Data access:** only `load_json` / `save_json`. Files live at the GCS bucket
  root in production. Deploys never update GCS data.
- **Grounding:** cite only ingested clauses/decisions; never invent clause
  numbers, standards, or holdings.
- **Gemini:** `gemini-2.5-flash` via `_gemini_model()`; parse JSON with
  `re.search`, not direct `json.loads`.
- **The caveat** appears in the UI and on every review. Do not remove it.
