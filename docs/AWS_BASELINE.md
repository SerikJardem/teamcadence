# AWS production baseline

Снимок перед миграцией в GCP.

## Runtime

- Region: `eu-west-1`
- Bot: `@FoqusTeamBot`
- Group tenant: `HostAI: Dream team`
- Webhook Lambda: `prod-telegram-bot-team-webhook-lambda`
- Reminder Lambda: `prod-telegram-bot-team-reminder-lambda`
- Sync Lambda: `prod-telegram-bot-team-sync-lambda`
- DynamoDB: `prod-telegram-bot-team-ddb`
- SSM prefix: `/prod/telegram-bot-team`

## Расписания

- Reminder worker: каждую минуту.
- Calendar sync: каждые две минуты.
- Временная зона приложения: `Asia/Almaty`.

## Переключение

AWS остаётся рабочим до отдельного подтверждённого переключения Telegram webhook.
Перед cutover необходимо остановить AWS EventBridge rules, иначе два scheduler-контура
будут одновременно отправлять сообщения.
