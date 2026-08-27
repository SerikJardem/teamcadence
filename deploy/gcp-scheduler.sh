#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-hostai-505414}"
REGION="${REGION:-asia-south1}"
RUNTIME_SA="teamcadence-runtime@${PROJECT_ID}.iam.gserviceaccount.com"

for mode in reminder sync; do
  service="teamcadence-${mode}"
  uri="$(gcloud run services describe "${service}" --region "${REGION}" --project "${PROJECT_ID}" --format='value(status.url)')/run"
  gcloud run services add-iam-policy-binding "${service}" \
    --region "${REGION}" --project "${PROJECT_ID}" \
    --member "serviceAccount:${RUNTIME_SA}" --role roles/run.invoker --quiet >/dev/null
  schedule="* * * * *"
  if [[ "${mode}" == "sync" ]]; then
    schedule="*/2 * * * *"
  fi
  if gcloud scheduler jobs describe "teamcadence-${mode}" --location "${REGION}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud scheduler jobs update http "teamcadence-${mode}" \
      --location "${REGION}" --project "${PROJECT_ID}" \
      --schedule "${schedule}" --time-zone "Asia/Almaty" \
      --uri "${uri}" --http-method POST \
      --oidc-service-account-email "${RUNTIME_SA}" --oidc-token-audience "${uri%/run}"
  else
    gcloud scheduler jobs create http "teamcadence-${mode}" \
      --location "${REGION}" --project "${PROJECT_ID}" \
      --schedule "${schedule}" --time-zone "Asia/Almaty" \
      --uri "${uri}" --http-method POST \
      --oidc-service-account-email "${RUNTIME_SA}" --oidc-token-audience "${uri%/run}"
  fi
  gcloud scheduler jobs pause "teamcadence-${mode}" --location "${REGION}" --project "${PROJECT_ID}"
done

echo "Cloud Scheduler jobs are configured and PAUSED."
