# TeamCadence

Командный Telegram-бот HostAI для группы `HostAI: Dream team` (`@FoqusTeamBot`).
Он связывает Telegram, Google Calendar и Google Sheets Tracker-HostAI:

- принимает задачи через `/new` и пишет их в Google Sheet;
- синхронизирует созвоны из Google Calendar;
- отправляет мемы, напоминания и запросы статусов;
- хранит пользователей, настройки, задачи, reminders и aura ledger;
- поддерживает отдельные Telegram topics `board`, `work`, `calls`.

## Архитектура GCP

Один Docker-образ разворачивается в три изолированных Cloud Run сервиса:

| Сервис | Назначение | Вызов |
|---|---|---|
| `teamcadence-webhook` | Telegram updates и команды | Telegram webhook + secret header |
| `teamcadence-reminder` | Мемы, напоминания, задачи и статусы | Cloud Scheduler каждую минуту |
| `teamcadence-sync` | Google Calendar → задачи/созвоны | Cloud Scheduler каждые 2 минуты |

Состояние хранится в именованной Firestore DB `teamcadence`; секреты — в Secret
Manager. Google Sheets и Calendar остаются внешними рабочими интерфейсами. AWS
Lambda/DynamoDB оставлены только как обратимый источник миграции и rollback-контур.

## Исходный production baseline

На момент переноса исходников production работает в AWS `eu-west-1`:

| Компонент | Ресурс |
|---|---|
| Telegram webhook | API Gateway → `prod-telegram-bot-team-webhook-lambda` |
| Напоминания | EventBridge `rate(1 minute)` → `prod-telegram-bot-team-reminder-lambda` |
| Calendar sync | EventBridge `rate(2 minutes)` → `prod-telegram-bot-team-sync-lambda` |
| Состояние | DynamoDB `prod-telegram-bot-team-ddb` |
| Секреты | SSM `/prod/telegram-bot-team/*` |

Код в `bot/` соответствует production-пакету AWS, обновлённому 27 августа 2026.
Секреты и экспорт production-данных в Git не входят.

## Локальный запуск

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
.venv/bin/python -m bot.main
```

## Проверка

```bash
pytest -q
python -m compileall -q bot
```

## Структура

```text
bot/
  handlers.py          Telegram-команды и callback handlers
  scheduler.py         reminders, status prompts и Calendar sync
  sheets.py            Tracker-HostAI в Google Sheets
  ddb.py               выбор storage backend
  storage/              AWS DynamoDB и GCP Firestore adapters
  cloudrun.py           Cloud Run HTTP entrypoint
  lambda_*.py          AWS Lambda entrypoints
deploy/                 bootstrap, deploy, cutover и rollback
scripts/                экспорт, импорт и сверка production state
tests/                  регрессионные тесты пользовательских flows
docs/                   архитектура и runbooks
```

Полный порядок миграции — в `docs/GCP_MIGRATION_RUNBOOK.md`.
