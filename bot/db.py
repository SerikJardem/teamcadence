"""
Слой данных на SQLite. Каждая таблица тут = будущая таблица в DynamoDB:
  users, tasks, reminders, events (append-only ledger).
Порт на DDB меняет только реализацию этих функций, а не вызовы из хендлеров.
"""
import sqlite3
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    global _conn
    _conn = _connect()
    _conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username    TEXT,
            email       TEXT,               -- мост к календарю (attendee.email)
            role        TEXT NOT NULL,
            created_at  INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            title           TEXT NOT NULL,
            assignee_id     INTEGER NOT NULL,
            requester_id    INTEGER,          -- заказчик (кто создал таск)
            deadline_ts     INTEGER,
            status          TEXT NOT NULL DEFAULT 'open',   -- open | done | skipped | expired
            brief           TEXT,             -- что сделать
            result_format   TEXT,             -- в каком виде сдать
            end_ts          INTEGER,          -- конец события (для авто-удаления коллов)
            join_url        TEXT,             -- ссылка на созвон (кнопка «Присоединиться»)
            gcal_event_id   TEXT,             -- id инстанса события Google Calendar (для дедупа)
            card_chat_id    INTEGER,          -- где висит карточка таска (для обновления статуса)
            card_message_id INTEGER,
            created_at      INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reminders (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id         INTEGER NOT NULL REFERENCES tasks(id),
            assignee_id     INTEGER NOT NULL,
            fire_at_ts      INTEGER NOT NULL,
            kind            TEXT NOT NULL DEFAULT 'work',
            status          TEXT NOT NULL DEFAULT 'pending', -- pending|sent|done|skipped|expired
            sent_at         INTEGER,
            sent_chat_id    INTEGER,          -- куда отправили (для авто-удаления коллов)
            sent_message_id INTEGER,
            created_at      INTEGER NOT NULL
        );
        -- главный запрос напоминалки: все due & pending
        CREATE INDEX IF NOT EXISTS idx_rem_due ON reminders(status, fire_at_ts);

        CREATE TABLE IF NOT EXISTS events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            kind       TEXT NOT NULL,        -- done | later | skip | expire
            delta_aura INTEGER NOT NULL DEFAULT 0,
            task_id    INTEGER,
            meta       TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ev_user ON events(user_id);

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    _conn.commit()
    _migrate()


def _migrate() -> None:
    """Догоняем старые bot.db, созданные до появления email / gcal_event_id."""
    def cols(table: str) -> set[str]:
        return {r["name"] for r in _conn.execute(f"PRAGMA table_info({table})")}

    if "email" not in cols("users"):
        _conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
    if "gcal_event_id" not in cols("tasks"):
        _conn.execute("ALTER TABLE tasks ADD COLUMN gcal_event_id TEXT")
    for col, ddl in [
        ("requester_id", "requester_id INTEGER"),
        ("brief", "brief TEXT"),
        ("result_format", "result_format TEXT"),
        ("card_chat_id", "card_chat_id INTEGER"),
        ("card_message_id", "card_message_id INTEGER"),
        ("end_ts", "end_ts INTEGER"),
        ("join_url", "join_url TEXT"),
    ]:
        if col not in cols("tasks"):
            _conn.execute(f"ALTER TABLE tasks ADD COLUMN {ddl}")
    for col, ddl in [
        ("sent_chat_id", "sent_chat_id INTEGER"),
        ("sent_message_id", "sent_message_id INTEGER"),
    ]:
        if col not in cols("reminders"):
            _conn.execute(f"ALTER TABLE reminders ADD COLUMN {ddl}")
    # индекс создаём здесь (после гарантии колонки) — годится и для свежей, и для старой БД
    # ключ идемпотентности синка: одно событие на одного человека = одна задача
    _conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_task_gcal "
        "ON tasks(gcal_event_id, assignee_id) WHERE gcal_event_id IS NOT NULL"
    )
    _conn.commit()


def now_ts() -> int:
    return int(time.time())


def _q(sql: str, args: tuple = ()):  # helper: execute + commit under lock
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur


# ---------- users ----------
def upsert_user(telegram_id: int, username: str | None, role: str | None = None) -> None:
    existing = get_user(telegram_id)
    if existing:
        if role:
            _q("UPDATE users SET username=?, role=? WHERE telegram_id=?",
               (username, role, telegram_id))
        else:
            _q("UPDATE users SET username=? WHERE telegram_id=?", (username, telegram_id))
    else:
        _q("INSERT INTO users(telegram_id, username, role, created_at) VALUES(?,?,?,?)",
           (telegram_id, username, role or config.DEFAULT_ROLE, now_ts()))


def get_user(telegram_id: int):
    with _lock:
        row = _conn.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
    return row


def set_user_email(telegram_id: int, email: str) -> None:
    _q("UPDATE users SET email=? WHERE telegram_id=?", (email.strip().lower(), telegram_id))


def get_user_by_email(email: str):
    with _lock:
        return _conn.execute(
            "SELECT * FROM users WHERE email=?", (email.strip().lower(),)
        ).fetchone()


def get_user_by_username(username: str):
    uname = username.lstrip("@").strip().lower()
    with _lock:
        return _conn.execute(
            "SELECT * FROM users WHERE lower(username)=?", (uname,)
        ).fetchone()


def users_by_email() -> dict[str, int]:
    """Карта email -> telegram_id для резолва участников события."""
    with _lock:
        rows = _conn.execute(
            "SELECT email, telegram_id FROM users WHERE email IS NOT NULL AND email <> ''"
        ).fetchall()
    return {r["email"]: r["telegram_id"] for r in rows}


def all_users():
    with _lock:
        return _conn.execute("SELECT * FROM users ORDER BY role").fetchall()


# ---------- settings ----------
def set_setting(key: str, value: str) -> None:
    _q("INSERT INTO settings(key, value) VALUES(?,?) "
       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def get_setting(key: str) -> str | None:
    with _lock:
        row = _conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def get_target_group() -> int | None:
    """Куда слать напоминания: сохранённая через /setgroup группа, иначе GROUP_CHAT_ID из .env."""
    v = get_setting("group_chat_id")
    if v:
        return int(v)
    return config.GROUP_CHAT_ID or None


def get_target_thread() -> int | None:
    """Топик форум-группы (message_thread_id). None = обычная группа / General."""
    v = get_setting("group_thread_id")
    if v:
        return int(v)
    return config.GROUP_THREAD_ID or None


# ---------- топики по видам (board / work / calls) ----------
def set_topic(kind: str, chat_id: int, thread_id: int | None) -> None:
    set_setting(f"topic_{kind}_chat", str(chat_id))
    set_setting(f"topic_{kind}_thread", str(thread_id) if thread_id else "")


def get_topic(kind: str) -> tuple[int, int | None] | None:
    chat = get_setting(f"topic_{kind}_chat")
    if not chat:
        return None
    thread = get_setting(f"topic_{kind}_thread")
    return int(chat), (int(thread) if thread else None)


def dest_for_kind(reminder_kind: str) -> tuple[int, int | None] | None:
    """Куда слать напоминание данного вида. Цепочка: спец.топик -> общая группа -> None(=ЛС)."""
    topic_kind = "calls" if reminder_kind == "call" else "work"
    t = get_topic(topic_kind)
    if t:
        return t
    g = get_target_group()
    if g:
        return g, get_target_thread()
    return None


# ---------- tasks + reminders ----------
def create_task(title: str, assignee_id: int, deadline_ts: int) -> int:
    cur = _q(
        "INSERT INTO tasks(title, assignee_id, deadline_ts, created_at) VALUES(?,?,?,?)",
        (title, assignee_id, deadline_ts, now_ts()),
    )
    return cur.lastrowid


def create_task_full(title: str, assignee_id: int, requester_id: int, deadline_ts: int,
                     brief: str, result_format: str) -> int:
    cur = _q(
        "INSERT INTO tasks(title, assignee_id, requester_id, deadline_ts, brief, "
        "result_format, created_at) VALUES(?,?,?,?,?,?,?)",
        (title, assignee_id, requester_id, deadline_ts, brief, result_format, now_ts()),
    )
    return cur.lastrowid


def get_task(task_id: int):
    with _lock:
        return _conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()


def set_task_card(task_id: int, chat_id: int, message_id: int) -> None:
    _q("UPDATE tasks SET card_chat_id=?, card_message_id=? WHERE id=?",
       (chat_id, message_id, task_id))


def create_reminder(task_id: int, assignee_id: int, fire_at_ts: int, kind: str = "work") -> int:
    cur = _q(
        "INSERT INTO reminders(task_id, assignee_id, fire_at_ts, kind, created_at) "
        "VALUES(?,?,?,?,?)",
        (task_id, assignee_id, fire_at_ts, kind, now_ts()),
    )
    return cur.lastrowid


def upsert_gcal_task(event_id: str, title: str, assignee_id: int, deadline_ts: int,
                     end_ts: int | None = None, join_url: str | None = None):
    """Идемпотентно по (event_id, assignee). Возвращает (task_id, created).
    Для коллов assignee — организатор/0, одно событие = одна задача (без фан-аута)."""
    with _lock:
        row = _conn.execute(
            "SELECT id FROM tasks WHERE gcal_event_id=? AND assignee_id=?",
            (event_id, assignee_id),
        ).fetchone()
        if row:
            _conn.execute(
                "UPDATE tasks SET title=?, deadline_ts=?, end_ts=?, join_url=? WHERE id=?",
                (title, deadline_ts, end_ts, join_url, row["id"]),
            )
            _conn.commit()
            return row["id"], False
        cur = _conn.execute(
            "INSERT INTO tasks(title, assignee_id, deadline_ts, end_ts, join_url, "
            "gcal_event_id, created_at) VALUES(?,?,?,?,?,?,?)",
            (title, assignee_id, deadline_ts, end_ts, join_url, event_id, now_ts()),
        )
        _conn.commit()
        return cur.lastrowid, True


def ensure_reminder(task_id: int, assignee_id: int, fire_at_ts: int, kind: str = "work") -> None:
    """Одно напоминание на задачу. Если ещё pending — двигаем время под новый старт события.
    Если уже sent/done/skipped — не трогаем (не переспамливаем)."""
    with _lock:
        row = _conn.execute(
            "SELECT id, status FROM reminders WHERE task_id=? ORDER BY id LIMIT 1",
            (task_id,),
        ).fetchone()
        if row is None:
            _conn.execute(
                "INSERT INTO reminders(task_id, assignee_id, fire_at_ts, kind, created_at) "
                "VALUES(?,?,?,?,?)",
                (task_id, assignee_id, fire_at_ts, kind, now_ts()),
            )
        elif row["status"] == "pending":
            _conn.execute(
                "UPDATE reminders SET fire_at_ts=? WHERE id=?", (fire_at_ts, row["id"])
            )
        _conn.commit()


def get_reminder(rid: int):
    with _lock:
        return _conn.execute(
            "SELECT r.*, t.title AS task_title, t.status AS task_status "
            "FROM reminders r JOIN tasks t ON t.id = r.task_id WHERE r.id=?",
            (rid,),
        ).fetchone()


def due_reminders(now: int):
    """Ядро идемпотентности: берём только pending, у которых время пришло."""
    with _lock:
        return _conn.execute(
            "SELECT r.*, t.title AS task_title, t.deadline_ts AS task_deadline, "
            "t.join_url AS task_join_url "
            "FROM reminders r JOIN tasks t ON t.id = r.task_id "
            "WHERE r.status='pending' AND r.fire_at_ts <= ? ORDER BY r.fire_at_ts",
            (now,),
        ).fetchall()


def stale_sent_reminders(older_than_ts: int):
    """Личные таски, отправленные, но без ответа дольше N — кандидаты на авто-протухание.
    Коллы сюда не попадают: у них нет кнопок и их убирает отдельный проход."""
    with _lock:
        return _conn.execute(
            "SELECT * FROM reminders WHERE kind='work' AND status='sent' AND sent_at <= ?",
            (older_than_ts,),
        ).fetchall()


def mark_reminder(rid: int, status: str, sent_at: int | None = None) -> None:
    if sent_at is not None:
        _q("UPDATE reminders SET status=?, sent_at=? WHERE id=?", (status, sent_at, rid))
    else:
        _q("UPDATE reminders SET status=? WHERE id=?", (status, rid))


def mark_reminder_sent(rid: int, sent_at: int, chat_id: int, message_id: int) -> None:
    """Пометить отправленным и запомнить, где висит сообщение (нужно для авто-удаления коллов)."""
    _q("UPDATE reminders SET status='sent', sent_at=?, sent_chat_id=?, sent_message_id=? "
       "WHERE id=?", (sent_at, chat_id, message_id, rid))


def stale_call_messages(now: int):
    """Коллы, которые уже закончились (end_ts прошёл) и чьё сообщение пора удалить."""
    with _lock:
        return _conn.execute(
            "SELECT r.id, r.sent_chat_id, r.sent_message_id "
            "FROM reminders r JOIN tasks t ON t.id = r.task_id "
            "WHERE r.kind IN ('call','call_dm') AND r.status='sent' "
            "AND r.sent_message_id IS NOT NULL "
            "AND COALESCE(t.end_ts, t.deadline_ts) <= ?",
            (now,),
        ).fetchall()


def reschedule_reminder(rid: int, fire_at_ts: int) -> None:
    # снова pending -> сканер подхватит в нужный момент
    _q("UPDATE reminders SET status='pending', fire_at_ts=?, sent_at=NULL WHERE id=?",
       (fire_at_ts, rid))


def set_task_status(task_id: int, status: str) -> None:
    _q("UPDATE tasks SET status=? WHERE id=?", (status, task_id))


# ---------- ledger ----------
def add_event(user_id: int, kind: str, delta_aura: int, task_id: int | None = None,
              meta: str | None = None) -> None:
    _q("INSERT INTO events(user_id, kind, delta_aura, task_id, meta, created_at) "
       "VALUES(?,?,?,?,?,?)",
       (user_id, kind, delta_aura, task_id, meta, now_ts()))


def user_aura(user_id: int) -> int:
    with _lock:
        row = _conn.execute(
            "SELECT COALESCE(SUM(delta_aura),0) AS a FROM events WHERE user_id=?",
            (user_id,),
        ).fetchone()
    return int(row["a"])


def leaderboard(limit: int = 10):
    with _lock:
        return _conn.execute(
            "SELECT e.user_id, COALESCE(SUM(e.delta_aura),0) AS aura, "
            "       COALESCE(u.username, CAST(e.user_id AS TEXT)) AS name "
            "FROM events e LEFT JOIN users u ON u.telegram_id = e.user_id "
            "GROUP BY e.user_id ORDER BY aura DESC LIMIT ?",
            (limit,),
        ).fetchall()


# ---------- digest ----------
def _today_bounds() -> tuple[int, int]:
    tz = ZoneInfo(config.TZ)
    now = datetime.now(tz)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), int(now.timestamp())


def founder_digest():
    """Сводка по каждому: сколько задач сегодня и сколько закрыто."""
    start, _ = _today_bounds()
    with _lock:
        return _conn.execute(
            "SELECT r.assignee_id, "
            "       COALESCE(u.username, CAST(r.assignee_id AS TEXT)) AS name, "
            "       COUNT(*) AS total, "
            "       SUM(CASE WHEN r.status='done' THEN 1 ELSE 0 END) AS done "
            "FROM reminders r LEFT JOIN users u ON u.telegram_id = r.assignee_id "
            "WHERE r.created_at >= ? "
            "GROUP BY r.assignee_id ORDER BY name",
            (start,),
        ).fetchall()
