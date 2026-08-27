# TeamCadence

Командный Telegram-бот HostAI для группы `HostAI: Dream team` (`@FoqusTeamBot`).
Он связывает Telegram, Google Calendar и Google Sheets Tracker-HostAI:

- принимает задачи через `/new` и пишет их в Google Sheet;
- синхронизирует созвоны из Google Calendar;
- отправляет мемы, напоминания и запросы статусов;
- хранит пользователей, настройки, задачи, reminders и aura ledger;
- поддерживает отдельные Telegram topics `board`, `work`, `calls`.

## Текущий production baseline

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
  ddb.py               AWS DynamoDB storage
  lambda_*.py          AWS Lambda entrypoints
tests/                  регрессионные тесты пользовательских flows
docs/                   архитектура и runbooks
```

GCP-реализация будет добавлена отдельным коммитом поверх этого baseline.
