"""
Проактивная часть (мультитенант) — в облаке это EventBridge → Reminder/Sync Lambda.
Скан напоминаний глобальный: due_reminders отдаёт записи по всем тенантам, у каждой свой gid.
Синк календаря — цикл по тенантам, у каждого свой calendar_id.
"""
import asyncio
import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError

from . import config, ddb, gcal, media, sheets
from .keyboards import call_join_kb, reminder_kb, sheet_status_kb

# apscheduler нужен только для локального запуска (setup_scheduler); в Lambda его нет —
# импортируем внутри функции, чтобы модуль грузился в лямбде без этой зависимости.

log = logging.getLogger("scheduler")


def _mention(user_id: int, name: str | None) -> str:
    safe = html.escape(name or "коллега")
    return f'<a href="tg://user?id={user_id}">{safe}</a>'


def _week_emoji(when=None) -> str:
    """ISO-неделя цифрами-эмодзи: 35 -> 3️⃣5️⃣."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    dt = when or datetime.now(ZoneInfo(config.TZ))
    return "".join(f"{digit}\ufe0f\u20e3" for digit in str(dt.isocalendar().week))


def _mins(delta_s: int) -> str:
    m = round(delta_s / 60)
    if m <= 0:
        return "сейчас"
    if m < 60:
        return f"через {m} мин"
    return f"через {m // 60} ч {m % 60} мин" if m % 60 else f"через {m // 60} ч"


async def _emit(bot, chat_id, thread_id, m, text, kb, parse_mode=None):
    """m=(kind,ref) -> фото/гиф с подписью text и кнопками; m=None -> обычный текст."""
    if m:
        return await media.send_media(bot, chat_id, thread_id, m,
                                       caption=text, reply_markup=kb, parse_mode=parse_mode)
    return await bot.send_message(chat_id=chat_id, message_thread_id=thread_id,
                                  text=text, reply_markup=kb, parse_mode=parse_mode)


async def _send_work(bot: Bot, rem, now: int) -> None:
    """Личный таск: адресное упоминание исполнителя + кнопки. Тенант — из записи."""
    gid, rid, title, assignee = rem["gid"], rem["rid"], rem["task_title"], rem["assignee_id"]
    m = media.get_media(gid, "reminder")
    dest = ddb.dest_for_kind(gid, "work")
    if dest:
        chat_id, thread_id = dest
        user = ddb.get_user(gid, assignee)
        text = (f"🎯 {_mention(assignee, user['username'] if user else None)}, пора\n"
                f"«{html.escape(title)}»")
        await _emit(bot, chat_id, thread_id, m, text, reminder_kb(gid, rid), parse_mode="HTML")
    else:
        await _emit(bot, assignee, None, m, f"🎯 Пора\n«{title}»", reminder_kb(gid, rid))
    ddb.mark_reminder_sent(gid, rid, now, 0, 0)  # work -> индекс EXPIRE(sent_at)


async def _send_call(bot: Bot, rem, now: int) -> None:
    """Колл: без упоминаний и кнопок; id сообщения запоминаем для авто-удаления."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    gid, rid, title = rem["gid"], rem["rid"], rem["task_title"]
    start = rem.get("task_deadline") or now
    # точное время начала (фикс.) + относительное — чтобы не выглядело «рандомным»
    hhmm = datetime.fromtimestamp(start, ZoneInfo(config.TZ)).strftime("%H:%M")
    text = f"🔔 Созвон в {hhmm} ({_mins(start - now)})\n«{title}»"
    kb = call_join_kb(rem["task_join_url"]) if rem.get("task_join_url") else None
    m = media.get_call_media(gid, title)  # фото по НАЗВАНИЮ созвона; иначе общий call; иначе текст

    if rem["kind"] == "call_dm":
        target = rem["assignee_id"]
        if not target:
            ddb.mark_reminder(gid, rid, "expired")
            return
        try:
            sent = await _emit(bot, target, None, m, text, kb)
        except TelegramForbiddenError:
            # у юзера не открыт диалог с ботом — не долбим каждую минуту, гасим разово
            log.warning("call_dm: нет доступа к личке uid=%s (tenant %s) — expired", target, gid)
            ddb.mark_reminder(gid, rid, "expired")
            return
        ddb.mark_reminder_sent(gid, rid, now, target, sent.message_id)
        return

    dest = ddb.dest_for_kind(gid, "call")
    if dest:
        chat_id, thread_id = dest
        sent = await _emit(bot, chat_id, thread_id, m, text, kb)
        ddb.mark_reminder_sent(gid, rid, now, chat_id, sent.message_id)
    else:
        ddb.mark_reminder(gid, rid, "expired")


def _match_standup(title) -> bool:
    t = (title or "").lower()
    return any(k in t for k in config.STANDUP_EVENTS)


async def _send_standup(bot: Bot, rem, now: int) -> None:
    """Перед StandUP запросить статус только у создавших задачи на сегодня."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    gid, rid = rem["gid"], rem["rid"]
    start = rem.get("task_deadline") or now
    week_dt = datetime.fromtimestamp(start, ZoneInfo(config.TZ))
    week_label = f"<b>W</b> {_week_emoji(week_dt)}"
    dest = ddb.dest_for_kind(gid, "work")
    try:
        for key, slot in ddb.list_settings(gid, "iam:").items():
            if not (slot or "").strip():
                continue
            try:
                uid = int(key.split("iam:", 1)[1])
            except (ValueError, IndexError):
                continue
            tasks = await asyncio.to_thread(sheets.tracker_today_tasks, slot, now)
            if not tasks:
                continue

            # Одна статус-ячейка может относиться к нескольким строкам задач в одной
            # дневной ячейке. Группируем по координатам, чтобы кнопка меняла именно
            # тот список, который показан в сообщении.
            groups = {}
            for task in tasks:
                groups.setdefault((task["row"], task["status_col"]), []).append(task)
            for (row, col), items in groups.items():
                lines = [week_label, f"📊 {_mention(uid, f'engnr {slot}')}, отметь статус задач:"]
                lines.extend(f"• {html.escape(item['text'])}" for item in items)
                kb = sheet_status_kb(gid, row, col, items[0]["line"])
                if dest:
                    await bot.send_message(
                        dest[0], "\n".join(lines), message_thread_id=dest[1],
                        reply_markup=kb, parse_mode="HTML")
                else:
                    await bot.send_message(uid, "\n".join(lines), reply_markup=kb, parse_mode="HTML")
    except TelegramForbiddenError:
        log.warning("standup: нет доступа в work-топик (tenant %s)", gid)
    except Exception:  # noqa: BLE001
        log.exception("standup: ошибка отправки (tenant %s)", gid)
    ddb.mark_reminder(gid, rid, "done")   # терминально, повторно не шлём


def _match_report(title):
    """Если название события — отчётное (observability/merge), вернуть его конфиг, иначе None."""
    t = (title or "").lower()
    for kw, cfg in config.REPORT_EVENTS.items():
        if kw in t:
            return cfg
    return None


async def _send_report(bot: Bot, rem, now: int) -> None:
    """Событие-отчёт: DM ответственному промпт, его следующий текст уйдёт в ячейку категории."""
    gid, rid = rem["gid"], rem["rid"]
    uid = int(rem.get("assignee_id") or 0)
    cfg = _match_report(rem.get("task_title", ""))
    if not uid or not cfg:
        ddb.mark_reminder(gid, rid, "done")
        return
    ddb.wiz_set(gid, uid, step="report", category=cfg["category"])
    try:
        await bot.send_message(uid, cfg["prompt"])
    except TelegramForbiddenError:
        log.warning("report: нет доступа к личке uid=%s (tenant %s)", uid, gid)
    except Exception:  # noqa: BLE001
        log.exception("report: ошибка отправки uid=%s", uid)
    ddb.mark_reminder(gid, rid, "done")   # одноразово на это событие


async def scan_due_reminders(bot: Bot) -> None:
    now = ddb.now_ts()

    # 1) наступившие (глобально по всем тенантам)
    for rem in ddb.due_reminders(now):
        try:
            if rem["kind"] == "report":
                await _send_report(bot, rem, now)
            elif rem["kind"] == "standup_before":
                await _send_standup(bot, rem, now)
            elif rem["kind"] in ("standup_after", "call_after"):
                # пинг «после» и старые call_after отключены — тихо снимаем
                ddb.mark_reminder(rem["gid"], rem["rid"], "done")
            elif rem["kind"] in ("call", "call_dm"):
                await _send_call(bot, rem, now)
            else:
                await _send_work(bot, rem, now)
        except TelegramForbiddenError:
            log.warning("Нет доступа для rid=%s (tenant %s) — оставляю pending", rem["rid"], rem["gid"])
        except Exception:  # noqa: BLE001
            log.exception("Ошибка отправки rid=%s", rem["rid"])

    # 2) убрать сообщения о коллах, которые закончились
    for r in ddb.stale_call_messages(now):
        try:
            await bot.delete_message(r["sent_chat_id"], r["sent_message_id"])
        except Exception:  # noqa: BLE001
            pass
        ddb.mark_reminder(r["gid"], r["rid"], "done")

    # 3) авто-протухание молчунов (только личные таски)
    threshold = now - config.EXPIRE_AFTER_HOURS * 3600
    for rem in ddb.stale_sent_reminders(threshold):
        gid = rem["gid"]
        ddb.mark_reminder(gid, rem["rid"], "expired")
        ddb.set_task_status(gid, rem["task_id"], "expired")
        ddb.add_event(gid, rem["assignee_id"], "expire", config.AURA_EXPIRE, rem["task_id"])
        # опц. медиа на протухание -> в work-топик тенанта (если задано /setmedia expired)
        m = media.get_media(gid, "expired")
        dest = ddb.dest_for_kind(gid, "work")
        if m and dest:
            try:
                await _emit(bot, dest[0], dest[1], m,
                            f"💀 Протухло без ответа: «{rem.get('task_title', '')}»", None)
            except Exception:  # noqa: BLE001
                log.exception("expired media send failed for %s", rem["rid"])


async def scan_cadence(bot: Bot) -> None:
    """ОТКЛЮЧЕНО: каденс-пинги «⏰ engnr X, время отметиться» больше не шлём."""
    return
    # --- старое поведение (оставлено на случай отката) ---
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(config.TZ))
    hhmm = now.strftime("%H:%M")
    today = now.strftime("%Y-%m-%d")

    for gid in ddb.list_tenants():
        for key, name in ddb.list_settings(gid, "iam:").items():
            if not (name or "").strip():
                continue  # отвязанный/удалённый (iam очищен) — не пингуем
            try:
                uid = int(key.split("iam:", 1)[1])
            except (ValueError, IndexError):
                continue
            cad = ddb.get_setting(gid, f"cadence:{name.lower()}")
            times = [t.strip() for t in cad.split(",")] if cad else config.DEFAULT_CADENCE
            if hhmm not in times:
                continue
            if ddb.get_setting(gid, f"cadlast:{uid}") == f"{today} {hhmm}":
                continue  # уже пинганули в эту минуту
            # авто-скип: если человек больше не в группе — не пингуем и чистим его iam
            try:
                mem = await bot.get_chat_member(gid, uid)
                if getattr(mem, "status", None) in ("left", "kicked"):
                    ddb.set_setting(gid, key, "")
                    log.info("Каденс: uid=%s не в группе (%s) — отвязал iam", uid, gid)
                    continue
            except Exception:  # noqa: BLE001
                pass  # не смогли проверить — не блокируем пинг
            ddb.set_setting(gid, f"cadlast:{uid}", f"{today} {hhmm}")

            text = (f"⏰ {_mention(uid, f'engnr {name}')}, время отметиться.\n"
                    "Добавь задачу через /new и проставь статусы.")
            dest = ddb.dest_for_kind(gid, "work")
            try:
                if dest:
                    chat_id, thread_id = dest
                    await bot.send_message(chat_id, text, message_thread_id=thread_id,
                                           parse_mode="HTML")
                else:
                    await bot.send_message(uid, text, parse_mode="HTML")
            except TelegramForbiddenError:
                log.warning("Каденс: нет доступа написать %s (tenant %s)", name, gid)
            except Exception:  # noqa: BLE001
                log.exception("Каденс: ошибка пинга %s", name)


def _push_media(gid: int, event: str):
    """Мем для планового пуша: сначала событие из расписания, потом соседние ключи."""
    m = media.get_media(gid, event)
    if m:
        return m
    for fallback in ("push_create", "push_missing", "push_morning"):
        if fallback == event:
            continue
        m = media.get_media(gid, fallback)
        if m:
            return m
    return None


async def scan_pushes(bot: Bot) -> None:
    """Один плановый мем в work-топик (config.PUSH_SCHEDULE, локальное HH:MM).
    Время можно переопределить настройкой pushtime:<event>. Картинка обязательна;
    подпись — «W <неделя> - напиши задачу:»."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(config.TZ))
    hhmm = now.strftime("%H:%M")
    today = now.strftime("%Y-%m-%d")
    caption = f"W {_week_emoji(now)} - напиши задачу:"

    for gid in ddb.list_tenants():
        dest = ddb.dest_for_kind(gid, "work")
        if not dest:
            continue
        for event, default_time in config.PUSH_SCHEDULE.items():
            when = ddb.get_setting(gid, f"pushtime:{event}") or default_time
            if when != hhmm:
                continue
            if ddb.get_setting(gid, f"pushlast:{event}") == f"{today} {hhmm}":
                continue
            m = _push_media(gid, event)
            if not m:
                continue  # плановый пуш без настроенного мема не заменяем текстом
            ddb.set_setting(gid, f"pushlast:{event}", f"{today} {hhmm}")
            try:
                await _emit(bot, dest[0], dest[1], m, caption, None)
            except Exception:  # noqa: BLE001
                log.exception("Плановый пуш %s: ошибка (tenant %s)", event, gid)


async def _someone_without_tasks(gid) -> bool:
    """True, если хотя бы у одного привязанного (/iam) человека нет задач на сегодня."""
    import asyncio

    from . import sheets
    mapped = {n.strip().lower() for n in ddb.list_settings(gid, "iam:").values() if n}
    if not mapped:
        return False
    try:
        tasks = await asyncio.to_thread(sheets.tracker_read_today, ddb.now_ts())
    except Exception:  # noqa: BLE001
        return True   # не смогли прочитать — на всякий пушим
    return any(not tasks.get(p) for p in tasks if p.strip().lower() in mapped)


async def scan_df_reminders(bot: Bot) -> None:
    """ОТКЛЮЧЕНО: пинги о дедлайне, о прошедшем дедлайне и авто-добивание убраны.
    Задачи отмечаются кнопками done/later/skipped прямо под карточкой при создании.
    Легаси-DREM'ы, которые ещё висят с прошлого поведения и становятся due, тихо
    снимаем (без сообщений), чтобы они не копились в индексе."""
    now = ddb.now_ts()
    for rem in ddb.due_df_reminders(now):
        try:
            ddb.stop_df_reminder(rem["gid"], rem["rid"])
        except Exception:  # noqa: BLE001
            pass


async def scan_daily_tasks(bot: Bot) -> None:
    """ОТКЛЮЧЕНО: ежедневный дайджест «Задачи на сегодня» + лидерборд ауры больше не шлём."""
    return
    # --- старое поведение (оставлено на случай отката) ---
    import asyncio
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from . import sheets

    now = datetime.now(ZoneInfo(config.TZ))
    if now.strftime("%H:%M") != config.DAILY_TASK_TIME:
        return
    today = now.strftime("%Y-%m-%d")
    mark = {"done": "✅", "skipped": "❌", "later": "🕓"}

    for gid in ddb.list_tenants():
        if ddb.get_setting(gid, "dailylast") == today:
            continue
        ddb.set_setting(gid, "dailylast", today)
        try:
            tasks = await asyncio.to_thread(sheets.tracker_read_today, ddb.now_ts())
        except Exception:  # noqa: BLE001
            log.exception("daily: не смог прочитать лист (tenant %s)", gid)
            continue
        name2uid = {}
        for key, name in ddb.list_settings(gid, "iam:").items():
            try:
                name2uid[name.lower()] = int(key.split("iam:", 1)[1])
            except (ValueError, IndexError):
                pass

        # 1) в личку каждому привязанному
        for person, items in tasks.items():
            uid = name2uid.get(person.lower())
            if not uid:
                continue
            if items:
                body = "📋 Твои задачи на сегодня:\n" + "\n".join(
                    f"{mark.get(st, '⬜')} {html.escape(txt)}"
                    for lbl, txt, st in items) + "\n\nОтмечай статусы кнопками под /new."
            else:
                body = ("☀️ Доброе утро! Задач на сегодня ещё нет — создай через /new.")
            try:
                await bot.send_message(uid, body, parse_mode="HTML")
            except Exception:  # noqa: BLE001
                log.warning("daily: не смог написать в личку %s", person)

        # 2) в группу с разбивкой по юзерам
        dest = ddb.dest_for_kind(gid, "work")
        if dest:
            lines = [f"📋 <b>Задачи на сегодня</b> ({now.strftime('%d.%m')})"]
            for person, items in tasks.items():
                uid = name2uid.get(person.lower())
                if not uid and not items:
                    continue
                who = _mention(uid, f"engnr {person}") if uid else f"engnr {html.escape(person)}"
                lines.append(f"\n{who}:")
                if items:
                    for lbl, txt, st in items:
                        lines.append(f"  {mark.get(st, '⬜')} {html.escape(txt)}")
                else:
                    lines.append("  — нет задач")
            # лидерборд по ауре
            lb = ddb.leaderboard(gid, 5)
            if lb:
                uid2name = {}
                for key, nm in ddb.list_settings(gid, "iam:").items():
                    try:
                        uid2name[int(key.split("iam:", 1)[1])] = nm
                    except (ValueError, IndexError):
                        pass
                lines.append("\n🏆 <b>Аура</b>:")
                for i, u in enumerate(lb, 1):
                    disp = uid2name.get(int(u["uid"])) or u.get("username") or str(u["uid"])
                    lines.append(f"  {i}. {html.escape(str(disp))} — {u.get('aura', 0)}")
            try:
                await bot.send_message(dest[0], "\n".join(lines),
                                       message_thread_id=dest[1], parse_mode="HTML")
                m_top = media.get_media(gid, "top_aura")
                if m_top:
                    await _emit(bot, dest[0], dest[1], m_top, None, None)
            except Exception:  # noqa: BLE001
                log.exception("daily: не смог запостить в группу (tenant %s)", gid)


async def sync_calendar(bot: Bot) -> None:
    """Цикл по тенантам: у каждого свой calendar_id, расшаренный на общий SA."""
    if not gcal.is_configured():
        return
    now = ddb.now_ts()
    lead = config.CALENDAR_LEAD_MINUTES * 60

    for gid in ddb.list_tenants():
        ten = ddb.get_tenant(gid)
        cal = ten.get("calendar_id") if ten else None
        if not ten or not ten.get("active", True) or not cal:
            continue
        try:
            raw = await asyncio.to_thread(gcal.fetch_upcoming, cal, config.CALENDAR_HORIZON_HOURS)
        except Exception:  # noqa: BLE001
            log.exception("Тенант %s: не удалось получить события календаря", gid)
            continue

        mapping = ddb.users_email_map(gid)
        parsed, unknown = gcal.parse_events(raw, mapping, now, lead)
        created = 0
        for ev in parsed:
            # Событие-отчёт (observability/merge) сохраняет персональный запрос отчёта.
            # Ниже для него также создаётся общий call-пуш, как для любого события календаря.
            rep = _match_report(ev["title"])
            if rep:
                owner = ddb.get_setting(gid, f"reportowner:{rep['category']}")
                owner_uid = ddb.uid_by_iam_name(gid, owner) if owner else None
                if owner_uid:
                    tid, is_new = ddb.upsert_gcal_task(
                        gid, ev["event_id"], ev["title"], owner_uid, ev["deadline_ts"],
                        end_ts=ev["end_ts"])
                    ddb.ensure_reminder(gid, tid, owner_uid, ev["fire_at_ts"], kind="report")
                    created += int(is_new)
            # стендапы (StandUP/SyncUP): короткий пинг «за 30 мин» + КОЛЛ-уведомление.
            # Пинг «после» (standup_after) не создаём.
            if _match_standup(ev["title"]):
                tid, is_new = ddb.upsert_gcal_task(
                    gid, ev["event_id"], ev["title"], 0, ev["deadline_ts"],
                    end_ts=ev["end_ts"], join_url=ev["join_url"])
                before_at = ev["deadline_ts"] - config.STANDUP_BEFORE_MINUTES * 60
                ddb.ensure_reminder(gid, tid, 0, max(before_at, now),
                                    kind="standup_before", slot="before")
                ddb.ensure_reminder(gid, tid, 0, ev["fire_at_ts"], kind="call", slot="call")
                created += int(is_new)
                continue

            # Любое обычное событие общего календаря всегда идёт одним пушем в calls-топик.
            # Участники и привязка /email больше не влияют на создание уведомления.
            tid, is_new = ddb.upsert_gcal_task(
                gid, ev["event_id"], ev["title"], 0, ev["deadline_ts"],
                end_ts=ev["end_ts"], join_url=ev["join_url"])
            ddb.ensure_reminder(gid, tid, 0, ev["fire_at_ts"], kind="call")
            created += int(is_new)
        if created:
            log.info("Тенант %s: новых календарных уведомлений %s из %s событий",
                     gid, created, len(raw))


def setup_scheduler(bot: "Bot"):
    from datetime import datetime, timedelta

    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    scheduler = AsyncIOScheduler(timezone=config.TZ)
    scheduler.add_job(
        scan_due_reminders, "interval", seconds=config.SCAN_INTERVAL_SECONDS,
        args=[bot], id="scan_due_reminders", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        scan_cadence, "interval", seconds=config.CADENCE_SCAN_SECONDS,
        args=[bot], id="scan_cadence", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        scan_pushes, "interval", seconds=config.CADENCE_SCAN_SECONDS,
        args=[bot], id="scan_pushes", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        scan_df_reminders, "interval", seconds=config.SCAN_INTERVAL_SECONDS,
        args=[bot], id="scan_df_reminders", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        scan_daily_tasks, "interval", seconds=config.CADENCE_SCAN_SECONDS,
        args=[bot], id="scan_daily_tasks", max_instances=1, coalesce=True,
    )
    if gcal.is_configured():
        scheduler.add_job(
            sync_calendar, "interval", seconds=config.CALENDAR_SYNC_INTERVAL_SECONDS,
            args=[bot], id="sync_calendar", max_instances=1, coalesce=True,
            next_run_time=datetime.now() + timedelta(seconds=3),
        )
        log.info("Google Calendar sync включён (каждые %sс), цикл по тенантам",
                 config.CALENDAR_SYNC_INTERVAL_SECONDS)
    else:
        log.info("Google Calendar sync выключен (нет SA-ключа)")
    return scheduler
