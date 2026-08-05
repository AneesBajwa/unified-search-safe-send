#!/usr/bin/env bash
# Build and deploy api + worker to Cloud Run. Follows runbooks.md §4.
# EXECUTED 2026-08-04 against project unified-search-1785899621 — the deployed
# services in README.md §Deployment came from exactly this sequence.
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
# seconds rather than after five minutes of emulated compilation. A target
# that quietly skips a blocked step is how "not deployed" becomes "deploy
# passed" in the next status report.
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

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
API_URL="https://api-${PROJECT_NUMBER}.${REGION}.run.app"
WORKER_URL="https://worker-${PROJECT_NUMBER}.${REGION}.run.app"

# The nudge (core/jobs/nudge.py, design D4): the API creates one Cloud Tasks
# task per job-creating commit, pushed to the worker's /work with OIDC.
echo "==> cloud tasks queue + IAM for the hot path"
gcloud tasks queues describe work --location="$REGION" >/dev/null 2>&1 || \
    gcloud tasks queues create work --location="$REGION"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$SA" --role=roles/cloudtasks.enqueuer --condition=None >/dev/null
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor --condition=None >/dev/null
gcloud iam service-accounts add-iam-policy-binding "$SA" \
    --member="serviceAccount:$SA" --role=roles/iam.serviceAccountUser >/dev/null

# --platform linux/amd64 is MANDATORY on Apple Silicon. An arm64 image fails
# on Cloud Run with an opaque "container failed to start" and no useful log.
#
# Deliberately not `gcloud run deploy --source`: that pulls in Cloud Build,
# auto-creates a repository, and produces buildpack images 2-3x larger than
# our Dockerfile, which blows the 0.5 GiB Artifact Registry free tier.
echo "==> build + push $IMG"
docker buildx build --platform linux/amd64 -t "$IMG" --push .

echo "==> migrate (one-shot, against Neon)"
# A Cloud Run *Job* would bill a 1-minute minimum per execution. Run the
# migration locally against the pooled endpoint instead — same result, no job
# resource:
echo "    locally:  DATABASE_URL=<neon-pooled-url> uv run alembic upgrade head"

# Client ids are not secrets; the client *secrets* and the keyring are, and the
# keyring is mounted as a FILE at /secrets/keyring (risks.md R9 — env vars
# resolve at instance start and leak through /proc/self/environ).
GOOGLE_CLIENT_ID="${GOOGLE_CLIENT_ID:-$(sed -nE 's/^GOOGLE_CLIENT_ID=//p' .env | head -1)}"
SLACK_CLIENT_ID="${SLACK_CLIENT_ID:-$(sed -nE 's/^SLACK_CLIENT_ID=//p' .env | head -1)}"
SPA_ORIGIN="${SPA_ORIGIN:-https://${PROJECT_ID}.web.app}"

echo "==> deploy api (public)"
gcloud run deploy api \
    --image="$IMG" --region="$REGION" \
    --allow-unauthenticated \
    --min-instances=0 --max-instances=2 \
    --cpu=1 --memory=512Mi \
    --set-secrets=DATABASE_URL=database-url:latest,GOOGLE_CLIENT_SECRET=google-client-secret:latest,SLACK_CLIENT_SECRET=slack-client-secret:latest,/secrets/keyring=token-keyring:latest \
    --set-env-vars="^##^APP_BASE_URL=${API_URL}##CORS_ORIGINS=${SPA_ORIGIN},https://${PROJECT_ID}.firebaseapp.com,http://localhost:5173##RUN_WORKER_INLINE=0##DEV_ROUTES=0##CLOUD_TASKS_QUEUE=projects/${PROJECT_ID}/locations/${REGION}/queues/work##WORKER_URL=${WORKER_URL}##TASKS_OIDC_SERVICE_ACCOUNT=${SA}##GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID}##SLACK_CLIENT_ID=${SLACK_CLIENT_ID}"

echo "==> deploy worker (PRIVATE — never allow-unauthenticated)"
gcloud run deploy worker \
    --image="$IMG" --region="$REGION" \
    --no-allow-unauthenticated \
    --min-instances=0 --max-instances=2 \
    --cpu=1 --memory=512Mi --timeout=300 \
    --set-secrets=DATABASE_URL=database-url:latest,GOOGLE_CLIENT_SECRET=google-client-secret:latest,SLACK_CLIENT_SECRET=slack-client-secret:latest,/secrets/keyring=token-keyring:latest \
    --set-env-vars="RUN_WORKER_INLINE=0,DEV_ROUTES=0,GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID},SLACK_CLIENT_ID=${SLACK_CLIENT_ID}" \
    --command=sh --args='-c,exec uvicorn worker.main:app --host 0.0.0.0 --port $PORT'

gcloud run services add-iam-policy-binding worker --region="$REGION" \
    --member="serviceAccount:$SA" --role=roles/run.invoker >/dev/null

echo "==> cloud scheduler sweep (*/15, OIDC, against the worker SERVICE — never a Job)"
gcloud scheduler jobs describe sweep --location="$REGION" >/dev/null 2>&1 || \
    gcloud scheduler jobs create http sweep --schedule="*/15 * * * *" \
        --uri="${WORKER_URL}/sweep" --http-method=POST \
        --oidc-service-account-email="$SA" \
        --oidc-token-audience="$WORKER_URL" \
        --location="$REGION"

echo
echo "==> api: $API_URL"
curl -fsS "$API_URL/health" && echo

cat <<EOF

Next:
  * SPA: VITE_API_BASE_URL=$API_URL npm run build (in apps/web), then
    firebase deploy --only hosting.
  * Register OAuth redirect URIs against \$API_URL — EXACT match, no trailing
    slash, no wildcards, up to a few hours to propagate:
      $API_URL/v1/connections/callback/gmail
      $API_URL/v1/connections/callback/slack
  * Seed: DATABASE_URL=<neon-pooled-url> uv run python scripts/seed.py
  * Check billing shows \$0 and no unexpected line item.
EOF
