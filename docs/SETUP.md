# TasPlan Review — Setup & Operations Guide

## Overview

TasPlan Review is a Tasmanian planning assessment assistant deployed on Google Cloud Run.
It reviews development proposals against the Tasmanian Planning Scheme (SPP + LPS) and
TASCAT precedent, returning a grounded, cited assessment.

**Production URL:** https://aus-planner.allbridge.com.au
**GCP Project:** `aus-town-planner`
**Cloud Run service:** `deploy` (us-central1)
**GCS bucket:** `gs://aus-town-planner-data`

---

## Architecture

| Component | Detail |
|-----------|--------|
| Backend | Python 3.11 / Flask 3.x — single `main.py` |
| Data | JSON files at the **root** of `gs://aus-town-planner-data` (not in a subfolder) |
| Auth | Session-based, Werkzeug scrypt hashing |
| AI | Gemini (`gemini-2.5-flash`) via Secret Manager — degrades gracefully without it |
| Serving | Cloud Run → Gunicorn (1 worker, 8 threads) |
| Domain | `aus-planner.allbridge.com.au` — Cloud Run domain mapping, Cloudflare DNS-only |

---

## Local Development

```bash
cd aus-town-planner
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# Create admin user (first time only)
ADMIN_USERNAME=admin ADMIN_PASSWORD=yourpassword ADMIN_NAME="Your Name" ./venv/bin/python init_admin.py

./venv/bin/python main.py   # http://localhost:8080
```

Environment variables (all optional locally):

| Var | Purpose | Default |
|-----|---------|---------|
| `SECRET_KEY` | Flask session secret | dev default (change in prod) |
| `GEMINI_API_KEY` | Gemini AI reviews | unset — heuristic engine used |
| `GCS_BUCKET` | GCS bucket name | unset — reads from local `data/` |

---

## Production Deployment

Deploys trigger automatically on push to `main` via a Cloud Build trigger that builds
a Docker image, pushes to Artifact Registry, and deploys to Cloud Run.

```
Push to main
  → Cloud Build trigger (auto-generated)
  → docker build → push to us-central1-docker.pkg.dev/aus-town-planner/cloud-run-source-deploy/deploy
  → gcloud run deploy → us-central1
```

The repo's `cloudbuild.yaml` runs a Python syntax check only — it does not deploy.

### Manual deploy (if needed)

```bash
gcloud run deploy deploy \
  --source . \
  --region us-central1 \
  --project aus-town-planner \
  --allow-unauthenticated
```

---

## GCS Data

All JSON data lives at the **root** of `gs://aus-town-planner-data` (no subfolder).
`load_json("users.json")` maps directly to `gs://aus-town-planner-data/users.json`.

### Upload / re-seed data

```bash
gcloud storage cp data/users.json gs://aus-town-planner-data/users.json --project=aus-town-planner
gcloud storage cp data/app_config.json gs://aus-town-planner-data/app_config.json --project=aus-town-planner
gcloud storage cp data/decisions.json gs://aus-town-planner-data/decisions.json --project=aus-town-planner
gcloud storage cp data/scheme_chunks.json gs://aus-town-planner-data/scheme_chunks.json --project=aus-town-planner
gcloud storage cp data/scheme_manifest.json gs://aus-town-planner-data/scheme_manifest.json --project=aus-town-planner
```

Verify:

```bash
gcloud storage ls gs://aus-town-planner-data/ --project=aus-town-planner
```

### Reset admin password

```bash
ADMIN_USERNAME=admin ADMIN_PASSWORD=newpassword ADMIN_NAME="Your Name" ./venv/bin/python init_admin.py
gcloud storage cp data/users.json gs://aus-town-planner-data/users.json --project=aus-town-planner
```

---

## GCS IAM

The Cloud Run service account needs Storage Object Admin on the bucket.
Project number is `969779006317`.

```bash
gcloud storage buckets add-iam-policy-binding gs://aus-town-planner-data \
  --member="serviceAccount:969779006317-compute@developer.gserviceaccount.com" \
  --role=roles/storage.objectAdmin \
  --project=aus-town-planner
```

---

## Secret Manager

`GEMINI_API_KEY` is stored in Secret Manager and mounted as an environment variable
in the Cloud Run service. To update it:

```bash
echo -n "NEW_KEY" | gcloud secrets versions add GEMINI_API_KEY \
  --data-file=- \
  --project=aus-town-planner
```

The Cloud Run service picks up new secret versions on next cold start (or redeploy).

---

## Custom Domain

| Setting | Value |
|---------|-------|
| Domain | `aus-planner.allbridge.com.au` |
| Cloud Run mapping | us-central1, service `deploy` |
| DNS (Cloudflare) | CNAME `aus-planner` → `ghs.googlehosted.com`, **DNS only** (grey cloud) |

Cloudflare must be set to **DNS only** (grey cloud). Orange-cloud proxy causes 404s
because Cloud Run rejects requests with non-matching Host headers.

Check domain mapping status:

```bash
gcloud beta run domain-mappings describe \
  --domain=aus-planner.allbridge.com.au \
  --project=aus-town-planner \
  --region=us-central1 \
  --format='value(status.conditions)'
```

---

## Corpus Ingestion

The deployed corpus is **illustrative SAMPLE data**. To ingest real data, run locally
with outbound access to `tpso.planning.tas.gov.au`, `austlii.edu.au`, `tascat.tas.gov.au`.

```bash
# Planning scheme clauses (SPP + LPS)
GEMINI_API_KEY=... ./venv/bin/python -m ingest.scheme

# TASCAT / TASRMPAT decisions (small test run)
GEMINI_API_KEY=... ./venv/bin/python -m ingest.decisions --year-from 2024 --limit 20

# Full historical run
GEMINI_API_KEY=... ./venv/bin/python -m ingest.decisions --db both --year-from 2010
```

After ingestion, upload the updated JSON to GCS:

```bash
gcloud storage cp data/decisions.json gs://aus-town-planner-data/decisions.json --project=aus-town-planner
gcloud storage cp data/scheme_chunks.json gs://aus-town-planner-data/scheme_chunks.json --project=aus-town-planner
gcloud storage cp data/scheme_manifest.json gs://aus-town-planner-data/scheme_manifest.json --project=aus-town-planner
```

---

## Key Gotchas

1. **GCS path** — files must be at bucket root, not in a `data/` subfolder. `load_json("users.json")` → `gs://aus-town-planner-data/users.json`.
2. **data/users.json is gitignored** — never committed. Reset via `init_admin.py` + upload to GCS.
3. **Cloudflare must be DNS-only** — orange-cloud proxy breaks the Cloud Run domain mapping.
4. **Domain mappings** — only supported in specific Cloud Run regions. us-central1 is confirmed working.
5. **Deploys don't touch GCS** — data changes require a manual `gcloud storage cp` after deploy.
6. **`cloudbuild.yaml`** — runs syntax check only; actual deployment is the auto-generated Cloud Run trigger.
