#!/bin/bash
# run_models.sh - run the HW6 models end to end:
#   1. start the Cloud SQL database
#   2. create a VM that trains both models (models-startup.sh + models.py)
#   3. wait for it to finish and print the output files from the bucket
#   4. ALWAYS stop the database and delete the VM at the end, even on failure
#
# Usage (from this folder):
#   ./run_models.sh          print the summary and the first rows of each file
#   ./run_models.sh --full   print the complete test-set prediction files

set -uo pipefail

# ---------- Configuration (project ID is hardcoded on purpose) ----------
PROJECT_ID="project-8aeecca0-4f70-4c6b-8e3"
ZONE="us-east1-b"                    # same region as the database
VM_NAME="hw6-models-vm"
MACHINE_TYPE="e2-medium"

SA_NAME="hw6-models-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

DB_INSTANCE="my-class-db"
DB_PASS_SECRET="db-root-password"

BUCKET="html-files-for-class"
OUTPUT_PREFIX="hw6"
OUTPUT_FILES=("results_summary.txt" "model1_ip_to_country_test.csv" "model2_income_test.csv")

TIMEOUT_SECONDS=1800                 # package install + training; normally ~5 min
# ------------------------------------------------------------------------

FULL=0
[[ "${1:-}" == "--full" ]] && FULL=1

cd "$(dirname "$0")"
for f in models.py models-startup.sh; do
  [[ -f "$f" ]] || { echo "ERROR: missing $f (run from the hw6 folder)"; exit 1; }
done

GC=(gcloud --project="$PROJECT_ID" --quiet)
log() { echo; echo "==> $*"; }

# ---------- Always clean up: delete the VM and stop the database ----------
cleanup() {
  log "Cleaning up"
  if "${GC[@]}" compute instances describe "$VM_NAME" --zone="$ZONE" >/dev/null 2>&1; then
    echo "Deleting VM $VM_NAME..."
    "${GC[@]}" compute instances delete "$VM_NAME" --zone="$ZONE" \
      || echo "ERROR: could not delete $VM_NAME; delete it by hand."
  fi
  echo "Stopping Cloud SQL instance $DB_INSTANCE..."
  "${GC[@]}" sql instances patch "$DB_INSTANCE" --activation-policy=NEVER >/dev/null \
    || echo "ERROR: could not stop $DB_INSTANCE; stop it by hand."
  echo "Done: VM deleted, database stopped."
}
trap cleanup EXIT

# ---------- 1. APIs and service account ----------
log "Enabling APIs and preparing service account $SA_NAME"
"${GC[@]}" services enable compute.googleapis.com sqladmin.googleapis.com \
  secretmanager.googleapis.com storage.googleapis.com iam.googleapis.com

if ! "${GC[@]}" iam service-accounts describe "$SA_EMAIL" >/dev/null 2>&1; then
  "${GC[@]}" iam service-accounts create "$SA_NAME" --display-name="HW6 models VM"
  sleep 10   # new service accounts take a moment before roles can be granted
fi
# Connect to Cloud SQL, read the DB password, write results to the bucket.
"${GC[@]}" projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/cloudsql.client" \
  --condition=None --format=none
"${GC[@]}" secrets add-iam-policy-binding "$DB_PASS_SECRET" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/secretmanager.secretAccessor" \
  --format=none
"${GC[@]}" storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/storage.objectAdmin" \
  --format=none
echo "Ready."

# ---------- 2. Start the database ----------
log "Starting Cloud SQL instance $DB_INSTANCE"
policy=$("${GC[@]}" sql instances describe "$DB_INSTANCE" \
           --format="value(settings.activationPolicy)") \
  || { echo "ERROR: instance $DB_INSTANCE not found."; exit 1; }
if [[ "$policy" == "ALWAYS" ]]; then
  echo "Already running."
else
  "${GC[@]}" sql instances patch "$DB_INSTANCE" --activation-policy=ALWAYS >/dev/null \
    || { echo "ERROR: could not start $DB_INSTANCE."; exit 1; }
  echo "Started."
fi

# Remove results from earlier runs, so everything printed below is from this run.
"${GC[@]}" storage rm "gs://${BUCKET}/${OUTPUT_PREFIX}/**" >/dev/null 2>&1 || true

# ---------- 3. Create the VM; its startup script runs the models ----------
log "Creating VM $VM_NAME"
if "${GC[@]}" compute instances describe "$VM_NAME" --zone="$ZONE" >/dev/null 2>&1; then
  echo "Deleting a leftover $VM_NAME from an earlier run..."
  "${GC[@]}" compute instances delete "$VM_NAME" --zone="$ZONE"
fi
"${GC[@]}" compute instances create "$VM_NAME" \
  --zone="$ZONE" \
  --machine-type="$MACHINE_TYPE" \
  --image-family=debian-12 --image-project=debian-cloud \
  --service-account="$SA_EMAIL" \
  --scopes=cloud-platform \
  --metadata-from-file=startup-script=models-startup.sh,models-py=models.py \
  || { echo "ERROR: could not create the VM."; exit 1; }

# ---------- 4. Wait for the models to finish ----------
# The startup script prints HW6_MODELS_EXIT=<code> to the serial console when done.
log "Waiting for the models to run (first boot installs packages; usually ~5 minutes)"
waited=0
exit_code=""
while (( waited < TIMEOUT_SECONDS )); do
  exit_code=$("${GC[@]}" compute instances get-serial-port-output "$VM_NAME" \
                --zone="$ZONE" 2>/dev/null \
              | grep -o 'HW6_MODELS_EXIT=[0-9]*' | tail -1 | cut -d= -f2)
  [[ -n "$exit_code" ]] && break
  sleep 20
  waited=$((waited + 20))
  echo "  ...${waited}s"
done

if [[ -z "$exit_code" ]]; then
  echo "ERROR: the models did not finish within ${TIMEOUT_SECONDS}s."
  exit 1
fi
if [[ "$exit_code" != "0" ]]; then
  echo "ERROR: models.py failed (exit code $exit_code). Last lines of its output:"
  "${GC[@]}" compute instances get-serial-port-output "$VM_NAME" --zone="$ZONE" 2>/dev/null \
    | grep 'startup-script' | tail -30
  exit 1
fi
echo "Models finished successfully after ~${waited}s."

# ---------- 5. Print the output files from the bucket ----------
for f in "${OUTPUT_FILES[@]}"; do
  path="gs://${BUCKET}/${OUTPUT_PREFIX}/${f}"
  log "$path"
  # Download the whole file once; cutting gcloud's output short (e.g. with head)
  # makes it report a hash mismatch.
  if ! content=$("${GC[@]}" storage cat "$path"); then
    echo "ERROR: could not read $path"
    continue
  fi
  if [[ "$f" == *.csv && $FULL -eq 0 ]]; then
    rows=$(printf '%s\n' "$content" | tail -n +2 | wc -l | tr -d ' ')
    printf '%s\n' "$content" | head -n 11
    echo "... ($rows rows in total; run with --full to print everything)"
  else
    printf '%s\n' "$content"
  fi
done

# cleanup() runs automatically on exit: deletes the VM and stops the database.
