# TeamCadence

Командный Telegram-бот HostAI для группы `HostAI: Dream team`
(`@FoqusTeamBot`). Бот принимает задачи, работает с Google Calendar и
Tracker-HostAI в Google Sheets, отправляет напоминания, мемы и запросы статусов.

## Где работает production

Production работает в GCP:

- проект: `hostai-505414`;
- регион: `asia-south1`;
- данные: Firestore database `teamcadence`;
- секреты: Secret Manager;
- Docker-образы: Artifact Registry `hostai-services/teamcadence`.

Один Docker-образ независимо запускается в трёх Cloud Run services. У каждого
сервиса собственные revisions и экземпляры, а роль процесса задаёт
`SERVICE_MODE`.

| Cloud Run service | Что делает | Кто вызывает |
|---|---|---|
| `teamcadence-webhook` | Принимает Telegram updates и выполняет команды | Telegram webhook |
| `teamcadence-reminder` | Отправляет мемы, напоминания и запросы статусов | Cloud Scheduler каждую минуту |
| `teamcadence-sync` | Синхронизирует Google Calendar с задачами и созвонами | Cloud Scheduler каждые 2 минуты |

Google Sheets и Google Calendar остаются внешними рабочими интерфейсами.

## Как проходит изменение

```text
Изменение кода
    ↓
Commit и push в main
    ↓
GitHub Actions: Ruff + тесты + compileall + тестовая сборка Docker
    ↓
Ручной запуск deploy/gcp-deploy.sh
    ↓
Cloud Build собирает и публикует образ с тегом Git SHA
    ↓
Три Cloud Run service получают новую revision
    ↓
Проверка health, Scheduler, Telegram webhook и логов
```

Важно: push в GitHub **не обновляет production**. Workflow
`.github/workflows/ci.yml` только проверяет код. Cloud Build Trigger и
автоматический deploy для репозитория не настроены.

Для выпуска новой версии не нужно открывать интерфейс GCP Console. Деплой
запускается из терминала в чистом checkout репозитория через `gcloud`.

## 1. Подготовить изменение

```bash
git switch main
git pull --ff-only

python -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

Внести изменение и проверить его:

```bash
.venv/bin/ruff check .
.venv/bin/pytest -q
.venv/bin/python -m compileall -q bot scripts
docker build -t teamcadence:local .
```

После проверки создать commit и отправить его в `main`. Дождаться зелёного CI
в GitHub Actions.

## 2. Подготовить доступ к GCP

Нужен `gcloud` и права на Cloud Build, Artifact Registry, Cloud Run и Secret
Manager в проекте `hostai-505414`.

```bash
gcloud auth login
gcloud config set project hostai-505414
gcloud config set run/region asia-south1
```

Проверить активный аккаунт и проект:

```bash
gcloud auth list --filter=status:ACTIVE
gcloud config get-value project
```

## 3. Развернуть production

Перед деплоем checkout должен быть чистым. Скрипт отправляет в Cloud Build
содержимое **текущей локальной папки**, а не скачивает код из GitHub.

```bash
git status --short
git rev-parse HEAD
```

Если `git status --short` ничего не вывел, запустить:

```bash
PROJECT_ID=hostai-505414 \
REGION=asia-south1 \
./deploy/gcp-deploy.sh
```

Скрипт:

1. собирает Docker-образ в Cloud Build;
2. публикует его как
   `asia-south1-docker.pkg.dev/hostai-505414/hostai-services/teamcadence:<git-sha>`;
3. создаёт новые revisions для `webhook`, `reminder` и `sync`;
4. направляет трафик сервисов на новые revisions.

Скрипт не меняет Telegram webhook и расписание Cloud Scheduler. Они уже
настроены на GCP и продолжают работать после обновления revisions.

## 4. Проверить production после деплоя

Проверить образ, активную revision и процент трафика:

```bash
for service in teamcadence-webhook teamcadence-reminder teamcadence-sync; do
  gcloud run services describe "$service" \
    --project hostai-505414 \
    --region asia-south1 \
    --format='value(metadata.name,status.latestReadyRevisionName,status.traffic[0].percent,spec.template.spec.containers[0].image)'
done
```

Проверить расписания:

```bash
gcloud scheduler jobs list \
  --project hostai-505414 \
  --location asia-south1 \
  --filter='name:teamcadence' \
  --format='table(name.basename(),state,schedule,timeZone,httpTarget.uri)'
```

Проверить health webhook-сервиса:

```bash
SERVICE_URL="$(gcloud run services describe teamcadence-webhook \
  --project hostai-505414 \
  --region asia-south1 \
  --format='value(status.url)')"
curl --fail --silent --show-error "$SERVICE_URL/health"
```

Ожидаемый ответ:

```json
{"ok":true,"service_mode":"webhook","storage":"firestore"}
```

Проверить свежие логи всех трёх процессов:

```bash
for service in teamcadence-webhook teamcadence-reminder teamcadence-sync; do
  gcloud run services logs read "$service" \
    --project hostai-505414 \
    --region asia-south1 \
    --limit 50
done
```

После технической проверки выполнить короткий Telegram smoke-test: `/start`,
создание одной тестовой задачи и проверка одного планового сообщения.

## Откат версии

Cloud Run сохраняет предыдущие revisions и образы. Для отката нужно выбрать
последний исправный тег образа и обновить **все три** сервиса на один и тот же
образ:

```bash
IMAGE="asia-south1-docker.pkg.dev/hostai-505414/hostai-services/teamcadence:<previous-git-sha>"

for service in teamcadence-webhook teamcadence-reminder teamcadence-sync; do
  gcloud run services update "$service" \
    --project hostai-505414 \
    --region asia-south1 \
    --image "$IMAGE"
done
```

После отката повторить проверки из предыдущего раздела.

## Секреты production

Cloud Run получает секреты из Secret Manager:

| Переменная | Secret Manager secret |
|---|---|
| `BOT_TOKEN` | `teamcadence-bot-token` |
| `GOOGLE_SA_JSON` | `teamcadence-google-sa-json` |
| `TELEGRAM_WEBHOOK_SECRET` | `teamcadence-webhook-secret` |

Значения секретов нельзя добавлять в `.env`, README, GitHub commits или логи.
Обновление версии секрета не требует изменения исходного кода, но после замены
нужно создать новую Cloud Run revision, чтобы экземпляры получили актуальное
значение.

## Локальный запуск

```bash
cp .env.example .env
.venv/bin/python -m bot.main
```

Локальный `.env` не является конфигурацией production.

## Структура репозитория

```text
bot/
  handlers.py           Telegram-команды и callback handlers
  scheduler.py          reminders, status prompts и Calendar sync
  sheets.py             Tracker-HostAI в Google Sheets
  storage/firestore.py  хранение состояния в Firestore
  cloudrun.py            HTTP entrypoint для Cloud Run
.github/workflows/
  ci.yml                 проверки pull request и main без деплоя
deploy/
  gcp-deploy.sh          ручная сборка и обновление Cloud Run
  gcp-scheduler.sh       настройка расписаний; после создания ставит их на паузу
tests/                   регрессионные тесты пользовательских flows
docs/                    дополнительная техническая документация
```
