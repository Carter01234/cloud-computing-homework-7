#!/bin/bash
# setup.sh - one-time setup for Homework 7:
#   1. enable the APIs Dataflow needs
#   2. create homework7-service-account and grant it the roles to run Dataflow
#      jobs and read/write the bucket
#   3. let your own gcloud account impersonate it (no key file needed)
#   4. create the Python virtual environment in .venv and install Apache Beam
# Safe to run more than once.
set -euo pipefail
cd "$(dirname "$0")"
source config.sh

GC=(gcloud --project="$PROJECT_ID" --quiet)
log() { echo; echo "==> $*"; }

log "Enabling APIs"
"${GC[@]}" services enable dataflow.googleapis.com compute.googleapis.com \
  storage.googleapis.com logging.googleapis.com iam.googleapis.com \
  iamcredentials.googleapis.com

log "Service account $SA_EMAIL"
if "${GC[@]}" iam service-accounts describe "$SA_EMAIL" >/dev/null 2>&1; then
  echo "Already exists."
else
  "${GC[@]}" iam service-accounts create "$SA_NAME" \
    --display-name="Homework 7 service account (Dataflow)"
  sleep 10   # a brand-new account takes a moment before it can be granted roles
fi

log "Project roles for the service account"
# dataflow.admin: submit/manage jobs; dataflow.worker: run as the worker VMs;
# logging.logWriter: worker logs.
for role in roles/dataflow.admin roles/dataflow.worker roles/logging.logWriter; do
  "${GC[@]}" projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" --role="$role" \
    --condition=None --format=none
done

log "Bucket access (read pages, write staging/temp/output)"
"${GC[@]}" storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/storage.objectAdmin" \
  --format=none

log "Impersonation permissions"
# The account launches its own worker VMs, so it needs actAs on itself.
"${GC[@]}" iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/iam.serviceAccountUser" \
  --format=none
ME="$(gcloud config get-value account 2>/dev/null)"
"${GC[@]}" iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
  --member="user:${ME}" --role="roles/iam.serviceAccountTokenCreator" \
  --format=none
echo "$ME can now act as $SA_EMAIL."

log "Application Default Credentials"
if ! gcloud auth application-default print-access-token >/dev/null 2>&1; then
  gcloud auth application-default login
fi
gcloud auth application-default set-quota-project "$PROJECT_ID" >/dev/null 2>&1 || true

log "Python virtual environment (.venv)"
PY="${PYTHON:-python3}"
"$PY" - <<'PYEOF'
import sys
v = sys.version_info[:2]
if not ((3, 10) <= v <= (3, 14)):
    sys.exit(f"Apache Beam needs Python 3.10-3.14, found {sys.version.split()[0]}. "
             "Re-run with PYTHON=python3.12 ./setup.sh (or similar).")
print("Using Python", sys.version.split()[0])
PYEOF
[[ -d .venv ]] || "$PY" -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/python -c "import apache_beam; print('Apache Beam', apache_beam.__version__)"

log "Done. IAM changes can take a minute or two to apply before the first run."
