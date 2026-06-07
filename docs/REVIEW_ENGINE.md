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
- **Zone-aware scoring.** `_detect_zone(text)` maps a zone name in the query
  ("Inner Residential" → 9) via the longest-phrase match in `_SPP_ZONES`;
  `_zone_bonus()` then adds **+6** to clauses whose `clause_id` is governed by that
  zone (e.g. `SPP 9.*`). This stops a query about one zone surfacing another
  zone's standards. `_detect_municipality(text)` likewise picks the council named
  in free-text questions (used by Ask Holly and the Council, which have no
  separate municipality field).

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

## Ask Holly — `/ask`

Free-form planning Q&A. The route detects the municipality from the question
(`_detect_municipality`), calls `retrieve()` (with zone-aware scoring, larger
`k_scheme=12`), formats that context, and prompts Gemini with `_HOLLY_SYSTEM` —
which instructs Holly to answer **only** from the supplied clauses/decisions,
name the zone each cited clause governs, and never apply a standard from a
different zone. With no Gemini key, Holly is offline (the form is disabled).
Retrieval and answer/error events are logged as `ask.retrieve` / `ask.answer` /
`ask.error`. The client shows a spinner during the synchronous POST. The page is
laid out like the Council — full-width question on top, full-width answer below —
and the answer is rendered through the **`mdlite`** Jinja filter (`_md_lite` in
`main.py`), which escapes HTML first then applies the same safe markdown subset
(bold, headings, bullets, dividers) as the council's client-side renderer.

**Clarify → refine loop.** When an answer trips `_is_insufficient()` (it flagged
missing info), `_holly_followups()` makes a second Gemini call to extract up to 6
specific follow-up questions (concrete proposal facts, or an invitation to paste a
clause the corpus lacks). These render as inline fields under the answer. On
submit, `_collect_supplied()` merges the answered ones (carried as a JSON
`supplied` field) and the next prompt includes an **ADDITIONAL CONTEXT SUPPLIED BY
THE PLANNER** block. Holly treats those facts as established and may quote
planner-pasted clause text **attributed as `(planner-supplied)`**, kept distinct
from the ingested corpus (it still never invents). The loop repeats — each refine
accumulates context and may surface new questions.

**Comment, correct & rework.** The refine loop only opens when Holly flags its *own*
gaps; a confident-but-wrong answer (notably a street name or dimension misread from an
attached image) gave the planner no way in. So a **Comment, correct & rework** form is
shown after **every** answer with a free-text box used either to **correct a detail** or
to **ask for further points** to be addressed. On submit, `_collect_supplied()` captures
the text as an authoritative entry (`q = "Planner correction (authoritative — overrides
any image-derived or previously stated value)"`), and the **ADDITIONAL CONTEXT SUPPLIED
BY THE PLANNER** block instructs Holly that where a correction conflicts with a value she
would otherwise use — especially an image-derived street name or dimension — the
correction is authoritative: adopt it and discard the earlier value.

Crucially, the rework **builds on Holly's previous answer rather than regenerating from
scratch.** The form carries that answer in a hidden `prior_answer` field (the refine form
does too); when present, the prompt gains a **YOUR PREVIOUS ANSWER** block instructing
Holly to produce an *updated* answer — apply the planner's corrections/comments, keep
what's still correct, and address any further points — without dropping established
findings unless a correction supersedes them. (A fresh re-ask via the top **Ask Holly**
button sends no `prior_answer`, so it regenerates; the rework/continuity path is the
comment form.) Each rework is saved as a new History run and the correction carries
forward like any other supplied context.

**Image attachments (multimodal).** Planners can drag-drop (or browse) up to **4
images** — site plans, architect drawings, Google Maps / aerial screenshots — in a
drop zone under the question (PNG/JPG/WebP/GIF, ≤8 MB each; the form is
`multipart/form-data`). On submit:

1. `_store_uploaded_images()` validates each file (MIME + size) and writes it as a
   random-keyed blob under `uploads/` via `save_bytes` (GCS in prod).
2. `_images_as_gemini_parts()` loads the blobs and base64-encodes them as Gemini
   `inlineData` parts; `generate_content(prompt, images=…)` sends them with the
   grounded text prompt.
3. `_HOLLY_SYSTEM` instructs Holly to first report what it can **observe** under an
   *Image observations* heading (dimensions, scale bars, lot boundaries, setbacks,
   storeys), flag anything scaled without a stated scale bar as an **ESTIMATE**, and
   say plainly when something is illegible — **never guess a measurement**.
   Image-derived figures are treated as planner input, attributed **`(image-derived)`**,
   and are **not** corpus citations. The assessment still cites scheme clauses /
   TASCAT decisions only.

The refine loop carries the images forward via a hidden `images_json` field
(`_collect_images` merges prior refs with any newly-attached files), so a sharpened
follow-up keeps the same visual context. Thumbnails of the images Holly read are
shown under the answer and on the History detail (served via `/uploads/<name>`).
Upload failures are logged as `ask.upload_error` and never block the answer.

**PDF export.** `POST /ask/pdf` renders the question, any planner-supplied context,
and the assessment to a downloadable PDF via `_report_pdf()` (fpdf2, pure-Python;
`_pdf_safe` maps non-Latin-1 glyphs, `_pdf_render_markdown` handles
headings/bold/bullets/dividers). The caveat is stamped in the footer.

The same `retrieve()` + grounding rules power the **Planning Council** — see
[COUNCIL.md](COUNCIL.md).

## Run history

Every AI run across the four surfaces is persisted so planners can find and re-open
past work. `_record_run(kind, title, output, prompt, supplied, meta)` appends a
record to `history.json` (newest first, capped 1000) — best-effort, wrapped in
try/except so a write failure never breaks the user flow:

| Field | Contents |
|-------|----------|
| `kind` | `ask` \| `council` \| `review` \| `caselaw` |
| `title` | short excerpt for the list view (≤200 chars) |
| `prompt` | the **full** input that produced the run (Holly/Council question, the proposal fields, or the pasted decision text), capped at 20k chars |
| `output` | the answer/synthesis (prose) or the assessment JSON |
| `supplied` | planner-supplied context (Ask Holly refine answers) |
| `meta` | per-kind extras — e.g. `municipality`, `zone`, council `members`, and `images` (attached-image refs) |

`/history` lists runs with search + kind filter + "show more"; `/history/<id>`
shows the full prompt, output (rendered via `mdlite` for ask/council, JSON for
review/caselaw), planner-supplied context, and attached-image thumbnails;
`/history/<id>/pdf` exports the run. Records written before the `prompt` field
existed fall back to the title. Each surface calls `_record_run` itself — Ask Holly
records **before** the (slower) follow-up step so the answer survives even if the
user navigates away.

**Continue in Ask Holly.** The History detail page has a **Continue in Ask Holly**
button that opens `/ask?from=<id>` pre-loaded with that run's question, planner-supplied
context, attached images, **and — for `ask`/`council` runs — Holly's last response** —
so a saved run can be worked further (comment to correct or extend, add context/images,
edit the question, re-ask). The `/ask` GET handler reads `?from=`, looks the run up in
`history.json`, and seeds `question` / `supplied` / `image_refs` (image keys re-validated
against `_UPLOAD_NAME_RE`) plus `answer` from the run's `output`; the main ask form carries
the prior `supplied` and `images_json` forward via hidden fields, so the re-ask reuses the
same carry-forward path as the refine loop. Because the prior answer is loaded, the planner
lands straight on the **Comment, correct & rework** form and Holly reworks *from* that
answer (see above). The re-ask/rework is saved as a **new** History run — the original is
never modified. No new route, engine, or storage; it rides the existing Ask Holly surface.

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
