#!/bin/bash
# cleanup.sh - tear down the HW5 infrastructure so nothing keeps billing.
#
#   * deletes the file-server, error-logger and client VMs
#   * releases the reserved static external IP
#   * STOPS (does not delete) the Cloud SQL instance
#   * revokes Application Default Credentials
#
# Safe to run more than once: anything already gone is skipped.
# Usage:  ./cleanup.sh

set -uo pipefail   # no -e: keep cleaning up even if one step fails

# ---------- Configuration (project ID is hardcoded on purpose) ----------
PROJECT_ID="project-8aeecca0-4f70-4c6b-8e3"
ZONE="us-central1-a"
REGION="us-central1"                 # region of the static IP

VMS=("my-vm" "error-logger-vm" "client-vm")
# hw5-ip is what setup.sh allocates; homework4-ip is the older IP from before setup.sh.
STATIC_IP_NAMES=("hw5-ip" "homework4-ip")

DB_INSTANCE="my-class-db"
# ------------------------------------------------------------------------

failures=0

# ---------- VMs ----------
delete_vms() {
  local pids=()
  for vm in "${VMS[@]}"; do
    if gcloud compute instances describe "$vm" --zone="$ZONE" \
         --project="$PROJECT_ID" --format="value(name)" >/dev/null 2>&1; then
      echo "Deleting VM $vm..."
      gcloud compute instances delete "$vm" --zone="$ZONE" \
        --project="$PROJECT_ID" --quiet &
      pids+=($!)
    else
      echo "VM $vm does not exist, skipping."
    fi
  done
  # Delete in parallel, then wait for all of them.
  for pid in "${pids[@]+"${pids[@]}"}"; do
    wait "$pid" || { echo "ERROR: a VM deletion failed."; failures=$((failures + 1)); }
  done
}

# ---------- Static IP ----------
# Must run after the VMs are gone: an IP still attached to a VM can't be released.
release_static_ip() {
  for ip_name in "${STATIC_IP_NAMES[@]}"; do
    if gcloud compute addresses describe "$ip_name" --region="$REGION" \
         --project="$PROJECT_ID" --format="value(name)" >/dev/null 2>&1; then
      echo "Releasing static IP $ip_name..."
      gcloud compute addresses delete "$ip_name" --region="$REGION" \
        --project="$PROJECT_ID" --quiet \
        || { echo "ERROR: could not release $ip_name."; failures=$((failures + 1)); }
    else
      echo "Static IP $ip_name does not exist, skipping."
    fi
  done
}

# ---------- Cloud SQL: stop, never delete ----------
stop_database() {
  local policy
  if ! policy=$(gcloud sql instances describe "$DB_INSTANCE" \
                  --project="$PROJECT_ID" \
                  --format="value(settings.activationPolicy)" 2>/dev/null); then
    echo "Cloud SQL instance $DB_INSTANCE does not exist, skipping."
    return
  fi

  if [[ "$policy" == "NEVER" ]]; then
    echo "Cloud SQL instance $DB_INSTANCE is already stopped."
  else
    echo "Stopping Cloud SQL instance $DB_INSTANCE (this can take a few minutes)..."
    gcloud sql instances patch "$DB_INSTANCE" --project="$PROJECT_ID" \
      --activation-policy=NEVER --quiet \
      || { echo "ERROR: could not stop $DB_INSTANCE."; failures=$((failures + 1)); }
  fi
}

# ---------- Application Default Credentials ----------
revoke_adc() {
  if gcloud auth application-default print-access-token >/dev/null 2>&1; then
    echo "Revoking Application Default Credentials..."
    gcloud auth application-default revoke --quiet \
      || { echo "ERROR: could not revoke ADC."; failures=$((failures + 1)); }
  else
    echo "No Application Default Credentials to revoke, skipping."
  fi
}

# ---------- Run ----------
echo "Cleaning up HW5 resources in project $PROJECT_ID"
delete_vms
release_static_ip
stop_database
revoke_adc   # last, in case anything above needed these credentials

echo
if (( failures == 0 )); then
  echo "Cleanup complete. (Cloud SQL instance $DB_INSTANCE was stopped, not deleted.)"
else
  echo "Cleanup finished with $failures error(s); see messages above."
  exit 1
fi
