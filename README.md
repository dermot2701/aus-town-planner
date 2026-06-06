# TasPlan Review

A Tasmanian town-planning assessment assistant. Review a development proposal
against the **Tasmanian Planning Scheme** (SPP + LPS) and **TASCAT** precedent, and
get a grounded, cited assessment.

> **Analytical aid for qualified planners — not a statutory determination or legal
> advice.** Every review is grounded only in ingested scheme clauses and tribunal
> decisions; no clause numbers, standards, or holdings are invented.

## Status

This repository was scaffolded from the "Will" Flask design system. The committed
corpus is **illustrative SAMPLE data** — clearly labelled in the UI — pending live
ingestion. Build progress:

- [x] **Step 1 — Scaffold + rebrand.** Bootable Flask app, auth, Palantir-dark design
  system (teal accent), `/scheme`, `/decisions`, `/review`, `/admin`.
- [x] **Step 5 — Retrieval + engine.** `retrieve()` (municipality-scoped) and
  `review_proposal()` returning the mandated JSON; deterministic heuristic fallback
  when no Gemini key is present.
- [x] **Step 6 — UI.** Scheme browser, decision keyword search, review form + result,
  admin ingestion panel.
- [ ] **Steps 3–4 — Live ingestion.** `ingest/scheme.py` and `ingest/decisions.py`
  are written but **network-gated**: outbound access to TPSO/AustLII/TASCAT is
  required and was blocked in the build environment. Run them once access is enabled
  to replace the SAMPLE corpus.

## Quick start

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
ADMIN_USERNAME=admin ADMIN_PASSWORD=changeme ./venv/bin/python init_admin.py
./venv/bin/python main.py        # http://localhost:8080  (login: admin / changeme)
```

## Ingestion (when network access is available)

```bash
python -m ingest.scheme       # SPP + LPS  → data/scheme_chunks.json
python -m ingest.decisions    # TASCAT     → data/decisions.json
```

Both throttle and cache requests (`ingest/cache/`, gitignored) and respect source
usage terms. See `CLAUDE.md` for architecture and the grounding rules.
