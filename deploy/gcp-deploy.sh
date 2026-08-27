#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-hostai-505414}"
REGION="${REGION:-asia-south1}"
REPOSITORY="${REPOSITORY:-hostai-services}"
TAG="${TAG:-$(git rev-parse --short HEAD)}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/teamcadence:${TAG}"
RUNTIME_SA="teamcadence-runtime@${PROJECT_ID}.iam.gserviceaccount.com"
COMMON_ENV="RUNTIME_PLATFORM=gcp,STORAGE_BACKEND=firestore,GCP_PROJECT=${PROJECT_ID},FIRESTORE_DATABASE=teamcadence,TZ=Asia/Almaty"
SECRETS="BOT_TOKEN=teamcadence-bot-token:latest,GOOGLE_SA_JSON=teamcadence-google-sa-json:latest,TELEGRAM_WEBHOOK_SECRET=teamcadence-webhook-secret:latest"

gcloud builds submit --tag "${IMAGE}" --project "${PROJECT_ID}" .

gcloud run deploy teamcadence-webhook \
  --image "${IMAGE}" --region "${REGION}" --project "${PROJECT_ID}" \
  --service-account "${RUNTIME_SA}" --allow-unauthenticated \
  --set-env-vars "${COMMON_ENV},SERVICE_MODE=webhook" --set-secrets "${SECRETS}" \
  --min 0 --max 3 --concurrency 20 --timeout 60

for mode in reminder sync; do
  gcloud run deploy "teamcadence-${mode}" \
    --image "${IMAGE}" --region "${REGION}" --project "${PROJECT_ID}" \
    --service-account "${RUNTIME_SA}" --no-allow-unauthenticated \
    --set-env-vars "${COMMON_ENV},SERVICE_MODE=${mode}" --set-secrets "${SECRETS}" \
    --min 0 --max 1 --concurrency 1 --timeout 300
done

echo "Deployed ${IMAGE}. Scheduler and Telegram cutover are separate guarded steps."
