#!/bin/bash
# GCE startup script for the error-logger VM: runs as root every time the VM boots. 
APP_DIR=/opt/error-logger
VENV_DIR=$APP_DIR/venv
 
mkdir -p "$APP_DIR"
 
# One-time setup: create a virtual environment with google-cloud-pubsub.
# Skipped only when the library actually imports, so an interrupted or failed
# install is retried on the next boot instead of being skipped forever.
if ! "$VENV_DIR/bin/python" -c "from google.cloud import pubsub_v1" 2>/dev/null; then
  rm -rf "$VENV_DIR"
  apt-get update
  apt-get install -y python3-venv
  python3 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install --upgrade pip
  "$VENV_DIR/bin/pip" install google-cloud-pubsub
fi
 
# Pull error-logger.py out of the instance metadata and save it on the VM
curl -fsS -H "Metadata-Flavor: Google" \
  "http://metadata.google.internal/computeMetadata/v1/instance/attributes/error-logger-py" \
  -o "$APP_DIR/error-logger.py"
 
# Start the logger in the background; -u disables output buffering so each
# message shows up in the log file immediately
nohup "$VENV_DIR/bin/python" -u "$APP_DIR/error-logger.py" > /var/log/error-logger.log 2>&1 &