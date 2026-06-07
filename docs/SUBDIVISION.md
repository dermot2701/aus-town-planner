# Minimum Compliant Subdivision (Ask Holly)

A grounded feasibility capability inside **Ask Holly**: given a site (dimensions
from an attached plan or planner-supplied facts) and the applicable scheme
standards, Holly derives the **smallest compliant subdivision** — minimum lot
size(s), frontage, the buildable envelope after setbacks, and indicative lot
yield — citing every standard to a clause and showing its arithmetic.

> **Analytical aid only; not a statutory determination or legal advice.** Every
> figure is an **indicative** feasibility estimate, subject to confirmation by a
> licensed surveyor and the planning authority. It is **not a sealed plan.**

## How to use it

Ask a subdivision-feasibility question on `/ask` — e.g.
*"What is the minimum compliant subdivision of this site in the General
Residential zone, Hobart?"* — and (optionally) attach the site plan so Holly can
read the boundaries and scale. Holly answers from the ingested scheme clauses and
TASCAT decisions only; with no Gemini key it is offline.

## What Holly produces

A **Minimum compliant subdivision** section, in this order:

1. **Site basis** — the site dimensions/area it is working from, attributed
   `(image-derived)` or `(planner-supplied)` and flagged `ESTIMATE` where scaled
   off a plan rather than read from a labelled dimension. See the *Image
   attachments* and site-dimension behaviour in
   [REVIEW_ENGINE.md](REVIEW_ENGINE.md#ask-holly--ask).
2. **Applicable standards** — every relevant standard found in the retrieved
   context, each cited to its clause ID:
   - minimum lot size,
   - minimum frontage,
   - front / side / rear setbacks,
   - private open space,
   - site coverage / building envelope,
   - access, including any **battle-axe access-strip width**.
3. **Derivation** — using **only** those cited standards, the minimum complying
   lot size(s), required frontage, the buildable envelope remaining after
   setbacks, and the indicative maximum number of lots — **with the arithmetic
   shown** step by step.
4. **Binding constraint** — the standard that limits the yield, plus any
   hazard/overlay in context that further constrains it (e.g. a landslip or
   bushfire-prone overlay clause).

## Grounding guardrails (non-negotiable)

This feature lives or dies by the app's grounding rules — the danger is a
confidently fabricated minimum (e.g. *"minimum lot size 450 m²"*) that isn't in
the corpus.

- **Standards come only from the retrieved context** (`scheme_chunks.json` —
  SPP + the municipality's LPS) and are cited to clause IDs. Holly **never
  invents** a lot size, frontage, or setback.
- **Missing standard → say so, don't guess.** If a standard Holly needs is not in
  context, it names the missing standard/clause and what value is required,
  instead of filling the gap. That trips `_is_insufficient()` and the
  **clarify → refine loop** offers the planner a field to paste the clause text;
  on resubmit Holly recomputes with the supplied standard (attributed
  `(planner-supplied)`). See the refine loop in
  [REVIEW_ENGINE.md](REVIEW_ENGINE.md#ask-holly--ask).
- **Geometry is image-derived, not surveyed.** Dimensions scaled from a plan are
  `ESTIMATE`s; the output is explicitly indicative and not a sealed plan.
- **The caveat** is stamped on every answer.

## Where it lives in the code

There is **no new route or solver** — it is a capability of the existing
`/ask` surface, driven by an instruction block appended to `_HOLLY_SYSTEM` in
`main.py`. Gemini does the grounded reasoning over the retrieved clauses and the
site inputs; the same `retrieve()` (with zone-aware scoring and `k_scheme=12`)
surfaces the subdivision/zone standards, and the same record/refine/PDF
machinery as the rest of Ask Holly applies. See
[REVIEW_ENGINE.md](REVIEW_ENGINE.md) for retrieval and the Ask Holly flow, and
[ARCHITECTURE.md](ARCHITECTURE.md) for the AI surfaces and data rule.

## Limitations

- It is a **feasibility indication**, not a design or a survey. Real lot
  geometry, easements, services, contours, and tree/vegetation constraints can
  change the outcome.
- It can only apply standards that have been **ingested** for the relevant
  municipality. If the corpus lacks the subdivision standards for that zone,
  Holly will ask for them rather than estimate.
- It does not draw a plan; it describes the minimum compliant configuration in
  words and numbers.
