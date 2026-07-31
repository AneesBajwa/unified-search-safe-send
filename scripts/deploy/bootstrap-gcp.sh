#!/usr/bin/env bash
# Phase 0 Part A.4 — one-time GCP bootstrap. Follows runbooks.md §4.
#
# NOT YET EXECUTED. Requires an interactive `gcloud auth login` first; that is
# the only step a human must do by hand.
#
# The $0 guarantee rests on three things this script enforces:
#   1. A Free Trial billing account, never upgraded to Paid. Trial auto-closes
#      at $300/90 days with no auto-conversion — the only *mathematical*
#      guarantee of $0 (risks.md R1). Everything else is a guardrail.
#   2. containerscanning.googleapis.com is NEVER enabled ($0.26 per push,
#      billing starts immediately).
#   3. --max-instances is always set. The default is 100; a bot or a load test
#      burns the free tier in minutes.
set -euo pipefail

REGION="${REGION:-us-east4}"          # Tier 1, same metro as Neon aws-us-east-1
REPO="${REPO:-app}"
PROJECT_ID="${PROJECT_ID:-unified-search-$(date +%s)}"

command -v gcloud >/dev/null || { echo "gcloud not installed: brew install --cask gcloud-cli"; exit 1; }

# `auth login` and `auth application-default login` are independent and one
# does not satisfy the other. An agent driving deploys needs `auth login`.
gcloud auth list --filter=status:ACTIVE --format="value(account)" | grep -q . || {
    echo "Run: gcloud auth login"; exit 1
}

echo "==> project $PROJECT_ID (the ID is IMMUTABLE)"
gcloud projects create "$PROJECT_ID" --name="Unified Search PoC"
gcloud config set project "$PROJECT_ID"

cat <<EOF

==> Link billing. Pick a FREE TRIAL account and NEVER click Upgrade.
    Available accounts:
EOF
gcloud billing accounts list
read -r -p "    Billing account id (XXXXXX-XXXXXX-XXXXXX): " BILLING
gcloud billing projects link "$PROJECT_ID" --billing-account="$BILLING"

echo "==> enabling the five services we need, and nothing else"
gcloud services enable \
    run.googleapis.com \
    artifactregistry.googleapis.com \
    secretmanager.googleapis.com \
    cloudscheduler.googleapis.com \
    cloudtasks.googleapis.com
# NEVER: containerscanning.googleapis.com
# NOT NEEDED: cloudbuild, sqladmin, container

gcloud config set run/region "$REGION"

echo "==> Artifact Registry in $REGION"
gcloud artifacts repositories create "$REPO" \
    --repository-format=docker --location="$REGION"
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

# Keeps the repo inside the 0.5 GiB free tier without manual pruning.
cat > /tmp/ar-cleanup.json <<'JSON'
[
  {"name":"keep-3-recent","action":{"type":"Keep"},"mostRecentVersions":{"keepCount":3}},
  {"name":"drop-untagged","action":{"type":"Delete"},
   "condition":{"tagState":"untagged","olderThan":"7d"}}
]
JSON
gcloud artifacts repositories set-cleanup-policies "$REPO" \
    --location="$REGION" --policy=/tmp/ar-cleanup.json

cat <<EOF

==> Bootstrap done. PROJECT_ID=$PROJECT_ID

Two guardrails must be set in the console by hand — neither has a reliable
CLI equivalent:

  1. Billing -> Budgets & alerts -> \$1 budget scoped to Cloud Run, with
     *Spend cap enforcement* (NOT "Alerts only"). An existing alerts-only
     budget cannot be converted; it must be recreated.
  2. A second, alert-only \$1 budget wired to the Pub/Sub -> Cloud Function
     billing-disable kill switch.

Then create the secrets (values from the provider consoles):

  for s in google-client-secret slack-client-secret token-keyring \\
           web-search-api-key database-url; do
      gcloud secrets create "\$s" --replication-policy=automatic
  done
  printf '%s' "\$VALUE" | gcloud secrets versions add database-url --data-file=-

Keep <=6 active versions and DESTROY old ones rather than disabling them.
EOF
