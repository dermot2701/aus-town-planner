# Claude Code — TasPlan Review

**TasPlan Review** is a Tasmanian town-planning assessment assistant. It reviews a
development proposal against the **Tasmanian Planning Scheme** (State Planning
Provisions + Local Provisions Schedules) and relevant **TASCAT** precedent, and
returns a grounded, cited assessment.

> It is an **analytical aid for qualified planners — NOT a statutory determination
> and NOT legal advice.** This caveat is load-bearing: it appears in the UI and is
> stamped on every review. Do not remove it.

## What it does

| Capability | Description |
|------------|-------------|
| Review | `/review` — submit a proposal (municipality, PID, zone, use class, description); get a single grounded JSON assessment |
| Planning Scheme | `/scheme` — browse/search ingested SPP + LPS clauses, scoped by municipality |
| TASCAT Decisions | `/decisions` — keyword search of Resource & Planning decisions with outcomes + principle |
| Admin / Ingestion | `/admin` — corpus status, source manifest, ingestion controls |

## Architecture

| Component | Detail |
|-----------|--------|
| Backend | Python 3.11 / Flask 3.x, single `main.py` (routes + helpers + review engine) |
| Data | JSON in `data/` (local) or GCS when `GCS_BUCKET` is set. Access ONLY via `load_json`/`save_json` |
| Auth | Session-based, Werkzeug hashing, `@login_required` / `@admin_required` |
| AI | Google Gemini (`gemini-2.5-flash`) via `_gemini_model()`; degrades gracefully when no key |
| Ingestion | `ingest/scheme.py` (SPP), `ingest/lps.py` (LPS), `ingest/decisions.py`, `ingest/embed.py` — network-gated, run as `python -m ingest.<name>` |
| Frontend | Jinja2 + Bootstrap 5 (layout only) + Palantir-dark design system (teal accent) |

## The review engine (main.py)

- `retrieve(query, municipality, zone, use_class)` — returns scheme clauses scoped to
  **statewide SPP + the proposal's municipality (LPS)**, plus keyword-matched decisions.
- `review_proposal(proposal)` — retrieves context, then:
  - with a Gemini key: prompts the model **grounded only on retrieved context**, parses
    JSON via `re.search(r'\{.*\}', text, re.DOTALL)`.
  - without a key (or on failure): `_heuristic_review()` builds the same JSON
    deterministically — conservative, marks unverifiable matters `insufficient_info`.
- Both paths return the mandated schema and the `CAVEAT`.

## Grounding rules (non-negotiable)

- Use **only** ingested clauses/decisions. Cite every finding to a supplied clause ID
  or TASCAT citation. **Never invent** clause numbers, standards, or holdings.
- If context is insufficient, say so and list what's needed. Don't guess.

## Corpus status

The committed corpus is **illustrative SAMPLE data** (`provenance: "SAMPLE"`, citations
suffixed `(SAMPLE)`, persistent UI banner). Real ingestion requires outbound access to
`tpso.planning.tas.gov.au`, `austlii.edu.au`, `tascat.tas.gov.au` — confirm the
environment network policy allows these, then run the ingest scripts. Deploys do NOT
update GCS data.

## Inherited gotchas (from the Will scaffold — honour these)

1. **Modal trap** — never add `transform` to `.fade-in`; page fade is opacity-only.
2. **Modal animation** — animate `.modal-overlay.active .modal-box`, not `.modal-box`.
3. **Jinja loop counters** — use `namespace()` for accumulators inside `{% for %}`.
4. **Bootstrap** — `app.css` loads after Bootstrap; don't change load order; no `data-bs-toggle`.
5. **Gemini** — `gemini-2.5-flash` only, via `_gemini_model()`, JSON via `re.search`.
6. **Data** — `load_json`/`save_json` only; never open JSON files directly.

## Local dev

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
ADMIN_USERNAME=admin ADMIN_PASSWORD=changeme ./venv/bin/python init_admin.py
./venv/bin/python main.py        # http://localhost:8080
```

| Var | Purpose | Default |
|-----|---------|---------|
| `SECRET_KEY` | Flask session secret | dev default |
| `GEMINI_API_KEY` | AI-assisted review (optional — heuristic engine used without it) | unset |
| `GCS_BUCKET` | Use Cloud Storage instead of `data/` | unset (local) |

## Git workflow

Branch: `claude/modest-wozniak-Cc3mm`. Commit + push there; open a draft PR. Never push to main.
