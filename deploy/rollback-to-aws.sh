#!/usr/bin/env bash
set -euo pipefail

if [[ "${CONFIRM_ROLLBACK:-}" != "teamcadence" ]]; then
  echo "Set CONFIRM_ROLLBACK=teamcadence to restore the AWS runtime."
  exit 2
fi

PROJECT_ID="${PROJECT_ID:-hostai-505414}"
REGION="${REGION:-asia-south1}"
AWS_PROFILE="${AWS_PROFILE:-hostai}"
AWS_REGION="${AWS_REGION:-eu-west-1}"
AWS_WEBHOOK_URL="${AWS_WEBHOOK_URL:-https://l7xjkx18h2.execute-api.eu-west-1.amazonaws.com/webhook}"

gcloud scheduler jobs pause teamcadence-reminder --location "${REGION}" --project "${PROJECT_ID}"
gcloud scheduler jobs pause teamcadence-sync --location "${REGION}" --project "${PROJECT_ID}"

bot_token="$(gcloud secrets versions access latest --secret teamcadence-bot-token --project "${PROJECT_ID}")"
curl --fail --silent --show-error \
  --request POST "https://api.telegram.org/bot${bot_token}/setWebhook" \
  --data-urlencode "url=${AWS_WEBHOOK_URL}" \
  --data-urlencode "drop_pending_updates=false" >/dev/null
unset bot_token

aws events enable-rule --name prod-telegram-bot-team-reminder-rule --profile "${AWS_PROFILE}" --region "${AWS_REGION}"
aws events enable-rule --name prod-telegram-bot-team-sync-rule --profile "${AWS_PROFILE}" --region "${AWS_REGION}"
echo "TeamCadence production traffic restored to AWS."
