#!/bin/bash
# GCE startup script for the HW6 models VM: runs as root every time the VM boots.
#
# First boot: install Python and the libraries models.py needs.
# Every boot: fetch models.py from instance metadata and run it. Its output
# goes to /var/log/models.log and to the serial console, where run_models.sh
# watches for the HW6_MODELS_EXIT line.

APP_DIR=/opt/models
VENV_DIR=$APP_DIR/venv
LOCK_FILE=/var/log/startup_already_done

mkdir -p "$APP_DIR"

# One-time setup, guarded by the lock file so packages aren't reinstalled on reboot.
if [ -f "$LOCK_FILE" ]; then
  echo "Startup script already ran once. Skipping package installation."
else
  if apt-get update && apt-get install -y python3-venv \
     && python3 -m venv "$VENV_DIR" \
     && "$VENV_DIR/bin/pip" install --upgrade pip \
     && "$VENV_DIR/bin/pip" install \
          pandas scikit-learn \
          google-cloud-storage google-cloud-secret-manager \
          "cloud-sql-python-connector[pymysql]" \
          "pymysql==1.2.0"; then  # 1.2.1-1.2.3 break MySQL 8.4 logins
    touch "$LOCK_FILE"
  else
    echo "HW6_MODELS_EXIT=99 (package installation failed)"
    exit 1
  fi
fi

# Pull models.py out of the instance metadata and run it.
curl -fsS -H "Metadata-Flavor: Google" \
  "http://metadata.google.internal/computeMetadata/v1/instance/attributes/models-py" \
  -o "$APP_DIR/models.py"

cd "$APP_DIR"
PYTHONUNBUFFERED=1 "$VENV_DIR/bin/python" models.py 2>&1 | tee /var/log/models.log
rc=${PIPESTATUS[0]}
echo "HW6_MODELS_EXIT=$rc"
