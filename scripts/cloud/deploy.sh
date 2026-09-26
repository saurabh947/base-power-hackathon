#!/usr/bin/env bash
# Build the backend image with Cloud Build (not on the laptop) and deploy the Cloud Run service.
# Usage: scripts/cloud/deploy.sh [extra CORS origin, e.g. https://fleet-sim.vercel.app]
set -euo pipefail
cd "$(dirname "$0")/../.."
source scripts/cloud/env.sh
CORS="${1:-}"
gcloud builds submit --project "$P" --region "$R" --tag "$REPO/api:$TAG" .
gcloud run deploy fleet-sim --project "$P" --region "$R" --image "$REPO/api:$TAG" \
  --service-account "$SA" --execution-environment gen2 \
  --cpu 4 --memory 4Gi --min-instances 0 --max-instances 1 --no-cpu-throttling \
  --concurrency 250 --timeout 3600 --allow-unauthenticated \
  --add-cloudsql-instances "$SQL" \
  --set-env-vars "$DB_ENV,PRICE_SOURCE=postgres,CORS_ORIGINS=$CORS" \
  --set-secrets "$DB_SECRET,/secrets/presenter/passcode=fleet-sim-presenter-passcode:latest"
gcloud run services describe fleet-sim --project "$P" --region "$R" --format='value(status.url)'
