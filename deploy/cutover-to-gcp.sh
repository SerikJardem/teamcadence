#!/usr/bin/env bash
set -euo pipefail

if [[ "${CONFIRM_CUTOVER:-}" != "teamcadence" ]]; then
  echo "Set CONFIRM_CUTOVER=teamcadence to switch the production Telegram bot."
  exit 2
fi

PROJECT_ID="${PROJECT_ID:-hostai-505414}"
REGION="${REGION:-asia-south1}"
AWS_PROFILE="${AWS_PROFILE:-hostai}"
AWS_REGION="${AWS_REGION:-eu-west-1}"
WEBHOOK_URL="$(gcloud run services describe teamcadence-webhook --region "${REGION}" --project "${PROJECT_ID}" --format='value(status.url)')/webhook"
AWS_WEBHOOK_URL="${AWS_WEBHOOK_URL:-https://l7xjkx18h2.execute-api.eu-west-1.amazonaws.com/webhook}"
SNAPSHOT="${SNAPSHOT:-.migration/aws-state-cutover.json}"
if [[ -x .venv/bin/python ]]; then
  PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
else
  PYTHON_BIN="${PYTHON_BIN:-python3}"
fi

bot_token="$(gcloud secrets versions access latest --secret teamcadence-bot-token --project "${PROJECT_ID}")"
webhook_secret="$(gcloud secrets versions access latest --secret teamcadence-webhook-secret --project "${PROJECT_ID}")"

rollback_on_error() {
  status=$?
  if [[ ${status} -eq 0 ]]; then
    return
  fi
  echo "Cutover failed; restoring AWS runtime." >&2
  gcloud scheduler jobs pause teamcadence-reminder --location "${REGION}" --project "${PROJECT_ID}" >/dev/null 2>&1 || true
  gcloud scheduler jobs pause teamcadence-sync --location "${REGION}" --project "${PROJECT_ID}" >/dev/null 2>&1 || true
  curl --silent --show-error --request POST \
    "https://api.telegram.org/bot${bot_token}/setWebhook" \
    --data-urlencode "url=${AWS_WEBHOOK_URL}" \
    --data-urlencode "drop_pending_updates=false" >/dev/null || true
  aws events enable-rule --name prod-telegram-bot-team-reminder-rule --profile "${AWS_PROFILE}" --region "${AWS_REGION}" || true
  aws events enable-rule --name prod-telegram-bot-team-sync-rule --profile "${AWS_PROFILE}" --region "${AWS_REGION}" || true
  exit "${status}"
}
trap rollback_on_error ERR

# Freeze incoming updates without dropping them, then stop AWS background writes.
curl --fail --silent --show-error --request POST \
  "https://api.telegram.org/bot${bot_token}/deleteWebhook" \
  --data-urlencode "drop_pending_updates=false" | jq -e '.ok == true' >/dev/null
aws events disable-rule --name prod-telegram-bot-team-reminder-rule --profile "${AWS_PROFILE}" --region "${AWS_REGION}"
aws events disable-rule --name prod-telegram-bot-team-sync-rule --profile "${AWS_PROFILE}" --region "${AWS_REGION}"

# Copy the authoritative final state while both runtimes are quiescent.
"${PYTHON_BIN}" scripts/export_aws_state.py \
  --profile "${AWS_PROFILE}" --region "${AWS_REGION}" --output "${SNAPSHOT}"
"${PYTHON_BIN}" scripts/import_firestore_state.py \
  --input "${SNAPSHOT}" --project "${PROJECT_ID}" --delete-extra
"${PYTHON_BIN}" scripts/verify_firestore_state.py \
  --input "${SNAPSHOT}" --project "${PROJECT_ID}"

curl --fail --silent --show-error \
  --request POST "https://api.telegram.org/bot${bot_token}/setWebhook" \
  --data-urlencode "url=${WEBHOOK_URL}" \
  --data-urlencode "secret_token=${webhook_secret}" \
  --data-urlencode "drop_pending_updates=false" | jq -e '.ok == true' >/dev/null

gcloud scheduler jobs resume teamcadence-reminder --location "${REGION}" --project "${PROJECT_ID}"
gcloud scheduler jobs resume teamcadence-sync --location "${REGION}" --project "${PROJECT_ID}"

unset bot_token webhook_secret
trap - ERR
echo "TeamCadence production traffic now points to GCP: ${WEBHOOK_URL}"
