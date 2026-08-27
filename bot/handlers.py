"""Реактивная часть (мультитенант) — в облаке станет Webhook Lambda.
Тенант резолвится из chat_id группы; в callback_data тенант закодирован явно."""
import asyncio
import html
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           Message, ReplyKeyboardRemove)

from . import config, ddb, media, reactions, sheets
from .keyboards import (BTN_BACK, BTN_CANCEL, BTN_DELETE, BTN_DEL_YES, BTN_D_OTHER, BTN_D_TODAY,
                        BTN_D_TOMORROW, BTN_IAM, BTN_NEW, BTN_STATUS, BTN_TODAY, REMIND_MAP,
                        SLOT_BUTTONS, STATUS_MAP, ActCB, LaterCB, MenuCB, MenuPickCB, SheetCB,
                        SheetEditCB, SheetLaterCB, WizCB, cancel_reply_kb, confirm_del_reply_kb,
                        day_reply_kb, later_kb, main_menu_kb, main_reply_kb, menu_slot_kb,
                        menu_tasks_kb, reminder_kb, remind_reply_kb, sheet_later_kb, sheet_status_kb,
                        slot_reply_kb, status_reply_kb, tasks_reply_kb, tracker_link_kb,
                        wiz_category_kb, wiz_date_kb, wiz_time_kb)
from .taskparse import parse_df_when, parse_task, wiz_deadline

router = Router()

_STATUS_LABEL = {"open": "🟡 open", "done": "✅ done", "skipped": "➖ skipped", "expired": "💀 expired"}


def _gid(message: Message) -> int | None:
    """Тенант = id группы. В личке тенант не определить -> None."""
    if message.chat.type in ("group", "supergroup"):
        return message.chat.id
    return None


async def _is_admin(bot, gid: int, uid: int) -> bool:
    try:
        m = await bot.get_chat_member(gid, uid)
        return m.status in ("creator", "administrator")
    except Exception:  # noqa: BLE001
        return False


def _is_super(user) -> bool:
    """Супер-админ по username (config.SUPER_ADMINS) — может рулить из лички."""
    return bool(user and (user.username or "").lower() in config.SUPER_ADMINS)


def _resolve_gid_for(chat, user) -> int | None:
    """gid для управления: в группе — id чата; в личке — единственный тенант юзера.
    Супер-админу без явной привязки отдаём единственный тенант системы (если он один)."""
    if chat.type in ("group", "supergroup"):
        return chat.id
    tenants = ddb.tenants_of_user(user.id)
    if len(tenants) == 1:
        return tenants[0]
    if _is_super(user):
        allt = ddb.list_tenants()
        if len(allt) == 1:
            return allt[0]
    return None


def _resolve_gid(message: Message) -> int | None:
    return _resolve_gid_for(message.chat, message.from_user)


async def _create_sheet_task(gid, slot, _cat_unused, text, date_str, deadline_ts,
                             chat_id, thread_id, uid):
    """Пишет задачу в трекер и возвращает её текст со ссылкой на Google Sheet.

    Статус при создании не спрашиваем: бот запросит его перед StandUP только у тех,
    кто действительно создал задачи на сегодня.
    """
    ts = ddb.now_ts()
    res = await asyncio.to_thread(sheets.tracker_write, slot, text, ts, date_str)
    # дедлайн, если задан, показываем только как справку (без пинга)
    note = ("\n⏰ срок: " + datetime.fromtimestamp(
        deadline_ts, ZoneInfo(config.TZ)).strftime("%d.%m %H:%M")) if deadline_ts else ""
    conf = (f"📝 <b>{html.escape(res['label'])}</b> · {res['date']}\n"
            f"«{html.escape(text)}»{note}")
    return conf, tracker_link_kb(config.TRACKER_URL)


async def _can_manage(message: Message, gid: int) -> bool:
    """Право менять настройки: супер-админ (в т.ч. в личке) или админ группы."""
    if _is_super(message.from_user):
        return True
    return await _is_admin(message.bot, gid, message.from_user.id)


def _mention(user_id: int, name: str | None) -> str:
    return f'<a href="tg://user?id={user_id}">{html.escape(name or "коллега")}</a>'


def _fmt_deadline(ts: int | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, ZoneInfo(config.TZ)).strftime("%d.%m %H:%M")


def render_card(gid: int, task) -> str:
    au = ddb.get_user(gid, task["assignee_id"])
    ru = ddb.get_user(gid, task["requester_id"]) if task.get("requester_id") else None
    assignee = _mention(task["assignee_id"], au["username"] if au else None)
    requester = _mention(task["requester_id"], ru["username"] if ru else None) if ru else "—"
    return (
        "🗂 <b>Таск</b>\n"
        f"Исполнитель: {assignee}\n"
        f"Заказчик: {requester}\n"
        f"Дедлайн: {_fmt_deadline(task.get('deadline_ts'))}\n"
        f"Статус: {_STATUS_LABEL.get(task['status'], task['status'])}\n"
        f"Бриф: {html.escape(task.get('brief') or task.get('title') or '—')}\n"
        f"Формат: {html.escape(task.get('result_format') or '—')}"
    )


async def refresh_card(bot, gid: int, task_id: str) -> None:
    task = ddb.get_task(gid, task_id)
    if not task or not task.get("card_chat_id"):
        return
    try:
        await bot.edit_message_text(
            text=render_card(gid, task), chat_id=task["card_chat_id"],
            message_id=task["card_message_id"], parse_mode="HTML",
        )
    except Exception:  # noqa: BLE001
        pass


# ---------- онбординг / регистрация ----------
@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject) -> None:
    u = message.from_user
    gid = _gid(message)
    if gid:
        ddb.upsert_user(gid, u.id, u.username or u.full_name)
        role = ddb.get_user(gid, u.id)["role"]
        await message.answer(
            f"Йо, {u.full_name}! Ты в игре (роль: {role}).\n"
            "Привяжи почту для календаря: /email you@gmail.com\n"
            "Команды — /help",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    # --- личка ---
    payload = (command.args or "").strip()
    if payload.lstrip("-").isdigit():   # deep-link: t.me/Bot?start=<gid>
        target = int(payload)
        # подключаем, только если человек реально в той группе
        try:
            m = await message.bot.get_chat_member(target, u.id)
            member = m.status not in ("left", "kicked")
        except Exception:  # noqa: BLE001
            member = False
        ten = ddb.get_tenant(target)
        if member and ten:
            ddb.upsert_user(target, u.id, u.username or u.full_name)
            await message.answer(
                f"Готово ✅ Ты подключён к команде «{ten.get('name') or 'команда'}».\n"
                "Привяжи рабочую почту: /email you@gmail.com — и я найду твои созвоны.",
                reply_markup=ReplyKeyboardRemove(),
            )
        else:
            await message.answer("Не вижу тебя в той группе. Открой ссылку-приглашение от своей команды.")
        return

    tenants = ddb.tenants_of_user(u.id)
    if tenants:
        await message.answer(
            "Привет! Личку использую для персональных напоминаний.\n"
            "Привязать почту: /email you@gmail.com",
            reply_markup=ReplyKeyboardRemove())
    else:
        await message.answer(
            "Привет! Я командный бот. Тебя добавят через ссылку-приглашение из группы команды, "
            "либо напиши что-нибудь в самой группе.")


@router.message(Command("setup"))
async def cmd_setup(message: Message) -> None:
    gid = _gid(message)
    if gid is None:
        await message.answer("Команду /setup нужно вызвать в группе команды.")
        return
    if not await _is_admin(message.bot, gid, message.from_user.id):
        await message.answer("Только админ группы может подключить команду.")
        return
    ddb.register_tenant(gid, name=message.chat.title or "")
    me = await message.bot.get_me()
    link = f"https://t.me/{me.username}?start={gid}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔗 Подключиться (в личке)", url=link)]])
    await message.answer(
        "Готово ✅ Команда подключена.\n\n"
        "Дальше:\n"
        "1) /settopic board|work|calls в нужных топиках\n"
        "2) /setcalendar <Calendar ID>\n"
        "3) участники жмут кнопку ниже и делают /email в личке",
        reply_markup=kb,
    )


@router.message(Command("role"))
async def cmd_role(message: Message) -> None:
    gid = _gid(message)
    if gid is None:
        await message.answer("Команда работает в группе команды.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or parts[1].strip() not in config.ROLES:
        await message.answer("Формат: /role <founder|assistant|backend|intern>")
        return
    u = message.from_user
    ddb.upsert_user(gid, u.id, u.username or u.full_name, role=parts[1].strip())
    await message.answer(f"Роль обновлена: {parts[1].strip()}")


@router.message(Command("email"))
async def cmd_email(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    email = parts[1].strip() if len(parts) > 1 else ""
    if "@" not in email or " " in email:
        await message.answer("Формат: /email you@gmail.com (та почта, которой тебя зовут в события).")
        return
    u = message.from_user
    gid = _gid(message)
    if gid is not None:
        # в группе — привязываем к этому тенанту
        targets = [gid]
    else:
        # в личке — ко всем командам пользователя (почта у человека одна)
        targets = ddb.tenants_of_user(u.id)
        if not targets:
            await message.answer("Ты ещё не в команде. Открой ссылку-приглашение из группы или "
                                 "напиши что-нибудь в самой группе.")
            return
    for t in targets:
        ddb.upsert_user(t, u.id, u.username or u.full_name, email=email)
    suffix = f" (команд: {len(targets)})" if len(targets) > 1 else ""
    await message.answer(f"Почта привязана: {email.lower()}{suffix}")


@router.message(Command("whoami"))
async def cmd_whoami(message: Message) -> None:
    gid = _gid(message)
    if gid is None:
        await message.answer("Спроси в группе команды.")
        return
    row = ddb.get_user(gid, message.from_user.id)
    if row is None:
        await message.answer("Тебя ещё нет в этой команде — напиши /start в группе.")
        return
    await message.answer(
        f"telegram_id: {message.from_user.id}\nusername: {row.get('username')}\n"
        f"роль: {row['role']}\nпочта: {row.get('email') or '— (/email)'}"
    )


@router.message(Command("settopic"))
async def cmd_settopic(message: Message) -> None:
    gid = _gid(message)
    if gid is None:
        await message.answer("Вызови /settopic в нужном топике группы.")
        return
    if not await _is_admin(message.bot, gid, message.from_user.id):
        await message.answer("Только админ группы может настраивать топики.")
        return
    parts = (message.text or "").split(maxsplit=1)
    kind = parts[1].strip().lower() if len(parts) > 1 else ""
    if kind not in config.TOPIC_KINDS:
        await message.answer("Формат: /settopic <board|work|calls>")
        return
    # message_thread_id надёжнее is_topic_message (тот часто None в топиках).
    thread_id = message.message_thread_id
    ddb.set_topic(gid, kind, gid, thread_id)
    where = f"топик #{thread_id}" if thread_id else "эту группу"
    await message.answer(f"Готово ✅ Канал «{kind}» → {where}.")


@router.message(Command("setcalendar"))
async def cmd_setcalendar(message: Message) -> None:
    gid = _gid(message)
    if gid is None:
        await message.answer("Вызови /setcalendar в группе команды.")
        return
    if not await _is_admin(message.bot, gid, message.from_user.id):
        await message.answer("Только админ группы может привязать календарь.")
        return
    if ddb.get_tenant(gid) is None:
        await message.answer("Сначала подключи команду: /setup")
        return
    parts = (message.text or "").split(maxsplit=1)
    cal = parts[1].strip() if len(parts) > 1 else ""
    if "@" not in cal:
        await message.answer("Формат: /setcalendar <Calendar ID>\n"
                             "Возьми в календаре: Settings → Integrate calendar → Calendar ID.\n"
                             "И расшарь календарь на email service account'а.")
        return
    ddb.set_tenant_calendar(gid, cal)
    await message.answer("Готово ✅ Календарь привязан. Коллы начнут приходить (синк раз в пару минут).")


@router.message(Command("chatid"))
async def cmd_chatid(message: Message) -> None:
    await message.answer(f"chat_id: {message.chat.id}\ntype: {message.chat.type}\n"
                         f"thread_id: {message.message_thread_id}")


# ---------- задачи ----------
@router.message(Command("task"))
async def cmd_task(message: Message) -> None:
    gid = _gid(message)
    if gid is None:
        await message.answer("Создавай таски в группе команды.")
        return
    parsed = parse_task(message.text or "", config.TZ, ddb.now_ts(), config.DEFAULT_PINGS)
    if not parsed["ok"]:
        await message.answer("Не собрал таск: " + parsed["error"] + "\n\n"
                             "Формат:\n/task\n@исполнитель\n15.07 18:00\nбриф: ...\nформат: ...")
        return

    assignee_id = None
    for ent in (message.entities or []):
        if ent.type == "text_mention" and ent.user:
            assignee_id = ent.user.id
            break
    if assignee_id is None:
        u = ddb.get_user_by_username(gid, parsed["assignee_username"])
        if u is None:
            await message.answer(f"Не нашёл @{parsed['assignee_username']} в команде. Пусть напишет /start.")
            return
        assignee_id = u["uid"]

    if parsed["deadline_ts"] <= ddb.now_ts():
        await message.answer("Дедлайн уже в прошлом.")
        return

    r = message.from_user
    ddb.upsert_user(gid, r.id, r.username or r.full_name)
    tid = ddb.create_task_full(gid, parsed["title"], assignee_id, r.id,
                               parsed["deadline_ts"], parsed["brief"], parsed["result_format"] or "")

    now = ddb.now_ts()
    made = 0
    for off in parsed["pings"]:
        fire_at = parsed["deadline_ts"] + off
        if fire_at <= now and off != 0:
            continue
        ddb.create_reminder(gid, tid, assignee_id, max(fire_at, now), kind="work")
        made += 1
    if made == 0:
        ddb.create_reminder(gid, tid, assignee_id, parsed["deadline_ts"], kind="work")

    task = ddb.get_task(gid, tid)
    board = ddb.get_topic(gid, "board")
    chat_id, thread_id = board if board else (message.chat.id, message.message_thread_id)
    sent = await message.bot.send_message(chat_id=chat_id, message_thread_id=thread_id,
                                          text=render_card(gid, task), parse_mode="HTML")
    ddb.set_task_card(gid, tid, chat_id, sent.message_id)
    if board and chat_id != message.chat.id:
        await message.answer("Таск создан ✅ Карточка в board, напоминания поставлены.")


@router.message(Command("newtask"))
async def cmd_newtask(message: Message) -> None:
    gid = _gid(message)
    if gid is None:
        await message.answer("Команда работает в группе команды.")
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.answer("Формат: /newtask <минут> <текст>")
        return
    minutes, title = int(parts[1]), parts[2].strip()
    u = message.from_user
    ddb.upsert_user(gid, u.id, u.username or u.full_name)
    fire_at = ddb.now_ts() + minutes * 60
    tid = ddb.create_task(gid, title, u.id, deadline_ts=fire_at)
    ddb.create_reminder(gid, tid, u.id, fire_at, kind="work")
    await message.answer(f"New Quest unlocked ⚡\n«{title}»\nНапомню через {minutes} мин.")


@router.message(Command("me"))
async def cmd_me(message: Message) -> None:
    gid = _gid(message)
    if gid is None:
        await message.answer("Спроси в группе команды.")
        return
    await message.answer(f"Твоя aura: {ddb.user_aura(gid, message.from_user.id)}")


@router.message(Command("top"))
async def cmd_top(message: Message) -> None:
    gid = _gid(message)
    if gid is None:
        await message.answer("Спроси в группе команды.")
        return
    rows = ddb.leaderboard(gid)
    if not rows:
        await message.answer("Лидерборд пуст — go cook 🍳")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 TOP Aura"]
    for i, r in enumerate(rows):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{prefix} {r.get('username') or r['uid']} — {r['aura']}")
    await message.answer("\n".join(lines))


@router.message(Command("digest"))
async def cmd_digest(message: Message) -> None:
    gid = _gid(message)
    if gid is None:
        await message.answer("Спроси в группе команды.")
        return
    rows = ddb.founder_digest(gid)
    if not rows:
        await message.answer("📊 Сегодня задач ещё не было.")
        return
    td = sum(r["done"] for r in rows)
    ta = sum(r["total"] for r in rows)
    pct = round(100 * td / ta) if ta else 0
    lines = ["📊 Team Pulse (сегодня)"]
    for r in rows:
        mark = "🔥" if r["total"] and r["done"] == r["total"] else "✅"
        lines.append(f"{mark} {r['name']}: {r['done']}/{r['total']}")
    lines.append(f"\nCompletion: {pct}%")
    await message.answer("\n".join(lines))


# ---------- Google Sheet: командный таск-трекер ----------
def _sheet_person(gid: int, uid: int) -> str | None:
    return ddb.get_setting(gid, f"iam:{uid}")


@router.message(Command("iam"))
async def cmd_iam(message: Message, command: CommandObject) -> None:
    """Привязка к слоту трекера: /iam A | /iam B | /iam C (engnr A/B/C)."""
    gid = _resolve_gid(message)
    if gid is None:
        await message.answer("Ты пока не привязан к команде. Напиши что-нибудь в группе команды "
                             "(или открой бота по ссылке-приглашению), потом снова /iam.")
        return
    slot = (command.args or "").strip().upper()
    if slot not in config.TRACKER_SLOTS:
        await message.answer(
            "Кто ты в трекере? Напиши: /iam A (или B / C)\n"
            "Слоты: " + ", ".join(f"engnr {s}" for s in config.TRACKER_SLOTS))
        return
    u = message.from_user
    if getattr(u, "is_bot", False):
        # анонимный админ пишет как GroupAnonymousBot — слот привязывать нельзя
        await message.answer("Похоже, ты пишешь анонимно (от имени группы). "
                             "Отключи анонимный режим и сделай /iam от своего аккаунта.")
        return
    # слот эксклюзивен: если его держит ДРУГОЙ аккаунт — блокируем и называем держателя
    for key, val in ddb.list_settings(gid, "iam:").items():
        if (val or "").strip().upper() != slot:
            continue
        try:
            holder_uid = int(key.split("iam:", 1)[1])
        except (ValueError, IndexError):
            continue
        if holder_uid == u.id:
            continue   # уже он сам — просто подтвердим ниже
        holder = ddb.get_user(gid, holder_uid)
        who = (holder.get("username") if holder else None) or f"id{holder_uid}"
        await message.answer(
            f"⛔ Слот <b>engnr {slot}</b> уже занят: {html.escape(str(who))}.\n"
            "Возьми свободный слот (/iam A|B|C), либо пусть текущий держатель "
            "переключится, либо админ уберёт его через /removeuser.",
            parse_mode="HTML")
        return
    ddb.upsert_user(gid, u.id, u.username or u.full_name)
    ddb.set_setting(gid, f"iam:{u.id}", slot)
    await message.answer(
        f"Ок, ты — <b>engnr {slot}</b>. Теперь /new пишет задачи в твой столбец трекера.",
        parse_mode="HTML")


@router.message(Command("adduser"))
async def cmd_adduser(message: Message, command: CommandObject) -> None:
    """Добавить человека в таблицу: /adduser Имя (создаёт колонки Имя / Имя-status)."""
    gid = _resolve_gid(message)
    if gid is None:
        await message.answer("Напиши в группе команды (или ты не привязан к команде).")
        return
    if not await _can_manage(message, gid):
        await message.answer("Только админ/владелец может менять состав таблицы.")
        return
    name = (command.args or "").strip()
    if not name:
        await message.answer("Формат: /adduser Имя")
        return
    if not sheets.is_configured():
        await message.answer("Google Sheet не настроен.")
        return
    try:
        added = await asyncio.to_thread(sheets.add_person, name)
    except Exception as e:  # noqa: BLE001
        await message.answer(f"⚠️ Не смог добавить: {str(e)[:120]}")
        return
    if added:
        await message.answer(
            f"✅ «{html.escape(name)}» добавлен в таблицу (колонки {html.escape(name)} / "
            f"{html.escape(name)}-status). Пусть сделает /iam {html.escape(name)}.")
    else:
        await message.answer(f"«{html.escape(name)}» уже есть в таблице.")


@router.message(Command("removeuser"))
async def cmd_removeuser(message: Message, command: CommandObject) -> None:
    """Удалить человека из таблицы: /removeuser Имя (удаляет его колонки, остальные сдвигаются)."""
    gid = _resolve_gid(message)
    if gid is None:
        await message.answer("Напиши в группе команды (или ты не привязан к команде).")
        return
    if not await _can_manage(message, gid):
        await message.answer("Только админ/владелец может менять состав таблицы.")
        return
    name = (command.args or "").strip()
    if not name:
        await message.answer("Формат: /removeuser <имя или слот A|B|C>")
        return
    # 1) ВСЕГДА чистим iam-привязки с этим значением (имя ИЛИ слот), даже если
    #    колонки в листе уже нет (под Tracker A/B/C именных колонок не существует).
    cleared = 0
    for key, val in ddb.list_settings(gid, "iam:").items():
        if (val or "").strip().lower() == name.lower():
            ddb.set_setting(gid, key, "")
            cleared += 1
    # 2) легаси: пробуем убрать колонку из листа (под Tracker не требуется)
    removed = False
    try:
        removed = await asyncio.to_thread(sheets.remove_person, name)
    except Exception:  # noqa: BLE001
        removed = False
    if cleared or removed:
        parts = []
        if cleared:
            parts.append(f"отвязан ({cleared})")
        if removed:
            parts.append("колонки убраны")
        await message.answer(f"✅ «{html.escape(name)}» — " + ", ".join(parts) + ".")
    else:
        await message.answer(f"«{html.escape(name)}» не найден ни в привязках, ни в таблице.")


# Старые команды /df /st /ma /md /ob убраны (перешли на Tracker-HostAI).
# Оставляем мягкий редирект, чтобы старая мышечная память вела на /new.
@router.message(Command("df", "st", "ma", "md", "ob"))
async def cmd_sheet_task_removed(message: Message) -> None:
    await message.answer("Эти команды убраны. Теперь задачи через /new (мастер по кнопкам).")


@router.message(Command("new"))
async def cmd_new(message: Message) -> None:
    """Мастер задачи по кнопкам: категория -> текст -> дата -> напоминание."""
    gid = _resolve_gid(message)
    if gid is None:
        await message.answer("Ты пока не привязан к команде. Напиши что-нибудь в группе, потом /iam.")
        return
    if not _sheet_person(gid, message.from_user.id):
        await message.answer("Сначала привяжись к слоту: /iam A (или B / C)")
        return
    ddb.wiz_set(gid, message.from_user.id, step="text")
    await message.answer("🆕 Новая задача. Напиши текст:", reply_markup=ReplyKeyboardRemove())


async def _finish_wiz(gid, uid, wiz, time_kind, chat_id, thread_id):
    slot = _sheet_person(gid, uid)
    if not slot:
        raise ValueError("no_iam")
    text = (wiz.get("text") or "").strip()
    date_str = wiz.get("date")
    deadline_ts = wiz_deadline(date_str, time_kind, config.TZ, ddb.now_ts())
    conf, kb = await _create_sheet_task(gid, slot, None, text, date_str, deadline_ts,
                                        chat_id, thread_id, uid)
    ddb.del_wiz(gid, uid)
    return conf, kb


_HELP_TEXT = (
    "🤖 <b>Команды бота</b>\n\n"
    "<b>Трекер (Tracker-HostAI)</b>\n"
    "/iam A|B|C — привязать себя к слоту (engnr A/B/C)\n"
    "/new — новая задача по кнопкам (текст → дата → срок)\n"
    "   после записи — текст задачи и кнопка открытия трекера\n"
    "/today [A|B|C] — задачи на сегодня (все или по слоту)\n\n"
    "<b>Пуши</b>\n"
    "в 10:30 — мем, если не все создали задачи\n"
    "перед StandUP/SyncUP — задачи и кнопки статуса только их авторам\n\n"
    "<b>Аура</b>\n"
    "/me — твоя аура · /top — лидерборд · /digest — сводка команды\n\n"
    "<b>Созвоны и медиа</b>\n"
    "созвоны — из Google Calendar (авто)\n"
    "/calls — список коллов недели + у кого есть фото\n"
    "/setmedia событие — фото/гиф/стикер в ответ · /media — что задано\n"
    "/setcallmedia Название — фото на конкретный созвон\n\n"
    "<b>Настройка</b>\n"
    "/cadence — времена пингов\n"
    "/settopic board|work|calls — топики (в нужном топике)\n"
    "/setup · /role · /email · /whoami · /chatid"
)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(_HELP_TEXT, parse_mode="HTML")


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    """Кнопки временно убраны — работаем командами. Заодно снимаем нижнюю клавиатуру."""
    await message.answer("Работаем командами. Список — /help", reply_markup=ReplyKeyboardRemove())


@router.message(Command("today"))
async def cmd_today(message: Message, command: CommandObject) -> None:
    """Задачи на сегодня: /today (все) или /today Имя (по человеку)."""
    gid = _resolve_gid(message)
    if gid is None:
        await message.answer("Ты пока не привязан к команде. Напиши в группе, потом /iam.")
        return
    if not sheets.is_configured():
        await message.answer("Google Sheet не настроен.")
        return
    arg = (command.args or "").strip().upper()
    try:
        tasks = await asyncio.to_thread(sheets.tracker_read_today, ddb.now_ts())
    except Exception as e:  # noqa: BLE001
        await message.answer(f"⚠️ Не смог прочитать лист: {str(e)[:100]}")
        return
    mark = {"done": "✅", "skipped": "❌", "later": "🕓"}
    today = datetime.now(ZoneInfo(config.TZ)).strftime("%d.%m")
    lines = [f"📋 <b>Задачи на сегодня</b> ({today})"]
    any_task = False
    for person, items in tasks.items():
        if arg and person.upper() != arg:
            continue
        if not items:
            continue
        any_task = True
        lines.append(f"\n<b>engnr {html.escape(person)}</b>:")
        for lbl, txt, st in items:
            lines.append(f"  {mark.get(st, '⬜')} {html.escape(txt)}")
    if not any_task:
        lines.append(f"\n— пусто{f' у «{html.escape(arg)}»' if arg else ''}")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("cadence"))
async def cmd_cadence(message: Message, command: CommandObject) -> None:
    """Каденс напоминаний: /cadence Uldana 10:00,15:00 . Без аргументов — показать текущие."""
    gid = _resolve_gid(message)
    if gid is None:
        await message.answer("Не понял, к какой команде применить (напиши в группе, либо ты не привязан к команде).")
        return
    args = (command.args or "").split()
    try:
        people = await asyncio.to_thread(sheets.get_people)
    except Exception:  # noqa: BLE001
        people = config.SHEET_PEOPLE
    if len(args) < 2:
        cur = ddb.list_settings(gid, "cadence:")
        lines = ["⏰ Каденс (локальное время):"]
        for p in people:
            times = cur.get(f"cadence:{p.lower()}")
            lines.append(f"• {p}: {times or ', '.join(config.DEFAULT_CADENCE) + ' (по умолч.)'}")
        lines.append("\nЗадать: /cadence <Имя> 10:00,15:00")
        await message.answer("\n".join(lines))
        return
    name = next((p for p in people if p.lower() == args[0].lower()), None)
    if not name:
        await message.answer("Имя не из списка: " + ", ".join(people))
        return
    times = []
    for t in args[1].replace(";", ",").split(","):
        t = t.strip()
        try:
            datetime.strptime(t, "%H:%M")
            times.append(t)
        except ValueError:
            pass
    if not times:
        await message.answer("Не понял времена. Формат: 10:00,15:00")
        return
    ddb.set_setting(gid, f"cadence:{name.lower()}", ",".join(times))
    await message.answer(f"⏰ {name}: каденс {', '.join(times)}")


def _extract_media(message):
    """Достаёт (kind, ref) из reply-фото/гиф, из самого сообщения, либо из URL-аргумента.
    -> (kind, ref) | None. arg берётся из последнего токена команды."""
    r = message.reply_to_message
    if r and r.photo:
        return "photo", r.photo[-1].file_id
    if r and r.animation:
        return "animation", r.animation.file_id
    if r and r.sticker:
        return "sticker", r.sticker.file_id
    if message.photo:
        return "photo", message.photo[-1].file_id
    if message.animation:
        return "animation", message.animation.file_id
    if message.sticker:
        return "sticker", message.sticker.file_id
    return None


@router.message(Command("setmedia"))
async def cmd_setmedia(message: Message, command: CommandObject) -> None:
    """Привязать фото/гиф к событию: /setmedia <event> (пришли фото/гиф в ответ),
    либо /setmedia <event> https://...  ; убрать: /setmedia <event> off.
    События: call, reminder, done, later, skip, expired."""
    gid = _resolve_gid(message)
    if gid is None:
        await message.answer("Не понял, к какой команде применить (напиши в группе, либо ты не привязан к команде).")
        return
    parts = (command.args or "").split(maxsplit=1)
    event = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""
    if event not in config.MEDIA_EVENTS:
        ev = "\n".join(f"• {k} — {v}" for k, v in config.MEDIA_EVENTS.items())
        await message.answer(
            "Формат: /setmedia &lt;event&gt; (фото/гиф в ответ | ссылка | off)\n\nСобытия:\n" + ev,
            parse_mode="HTML")
        return
    if rest.lower() in ("off", "clear", "-", "убрать"):
        media.clear_media(gid, event)
        await message.answer(f"Медиа для «{config.MEDIA_EVENTS[event]}» убрано.")
        return
    got = _extract_media(message)
    if got:
        kind, ref = got
    elif rest.startswith("http"):
        kind, ref = ("animation" if rest.lower().split("?")[0].endswith(".gif") else "photo"), rest
    else:
        await message.answer(
            f"Как задать медиа для «{config.MEDIA_EVENTS[event]}»:\n"
            f"• пришли фото/гиф <b>в ответ</b> на /setmedia {event}, или\n"
            f"• /setmedia {event} https://ссылка.jpg|gif\n"
            f"• убрать: /setmedia {event} off", parse_mode="HTML")
        return
    media.set_media(gid, event, kind, ref)
    await message.answer(f"✅ Медиа для «{config.MEDIA_EVENTS[event]}» сохранено ({kind}).")


@router.message(Command("media"))
async def cmd_media(message: Message) -> None:
    """Показать, у каких событий задано медиа."""
    gid = _resolve_gid(message)
    if gid is None:
        await message.answer("Не понял, к какой команде применить (напиши в группе, либо ты не привязан к команде).")
        return
    lines = ["🖼 Медиа по событиям:"]
    for ev, label in config.MEDIA_EVENTS.items():
        m = media.get_media(gid, ev)
        mark = f"✅ {m[0]}" if m else "—"
        lines.append(f"• {ev} ({label}): {mark}")
    cm = media.list_call_media(gid)
    if cm:
        lines.append("\n📞 Фото созвонов (матч по названию события):")
        for kw, (kind, _) in cm.items():
            lines.append(f"• «{kw}» — {kind}")
    lines.append("\nЗадать: /setmedia <event> …  |  /setcallmedia <название созвона> (фото в ответ)")
    await message.answer("\n".join(lines))


@router.message(Command("setcallmedia"))
async def cmd_setcallmedia(message: Message, command: CommandObject) -> None:
    """Фото/гиф/стикер на конкретный созвон — по НАЗВАНИЮ события в календаре.
    /setcallmedia Sprint planning  (пришли картинку В ОТВЕТ). Убрать: … off."""
    gid = _resolve_gid(message)
    if gid is None:
        await message.answer("Не понял, к какой команде применить (напиши в группе, либо ты не привязан к команде).")
        return
    kw = (command.args or "").strip()
    if not kw:
        await message.answer(
            "Формат: /setcallmedia &lt;название созвона&gt; — и пришли фото/гиф/стикер "
            "<b>в ответ</b>.\nНапр.: ответь картинкой на «/setcallmedia Sprint planning».\n"
            "Матчится по вхождению названия в тему события календаря.\n"
            "Убрать: /setcallmedia &lt;название&gt; off", parse_mode="HTML")
        return
    tail = kw.rsplit(" ", 1)
    if len(tail) == 2 and tail[1].lower() in ("off", "убрать", "-"):
        media.clear_call_media(gid, tail[0])
        await message.answer(f"Фото созвона «{tail[0]}» убрано.")
        return
    got = _extract_media(message)
    if not got:
        await message.answer(
            f"Пришли фото/гиф/стикер <b>в ответ</b> на /setcallmedia {kw}", parse_mode="HTML")
        return
    kind, ref = got
    media.set_call_media(gid, kw, kind, ref)
    await message.answer(
        f"✅ Фото для созвона «{kw}» сохранено ({kind}). "
        "Придёт, когда название события календаря содержит эту фразу.")


@router.message(Command("calls"))
async def cmd_calls(message: Message) -> None:
    """Список ближайших коллов из календаря + есть ли у каждого фото.
    Чтобы поставить фото под конкретный колл: ответь фото на /setcallmedia <название>."""
    from . import gcal
    gid = _resolve_gid(message)
    if gid is None:
        await message.answer("Напиши в группе команды (или ты не привязан к команде).")
        return
    ten = ddb.get_tenant(gid) or {}
    cal = ten.get("calendar_id")
    if not gcal.is_configured() or not cal:
        await message.answer("Календарь не подключён к этой команде.")
        return
    try:
        raw = await asyncio.to_thread(gcal.fetch_upcoming, cal, 168)   # неделя вперёд
    except Exception as e:  # noqa: BLE001
        await message.answer(f"⚠️ Не смог прочитать календарь: {str(e)[:100]}")
        return
    # уникальные названия по порядку появления
    seen, titles = set(), []
    for ev in raw:
        t = (ev.get("summary") or "").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower()); titles.append(t)
    if not titles:
        await message.answer("На неделю вперёд коллов в календаре нет.")
        return
    lines = ["📞 <b>Коллы на неделю</b> (фото в уведомлении):"]
    for t in titles[:30]:
        has = media.get_call_media(gid, t)
        mark = f"🖼 {has[0]}" if has else "— нет фото"
        lines.append(f"• {html.escape(t)} — {mark}")
    lines.append("\nПоставить фото под колл: ответь фото/гиф на\n"
                 "<code>/setcallmedia Название</code>\n"
                 "Общее фото на все коллы без своего: <code>/setmedia call</code> (фото в ответ)")
    await message.answer("\n".join(lines), parse_mode="HTML")


_DF_AURA = {"done": config.AURA_DONE, "skipped": config.AURA_SKIP, "later": config.AURA_DF_LATER}


def _apply_df_aura(gid, row, col, line, new_status, uid) -> int:
    """Аура за статус /df-задачи. Идемпотентно: храним последний статус+uid по ячейке+строке,
    применяем только разницу (смена done->skip корректно отматывает). -> применённая дельта."""
    key = f"dfst:{row}_{col}_{line}"
    prev = ddb.get_setting(gid, key) or ""
    old_uid, old_status = (prev.split("|", 1) + [""])[:2] if "|" in prev else ("", prev)
    old_delta = _DF_AURA.get(old_status, 0)
    new_delta = _DF_AURA.get(new_status, 0)
    applied = 0
    try:
        if old_uid.isdigit() and int(old_uid) != uid:
            if old_delta:
                ddb.add_event(gid, int(old_uid), "df_undo", -old_delta)
            if new_delta:
                ddb.add_event(gid, uid, f"df_{new_status}", new_delta)
            applied = new_delta
        else:
            diff = new_delta - old_delta
            if diff:
                ddb.add_event(gid, uid, f"df_{new_status}", diff)
            applied = diff
        ddb.set_setting(gid, key, f"{uid}|{new_status}")
    except Exception:  # noqa: BLE001
        pass
    return applied


async def _push_after_create(bot, gid, chat_id, thread_id) -> None:
    """Медиа-реакция после создания задачи (если задано /setmedia push_after_create)."""
    m = media.get_media(gid, "push_after_create")
    if m:
        try:
            await media.send_media(bot, chat_id, thread_id, m)
        except Exception:  # noqa: BLE001
            pass


async def _emit_event_media(cb: CallbackQuery, gid: int, event: str) -> None:
    """Отправляет медиа события (если задано) в тот же чат/топик, где нажали кнопку."""
    m = media.get_media(gid, event)
    if not m:
        return
    try:
        await media.send_media(cb.bot, cb.message.chat.id,
                               getattr(cb.message, "message_thread_id", None), m)
    except Exception:  # noqa: BLE001
        pass


# ---------- callbacks ----------
@router.callback_query(WizCB.filter())
async def on_wiz_cb(cb: CallbackQuery, callback_data: WizCB) -> None:
    gid = _resolve_gid_for(cb.message.chat, cb.from_user)
    if gid is None:
        await cb.answer("Не привязан к команде.", show_alert=True)
        return
    uid = cb.from_user.id
    wiz = ddb.get_wiz(gid, uid)
    if not wiz:
        await cb.answer("Мастер устарел, начни заново: /new", show_alert=True)
        return
    step, val = callback_data.step, callback_data.val

    if step == "cat":
        ddb.wiz_set(gid, uid, step="text", category=val)
        await cb.message.edit_text(
            f"Категория: <b>{html.escape(config.SHEET_CATEGORIES.get(val, val))}</b>\n\n"
            "Напиши текст задачи:", parse_mode="HTML")
        await cb.answer()

    elif step == "date":
        if val == "custom":
            ddb.wiz_set(gid, uid, step="date_input")
            await cb.message.edit_text("Напиши дату в формате ДД.ММ (напр. 05.08):")
            await cb.answer()
            return
        now = datetime.now(ZoneInfo(config.TZ))
        d = now if val == "today" else now + timedelta(days=1)
        ddb.wiz_set(gid, uid, step="time", date=d.strftime("%d.%m"))
        await cb.message.edit_text(f"Дата: <b>{d.strftime('%d.%m')}</b>\n\nСрок? (когда сделать)",
                                   reply_markup=wiz_time_kb(), parse_mode="HTML")
        await cb.answer()

    elif step == "time":
        if val == "custom":
            ddb.wiz_set(gid, uid, step="time_input")
            await cb.message.edit_text("Напиши время ЧЧ:ММ (напр. 18:30):")
            await cb.answer()
            return
        time_kind = {"1800": "18:00"}.get(val, val)   # none|1h|2h|3h|18:00
        try:
            conf, kb = await _finish_wiz(gid, uid, wiz, time_kind, cb.message.chat.id,
                                         cb.message.message_thread_id)
        except ValueError:
            await cb.answer("Сначала /iam", show_alert=True)
            ddb.del_wiz(gid, uid)
            return
        except Exception as e:  # noqa: BLE001
            await cb.message.edit_text(f"⚠️ Не смог записать: {str(e)[:120]}")
            ddb.del_wiz(gid, uid)
            return
        await cb.message.edit_text(conf, reply_markup=kb, parse_mode="HTML")
        await cb.answer("Готово ✅")


@router.callback_query(SheetCB.filter())
async def on_sheet_status(cb: CallbackQuery, callback_data: SheetCB) -> None:
    try:
        title = await asyncio.to_thread(sheets.current_tab_title)
        await asyncio.to_thread(
            sheets.write_status, title, callback_data.row, callback_data.col,
            callback_data.action, callback_data.line)
    except Exception as e:  # noqa: BLE001
        await cb.answer(f"Ошибка записи статуса: {str(e)[:60]}", show_alert=True)
        return
    gid, row, col, line, action = (callback_data.gid, callback_data.row, callback_data.col,
                                   callback_data.line, callback_data.action)
    # аура за /df-статус (идемпотентно: done +100, skip −50, later −10)
    delta = _apply_df_aura(gid, row, col, line, action, cb.from_user.id)
    note = f" · aura {'+' if delta > 0 else ''}{delta}" if delta else ""
    # медиа-реакция на статус, если задана
    ev = {"done": "done", "later": "later", "skipped": "skip"}.get(action)
    if ev:
        await _emit_event_media(cb, gid, ev)
    if action == "later":
        # −10 применён; спрашиваем «насколько отложить»
        await cb.answer(f"Позже{note}")
        try:
            await cb.message.edit_reply_markup(reply_markup=sheet_later_kb(gid, row, col, line))
        except Exception:  # noqa: BLE001
            pass
        return
    # done / skipped: закрываем напоминания и фиксируем статус
    await cb.answer(f"Статус: {action}{note}")
    base = (cb.message.text or "").split("\nСтатус?")[0]
    try:
        await cb.message.edit_text(f"{base}\n✅ статус: {action}")
    except Exception:  # noqa: BLE001
        pass
    try:
        ddb.close_df_reminders(gid, row, col, line)
    except Exception:  # noqa: BLE001
        pass


@router.callback_query(SheetLaterCB.filter())
async def on_sheet_later(cb: CallbackQuery, callback_data: SheetLaterCB) -> None:
    """Выбор «насколько позже» -> переносим напоминание, возвращаем статус-кнопки."""
    gid = callback_data.gid
    try:
        ddb.snooze_df_reminders(gid, callback_data.row, callback_data.col, callback_data.line,
                                ddb.now_ts() + callback_data.minutes * 60)
    except Exception:  # noqa: BLE001
        pass
    await cb.answer(f"Отложено на {callback_data.minutes} мин")
    try:
        await cb.message.edit_reply_markup(
            reply_markup=sheet_status_kb(gid, callback_data.row, callback_data.col, callback_data.line))
    except Exception:  # noqa: BLE001
        pass


@router.callback_query(SheetEditCB.filter())
async def on_sheet_edit(cb: CallbackQuery, callback_data: SheetEditCB) -> None:
    """✏️ Изменить / 🗑 Удалить задачу трекера. col = статус-колонка, текст = col-1."""
    gid = _resolve_gid_for(cb.message.chat, cb.from_user)
    if gid is None:
        await cb.answer("Не привязан к команде.", show_alert=True)
        return
    row, scol, line = callback_data.row, callback_data.col, callback_data.line
    vcol = scol - 1   # колонка текста задачи — слева от статус-колонки

    if callback_data.action == "del":
        try:
            await asyncio.to_thread(sheets.tracker_delete_line, row, vcol, scol, line)
        except Exception as e:  # noqa: BLE001
            await cb.answer(f"Не удалил: {str(e)[:60]}", show_alert=True)
            return
        try:
            ddb.close_df_reminders(gid, row, scol, line)   # снять напоминания по задаче
        except Exception:  # noqa: BLE001
            pass
        try:
            await cb.message.edit_text("🗑 Задача удалена.")
        except Exception:  # noqa: BLE001
            pass
        await cb.answer("Удалено")
        return

    # edit: просим новый текст, координаты кладём в мастер
    ddb.wiz_set(gid, cb.from_user.id, step="edit_task", erow=row, ecol=scol, eline=line)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    await cb.message.answer("✏️ Напиши новый текст задачи:")
    await cb.answer()


@router.callback_query(MenuCB.filter())
async def on_menu(cb: CallbackQuery, callback_data: MenuCB) -> None:
    """Главное меню и его ветки. Тенант резолвим из контекста (работает и в личке)."""
    gid = _resolve_gid_for(cb.message.chat, cb.from_user)
    if gid is None:
        await cb.answer("Не привязан к команде.", show_alert=True)
        return
    uid = cb.from_user.id
    a = callback_data.action

    if a == "home":
        await cb.message.edit_text("🤖 Меню трекера", reply_markup=main_menu_kb())
        await cb.answer()
    elif a == "iam":
        await cb.message.edit_text("Кто ты в трекере?", reply_markup=menu_slot_kb())
        await cb.answer()
    elif a == "iamset":
        slot = callback_data.val
        if slot not in config.TRACKER_SLOTS:
            await cb.answer("Неизвестный слот", show_alert=True)
            return
        u = cb.from_user
        ddb.upsert_user(gid, u.id, u.username or u.full_name)
        ddb.set_setting(gid, f"iam:{u.id}", slot)
        await cb.message.edit_text(
            f"Ок, ты — <b>engnr {slot}</b>. Теперь можно ставить задачи.",
            reply_markup=main_menu_kb(), parse_mode="HTML")
        await cb.answer("Готово ✅")
    elif a == "new":
        if not _sheet_person(gid, uid):
            await cb.answer("Сначала «Регистрация инженера»", show_alert=True)
            return
        ddb.wiz_set(gid, uid, step="text")
        await cb.message.answer("🆕 Новая задача. Напиши текст:")
        await cb.answer()
    elif a in ("status", "today"):
        slot = _sheet_person(gid, uid)
        if not slot:
            await cb.answer("Сначала «Регистрация инженера»", show_alert=True)
            return
        try:
            tasks = await asyncio.to_thread(sheets.tracker_today_tasks, slot, ddb.now_ts())
        except Exception as e:  # noqa: BLE001
            await cb.answer(f"Не смог прочитать трекер: {str(e)[:60]}", show_alert=True)
            return
        if not tasks:
            await cb.message.edit_text(
                f"На сегодня задач нет (engnr {slot}). Создай через ➕.",
                reply_markup=main_menu_kb())
            await cb.answer()
            return
        if a == "today":
            mark = {"done": "✅", "skipped": "❌", "later": "🕓"}
            lines = [f"📋 Твои задачи на сегодня (engnr {slot}):"]
            lines += [f"{mark.get(t['status'], '⬜')} {html.escape(t['text'])}" for t in tasks]
            await cb.message.edit_text("\n".join(lines), reply_markup=main_menu_kb(),
                                       parse_mode="HTML")
            await cb.answer()
        else:
            await cb.message.edit_text(f"Выбери задачу (engnr {slot}):",
                                       reply_markup=menu_tasks_kb(tasks))
            await cb.answer()


@router.callback_query(MenuPickCB.filter())
async def on_menu_pick(cb: CallbackQuery, callback_data: MenuPickCB) -> None:
    """Выбрал задачу в меню -> показываем кнопки статуса (дальше — существующая запись)."""
    gid = _resolve_gid_for(cb.message.chat, cb.from_user)
    if gid is None:
        await cb.answer("Не привязан к команде.", show_alert=True)
        return
    await cb.message.edit_text(
        "Отметь статус задачи:",
        reply_markup=sheet_status_kb(gid, callback_data.row, callback_data.col, callback_data.line))
    await cb.answer()


@router.callback_query(ActCB.filter())
async def on_action(cb: CallbackQuery, callback_data: ActCB) -> None:
    gid, rid = callback_data.gid, callback_data.rid
    rem = ddb.get_reminder(gid, rid)
    if rem is None:
        await cb.answer("Это напоминание уже неактуально.", show_alert=True)
        return
    if cb.from_user.id != rem["assignee_id"]:
        await cb.answer("Это не твоя миссия 👀", show_alert=True)
        return
    if rem["status"] in ("done", "skipped", "expired"):
        await cb.answer("Уже закрыто.")
        return

    action = callback_data.action
    if action == "done":
        ddb.mark_reminder(gid, rid, "done")
        ddb.set_task_status(gid, rem["task_id"], "done")
        ddb.add_event(gid, cb.from_user.id, "done", config.AURA_DONE, rem["task_id"])
        await cb.message.edit_text(
            f"✅ Mission Complete\n«{rem['task_title']}»\nHuge W. Aura +{config.AURA_DONE} 🔥")
        await refresh_card(cb.bot, gid, rem["task_id"])
        await cb.answer("Cooked 🔥")
        await _emit_event_media(cb, gid, "done")

    elif action == "later":
        await cb.message.edit_reply_markup(reply_markup=later_kb(gid, rid))
        await cb.answer("На сколько отложить?")

    elif action == "skip":
        ddb.mark_reminder(gid, rid, "skipped")
        ddb.set_task_status(gid, rem["task_id"], "skipped")
        ddb.add_event(gid, cb.from_user.id, "skip", config.AURA_SKIP, rem["task_id"])
        if cb.message.chat.type in ("group", "supergroup"):
            await cb.message.edit_text(f"➖ Пропущено: «{rem['task_title']}»")
            try:
                await cb.bot.send_message(cb.from_user.id,
                                          f"Пропустил «{rem['task_title']}». Aura {config.AURA_SKIP}.")
            except Exception:  # noqa: BLE001
                pass
        else:
            await cb.message.edit_text(f"❌ Skipped\n«{rem['task_title']}»\nAura {config.AURA_SKIP}")
        await refresh_card(cb.bot, gid, rem["task_id"])
        await cb.answer()
        await _emit_event_media(cb, gid, "skip")


@router.callback_query(LaterCB.filter())
async def on_later(cb: CallbackQuery, callback_data: LaterCB) -> None:
    gid, rid = callback_data.gid, callback_data.rid
    rem = ddb.get_reminder(gid, rid)
    if rem is None or cb.from_user.id != rem["assignee_id"]:
        await cb.answer("Недоступно.", show_alert=True)
        return
    new_fire = ddb.now_ts() + callback_data.minutes * 60
    ddb.reschedule_reminder(gid, rid, new_fire)
    ddb.add_event(gid, cb.from_user.id, "later", config.AURA_LATER, rem["task_id"])
    await cb.message.edit_text(f"⏳ Перенесено на {callback_data.minutes} мин\n«{rem['task_title']}»")
    await cb.answer("Ок, напомню позже")
    await _emit_event_media(cb, gid, "later")


# ---------- мастер /new: ввод текста/даты/времени (инлайн) ----------
async def _wiz_active(message: Message) -> bool:
    if not message.text or message.text.startswith("/"):
        return False
    gid = _resolve_gid(message)
    if gid is None:
        return False
    w = ddb.get_wiz(gid, message.from_user.id)
    return bool(w and w.get("step") in ("text", "date_input", "time_input", "report"))


@router.message(F.text, _wiz_active)
async def on_wiz_text(message: Message) -> None:
    gid = _resolve_gid(message)
    uid = message.from_user.id
    wiz = ddb.get_wiz(gid, uid)
    if not wiz:
        return
    step = wiz.get("step")
    text = message.text.strip()
    if step == "report":
        slot = _sheet_person(gid, uid)
        if not slot:
            await message.answer("Ты не привязан к слоту. Сделай /iam A (или B / C).")
            ddb.del_wiz(gid, uid)
            return
        if not text:
            await message.answer("Пусто. Напиши отчёт:")
            return
        try:
            res = await asyncio.to_thread(sheets.tracker_write, slot, text, ddb.now_ts())
        except Exception as e:  # noqa: BLE001
            await message.answer(f"⚠️ Не смог записать: {str(e)[:120]}")
            ddb.del_wiz(gid, uid)
            return
        ddb.del_wiz(gid, uid)
        await message.answer(
            f"✅ Записал в <b>engnr {html.escape(slot)}</b>:\n«{html.escape(text)}»",
            reply_markup=sheet_status_kb(gid, res["row"], res["status_col"], res["line"]),
            parse_mode="HTML")
    elif step == "text":
        if not text:
            await message.answer("Пусто. Напиши текст задачи:")
            return
        ddb.wiz_set(gid, uid, step="date", text=text)
        await message.answer("На какой день?", reply_markup=wiz_date_kb())
    elif step == "date_input":
        m = re.match(r"^\s*(\d{1,2})\.(\d{1,2})\s*$", text)
        if not m or not (1 <= int(m.group(1)) <= 31 and 1 <= int(m.group(2)) <= 12):
            await message.answer("Не понял дату. Формат ДД.ММ, напр. 05.08")
            return
        ds = f"{int(m.group(1)):02d}.{int(m.group(2)):02d}"
        ddb.wiz_set(gid, uid, step="time", date=ds)
        await message.answer(f"Дата: {ds}\n\nСрок? (когда сделать)", reply_markup=wiz_time_kb())
    elif step == "time_input":
        m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", text)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            await message.answer("Формат времени ЧЧ:ММ, напр. 18:30")
            return
        try:
            conf, kb = await _finish_wiz(gid, uid, wiz, f"{int(m.group(1)):02d}:{m.group(2)}",
                                         message.chat.id, message.message_thread_id)
        except ValueError:
            await message.answer("Сначала /iam")
            ddb.del_wiz(gid, uid)
            return
        except Exception as e:  # noqa: BLE001
            await message.answer(f"⚠️ Не смог записать: {str(e)[:120]}")
            ddb.del_wiz(gid, uid)
            return
        await message.answer(conf, reply_markup=kb, parse_mode="HTML")


@router.message(F.animation, F.chat.type == "private")
async def on_gif_private(message: Message) -> None:
    await message.answer(
        f"file_id этой GIF (в bot/reactions.py):\n<code>{message.animation.file_id}</code>",
        parse_mode="HTML")


# ---------- реакции на ключевые слова (последними) ----------
@router.message(F.text)
async def on_keyword(message: Message) -> None:
    if message.text.startswith("/"):
        return
    kw = reactions.match_keyword(message.text)
    if not kw or reactions.on_cooldown(message.chat.id, kw):
        return
    gif = await reactions.get_gif(kw)
    if not gif:
        return
    try:
        await message.bot.send_animation(chat_id=message.chat.id, animation=gif,
                                         message_thread_id=message.message_thread_id)
    except Exception:  # noqa: BLE001
        pass
