#!/usr/bin/env bash
# Build the backend image with Cloud Build (not on the laptop) and deploy the Cloud Run service.
# The service needs no database: its data is baked into the image (Dockerfile, data/frozen).
# Usage: scripts/cloud/deploy.sh [extra CORS origin, e.g. https://fleet-sim.vercel.app]
set -euo pipefail
cd "$(dirname "$0")/../.."
source scripts/cloud/env.sh
CORS="${1:-}"
for d in data/frozen/forecasts data/frozen/predictions; do  # not in git (data/frozen/README.md)
  [ -d "$d" ] || { echo "$d is missing: copy it from gs://appintellect-fleet-sim-backup/frozen/ first (data/frozen/README.md)" >&2; exit 1; }
done
gcloud builds submit --project "$P" --region "$R" --tag "$REPO/api:$TAG" .
gcloud run deploy fleet-sim --project "$P" --region "$R" --image "$REPO/api:$TAG" \
  --service-account "$SA" --execution-environment gen2 \
  --cpu 4 --memory 4Gi --min-instances 0 --max-instances 1 --no-cpu-throttling \
  --concurrency 250 --timeout 3600 --allow-unauthenticated \
  --clear-cloudsql-instances --clear-secrets \
  --set-env-vars "CORS_ORIGINS=$CORS"
gcloud run services describe fleet-sim --project "$P" --region "$R" --format='value(status.url)'
