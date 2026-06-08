# Why Surity360 Is a Product (Not a Pile of One-Offs)

> **Audience:** Dermot + the Allbridge team. This is the strategy/operations memo that
> explains how we turn the AI assistants we're building — **TasPlan Review (Holly)**,
> **xplan aifpx**, and the ones after them — into a single, sellable, multi-tenant
> product called **Surity360**, and how we develop, test, and ship it without breaking
> a live client.
>
> It is written against the stack we actually run today (Flask on Cloud Run, GCS, GitHub
> auto-deploy) and is honest about the gap between that and where we want to go
> (per-client MongoDB, on-prem Mac minis). Where something doesn't exist yet, it says so.

---

## 1. The thesis in one paragraph

We are not building one town-planning app. We are building a **platform of vertical AI
"workers"** — each one a domain expert (a planner, a paraplanner, a compliance reviewer)
that is grounded in that domain's real source documents and refuses to invent answers.
TasPlan Review is the first worker. `xplan aifpx` is the second. The *thing we sell* is
not any single worker — it's **Surity360: the trusted, auditable layer that runs these
workers on a client's own data, with the client's data kept under the client's control.**
That last clause is the product. Everyone can wire an LLM to a chatbot; very few
professional-services firms will let that chatbot touch their client files. Surity360's
answer — *"your data stays in your MongoDB, on your hardware if you want it, and every
answer is cited and stamped with a caveat"* — is the moat.

The same three properties that make TasPlan Review defensible are the product spec for
every worker:

1. **Grounded + cited** — answers only from ingested source material; never invents
   clause numbers, holdings, or figures. (Already enforced in `main.py`.)
2. **Auditable** — every AI run is recorded (`history.json` today; per-tenant audit
   collection tomorrow) with the full prompt, output, and sources.
3. **Load-bearing caveat** — every output is stamped *"analytical aid, not a statutory
   determination / not advice."* This is what makes a regulated professional comfortable
   putting it in front of a client.

If a feature doesn't preserve those three, it's not a Surity360 feature.

---

## 2. What "multi-tenant" means for us — pick the model deliberately

There are three standard tenancy models. They are not religions; we will use **different
ones for different clients** depending on what they'll pay for and what their compliance
people demand.

| Model | What's shared | Data isolation | Cost / client | When we use it |
|-------|---------------|----------------|---------------|----------------|
| **Pooled** | One app, one database, rows tagged `tenant_id` | Logical only (a query bug = a data leak) | Lowest | **Never** for client files. Maybe for our own internal demos. |
| **Bridged** *(recommended default)* | One app (Cloud Run), **one MongoDB database per client** | Strong — a whole database boundary per tenant | Low–medium | The default Surity360 SaaS tier. |
| **Siloed** | Nothing — a dedicated app + DB (and maybe a Mac mini) per client | Total — separate everything | High | Enterprise / data-sovereignty clients who pay for it. |

**The "bridged" model is the answer to your per-client-MongoDB question.** We run *one*
shared application (the same Cloud Run service, the same code), and at request time the
app looks at *which client this is* and connects to *that client's own MongoDB*. The app
is shared; the data is not. This gives us:

- A real, hard data boundary per client (a database, not a column).
- One codebase to maintain, test, and ship (we are a small team — this matters more than anything).
- A clean upgrade path to **siloed** for the one or two clients who will pay for their
  own Mac mini and their own everything.

```
                          ┌─────────────────────────────┐
   client-a.surity360 ──► │                             │ ──► MongoDB: client_a  (Atlas or Mac mini A)
   client-b.surity360 ──► │   Surity360 app (Cloud Run) │ ──► MongoDB: client_b  (Atlas or Mac mini B)
   client-c.surity360 ──► │   one image, one deploy     │ ──► MongoDB: client_c  (Atlas)
                          └─────────────────────────────┘
                                       │
                                  Tenant registry
                          (which client → which DB URI)
```

### How the app knows which tenant it is

Two clean options, both standard:

- **Host-based** (recommended): each client gets a subdomain — `client-a.surity360.com.au`
  — and the app maps the incoming `Host` header to a tenant. This is the same Cloud Run
  domain-mapping mechanism we already use for `aus-planner.allbridge.com.au`, just one
  mapping per client.
- **Login-based**: the user's account record carries their `tenant_id`; on login we
  resolve the tenant and pin the session to it.

Either way, the request lands holding a **tenant id**, and a small **tenant registry**
turns that id into a Mongo connection string (pulled from Secret Manager — never in
code). Everything downstream — auth, history, ingested corpus — reads/writes through
*that* tenant's database only.

### The one engineering change this requires

Today every read/write goes through `load_json()` / `save_json()` against GCS (see
`docs/ARCHITECTURE.md` — "the one rule"). That single choke-point is *exactly* why this
migration is tractable: we don't have data access scattered across the code. We swap the
implementation behind those two functions for a tenant-aware data layer:

```
load_json("users.json")  ──►  db(tenant).users.find(...)        # Mongo, per-tenant
save_json("users.json")  ──►  db(tenant).users.replace(...)
```

The route code barely changes. See `docs/WORKERS.md` → "The data contract" for the rule
every worker must follow so this stays true.

---

## 3. Per-client MongoDB — how it actually works in GCP + GitHub

You asked specifically: *how do I make this work in GitHub and gcloud where each client
has its own MongoDB?* Here is the concrete wiring.

### 3.1 Where each client's MongoDB lives

Two supported homes, chosen per client:

| Option | Where | Use it for | Notes |
|--------|-------|-----------|-------|
| **MongoDB Atlas** | Managed cloud (pin region to `australia-southeast1` / Sydney) | The default SaaS tier (bridged model) | One Atlas *project* for Surity360; one **database per client**, or a dedicated cluster for bigger clients. Atlas does backups, failover, patching for us. |
| **Self-hosted on a Mac mini** | Client premises or our MSP | Data-sovereignty / on-prem-integration clients (siloed model) | We own the ops. Covered in §5. |

**Start with Atlas.** It is the lowest-effort way to deliver true per-client database
isolation today, it keeps data in Sydney for residency, and it lets us prove the
multi-tenant model before we own a single piece of hardware. Mac minis come *after* a
client demands them (§5).

### 3.2 How the secret (the Mongo URI) flows

Each client's connection string is a secret. It never goes in the repo, in `app.yaml`,
or in a config file. It lives in **GCP Secret Manager**, one secret per tenant:

```bash
# one-time, per client onboarding
echo -n "mongodb+srv://app:PW@client-a.xxxx.mongodb.net/client_a" \
  | gcloud secrets create MONGO_URI_CLIENT_A --data-file=- --project=surity360-prod

# grant the Cloud Run runtime service account read access to just that secret
gcloud secrets add-iam-policy-binding MONGO_URI_CLIENT_A \
  --member="serviceAccount:run-prod@surity360-prod.iam.gserviceaccount.com" \
  --role=roles/secretmanager.secretAccessor --project=surity360-prod
```

The tenant registry (a small `tenants` collection in a control-plane DB, or a JSON map
during early days) holds, per tenant: `tenant_id`, `host`, `secret_name`, `worker(s)
enabled`, `deployment_tier`. At request time the app resolves the host → tenant →
secret name → URI, and caches the Mongo client per tenant for the life of the instance.

### 3.3 GitHub → gcloud deploy, made keyless

Today we deploy by pushing to `main` (Cloud Build trigger → build → Cloud Run). For a
product we harden it:

- **Keyless auth** from GitHub Actions to GCP via **Workload Identity Federation** — no
  long-lived service-account JSON key sitting in GitHub secrets. GitHub Actions exchanges
  its OIDC token for short-lived GCP credentials.
- **Two GCP projects**: `surity360-dev` and `surity360-prod`. Hard wall between them —
  separate billing visibility, separate Secret Manager, separate Cloud Run, separate
  Atlas project. A mistake in dev cannot touch a client.
- **Per-environment runtime service accounts** (`run-dev@…`, `run-prod@…`) that can only
  read the secrets for their own environment's tenants.

See §4 for the promotion flow that connects these.

---

## 4. Dev → prod: how we change, test, and ship without breaking a client

This is the part that turns "a thing that works on my machine" into "a product a
client trusts." The rule: **clients only ever see `prod`, and `prod` only ever gets
code that already ran green in `dev` against realistic-but-fake data.**

### 4.1 Environments

| Environment | GCP project | Data | Who sees it | Deployed from |
|-------------|-------------|------|-------------|---------------|
| **Local** | none | local Mongo / sample JSON | developer | working tree |
| **Dev / staging** | `surity360-dev` | a `demo` tenant with **synthetic** data only | us | every push to a feature branch / `main` |
| **Prod** | `surity360-prod` | real client tenants | clients | a **tagged release** promoted by hand |

> **Hard rule:** no real client data in dev, ever. Dev gets a `demo` tenant seeded with
> synthetic records. This keeps the blast radius of a bad deploy at zero client impact
> and keeps us clean on privacy.

### 4.2 The flow

```
feature branch ──PR──► main ──auto-deploy──► DEV (surity360-dev, demo tenant)
                                                  │  run smoke tests + manual check
                                                  ▼
                                          git tag v1.4.0
                                                  │  manual approval (GitHub Environments gate)
                                                  ▼
                                          promote SAME image ──► PROD (surity360-prod, all client tenants)
```

Key practices:

1. **Branch + PR for everything.** No direct commits to `main`. (This already matches
   the repo's branch workflow.)
2. **Build once, promote the artifact.** Dev and prod run the *same* container image,
   identified by digest. We don't rebuild for prod — we re-tag and deploy the exact
   bytes that passed in dev. This kills "worked in dev, broke in prod."
3. **Manual gate to prod.** Use a **GitHub Environments** protection rule (required
   reviewer) on the `prod` deploy job. A human clicks approve. Early on, that human is
   you.
4. **Gradual rollout.** Cloud Run supports revision traffic splitting — send 10% of a
   tenant's traffic to the new revision, watch logs, then 100%. For a nervous client,
   roll them last.
5. **Instant rollback.** `gcloud run services update-traffic deploy --to-revisions
   PREVIOUS=100` flips traffic back to the last good revision in seconds. No rebuild.

### 4.3 Database migrations — the bit people forget

With per-client MongoDB, a schema change has to run **once per tenant**, and prod and dev
will drift if you're not disciplined.

- Keep **versioned migration scripts** in the repo (`migrations/NNNN_description.py`),
  each idempotent and reversible where possible.
- A migration runner iterates the tenant registry and applies pending migrations to each
  tenant DB, recording the applied version in a `_migrations` collection **inside each
  tenant DB**.
- Run migrations against **dev first**, as a deploy step, *before* the new code serves
  traffic. Only promote to prod once dev is green.
- Mongo is schema-flexible, which is a trap: prefer additive changes (new fields,
  backfilled lazily) over destructive ones, so an old revision can still read the data if
  you need to roll back.

### 4.4 Per-client config vs. code

A client's *configuration* (which workers are enabled, branding, their Mongo secret,
their feature flags) lives in the **tenant registry**, not in code. So onboarding a
client or toggling a feature is a data change — not a deploy. Code ships on the release
train; tenant config changes immediately and independently. This separation is what lets
one shared app serve very different-looking clients.

---

## 5. The Mac mini question — straight answers

You asked three things: (a) one Mac mini per client, or is that needed early? (b) how does
it work? (c) can a second Mac mini at a different premises, maintained by an IT MSP, stand
in if the primary fails?

### 5.1 What is a Mac mini even for here?

A Mac mini is **on-premises / client-controlled compute**. It earns its place only when a
client's data *cannot* live in the cloud, or must integrate with something that lives on
their LAN. Concretely, for our workers that means:

- **Data residency / sovereignty** — a client (or their compliance regime) insists their
  client files never leave hardware they can point at. The Mac mini hosts **their
  MongoDB** and, optionally, a local copy of the worker.
- **On-prem system integration** — e.g. `xplan aifpx` may need to reach an XPLAN
  install, a local file share, or a practice-management system that isn't exposed to the
  internet. A small agent on the Mac mini bridges that gap securely (outbound-only
  tunnel; nothing inbound from the internet).

If a client needs *neither* of those, **they do not need a Mac mini** — they belong on
the Atlas-backed bridged tier and we never ship hardware.

### 5.2 Is one-per-client required early on? No.

**Recommendation: do not buy a Mac mini per client early.** Reasons:

- Atlas (per-client database, Sydney region) already gives true data isolation and
  residency-in-Australia for the vast majority of clients, with zero hardware ops on us.
- A Mac mini per client is real, recurring operational burden: patching, backups,
  physical failure, the client's flaky office internet, someone unplugging it to vacuum.
- Hardware-per-client only pays off when a *specific* client both (a) demands on-prem
  data, and (b) will pay a premium that covers the ops. Sell that as the **enterprise /
  siloed tier**, priced accordingly.

So: **start cloud-only (Atlas). Introduce the Mac mini as a paid "on-prem" tier** the
moment a real client requires it — not before. The bridged architecture (§2) means
moving a client from Atlas to a Mac mini is "point their tenant registry entry at a
different Mongo URI," not a re-architecture.

### 5.3 How a Mac mini deployment actually works

```
            Client premises (or our MSP rack)
        ┌──────────────────────────────────────┐
        │  Mac mini (primary)                  │
        │   • MongoDB (this client's data)     │◄── replicates ──┐
        │   • Surity360 worker (optional local)│                 │
        │   • outbound-only secure tunnel      │                 │
        └───────────────┬──────────────────────┘                 │
                        │ (Tailscale / WireGuard, outbound)      │
                        ▼                                         │
                 Cloud Run control plane                         │
                 (auth, updates, tenant registry)                │
                                                                  │
        ┌──────────────────────────────────────┐                 │
        │  Mac mini (standby) @ MSP premises   │─────────────────┘
        │   • MongoDB replica (hot copy)        │
        │   • can be promoted if primary dies   │
        └──────────────────────────────────────┘
```

Mechanics:

- **MongoDB on the mini.** The client's database runs on the primary Mac mini. Backups go
  off-box nightly (encrypted) to the standby and/or to GCS.
- **No inbound ports.** The mini opens an **outbound-only** secure tunnel
  (Tailscale/WireGuard) to our control plane. Nothing on the internet can reach into the
  client's office. This is critical for the security story we're selling.
- **Updates.** The mini runs a small managed agent that pulls signed worker updates from
  us on the same release train as the cloud tier — so an on-prem client isn't stuck on
  old code. We promote to on-prem tenants *after* cloud tenants, same gated flow as §4.
- **The tunnel keeps it controllable** even though it's hardware we don't physically
  touch day-to-day.

### 5.4 The second Mac mini for failover — yes, and here's the right shape

Your instinct is correct and it's the standard answer: **a second Mac mini at a different
premises, maintained by the IT MSP, as a standby — absolutely viable.** The clean way to
do it with MongoDB:

- Run the two minis as a **MongoDB replica set** (primary on mini A, secondary on mini B
  at the MSP). The secondary is a continuously-updated hot copy.
- For automatic failover, MongoDB needs a *third* voting member to break ties (so a
  network split doesn't promote both or neither). That third vote can be a tiny
  **arbiter** running cheaply in the cloud (it holds no data) — best of both worlds:
  data stays on the two minis, the tie-breaker is a cloud micro-instance.
- If mini A dies, mini B is promoted automatically (or the MSP promotes it), and the
  worker reconnects. **RPO** (data loss window) is near-zero because replication is
  continuous; **RTO** (downtime) is minutes, not a restore-from-backup day.
- **The MSP's job** is the physical/ops layer: power, network, OS patching, swapping a
  dead mini, and being the hands-on-site. We keep the software/release responsibility via
  the managed agent. Put this split in writing in the MSP contract.

Caveats to be honest about: two Macs + an MSP retainer is real cost and is **only worth
it for a client whose tier pays for it**. For everyone else, Atlas's built-in multi-region
replication and automated failover does the same job for less and with no hardware — which
is exactly why the default tier is Atlas, not minis.

### 5.5 Decision rule (put this on a slide)

> **Cloud (Atlas) by default. Mac mini only when a client requires on-prem data or
> on-prem integration and pays the enterprise tier. Second Mac mini (MSP standby) only
> when that client also requires high availability.** Each step up is a priced product
> tier, not an engineering whim.

---

## 6. Putting it together — the product tiers

The architecture above falls naturally into the commercial packaging. This is the
"why it's a product" answer for a buyer:

| Tier | Deployment | Data home | HA / DR | Sold to |
|------|------------|-----------|---------|---------|
| **Surity360 Cloud** | Shared Cloud Run app (bridged) | Per-client MongoDB **Atlas**, Sydney | Atlas multi-AZ, automated | SMB professional firms — the volume tier |
| **Surity360 On-Prem** | Worker + MongoDB on a client **Mac mini**, managed via tunnel | Client premises | Nightly off-box backup | Firms with data-sovereignty requirements |
| **Surity360 On-Prem HA** | Primary + **standby Mac mini at MSP**, replica set | Two premises + cloud arbiter | Auto-failover, near-zero RPO | Enterprise / mission-critical |

All three run the **same workers** and the **same release train**. That is the product:
one codebase, one quality bar, three trust/price points, every answer cited and stamped.

---

## 7. What to actually do next (sequenced)

1. **Don't buy hardware yet.** Validate the model on cloud.
2. **Introduce the tenant abstraction** behind `load_json`/`save_json` — add a tenant id
   to the request context and a tenant registry. (Small, because of the single
   data choke-point — see `docs/WORKERS.md`.)
3. **Stand up `surity360-dev` and `surity360-prod` GCP projects**, keyless GitHub→GCP
   deploy (Workload Identity Federation), and the dev→prod gated promotion flow (§4).
4. **Move one tenant (a `demo`) to MongoDB Atlas** end-to-end. Prove bridged tenancy with
   real isolation.
5. **Onboard the first real client on Cloud tier.** Learn.
6. **Only then**, when a client asks for it, pilot **one** Mac mini (§5) — and design the
   standby/MSP failover into that same pilot so HA isn't a bolt-on later.
7. **Apply the same pattern to `xplan aifpx`** as a second worker on the same platform —
   see `docs/WORKERS.md`. The day a second worker ships on this platform with zero new
   infrastructure is the day Surity360 is provably a product, not a project.

---

*This memo describes a target architecture. The app today stores data as JSON in GCS and
auto-deploys on push to `main` (see `docs/SETUP.md`, `docs/ARCHITECTURE.md`). The
MongoDB/multi-tenant/Mac-mini elements above are the plan, not the current state.*
