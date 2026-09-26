"""Конфиг. Локально читается из .env; в Lambda — из переменных окружения / SSM."""
import os

try:  # dotenv нужен только локально; в Lambda его может не быть
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # noqa: BLE001
    pass

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "bot.db")
TZ = os.getenv("TZ", "Asia/Almaty")
RUNTIME_PLATFORM = os.getenv("RUNTIME_PLATFORM", "aws").strip().lower()
STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "dynamodb").strip().lower()
GCP_PROJECT = os.getenv("GCP_PROJECT", "hostai-505414").strip()
FIRESTORE_DATABASE = os.getenv("FIRESTORE_DATABASE", "teamcadence").strip()
FIRESTORE_COLLECTION = os.getenv("FIRESTORE_COLLECTION", "partitions").strip()
SERVICE_MODE = os.getenv("SERVICE_MODE", "webhook").strip().lower()
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()

# Локальный аналог EventBridge cron 1 мин. Ставим чаще, чтобы не ждать.
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "15"))

# Правила геймификации (ledger). Держим в одном месте — потом легко крутить.
AURA_DONE = 100
AURA_LATER = 0
AURA_SKIP = -50
AURA_EXPIRE = -30

# Через сколько часов без ответа напоминание авто-протухает.
EXPIRE_AFTER_HOURS = 3

DEFAULT_ROLE = "intern"
ROLES = {"founder", "assistant", "backend", "intern"}

# Логины (без @, нижний регистр), кому можно менять настройки в ЛИЧКЕ (не только в группе).
SUPER_ADMINS = {u.strip().lower().lstrip("@")
                for u in os.getenv("SUPER_ADMINS", "bekk0zha").split(",") if u.strip()}

# --- AWS / DynamoDB (порт P1) ---
DDB_TABLE = os.getenv("DDB_TABLE", "prod-telegram-bot-team-ddb")
AWS_REGION = os.getenv("AWS_REGION", "").strip()
DDB_ENDPOINT = os.getenv("DDB_ENDPOINT", "").strip()  # для локального DynamoDB, необязательно

# Группа для напоминаний. Обычно задаётся динамически командой /setgroup в самой группе;
# это лишь фолбэк из .env (0 = не задано).
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0") or "0")
# Топик форум-группы (message_thread_id). 0 = слать в General / обычную группу.
GROUP_THREAD_ID = int(os.getenv("GROUP_THREAD_ID", "0") or "0")

# Пинги по задаче по умолчанию (смещения в секундах до дедлайна), если автор не задал «пинги:».
# -3600 = за час, 0 = в момент дедлайна.
DEFAULT_PINGS = [-3600, 0]
TOPIC_KINDS = {"board", "work", "calls"}

# Колл, касающийся не более этого числа людей из базы, шлём каждому в ЛИЧКУ.
# Больше — считаем общим коллом и постим одним сообщением в calls-топик.
CALL_DM_MAX = 2

# Ключ Tenor API — чтобы бот сам подбирал гифки по ключевым словам.
# Пусто = динамический поиск выключен (работает только статический KEYWORD_GIFS).
TENOR_API_KEY = os.getenv("TENOR_API_KEY", "").strip()

# --- Google Calendar (Sync Lambda локально) ---
# Путь к JSON service account'а и id общего календаря (обычно email календаря).
# Если не заданы — синк молча выключен, работает /newtask.
# В Lambda ключ SA приходит строкой JSON (из секрета), локально — файлом.
GOOGLE_SA_JSON = os.getenv("GOOGLE_SA_JSON", "").strip()
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "").strip()
# Фото для напоминаний о созвонах (file_id или URL). Легаси-фолбэк для события "call".
CALL_PHOTO = os.getenv("CALL_PHOTO", "").strip()

# Реестр медиа по событиям: событие -> подпись (для /media). Каждому событию можно
# привязать фото/гиф командой /setmedia <event>. Масштабируемо: добавить новое
# событие = добавить ключ сюда + вызвать media.get_media(gid, "<event>") в нужном месте.
MEDIA_EVENTS = {
    "call":     "созвон (общий фолбэк; по названию — /setcallmedia)",
    "reminder": "напоминание о задаче",
    "done":     "задача выполнена",
    "later":    "задача отложена",
    "skip":     "задача пропущена / не выполнена",
    "expired":  "задача протухла (нет ответа)",
    "push_morning": "плановый пуш (утро)",
    "push_create":  "пуш «создайте задачи»",
    "push_missing": "пуш «если задач нет»",
    "push_after_create": "после создания задачи",
    "top_aura":     "топ по ауре (в дайджесте)",
}

# Один плановый мем в день в work-топике («Задачки»), локальное HH:MM.
# Время можно переопределить настройкой pushtime:push_create.
# Картинку задаёшь /setmedia push_create (или push_missing / push_morning).
# Коллы (calls-топик + calendar memes) этим расписанием не трогаем.
PUSH_SCHEDULE = {
    "push_create": "09:30",
}

CALENDAR_LEAD_MINUTES = int(os.getenv("CALENDAR_LEAD_MINUTES", "10"))
CALENDAR_HORIZON_HOURS = int(os.getenv("CALENDAR_HORIZON_HOURS", "24"))
CALENDAR_SYNC_INTERVAL_SECONDS = int(os.getenv("CALENDAR_SYNC_INTERVAL_SECONDS", "120"))
# Стендапы: за STANDUP_BEFORE_MINUTES до начала и через STANDUP_AFTER_MINUTES после
# конца этих коллов бот пингует команду обновить статусы задач (снимок задач на сегодня
# по слотам). Матч по ключевым словам в названии события календаря (в нижнем регистре).
# слитные ключи, чтобы матчить только StandUP/SyncUP и не цеплять «sync up call» / «Sync-Up»
STANDUP_EVENTS = [s.strip().lower() for s in os.getenv(
    "STANDUP_EVENTS", "standup,syncup").split(",") if s.strip()]
STANDUP_BEFORE_MINUTES = int(os.getenv("STANDUP_BEFORE_MINUTES", "30"))
STANDUP_AFTER_MINUTES = int(os.getenv("STANDUP_AFTER_MINUTES", "30"))

# --- Google Sheet (командный таск-трекер) ---
# Один общий лист. Строки = категории по дням, колонки = люди (+ статус справа).
# SA тот же, что для календаря, но нужен scope Sheets и шер листа на SA-email как Editor.
SHEET_ID = os.getenv("SHEET_ID", "1sJ9ovDrbqmXhDFPQM6VSQQnCUWU4XjUnAtY4pLdOyV4").strip()
SHEET_TAB = os.getenv("SHEET_TAB", "").strip()  # пусто = первый лист книги

# Аббревиатура команды -> метка строки-категории в шите.
SHEET_CATEGORIES = {
    "df": "DEEP FOCUS",
    "st": "Short Tasks",
    "ma": "Maintenance",
    "md": "merge duplicate",
    "ob": "observability",
}
# Колонки-люди (как в шапке листа). Мапинг telegram->имя делается командой /iam.
SHEET_PEOPLE = ["Aru", "Bex", "Altyn", "Uldana", "Esther"]

# --- Tracker-HostAI (новая раскладка) ---
# Отдельная вкладка в том же SHEET_ID. Раскладка:
#   Date | engnr A | a-status | engnr B | b-status | engnr C | c-status
# Строки = дни («пн 10.08»), все задачи инженера за день лежат списком в одной ячейке.
# Статус в соседней ячейке общий для всего дневного списка. Категорий нет.
# Мы направляем SHEET_TAB на эту вкладку — тогда чтение/запись/статусы идут в неё.
TRACKER_TAB = os.getenv("TRACKER_TAB", "HostAI").strip()
# Слоты инженеров. /iam A|B|C привязывает юзера к слоту (вместо имени).
TRACKER_SLOTS = ["A", "B", "C"]
if not SHEET_TAB:                     # по умолчанию весь трекер работает на вкладке HostAI
    SHEET_TAB = TRACKER_TAB
# Ссылка на лист для кнопки после создания задачи (опубликованный вид или edit-URL).
TRACKER_URL = os.getenv(
    "TRACKER_URL",
    f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=0",
).strip()

# Каденс напоминаний по умолчанию (локальные времена HH:MM), если человеку не задан свой.
# Свой каденс: /cadence <Имя> 10:00,15:00 -> settings cadence:<имя>.
DEFAULT_CADENCE = [t.strip() for t in os.getenv("DEFAULT_CADENCE", "10:00,18:00").split(",") if t.strip()]
CADENCE_SCAN_SECONDS = int(os.getenv("CADENCE_SCAN_SECONDS", "60"))

# Напоминания по задачам из Sheet-трекера (/df ...).
# Дедлайн можно указать в тексте: «/df текст в 18:00» / «... 18:00» / «... !2ч».
# Без дедлайна незакрытую задачу добиваем каждые NUDGE_HOURS в течение NUDGE_MAX_HOURS.
NUDGE_HOURS = int(os.getenv("NUDGE_HOURS", "3"))
NUDGE_MAX_HOURS = int(os.getenv("NUDGE_MAX_HOURS", "9"))
# Через сколько часов после дедлайна без ответа -> штраф AURA_EXPIRE и переспросить.
DEADLINE_GRACE_HOURS = int(os.getenv("DEADLINE_GRACE_HOURS", "1"))
AURA_DF_LATER = int(os.getenv("AURA_DF_LATER", "-10"))   # «позже» по /df-задаче

# Ежедневное напоминание о задачах на сегодня (локальное HH:MM): в личку каждому +
# в group (work-топик) с разбивкой по юзерам.
DAILY_TASK_TIME = os.getenv("DAILY_TASK_TIME", "09:30")

# Календарные события-отчёты: когда событие (по ключевому слову) наступает, бот пишет
# ответственному (settings reportowner:<category>) промпт, а его ответ пишется в ячейку
# категории за сегодня. Ответственный — sheet-имя, резолвится в uid через /iam.
REPORT_EVENTS = {
    "observability": {"category": "ob",
                      "prompt": "🔍 Observability: какие ошибки/проблемы заметил? Напиши отчёт одним сообщением."},
    "merge duplicat": {"category": "md",
                       "prompt": "🔀 Merge Duplicates: сколько дубликатов объединил? Напиши число и детали."},
}
