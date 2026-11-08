#!/bin/bash
# GCE startup script: runs as root every time the VM boots.
#
# First boot: install system packages and the Python dependencies.
# Every boot: fetch the latest file-server.py from instance metadata and start it.

APP_DIR=/opt/file-server
VENV_DIR=$APP_DIR/venv
LOCK_FILE=/var/log/startup_already_done

mkdir -p "$APP_DIR"

# One-time setup, guarded by the lock file so packages aren't reinstalled on reboot.
if [ -f "$LOCK_FILE" ]; then
  echo "Startup script already ran once. Skipping package installation."
else
  set -e  # stop if any install step fails, so the lock file isn't written
  apt-get update
  apt-get install -y python3-venv
  python3 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install --upgrade pip
  "$VENV_DIR/bin/pip" install \
    google-cloud-storage \
    google-cloud-logging \
    google-cloud-pubsub \
    google-cloud-secret-manager \
    sqlalchemy \
    "cloud-sql-python-connector[pymysql]" \
    "pymysql==1.2.0"  # 1.2.1-1.2.3 break MySQL 8.4 logins
  set +e
  touch "$LOCK_FILE"
fi

# Pull file-server.py out of the instance metadata and save it on the VM
curl -s -H "Metadata-Flavor: Google" \
  "http://metadata.google.internal/computeMetadata/v1/instance/attributes/file-server-py" \
  -o "$APP_DIR/file-server.py"

# Start the server from the app folder, so timings.json is saved there.
# PYTHONUNBUFFERED makes print() output show up in the log right away.
cd "$APP_DIR"
PYTHONUNBUFFERED=1 nohup "$VENV_DIR/bin/python" "$APP_DIR/file-server.py" \
  > /var/log/file-server.log 2>&1 &
