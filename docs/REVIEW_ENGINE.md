# Review Engine & Retrieval

How a proposal becomes a grounded, cited assessment. All code is in `main.py`.

## Flow

```
proposal  ──▶  retrieve()  ──▶  context  ──▶  review_proposal()  ──▶  assessment JSON
              (scheme + decisions          (Gemini grounded on        (mandated schema
               + semantic passages)         context, or heuristic)     + CAVEAT)
```

## Grounding rules (non-negotiable)

- Use **only** ingested clauses/decisions. Cite every finding to a supplied
  clause ID or TASCAT citation. **Never invent** clause numbers, standards, or
  holdings.
- If context is insufficient, say so and list what's needed. Don't guess.

These are enforced by `_GEMINI_SYSTEM` (the system instruction) and structurally
by the heuristic fallback, which marks unverifiable matters `insufficient_info`.

## Retrieval — `retrieve()`

Returns `{scheme, decisions, decision_passages}`.

**Scheme clauses** (`scheme_chunks.json`)
- Scope filter: a chunk is in-scope if it is `statewide` (SPP) **or** its `scope`
  matches the proposal's municipality (LPS).
- Ranked by `_score_chunk()` — keyword overlap (exact keyword hit = +3, any other
  token hit = +1). Top `k_scheme` (default 8).

**Decisions** (`decisions.json`)
- Ranked by the same keyword overlap, **+2** when the decision's municipality
  matches the proposal. Top `k_decisions` (default 4).
- These are the ~400-char *summaries* — used for the heuristic precedent list and
  as a fallback.

**Semantic passages** (`decision_chunks.json`) — the RAG layer
- `retrieve_passages(query)` embeds the query (`gemini-embedding-001`,
  `RETRIEVAL_QUERY`), scores every stored chunk by **cosine similarity**
  (`_cosine`, pure stdlib), and returns the top `k` (default 6) above
  `min_score` (0.45).
- These carry **verbatim holding text**, so the model can ground on actual
  reasoning rather than a gist.
- **Graceful degradation:** if `decision_chunks.json` is absent or the query can't
  be embedded (no key), this returns `[]` and only the keyword path is used.
  Nothing breaks.

## Assessment — `review_proposal()`

1. Call `retrieve()`.
2. If a Gemini key is set: build a prompt with the proposal + `_format_context(ctx)`
   (scheme clauses, decision summaries, and **key precedent passages** verbatim),
   ask the model for a single JSON object, parse via
   `re.search(r'\{.*\}', text, re.DOTALL)`, ensure `caveat` is set, return.
3. On any failure, or with no key: fall back to `_heuristic_review()`.

Returns `(result_dict, ctx, engine)` where `engine` is `"gemini"` or `"heuristic"`.

## Heuristic fallback — `_heuristic_review()`

Deterministic and conservative — builds the **same schema** without an LLM:
- `_classify_use()` reads the zone Use Table to infer the pathway (No Permit
  Required / Permitted / Discretionary / Prohibited).
- `_CODE_TRIGGERS` keyword-matches hazard codes (bushfire, coastal, inundation,
  waterway, scenic, heritage, landslip) from the proposal text.
- Standards default to `insufficient_info` (acceptable solutions can't be verified
  without submitted plans).
- Risk rating rises with discretionary pathways and triggered hazards.
- Never claims `sufficient` while the corpus is SAMPLE.

## Output schema (both paths)

```json
{
  "municipality": "...",
  "applicable_zone": "...",
  "triggered_codes": ["bushfire", "..."],
  "use_classification": "Discretionary",
  "pathway_basis": "...",
  "standards_assessment": [{"standard","clause_id","acceptable_solution_met","performance_criterion_note","status"}],
  "compliance_gaps": ["..."],
  "relevant_precedents": [{"citation","relevance","outcome","principle"}],
  "risk_rating": "low|moderate|high",
  "recommended_conditions_or_info": ["..."],
  "caveat": "Analytical aid only; not a statutory determination or legal advice.",
  "context_sufficiency": "insufficient|partial|sufficient"
}
```

## Semantic RAG: what's stored vs. retrieved

| | Stored | Retrieved on |
|---|---|---|
| Scheme clauses | clause text, ≤1500 chars | keyword overlap |
| Decision summaries (`decisions.json`) | ~400-char summary + `principle` | keyword overlap (+ muni boost) |
| Decision passages (`decision_chunks.json`) | full text, ~1000-token chunks + 768-dim embedding | cosine similarity to query embedding |

The semantic layer currently covers the curated leading precedents
(`SEED_CITATIONS` in `ingest/decisions.py`). Build it with
`python -m ingest.embed` — see [`INGESTION.md`](INGESTION.md).

## Gotchas

- **Gemini model:** `gemini-2.5-flash` only, via `_gemini_model()`. Parse JSON with
  `re.search`, never `json.loads(response.text)` directly — responses are prose.
- **No SDK:** Gemini and embeddings call the REST API directly (the
  `google-generativeai` SDK was removed; its successor had a cffi dependency
  conflict in the container).
- **Embeddings dimension:** `gemini-embedding-001` returns 768 floats. If you change
  `EMBED_MODEL`, re-run `ingest.embed` so the index matches.
