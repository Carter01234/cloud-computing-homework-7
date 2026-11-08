#!/bin/bash
# Run the pipeline on Google Cloud Dataflow as homework7-service-account.
set -euo pipefail
cd "$(dirname "$0")"
source config.sh

.venv/bin/python hw7_pipeline.py \
  --input "gs://${BUCKET}/${PAGES_PREFIX}*.html" \
  --output "gs://${BUCKET}/hw7-output/dataflow" \
  --runner DataflowRunner \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --job_name "hw7-links-trigrams-$(date +%Y%m%d-%H%M%S)" \
  --temp_location "gs://${BUCKET}/hw7-dataflow/temp" \
  --staging_location "gs://${BUCKET}/hw7-dataflow/staging" \
  --service_account_email "$SA_EMAIL" \
  --impersonate_service_account "$SA_EMAIL" \
  --max_num_workers 4 \
  "$@" 2>&1 | tee output-dataflow.log
