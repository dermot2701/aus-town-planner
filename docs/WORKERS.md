# Surity360 Workers — Standard & Registry

> **What this is.** Surity360 is a platform that runs vertical AI **"workers"** — each a
> grounded, cited, auditable domain assistant — on a client's own data. This document is
> (a) the **registry** of workers we run, and (b) the **standard** every worker must meet
> so they can all share one multi-tenant platform, one release train, and one trust story.
>
> **This file is portable.** It is the canonical spec. The same `docs/WORKERS.md` should
> be mirrored into every worker repo (e.g. `xplan aifpx/docs/WORKERS.md`) so each team is
> building to the same contract. When the standard changes, change it here first.
>
> See [`../strategy-and-marketing/why-surity360-is-a-product.md`](../strategy-and-marketing/why-surity360-is-a-product.md)
> for the commercial/architecture rationale (tenancy models, dev→prod, Mac minis).

---

## 1. What is a "worker"?

A worker is a single-purpose AI assistant for one professional domain. It:

- **Answers only from ingested source material** for its domain, and **cites every
  finding**. It never invents identifiers, figures, standards, or holdings.
- **Degrades gracefully** — if no LLM key is present (or the call fails), it falls back to
  a deterministic path and clearly marks anything it can't verify.
- **Records every run** to an audit trail (prompt + output + sources).
- **Stamps a load-bearing caveat** on every output (e.g. *"analytical aid, not a statutory
  determination / not advice"*). This is non-negotiable — it's what lets a regulated
  professional rely on it.

If it doesn't do those four things, it isn't a Surity360 worker — it's just a chatbot.

---

## 2. Worker registry

| Worker | Repo | Domain | Source corpus | LLM(s) | Status |
|--------|------|--------|---------------|--------|--------|
| **TasPlan Review** ("Holly") | `dermot2701/aus-town-planner` | Tasmanian town planning | Tasmanian Planning Scheme (SPP + LPS) + TASCAT decisions | Gemini 2.5 Flash + Groq (Llama 3.3 70B) + MiniMax M2.1 | **Live** (sample corpus; real ingestion network-gated) |
| **xplan aifpx** | `xplan aifpx` *(sibling repo)* | Financial advice / paraplanning (XPLAN integration) | _TBD by that worker's team_ | _TBD_ | Planned — adopt this standard |

> Add a row when a new worker starts. Keep `Status` honest (Planned / In dev / Live).
> The point of the registry is that anyone can see, at a glance, what runs on the platform
> and whether it meets the standard.

---

## 3. The contracts every worker must honour

These are the interfaces that let many workers share one multi-tenant platform. A worker
is free to do whatever it likes *inside* these boundaries.

### 3.1 The data contract — one choke-point

**All persistence goes through a single data layer — never open a store directly.**

In `aus-town-planner` today that choke-point is `load_json(name)` / `save_json(name, data)`
(plus `load_bytes`/`save_bytes` for binary blobs), which switch between local files and
GCS. This single rule is what makes the move to per-tenant MongoDB a swap behind two
functions instead of a rewrite (see the strategy memo, §2).

The rule, stated generally:

```
read(name)         # NOT open("data/...") or a raw DB handle in route code
write(name, data)
```

Every worker must keep data access funnelled through its data layer so the platform can
make it **tenant-aware** in one place. When the platform moves to MongoDB, the
implementation becomes "resolve current tenant → that tenant's database → collection," and
route code is untouched.

### 3.2 The tenancy contract

A worker must not assume it is single-tenant. It must:

- Read the **current tenant** from request context (resolved by the platform from the
  host or the logged-in user — see strategy memo §2), never hard-code a database, bucket,
  or path.
- Scope **every** read and write to that tenant. No cross-tenant queries, ever.
- Treat per-client connection details (e.g. the MongoDB URI) as **secrets resolved at
  runtime** from Secret Manager via the tenant registry — never in code, `app.yaml`, or
  committed config.

### 3.3 The grounding contract

- Answer only from ingested, retrievable source material for the tenant's corpus.
- Cite every finding to a source identifier (clause id, citation, document ref).
- Never invent identifiers, standards, figures, or holdings. If context is insufficient,
  say so and list what's missing.
- Provide a deterministic fallback when no LLM key is available; mark unverifiable items
  explicitly rather than guessing.

### 3.4 The audit + caveat contract

- Append **every** AI run to the tenant's audit trail with the full (untruncated) prompt,
  the output, and the supplied sources. (`history.json` in TasPlan Review today; a
  per-tenant `runs` collection under MongoDB.)
- Stamp the load-bearing caveat on every output, in the UI and in any export. Do not
  remove it. Do not make it dismissable.

### 3.5 The LLM contract

- Use the platform's LLM factory pattern (in TasPlan Review: `_gemini_model(system=…)`,
  the single Gemini entry point; the council adds Groq + MiniMax via one outbound-POST
  helper). Don't scatter raw API calls through the code.
- Parse model JSON defensively (TasPlan Review uses `re.search(r'\{.*\}', text, re.DOTALL)`).
- Keys come from Secret Manager / env, never the repo. Absence of a key must degrade, not
  crash.
- Default to the most capable current models for the job; keep model ids in one place so
  they're easy to bump.

---

## 4. The build/ship contract (so all workers share one release train)

Every worker follows the same dev→prod discipline (full detail in strategy memo §4):

1. **Branch + PR for everything.** No direct commits to `main`.
2. **Two environments, two GCP projects:** `*-dev` (synthetic `demo` tenant only — *no
   real client data, ever*) and `*-prod` (real tenants).
3. **Build once, promote the artifact.** The same container image/digest that passed dev
   is the one deployed to prod — no rebuild for prod.
4. **Gated promotion to prod.** A human approves (GitHub Environments protection rule).
5. **Keyless deploy.** GitHub Actions → GCP via Workload Identity Federation (no
   long-lived SA keys in GitHub).
6. **Migrations run per-tenant, dev first.** Versioned, idempotent, recorded inside each
   tenant DB; additive-preferred so rollback stays safe.
7. **Rollback is traffic, not rebuild.** Cloud Run revision traffic-splitting for gradual
   rollout and instant rollback.

On-prem (Mac mini) tenants ride the **same** release train via a managed agent, and are
promoted **after** cloud tenants (strategy memo §5).

---

## 5. How to add a new worker

1. **Create the repo** and copy this `docs/WORKERS.md` into it (it's the shared contract).
2. **Implement the four contracts** in §3 — data choke-point, tenancy, grounding,
   audit+caveat. Reuse the platform's data-layer and LLM-factory patterns rather than
   reinventing them.
3. **Define the corpus + ingestion** for the domain (what sources, how retrieved, how
   cited). Keep ingestion network-gated and respectful of source terms.
4. **Wire it to the platform tenancy** — resolve tenant from request context; scope all
   I/O; pull secrets from Secret Manager via the tenant registry.
5. **Adopt the build/ship contract** (§4): dev/prod projects, keyless deploy, gated
   promotion.
6. **Register it** — add a row to the table in §2 (here and in the sibling copies).
7. **Prove it on the `demo` tenant in dev** before any client sees it.

The success test for the platform: a **new worker ships with zero new infrastructure** —
same tenancy, same secrets pattern, same release train. That's the moment Surity360 is
demonstrably a product, not a collection of apps.

---

## 6. Anti-patterns (don't ship these)

- ❌ Opening a file/DB directly in route code, bypassing the data layer.
- ❌ Hard-coding a tenant, bucket, path, or database.
- ❌ Any code path that can read across tenants.
- ❌ An LLM answer with no citation, or that invents an identifier/figure.
- ❌ Removing, weakening, or hiding the caveat.
- ❌ A connection string or API key in the repo / `app.yaml` / committed config.
- ❌ Real client data in a dev/staging environment.
- ❌ Rebuilding a separate image for prod instead of promoting the dev-tested one.

---

*Canonical location: `aus-town-planner/docs/WORKERS.md`. Mirror, don't fork — when the
standard changes, update it here and propagate to sibling worker repos (`xplan aifpx`,
and the next ones).*
