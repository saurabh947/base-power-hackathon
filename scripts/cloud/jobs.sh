#!/usr/bin/env bash
# Create/update and run the one-off Cloud Run jobs.
# Usage: scripts/cloud/jobs.sh ingest|forecast|backtest [--no-run]
set -euo pipefail
cd "$(dirname "$0")/../.."
source scripts/cloud/env.sh
case "${1:?ingest|forecast}" in
  ingest)
    gcloud run jobs deploy fleet-sim-ingest --project "$P" --region "$R" --image "$REPO/api:$TAG" \
      --service-account "$SA" --set-cloudsql-instances "$SQL" \
      --set-env-vars "$DB_ENV" --set-secrets "$DB_SECRET" \
      --cpu 2 --memory 4Gi --task-timeout 1800 --max-retries 1 \
      --command python --args="-m,src.ingest.ercot,--years,2024,2025"
    JOB=fleet-sim-ingest ;;
  forecast)
    gcloud builds submit --project "$P" --region "$R" --config scripts/cloud/cloudbuild.forecast.yaml \
      --substitutions "_IMAGE=$REPO/forecast:$TAG" .
    gcloud run jobs deploy fleet-sim-forecast --project "$P" --region "$R" --image "$REPO/forecast:$TAG" \
      --service-account "$SA" --set-cloudsql-instances "$SQL" \
      --set-env-vars "$DB_ENV" --set-secrets "$DB_SECRET" \
      --cpu 8 --memory 32Gi --task-timeout 10800 --max-retries 0
    JOB=fleet-sim-forecast ;;
  backtest)
    gcloud run jobs deploy fleet-sim-backtest --project "$P" --region "$R" --image "$REPO/api:$TAG" \
      --service-account "$SA" --set-cloudsql-instances "$SQL" \
      --set-env-vars "$DB_ENV" --set-secrets "$DB_SECRET" \
      --cpu 4 --memory 8Gi --task-timeout 1800 --max-retries 0 \
      --command python --args="^;^-m;src.backtest.runner;--strategies;bands,forecast,peak;--year;2025;--pitch"  # ^;^ = ";" separates args
    JOB=fleet-sim-backtest ;;
  *) echo "usage: $0 ingest|forecast|backtest [--no-run]" >&2; exit 2 ;;
esac
[ "${2:-}" = "--no-run" ] || gcloud run jobs execute "$JOB" --project "$P" --region "$R" --wait
