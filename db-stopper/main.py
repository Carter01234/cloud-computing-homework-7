"""
Cloud Function: stop the HW5 Cloud SQL instance if it is running.

Cloud Scheduler calls this function every hour (see setup.sh). "Stopping" a Cloud
SQL instance means setting its activation policy to NEVER; the instance and its
data are kept, and only storage is billed while it is stopped.

Pause the scheduler job while you're actively using the database:
    gcloud scheduler jobs pause stop-idle-db-hourly --location=us-east1
and resume it when you're done:
    gcloud scheduler jobs resume stop-idle-db-hourly --location=us-east1
"""
import json
import logging
import os

import google.auth
from google.auth.transport.requests import AuthorizedSession

PROJECT_ID = os.environ.get("PROJECT_ID", "project-8aeecca0-4f70-4c6b-8e3")
INSTANCE = os.environ.get("INSTANCE", "my-class-db")

SQLADMIN_URL = (
    f"https://sqladmin.googleapis.com/v1/projects/{PROJECT_ID}/instances/{INSTANCE}"
)

logging.basicConfig(level=logging.INFO)


def _session() -> AuthorizedSession:
    # Uses the function's own service account; no keys needed.
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    return AuthorizedSession(credentials)


def check_and_stop_database() -> dict:
    """Stop the instance if it is running. Returns a summary of what happened."""
    session = _session()

    resp = session.get(SQLADMIN_URL, timeout=30)
    resp.raise_for_status()
    instance = resp.json()

    state = instance.get("state")
    policy = instance.get("settings", {}).get("activationPolicy")
    logging.info(f"{INSTANCE}: state={state}, activationPolicy={policy}")

    if policy != "ALWAYS":
        return {"instance": INSTANCE, "action": "none",
                "reason": f"already stopped (activationPolicy={policy})"}

    # Running: stop it. The patch returns an operation that finishes in the
    # background, so we don't wait for it here.
    patch = session.patch(
        SQLADMIN_URL,
        json={"settings": {"activationPolicy": "NEVER"}},
        timeout=30,
    )
    patch.raise_for_status()
    operation = patch.json().get("name")
    logging.warning(f"{INSTANCE} was running; stop requested (operation {operation})")
    return {"instance": INSTANCE, "action": "stopped", "operation": operation}


def stop_db(request):
    """HTTP entry point, called by Cloud Scheduler."""
    try:
        result = check_and_stop_database()
        return (json.dumps(result), 200, {"Content-Type": "application/json"})
    except Exception as exc:  # report the failure so Cloud Scheduler marks the run as failed
        logging.exception("Failed to check or stop the database")
        return (json.dumps({"instance": INSTANCE, "error": str(exc)}), 500,
                {"Content-Type": "application/json"})
