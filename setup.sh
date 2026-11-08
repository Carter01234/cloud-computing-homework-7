#!/bin/bash
# setup.sh - provision everything HW5 needs, in this order:
#   1. enable the required Google Cloud APIs
#   2. service account and its roles
#   3. Pub/Sub topic + subscription for forbidden-country requests
#   4. Cloud SQL: start it if it exists, otherwise create it and set up the schema
#   5. static external IP (allocated, then read into a variable)
#   6. firewall rule for port 8080
#   7. file-server VM and error-logger VM (configured by their startup scripts)
#
# Safe to run more than once: anything that already exists is reused.
# Usage (from this folder):
#   ./setup.sh             create what's missing; start stopped VMs
#   ./setup.sh --replace   also delete and recreate the VMs (picks up code changes)

set -euo pipefail

# ---------- Configuration (project ID is hardcoded on purpose) ----------
PROJECT_ID="project-8aeecca0-4f70-4c6b-8e3"
REGION="us-central1"
ZONE="us-central1-a"

SA_NAME="homework5-service-account"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

BUCKET_NAME="html-files-for-class"
TOPIC="forbidden-requests"
SUBSCRIPTION="forbidden-requests-logger"

DB_INSTANCE="my-class-db"
DB_REGION_ZONE="us-east1-b"
DB_TIER="db-custom-1-3840"          # 1 vCPU / 3.75 GB: enough for this workload
DB_STORAGE="20GB"
DB_NAME="hw5"
DB_PASS_SECRET="db-root-password"

STATIC_IP_NAME="hw5-ip"
FIREWALL_RULE="hw5-allow-8080"
NETWORK_TAG="hw5-file-server"

SERVER_VM="my-vm"
SERVER_MACHINE_TYPE="e2-micro"
SERVER_PORT=8080

LOGGER_VM="error-logger-vm"
LOGGER_MACHINE_TYPE="e2-micro"

STARTUP_TIMEOUT_SECONDS=900          # first boot installs packages; allow 15 min
# ------------------------------------------------------------------------

REPLACE_VMS=0
[[ "${1:-}" == "--replace" ]] && REPLACE_VMS=1

# Always run from this script's folder, so the relative paths below resolve.
cd "$(dirname "$0")"

for f in startup.sh setup_schema.py file-server/file-server.py \
         error-logger/error-logger-startup.sh error-logger/error-logger.py; do
  [[ -f "$f" ]] || { echo "ERROR: missing $f (run setup.sh from the hw5 folder)"; exit 1; }
done

GC=(gcloud --project="$PROJECT_ID" --quiet)

log() { echo; echo "==> $*"; }

# ---------- 1. APIs ----------
enable_apis() {
  log "Enabling required APIs"
  "${GC[@]}" services enable \
    compute.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com \
    pubsub.googleapis.com logging.googleapis.com storage.googleapis.com iam.googleapis.com
}

# ---------- 2. Service account ----------
setup_service_account() {
  log "Service account $SA_EMAIL"
  if "${GC[@]}" iam service-accounts describe "$SA_EMAIL" >/dev/null 2>&1; then
    echo "Already exists."
  else
    "${GC[@]}" iam service-accounts create "$SA_NAME" \
      --display-name="Homework 5 service account"
    sleep 10   # new service accounts take a moment before they can be granted roles
  fi

  # Granting a role the account already has is a no-op, so this is safe to repeat.
  for role in roles/cloudsql.client roles/logging.logWriter \
              roles/pubsub.publisher roles/pubsub.subscriber; do
    "${GC[@]}" projects add-iam-policy-binding "$PROJECT_ID" \
      --member="serviceAccount:${SA_EMAIL}" --role="$role" \
      --condition=None --format=none
  done
  "${GC[@]}" storage buckets add-iam-policy-binding "gs://${BUCKET_NAME}" \
    --member="serviceAccount:${SA_EMAIL}" --role="roles/storage.objectViewer" \
    --format=none
  echo "Roles granted."
}

# ---------- 3. Pub/Sub ----------
setup_pubsub() {
  log "Pub/Sub topic $TOPIC and subscription $SUBSCRIPTION"
  "${GC[@]}" pubsub topics describe "$TOPIC" >/dev/null 2>&1 \
    || "${GC[@]}" pubsub topics create "$TOPIC"
  "${GC[@]}" pubsub subscriptions describe "$SUBSCRIPTION" >/dev/null 2>&1 \
    || "${GC[@]}" pubsub subscriptions create "$SUBSCRIPTION" --topic="$TOPIC"
  echo "Ready."
}

# ---------- 4. Cloud SQL ----------
ensure_db_password_secret() {
  if "${GC[@]}" secrets describe "$DB_PASS_SECRET" >/dev/null 2>&1; then
    echo "Password secret $DB_PASS_SECRET already exists."
  else
    echo "Generating a root password and storing it in Secret Manager ($DB_PASS_SECRET)..."
    printf '%s' "$(openssl rand -hex 16)" \
      | "${GC[@]}" secrets create "$DB_PASS_SECRET" \
          --replication-policy=automatic --data-file=-
  fi
  "${GC[@]}" secrets add-iam-policy-binding "$DB_PASS_SECRET" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/secretmanager.secretAccessor" --format=none
}

# setup_schema.py connects with Application Default Credentials and needs a few packages.
run_schema_script() {
  echo "Setting up the database schema with setup_schema.py..."
  if ! gcloud auth application-default print-access-token >/dev/null 2>&1; then
    echo "No Application Default Credentials found; opening a browser to log in..."
    gcloud auth application-default login
  fi
  if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
  fi
  .venv/bin/pip install -q "cloud-sql-python-connector[pymysql]" "pymysql==1.2.0" \
    google-cloud-secret-manager
  .venv/bin/python setup_schema.py
}

setup_database() {
  log "Cloud SQL instance $DB_INSTANCE"
  ensure_db_password_secret

  if "${GC[@]}" sql instances describe "$DB_INSTANCE" >/dev/null 2>&1; then
    # The instance exists: just make sure it's running.
    local policy
    policy=$("${GC[@]}" sql instances describe "$DB_INSTANCE" \
               --format="value(settings.activationPolicy)")
    if [[ "$policy" == "ALWAYS" ]]; then
      echo "Instance exists and is already running."
    else
      echo "Instance exists; starting it (this can take a few minutes)..."
      "${GC[@]}" sql instances patch "$DB_INSTANCE" --activation-policy=ALWAYS
    fi

    # The database exists, so nothing is created. setup_schema.py still runs:
    # it migrates an HW5-shaped database to the 3NF schema, and does nothing
    # if the migration has already happened.
    if "${GC[@]}" sql databases describe "$DB_NAME" --instance="$DB_INSTANCE" >/dev/null 2>&1; then
      echo "Database $DB_NAME already exists; checking it uses the 3NF schema."
    fi
    run_schema_script
  else
    echo "Instance does not exist; creating it (this takes around 5-10 minutes)..."
    local db_pass
    db_pass=$("${GC[@]}" secrets versions access latest --secret="$DB_PASS_SECRET")
    "${GC[@]}" sql instances create "$DB_INSTANCE" \
      --database-version=MYSQL_8_4 \
      --edition=ENTERPRISE \
      --tier="$DB_TIER" \
      --zone="$DB_REGION_ZONE" \
      --availability-type=ZONAL \
      --storage-type=SSD \
      --storage-size="$DB_STORAGE" \
      --root-password="$db_pass" \
      --assign-ip \
      --no-deletion-protection \
      --no-retain-backups-on-delete \
      --no-final-backup
    run_schema_script
  fi
}

# ---------- 5. Static IP ----------
setup_static_ip() {
  log "Static IP $STATIC_IP_NAME"
  if "${GC[@]}" compute addresses describe "$STATIC_IP_NAME" --region="$REGION" >/dev/null 2>&1; then
    echo "Already allocated."
  else
    "${GC[@]}" compute addresses create "$STATIC_IP_NAME" --region="$REGION"
  fi
  # Read the address into a variable instead of hardcoding it.
  SERVER_IP=$("${GC[@]}" compute addresses describe "$STATIC_IP_NAME" \
                --region="$REGION" --format="value(address)")
  echo "Server IP: $SERVER_IP"
}

# ---------- 6. Firewall ----------
setup_firewall() {
  log "Firewall rule $FIREWALL_RULE (tcp:$SERVER_PORT)"
  if "${GC[@]}" compute firewall-rules describe "$FIREWALL_RULE" >/dev/null 2>&1; then
    echo "Already exists."
  else
    "${GC[@]}" compute firewall-rules create "$FIREWALL_RULE" \
      --network=default --direction=INGRESS --action=ALLOW \
      --rules="tcp:${SERVER_PORT}" --source-ranges=0.0.0.0/0 \
      --target-tags="$NETWORK_TAG"
  fi
}

# ---------- 7. VMs ----------
vm_exists() {
  "${GC[@]}" compute instances describe "$1" --zone="$ZONE" >/dev/null 2>&1
}

# Prints "create" if the VM needs to be created, "skip" if an existing one is reused.
prepare_vm() {
  local vm=$1
  if ! vm_exists "$vm"; then
    echo create; return
  fi
  if (( REPLACE_VMS )); then
    echo "Deleting existing VM $vm..." >&2
    "${GC[@]}" compute instances delete "$vm" --zone="$ZONE" >&2
    echo create; return
  fi
  local status
  status=$("${GC[@]}" compute instances describe "$vm" --zone="$ZONE" --format="value(status)")
  if [[ "$status" != "RUNNING" ]]; then
    echo "VM $vm exists but is $status; starting it..." >&2
    "${GC[@]}" compute instances start "$vm" --zone="$ZONE" >&2
  else
    echo "VM $vm is already running; reusing it (use --replace to recreate)." >&2
  fi
  echo skip
}

create_vms() {
  log "VMs $SERVER_VM and $LOGGER_VM"
  local server_action logger_action server_pid="" logger_pid="" failed=0
  server_action=$(prepare_vm "$SERVER_VM")
  logger_action=$(prepare_vm "$LOGGER_VM")

  # Create both in parallel; each one configures itself with its startup script.
  if [[ "$server_action" == create ]]; then
    echo "Creating $SERVER_VM..."
    "${GC[@]}" compute instances create "$SERVER_VM" \
      --zone="$ZONE" \
      --machine-type="$SERVER_MACHINE_TYPE" \
      --address="$SERVER_IP" \
      --tags="$NETWORK_TAG" \
      --service-account="$SA_EMAIL" \
      --scopes=cloud-platform \
      --metadata-from-file=startup-script=startup.sh,file-server-py=file-server/file-server.py \
      > /tmp/hw5-setup-server.log 2>&1 &
    server_pid=$!
  fi
  if [[ "$logger_action" == create ]]; then
    echo "Creating $LOGGER_VM..."
    "${GC[@]}" compute instances create "$LOGGER_VM" \
      --zone="$ZONE" \
      --machine-type="$LOGGER_MACHINE_TYPE" \
      --service-account="$SA_EMAIL" \
      --scopes=cloud-platform \
      --metadata-from-file=startup-script=error-logger/error-logger-startup.sh,error-logger-py=error-logger/error-logger.py \
      > /tmp/hw5-setup-logger.log 2>&1 &
    logger_pid=$!
  fi

  if [[ -n "$server_pid" ]]; then
    wait "$server_pid" || { echo "ERROR creating $SERVER_VM:"; cat /tmp/hw5-setup-server.log; failed=1; }
  fi
  if [[ -n "$logger_pid" ]]; then
    wait "$logger_pid" || { echo "ERROR creating $LOGGER_VM:"; cat /tmp/hw5-setup-logger.log; failed=1; }
  fi
  (( failed == 0 )) || exit 1
}

# Reads the VM's serial console, which works even before SSH is available.
wait_for_startup() {
  local vm=$1 waited=0
  while (( waited < STARTUP_TIMEOUT_SECONDS )); do
    if "${GC[@]}" compute instances get-serial-port-output "$vm" --zone="$ZONE" 2>/dev/null \
        | grep -q "Finished running startup scripts"; then
      echo "  $vm: startup script finished after ~${waited}s"
      return 0
    fi
    sleep 15
    waited=$((waited + 15))
  done
  echo "  $vm: startup script did not finish within ${STARTUP_TIMEOUT_SECONDS}s"
  return 1
}

wait_and_check() {
  log "Waiting for the VMs' startup scripts (first boot can take several minutes)"
  local server_ok=0 logger_ok=0 http_ok=0 server_wait logger_wait
  wait_for_startup "$SERVER_VM" & server_wait=$!
  wait_for_startup "$LOGGER_VM" & logger_wait=$!
  wait "$server_wait" && server_ok=1
  wait "$logger_wait" && logger_ok=1

  if (( server_ok )); then
    for _ in {1..12}; do
      # Any HTTP status (even 404) means the server process is up and answering.
      if curl -s -o /dev/null --max-time 5 "http://${SERVER_IP}:${SERVER_PORT}/"; then
        http_ok=1; break
      fi
      sleep 5
    done
  fi

  echo
  echo "========== Setup summary =========="
  if (( http_ok )); then
    echo "File server:  RUNNING at http://${SERVER_IP}:${SERVER_PORT}"
  else
    echo "File server:  NOT RESPONDING at http://${SERVER_IP}:${SERVER_PORT}"
    echo "  Debug: gcloud compute ssh $SERVER_VM --zone=$ZONE --project=$PROJECT_ID --command='sudo tail -30 /var/log/file-server.log'"
  fi
  if (( logger_ok )); then
    echo "Error logger: running on $LOGGER_VM"
  else
    echo "Error logger: startup did NOT finish on $LOGGER_VM"
    echo "  Debug: gcloud compute ssh $LOGGER_VM --zone=$ZONE --project=$PROJECT_ID --command='sudo journalctl -u google-startup-scripts.service --no-pager | tail -40'"
  fi
  echo "Database:     $DB_INSTANCE is running (run ./cleanup.sh to stop it when done)"
  echo "==================================="

  (( http_ok && logger_ok ))
}

# ---------- Run ----------
echo "Setting up HW5 in project $PROJECT_ID"
enable_apis
setup_service_account
setup_pubsub
setup_database
setup_static_ip
setup_firewall
create_vms
wait_and_check
