#!/bin/bash
# Run the pipeline on this laptop, reading the pages from the bucket.
# FnApiRunner is Beam's in-process Python local runner. We name it explicitly
# because in Beam 2.77 "DirectRunner" hands the job to the Prism runner, whose
# connection times out after 5 minutes and ignores the threading options below.
set -euo pipefail
cd "$(dirname "$0")"
source config.sh

# macOS allows only 256 open files per process by default; the parallel GCS
# reads need more headroom than that.
ulimit -n 4096 2>/dev/null || ulimit -n "$(ulimit -Hn)" 2>/dev/null || true

.venv/bin/python hw7_pipeline.py \
  --input "gs://${BUCKET}/${PAGES_PREFIX}*.html" \
  --output output/local \
  --runner FnApiRunner \
  --direct_running_mode multi_threading \
  --direct_num_workers 0 \
  --project "$PROJECT_ID" \
  --impersonate_service_account "$SA_EMAIL" \
  "$@" 2>&1 | tee output-local.log
