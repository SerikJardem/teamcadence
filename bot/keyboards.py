"""Инлайн-клавиатуры и типизированные callback_data (влезают в лимит 64 байта).
gid (тенант) кодируется в callback_data — так тенант читается даже из лички."""
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                           ReplyKeyboardMarkup)

from . import config


class ActCB(CallbackData, prefix="act"):
    action: str   # done | later | skip
    gid: int      # тенант (id группы)
    rid: str      # id напоминания


class LaterCB(CallbackData, prefix="later"):
    gid: int
    rid: str
    minutes: int


def reminder_kb(gid: int, rid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔥 Done", callback_data=ActCB(action="done", gid=gid, rid=rid).pack()),
        InlineKeyboardButton(text="⌛ Later", callback_data=ActCB(action="later", gid=gid, rid=rid).pack()),
        InlineKeyboardButton(text="❌ Skip", callback_data=ActCB(action="skip", gid=gid, rid=rid).pack()),
    ]])


def later_kb(gid: int, rid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="15 мин", callback_data=LaterCB(gid=gid, rid=rid, minutes=15).pack()),
        InlineKeyboardButton(text="30 мин", callback_data=LaterCB(gid=gid, rid=rid, minutes=30).pack()),
        InlineKeyboardButton(text="1 час", callback_data=LaterCB(gid=gid, rid=rid, minutes=60).pack()),
    ]])


class SheetCB(CallbackData, prefix="shst"):
    """Статус записи в Google Sheet. row/col — координаты статус-ячейки (0-based),
    line — индекс строки внутри ячейки (несколько задач в одной ячейке = свой статус)."""
    action: str   # done | later | skipped
    gid: int
    row: int
    col: int
    line: int


class SheetEditCB(CallbackData, prefix="shed"):
    """Изменение/удаление задачи в трекере. col — СТАТУС-колонка (как в SheetCB);
    колонка текста задачи = col-1 (текст слева от статуса)."""
    action: str   # edit | del
    gid: int
    row: int
    col: int
    line: int


def sheet_status_kb(gid: int, row: int, col: int, line: int = 0) -> InlineKeyboardMarkup:
    def cb(a):
        return SheetCB(action=a, gid=gid, row=row, col=col, line=line).pack()

    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ done", callback_data=cb("done")),
        InlineKeyboardButton(text="🕓 later", callback_data=cb("later")),
        InlineKeyboardButton(text="❌ skipped", callback_data=cb("skipped")),
    ]])


class WizCB(CallbackData, prefix="wiz"):
    """Шаг мастера /new. step: cat|date|time ; val: значение (без ':' — это разделитель CallbackData)."""
    step: str
    val: str


def wiz_category_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=WizCB(step="cat", val=abbr).pack())]
            for abbr, label in config.SHEET_CATEGORIES.items()]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def wiz_date_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сегодня", callback_data=WizCB(step="date", val="today").pack()),
         InlineKeyboardButton(text="Завтра", callback_data=WizCB(step="date", val="tomorrow").pack())],
        [InlineKeyboardButton(text="📅 Своя дата", callback_data=WizCB(step="date", val="custom").pack())],
    ])


def wiz_time_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Без срока", callback_data=WizCB(step="time", val="none").pack())],
        [InlineKeyboardButton(text="+1ч", callback_data=WizCB(step="time", val="1h").pack()),
         InlineKeyboardButton(text="+2ч", callback_data=WizCB(step="time", val="2h").pack()),
         InlineKeyboardButton(text="+3ч", callback_data=WizCB(step="time", val="3h").pack())],
        [InlineKeyboardButton(text="к 18:00", callback_data=WizCB(step="time", val="1800").pack()),
         InlineKeyboardButton(text="🕒 Своё время", callback_data=WizCB(step="time", val="custom").pack())],
    ])


class SheetLaterCB(CallbackData, prefix="shlt"):
    """Насколько отложить /df-задачу (снуз дедлайна)."""
    gid: int
    row: int
    col: int
    line: int
    minutes: int


def sheet_later_kb(gid: int, row: int, col: int, line: int) -> InlineKeyboardMarkup:
    def cb(m):
        return SheetLaterCB(gid=gid, row=row, col=col, line=line, minutes=m).pack()
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="15 мин", callback_data=cb(15)),
        InlineKeyboardButton(text="30 мин", callback_data=cb(30)),
        InlineKeyboardButton(text="1 ч", callback_data=cb(60)),
        InlineKeyboardButton(text="3 ч", callback_data=cb(180)),
    ]])


def call_join_kb(url: str) -> InlineKeyboardMarkup:
    """URL-кнопка (открывает ссылку на созвон, не колбэк)."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔗 Присоединиться", url=url),
    ]])


def tracker_link_kb(url: str) -> InlineKeyboardMarkup | None:
    """Ссылка на командный Google Sheet после создания задачи."""
    if not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📋 Открыть трекер", url=url),
    ]])


# ---------- главное меню (инлайн вместо команд) ----------
class MenuCB(CallbackData, prefix="mnu"):
    action: str        # home | iam | iamset | new | status | today
    val: str = ""       # для iamset — слот A|B|C


class MenuPickCB(CallbackData, prefix="mpk"):
    """Выбор задачи в меню «изменить статус». col — статус-колонка."""
    row: int
    col: int
    line: int


_MARK = {"done": "✅", "skipped": "❌", "later": "🕓"}


def main_menu_kb() -> InlineKeyboardMarkup:
    def b(text, action):
        return InlineKeyboardButton(text=text, callback_data=MenuCB(action=action).pack())
    return InlineKeyboardMarkup(inline_keyboard=[
        [b("👤 Регистрация инженера", "iam")],
        [b("➕ Поставить задачу", "new")],
        [b("📊 Изменить статус задачи", "status")],
        [b("📋 Мои задачи на сегодня", "today")],
    ])


# ---------- постоянная нижняя клавиатура (ReplyKeyboard): весь флоу снизу ----------
BTN_NEW = "➕ Поставить задачу"
BTN_STATUS = "📊 Изменить статус"
BTN_DELETE = "🗑 Удалить задачу"
BTN_IAM = "👤 Регистрация"
BTN_TODAY = "📋 Мои задачи"
BTN_BACK = "⬅️ Назад"
BTN_CANCEL = "⬅️ Отмена"
# выбор дня
BTN_D_TODAY = "Сегодня"
BTN_D_TOMORROW = "Завтра"
BTN_D_OTHER = "📅 Другая дата"
# напоминание
BTN_R_NONE = "Без напоминания"
BTN_R_1H, BTN_R_2H, BTN_R_3H = "+1ч", "+2ч", "+3ч"
# статус
BTN_ST_DONE = "✅ done"
BTN_ST_LATER = "🕓 later"
BTN_ST_SKIP = "❌ skipped"
# подтверждение удаления
BTN_DEL_YES = "🗑 Да, удалить"

# главные кнопки, запускающие ветку из любого состояния
MAIN_BUTTONS = {BTN_NEW, BTN_STATUS, BTN_DELETE, BTN_IAM, BTN_TODAY}
NAV_BUTTONS = {BTN_BACK, BTN_CANCEL}
SLOT_BUTTONS = {f"engnr {s}": s for s in config.TRACKER_SLOTS}
DAY_BUTTONS = {BTN_D_TODAY, BTN_D_TOMORROW, BTN_D_OTHER}
REMIND_MAP = {BTN_R_NONE: "none", BTN_R_1H: "1h", BTN_R_2H: "2h", BTN_R_3H: "3h"}
STATUS_MAP = {BTN_ST_DONE: "done", BTN_ST_LATER: "later", BTN_ST_SKIP: "skipped"}
_MARKS = {"done": "✅", "skipped": "❌", "later": "🕓"}


def _rk(rows, placeholder=None) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
        resize_keyboard=True, is_persistent=True, input_field_placeholder=placeholder)


def main_reply_kb() -> ReplyKeyboardMarkup:
    return _rk([[BTN_NEW], [BTN_STATUS], [BTN_DELETE], [BTN_IAM, BTN_TODAY]],
               "Выбери действие")


def slot_reply_kb() -> ReplyKeyboardMarkup:
    return _rk([[f"engnr {s}" for s in config.TRACKER_SLOTS], [BTN_BACK]], "Выбери слот")


def day_reply_kb() -> ReplyKeyboardMarkup:
    return _rk([[BTN_D_TODAY, BTN_D_TOMORROW], [BTN_D_OTHER], [BTN_CANCEL]], "На какой день?")


def remind_reply_kb() -> ReplyKeyboardMarkup:
    return _rk([[BTN_R_NONE], [BTN_R_1H, BTN_R_2H, BTN_R_3H], [BTN_CANCEL]], "Напоминание?")


def status_reply_kb() -> ReplyKeyboardMarkup:
    return _rk([[BTN_ST_DONE, BTN_ST_LATER, BTN_ST_SKIP], [BTN_BACK]], "Отметь статус")


def confirm_del_reply_kb() -> ReplyKeyboardMarkup:
    return _rk([[BTN_DEL_YES], [BTN_BACK]], "Удалить задачу?")


def cancel_reply_kb() -> ReplyKeyboardMarkup:
    return _rk([[BTN_CANCEL]], "Напиши текст")


def tasks_reply_kb(tasks: list) -> ReplyKeyboardMarkup:
    """Нумерованные кнопки задач: «1 ✅ текст». Номер = порядок в списке (1-based)."""
    rows = [[f"{i + 1} {_MARKS.get(t['status'], '⬜')} {t['text'][:30]}"]
            for i, t in enumerate(tasks)]
    rows.append([BTN_BACK])
    return _rk(rows, "Выбери задачу по номеру")


def menu_slot_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"engnr {s}",
                              callback_data=MenuCB(action="iamset", val=s).pack())
         for s in config.TRACKER_SLOTS],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=MenuCB(action="home").pack())],
    ])


def menu_tasks_kb(tasks: list) -> InlineKeyboardMarkup:
    rows = []
    for t in tasks:
        label = f"{_MARK.get(t['status'], '⬜')} {t['text'][:40]}"
        rows.append([InlineKeyboardButton(
            text=label,
            callback_data=MenuPickCB(row=t["row"], col=t["status_col"], line=t["line"]).pack())])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=MenuCB(action="home").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)
