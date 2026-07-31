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
    --set-env-vars=RUN_WORKER_INLINE=0 \
    --command=sh --args='-c,exec uvicorn api.main:app --host 0.0.0.0 --port $PORT'

echo "==> deploy worker (PRIVATE — never allow-unauthenticated)"
gcloud run deploy worker \
    --image="$IMG" --region="$REGION" \
    --no-allow-unauthenticated \
    --min-instances=0 --max-instances=2 \
    --cpu=1 --memory=512Mi --timeout=300 \
    --set-secrets=DATABASE_URL=database-url:latest \
    --set-env-vars=RUN_WORKER_INLINE=0 \
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
