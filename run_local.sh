#!/bin/bash
# Run the pipeline on this laptop (DirectRunner), reading the pages from the bucket.
set -euo pipefail
cd "$(dirname "$0")"
source config.sh

.venv/bin/python hw7_pipeline.py \
  --input "gs://${BUCKET}/${PAGES_PREFIX}*.html" \
  --output output/local \
  --runner DirectRunner \
  --direct_running_mode multi_threading \
  --direct_num_workers 0 \
  --project "$PROJECT_ID" \
  --impersonate_service_account "$SA_EMAIL" \
  "$@" 2>&1 | tee output-local.log
