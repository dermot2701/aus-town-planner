# Architecture

TasPlan Review is a single-process Flask app that assesses a development proposal
against the **Tasmanian Planning Scheme** (State Planning Provisions + Local
Provisions Schedules) and **TASCAT** precedent, returning a grounded, cited
assessment.

> **Load-bearing caveat.** Every assessment carries:
> *"Analytical aid only; not a statutory determination or legal advice."*
> (`CAVEAT` in `main.py`). It appears in the UI and is stamped on every review.
> Do not remove it.

## Stack

| Component | Detail |
|-----------|--------|
| Backend | Python 3.11 / Flask 3.x — everything in `main.py` (routes + helpers + review engine) |
| Data | JSON files, read/written **only** via `load_json` / `save_json` |
| Storage | Local `data/` dir, or Google Cloud Storage when `GCS_BUCKET` is set |
| Auth | Session-based, Werkzeug password hashing, `@login_required` / `@admin_required` |
| AI | Google Gemini (`gemini-2.5-flash`) via `_gemini_model()`; degrades gracefully without a key |
| Embeddings | Gemini `gemini-embedding-001` (free tier) via `_embed_text()` |
| Multi-LLM | Gemini + Llama (Groq) + MiniMax for the Planning Council |
| Frontend | Jinja2 + Bootstrap 5 (layout only) + Palantir-dark design system (teal accent) |
| Serving | Cloud Run → Gunicorn (1 worker, 8 threads). Domain `aus-planner.allbridge.com.au` |

## Data access — the one rule

**All reads/writes go through `load_json(filename)` / `save_json(filename, data)`**
(`main.py:60`). These switch between the local `data/` directory and a GCS bucket
based on the `GCS_BUCKET` env var. Never open JSON files directly.

In production, files live at the **root** of `gs://aus-town-planner-data` — e.g.
`load_json("users.json")` maps to `gs://aus-town-planner-data/users.json`, *not* a
`data/` subfolder. **Deploys do NOT update GCS data** — re-ingested corpus must be
uploaded manually with `gcloud storage cp`.

**Binary blobs** (Ask Holly image attachments) use the parallel
`save_bytes(filename, data, content_type)` / `load_bytes(filename)` helpers —
same GCS-vs-local switch, but bytes in/out and never JSON-encoded. They are kept
deliberately separate from `load_json`/`save_json` (which are for structured data
only) and live under the `uploads/` prefix.

## Data files

| File | Shape | Written by |
|------|-------|------------|
| `app_config.json` | `{app_name, tagline, corpus_status, corpus_banner}` | Admin panel |
| `users.json` | `{username: {name, role, password_hash}}` | `init_admin.py`, Admin panel |
| `scheme_chunks.json` | `{chunks: [{clause_id, instrument, scope, kind, zone_or_code, title, text, keywords[], use_classes[], provenance}]}` | `ingest/scheme.py` (SPP), `ingest/lps.py` (LPS, merged) |
| `scheme_manifest.json` | `{provenance, sources[], municipalities_covered[], chunk_count, instruments[]}` | `ingest/scheme.py`, `ingest/lps.py` |
| `decisions.json` | `{decisions: [{citation, title, municipality, use_classes[], keywords[], summary, outcome, principle, provenance}]}` | `ingest/decisions.py` |
| `decision_chunks.json` | `{chunks: [{chunk_id, citation, title, municipality, text, embedding[768]}]}` | `ingest/embed.py` |
| `skills.json` | `{title, description, skills: [{id, number, title, summary, competencies[]}]}` | committed reference data |
| `history.json` | `{runs: [{id, ts, user, kind, title, prompt, output, supplied[], meta}]}` (newest first, capped 1000). `prompt` is the full question/input that produced the run; `meta.images` holds attached-image refs | `_record_run()` on each AI run |
| `uploads/<32-hex>.<ext>` | Binary image blobs — Ask Holly attachments (JPG/PNG/WebP/GIF). **Not JSON** — read/written via `load_bytes`/`save_bytes` | `_store_uploaded_images()` |

## Routes

| Route | Auth | Purpose |
|-------|------|---------|
| `GET/POST /login`, `GET /logout` | — | Session auth |
| `GET /` | login | Home / launch centre |
| `GET /review`, `POST /api/review` | login | Submit a proposal → grounded JSON assessment |
| `GET /scheme` | login | Browse/search SPP + LPS clauses, scoped by municipality |
| `GET /decisions` | login | Keyword search of TASCAT decisions |
| `GET/POST /caselaw` | login | Case Review — structured Gemini analysis of a pasted decision |
| `GET/POST /ask`, `POST /ask/pdf` | login | Ask Holly — free-form planning Q&A (multimodal: accepts image attachments), refine loop, PDF export |
| `GET /uploads/<name>` | login | Serve a stored Ask Holly image attachment — strict-regex key (rejects traversal / non-image names) |
| `GET /council`, `GET /council/stream` | login | Planning Council — 3-stage multi-LLM debate (SSE) |
| `GET /history`, `/history/<id>`, `/history/<id>/pdf` | login | Saved AI runs (Holly/Council/Review/Case Review) — list+filter, full prompt + output + attached-image thumbnails, PDF |
| `GET /skills` | login | Planner competency framework |
| `GET /admin` | admin | Corpus status, source manifest, user management |
| `POST /admin/users/{add,edit,delete}` | admin | User CRUD |

## AI surfaces

| Surface | Model(s) | System instruction |
|---------|----------|--------------------|
| Review engine | Gemini (heuristic fallback) | `_GEMINI_SYSTEM` — JSON-only, grounded on retrieved context |
| Ask Holly | Gemini (multimodal) | `_HOLLY_SYSTEM` — conversational planning advice; reads attached site plans/drawings/map screenshots and reports observable dimensions as `(image-derived)` facts, flagging estimates |
| Case Review | Gemini | `_CASELAW_SYSTEM` — structured JSON case analysis |
| Planning Council | Gemini (Chair) + Groq (Llama 3.3 70B) + MiniMax M2.1 | `_COUNCIL_MEMBER_SYSTEM` / `_COUNCIL_CHAIRMAN_PREAMBLE` — see [COUNCIL.md](COUNCIL.md) |

`_gemini_model(system=None)` is the **single Gemini factory** — it calls the REST
API directly (no SDK). Pass a system instruction for non-review surfaces. The
wrapper's `generate_content(prompt, images=None)` accepts optional `inlineData`
image parts (base64) for the multimodal Ask Holly path; they are placed before
the text prompt. All Gemini JSON responses are parsed via
`re.search(r'\{.*\}', text, re.DOTALL)`.

## Environment variables

| Var | Purpose | Default |
|-----|---------|---------|
| `SECRET_KEY` | Flask session secret | dev default — set in prod |
| `GEMINI_API_KEY` | Gemini reviews, embeddings, Holly, Case Review, Council Chair (Holly) | unset → heuristic |
| `GROQ_API_KEY` | Council member (Llama 3.3 70B) | unset |
| `MINIMAX_API_KEY` | Council member (MiniMax M2.1, via `api.minimax.io/anthropic`) | unset |
| `GCS_BUCKET` | Use Cloud Storage instead of `data/` | unset → local |

## Cross-cutting helpers & gotchas

- **`_http_post_json(url, payload, headers)`** is the single outbound-POST helper
  for the non-Gemini council providers. It sends a **browser `User-Agent`**
  (Groq's Cloudflare WAF 403s the default `Python-urllib` agent with error `1010`)
  and, on an HTTP error, **reads and surfaces the response body** so failures are
  legible. See [COUNCIL.md](COUNCIL.md).
- **Structured logging.** `_log(event, **fields)` emits one JSON line per event to
  stdout (Cloud Run captures it). Searchable events include `ask.retrieve` /
  `ask.answer` / `ask.error` / `ask.upload_error` (Ask Holly) and `council.retrieve` /
  `council.member_error` / `council.stage_done` / `council.synthesis_error` /
  `council.done` (Planning Council). When a council member fails, the real cause
  is in `council.member_error.detail`.
- **Run history.** `_record_run(kind, title, output, prompt, supplied, meta)` appends
  every AI run (Ask Holly, Council, Review, Case Review) to `history.json` (newest
  first, capped 1000) — best-effort, never breaking the user flow. It stores the
  full `prompt` (untruncated) plus a short `title` excerpt; `meta.images` carries
  any attached-image refs. Surfaced at `/history`. See [REVIEW_ENGINE.md](REVIEW_ENGINE.md#run-history).
- **Image uploads.** Ask Holly attachments are validated (`_store_uploaded_images`:
  ≤4 files, ≤8 MB, JPG/PNG/WebP/GIF), stored as random-keyed blobs under `uploads/`
  via `save_bytes`, and re-attached across the refine loop via a hidden `images_json`
  field (`_collect_images`). `_images_as_gemini_parts` base64-encodes them for the
  Gemini call. Served back through `/uploads/<name>`, gated by `_UPLOAD_NAME_RE`
  (a strict `^[0-9a-f]{32}\.(jpg|png|webp|gif)$` match) so the route can't be used
  for path traversal or to read arbitrary blobs.
- **App-shell layout.** `.app-shell` is a CSS grid whose first column is the
  (fixed) sidebar width and second column is content. Content elements
  (`.app-main`, `.news-ticker`) must **not** also carry `margin-left:
  var(--sidebar-w)` — the grid already offsets them, and a second offset leaves an
  empty band between the sidebar and content. The topbar (no margin) is the
  reference for correct alignment.
- **Inherited CSS gotchas** (from the shared scaffold): never add `transform` to
  `.fade-in` (it traps `position:fixed` modals in Chrome); animate
  `.modal-overlay.active .modal-box`, not `.modal-box`; `app.css` loads after
  Bootstrap on purpose — don't reorder; no `data-bs-toggle`.

See [`SETUP.md`](SETUP.md) for deployment, [`REVIEW_ENGINE.md`](REVIEW_ENGINE.md)
for how assessments are produced, [`COUNCIL.md`](COUNCIL.md) for the multi-LLM
council, and [`INGESTION.md`](INGESTION.md) for building the corpus.
