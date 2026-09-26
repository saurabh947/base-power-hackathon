#!/usr/bin/env bash
# Create/update and run the one-off Cloud Run jobs.
# Usage: scripts/cloud/jobs.sh ingest|forecast|predict|backtest [--no-run]
#   ingest    INGEST_YEARS (default "2024 2025 2026"). The current year is year-to-date and ERCOT republishes it
#             weekly: INGEST_YEARS=2026 refreshes it (then re-run predict, and forecast YEAR=2026).
#   forecast  YEAR (default 2025): rolling hourly TimesFM forecasts; for the current year they stop at real_until_utc.
#             Builds the forecast image unless SKIP_BUILD=1 (reuse the image of this TAG).
#   predict   YEAR (default 2026): the AI's prediction for the rest of YEAR from its real_until_utc
#             (python -m src.forecast.predict), in the forecast image. SKIP_BUILD=1 reuses this TAG's image.
#   backtest  YEAR (default 2025): the full-year pitch backtest into Cloud SQL (that year's own pitch set), in the
#             api image of this TAG. 2026 needs its forecasts (forecast YEAR=2026) and the prediction (predict) first.
set -euo pipefail
cd "$(dirname "$0")/../.."
source scripts/cloud/env.sh

build_forecast_image() {
  [ "${SKIP_BUILD:-0}" = "1" ] && return 0
  gcloud builds submit --project "$P" --region "$R" --config scripts/cloud/cloudbuild.forecast.yaml \
    --substitutions "_IMAGE=$REPO/forecast:$TAG" .
}

case "${1:?ingest|forecast|predict|backtest}" in
  ingest)
    YEARS_ARGS="$(printf ',%s' ${INGEST_YEARS:-2024 2025 2026})"
    gcloud run jobs deploy fleet-sim-ingest --project "$P" --region "$R" --image "$REPO/api:$TAG" \
      --service-account "$SA" --set-cloudsql-instances "$SQL" \
      --set-env-vars "$DB_ENV" --set-secrets "$DB_SECRET" \
      --cpu 2 --memory 4Gi --task-timeout 1800 --max-retries 1 \
      --command python --args="-m,src.ingest.ercot,--years${YEARS_ARGS}"
    JOB=fleet-sim-ingest ;;
  forecast)
    build_forecast_image
    gcloud run jobs deploy fleet-sim-forecast --project "$P" --region "$R" --image "$REPO/forecast:$TAG" \
      --service-account "$SA" --set-cloudsql-instances "$SQL" \
      --set-env-vars "$DB_ENV" --set-secrets "$DB_SECRET" \
      --cpu 8 --memory 32Gi --task-timeout 10800 --max-retries 0 \
      --args="--year,${YEAR:-2025},--migrate"
    JOB=fleet-sim-forecast ;;
  predict)
    build_forecast_image
    gcloud run jobs deploy fleet-sim-predict --project "$P" --region "$R" --image "$REPO/forecast:$TAG" \
      --service-account "$SA" --set-cloudsql-instances "$SQL" \
      --set-env-vars "$DB_ENV" --set-secrets "$DB_SECRET" \
      --cpu 8 --memory 16Gi --task-timeout 3600 --max-retries 0 \
      --command python --args="-m,src.forecast.predict,--year,${YEAR:-2026},--out,db,--migrate"
    JOB=fleet-sim-predict ;;
  backtest)
    gcloud run jobs deploy fleet-sim-backtest --project "$P" --region "$R" --image "$REPO/api:$TAG" \
      --service-account "$SA" --set-cloudsql-instances "$SQL" \
      --set-env-vars "$DB_ENV" --set-secrets "$DB_SECRET" \
      --cpu 4 --memory 8Gi --task-timeout 1800 --max-retries 0 \
      --command python --args="^;^-m;src.backtest.runner;--strategies;bands,forecast,peak;--year;${YEAR:-2025};--pitch"  # ^;^ = ";" separates args; YEAR=2026: that year's own pitch set
    JOB=fleet-sim-backtest ;;
  *) echo "usage: $0 ingest|forecast|predict|backtest [--no-run]" >&2; exit 2 ;;
esac
[ "${2:-}" = "--no-run" ] || gcloud run jobs execute "$JOB" --project "$P" --region "$R" --wait
