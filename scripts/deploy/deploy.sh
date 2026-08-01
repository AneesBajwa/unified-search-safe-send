#!/usr/bin/env bash
# Phase 0 Part A.4 — build and deploy api + worker to Cloud Run.
# Follows runbooks.md §4. NOT YET EXECUTED.
#
# Run bootstrap-gcp.sh first, and create the `database-url` secret pointing at
# the Neon POOLED endpoint (runbooks.md §5).
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-us-east4}"
REPO="${REPO:-app}"
TAG="${TAG:-v1}"
IMG="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/app:${TAG}"

cd "$(dirname "$0")/../.."

die() { printf '\n\033[31mDEPLOY BLOCKED: %s\033[0m\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Preflight. Runs before the cross-build so a blocked deploy fails in two
# seconds rather than after five minutes of emulated compilation.
#
# ⏸️ THIS IS EXPECTED TO FAIL TODAY, at the billing check below. The only
# billing account on this login is closed (`open: false`), so enabling
# run.googleapis.com returns UREQ_PROJECT_BILLING_NOT_OPEN. The script fails
# loudly there on purpose: a target that quietly skips a blocked step is how
# "not deployed" becomes "deploy passed" in the next status report.
# ---------------------------------------------------------------------------
echo "==> preflight"
command -v gcloud >/dev/null || die "gcloud is not installed (runbooks.md §4)"
docker buildx version >/dev/null 2>&1 || die \
"docker buildx is missing. Colima plus the Homebrew docker CLI does not ship it,
   and the legacy builder CANNOT cross-build — 'docker build --platform
   linux/amd64' fails with 'does not provide the specified platform', which
   reads like a broken base image rather than a missing tool.
   Fix:  brew install docker-buildx  (runbooks.md §4)"

# `tr` rather than bash's ${var,,}: macOS still ships bash 3.2 as /bin/bash and
# the parameter-expansion form is a 4.0 feature, so it fails as a syntax error
# on exactly the machine this is most likely to be run from.
BILLING_ENABLED="$(gcloud billing projects describe "$PROJECT_ID" \
    --format='value(billingEnabled)' 2>/dev/null | tr '[:upper:]' '[:lower:]' || echo false)"
if [ "$BILLING_ENABLED" != "true" ]; then
    die "billing is not open on ${PROJECT_ID}.

   'gcloud services enable run.googleapis.com artifactregistry.googleapis.com'
   returns UREQ_PROJECT_BILLING_NOT_OPEN, so Cloud Run and Artifact Registry
   cannot be turned on at all. Nothing downstream of this can be attempted.

   To unblock — open a FREE TRIAL billing account and link it:
       gcloud billing accounts list
       gcloud billing projects link ${PROJECT_ID} --billing-account=XXXXXX-XXXXXX-XXXXXX

   ⚠️  Stay on Free Trial and never click Upgrade. It auto-closes at \$300 / 90
       days with no auto-conversion, which is the only *mathematical* guarantee
       of \$0 (risks.md R1). Everything else is a guardrail."
fi

echo "==> enable services"
# 🚫 NEVER containerscanning.googleapis.com — \$0.26 per push, billed immediately.
# 🚫 NOT NEEDED: cloudbuild, sqladmin, container.
gcloud services enable \
    run.googleapis.com artifactregistry.googleapis.com \
    secretmanager.googleapis.com cloudscheduler.googleapis.com \
    cloudtasks.googleapis.com

# --platform linux/amd64 is MANDATORY on Apple Silicon. An arm64 image fails
# on Cloud Run with an opaque "container failed to start" and no useful log.
#
# Deliberately not `gcloud run deploy --source`: that pulls in Cloud Build,
# auto-creates a repository, and produces buildpack images 2-3x larger than
# our Dockerfile, which blows the 0.5 GiB Artifact Registry free tier.
echo "==> build + push $IMG"
docker buildx build --platform linux/amd64 -t "$IMG" --push .

echo "==> migrate (one-shot, against Neon)"
# A Cloud Run *Job* would bill a 1-minute minimum per execution. For a
# one-off migration that is acceptable; for anything periodic it is not
# (design D4). Run it locally against the pooled endpoint instead when you
# can — same result, no job resource.
echo "    locally:  DATABASE_URL=<neon-pooled-url> uv run alembic upgrade head"

echo "==> deploy api (public)"
gcloud run deploy api \
    --image="$IMG" --region="$REGION" \
    --allow-unauthenticated \
    --min-instances=0 --max-instances=2 \
    --cpu=1 --memory=512Mi \
    --set-secrets=DATABASE_URL=database-url:latest \
    --set-env-vars=RUN_WORKER_INLINE=0,DEV_ROUTES=0 \
    --command=sh --args='-c,exec uvicorn api.main:app --host 0.0.0.0 --port $PORT'

echo "==> deploy worker (PRIVATE — never allow-unauthenticated)"
gcloud run deploy worker \
    --image="$IMG" --region="$REGION" \
    --no-allow-unauthenticated \
    --min-instances=0 --max-instances=2 \
    --cpu=1 --memory=512Mi --timeout=300 \
    --set-secrets=DATABASE_URL=database-url:latest \
    --set-env-vars=RUN_WORKER_INLINE=0,DEV_ROUTES=0 \
    --command=sh --args='-c,exec uvicorn worker.main:app --host 0.0.0.0 --port $PORT'

API_URL=$(gcloud run services describe api --region="$REGION" --format='value(status.url)')
echo
echo "==> api: $API_URL"
curl -fsS "$API_URL/health" && echo

cat <<EOF

Next:
  * Register OAuth redirect URIs against \$API_URL — EXACT match including
    trailing slash, no wildcards, up to a few hours to propagate:
      \$API_URL/v1/connections/callback/google
      \$API_URL/v1/connections/callback/slack
  * Cloud Scheduler sweep against the worker SERVICE endpoint (never a
    Cloud Run Job — 1-minute minimum billing per execution):
      gcloud scheduler jobs create http sweep --schedule="*/15 * * * *" \\
        --uri="\$WORKER_URL/sweep" --http-method=POST \\
        --oidc-service-account-email=... --location=$REGION
  * Check billing shows \$0 and no unexpected line item.
EOF
