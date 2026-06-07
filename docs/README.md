# TasPlan Review — Documentation

Developer documentation for the Tasmanian planning assessment assistant.

| Doc | Read it for… |
|-----|--------------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Stack, data-access rule (incl. `save_bytes`/`load_bytes` for image blobs), routes, AI surfaces, env vars, data file schemas, run history & image uploads |
| [REVIEW_ENGINE.md](REVIEW_ENGINE.md) | How a proposal becomes a grounded assessment — retrieval, zone-aware scoring, semantic RAG, the Gemini + heuristic paths, output schema; Ask Holly (refine loop, correct & rework, multimodal image attachments, continue-from-history, PDF) and run history |
| [SUBDIVISION.md](SUBDIVISION.md) | Ask Holly's minimum-compliant-subdivision feasibility capability — min lot size/frontage/setbacks/yield derived from cited standards, with the grounding guardrails |
| [COUNCIL.md](COUNCIL.md) | Planning Council — the 3-stage multi-LLM debate, provider config, the page layout/SSE client, and every known provider failure mode with its fix (Groq Cloudflare 1010, MiniMax host) |
| [INGESTION.md](INGESTION.md) | Building the corpus — scheme clauses (SPP + LPS), TASCAT decisions, the semantic index, SAMPLE→LIVE provenance |
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
