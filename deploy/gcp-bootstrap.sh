#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-hostai-505414}"
REGION="${REGION:-asia-south1}"
DATABASE="${DATABASE:-teamcadence}"
RUNTIME_SA="teamcadence-runtime@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  firestore.googleapis.com \
  secretmanager.googleapis.com \
  cloudscheduler.googleapis.com \
  --project "${PROJECT_ID}"

if ! gcloud firestore databases describe --database "${DATABASE}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud firestore databases create \
    --database "${DATABASE}" \
    --location "${REGION}" \
    --type firestore-native \
    --delete-protection \
    --project "${PROJECT_ID}"
fi

if ! gcloud iam service-accounts describe "${RUNTIME_SA}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud iam service-accounts create teamcadence-runtime \
    --display-name "TeamCadence Cloud Run runtime" \
    --project "${PROJECT_ID}"
fi

for role in roles/datastore.user roles/secretmanager.secretAccessor roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member "serviceAccount:${RUNTIME_SA}" \
    --role "${role}" \
    --condition None \
    --quiet >/dev/null
done

for secret in teamcadence-bot-token teamcadence-google-sa-json teamcadence-webhook-secret; do
  if ! gcloud secrets describe "${secret}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud secrets create "${secret}" --replication-policy automatic --project "${PROJECT_ID}"
  fi
done

echo "GCP TeamCadence foundation is ready in ${PROJECT_ID}/${REGION}."
