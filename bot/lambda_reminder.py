"""Reminder Lambda: EventBridge (cron ~1 мин) -> scan_due_reminders по всем тенантам."""
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot

from . import awssecrets, config, ddb, sheets
from .scheduler import scan_cadence, scan_daily_tasks, scan_df_reminders, scan_due_reminders, scan_pushes

logging.getLogger().setLevel(logging.INFO)
awssecrets.load_from_ssm()
_date_ensured = None
_week_visibility_ensured = None


def handler(event, context):
    async def run():
        global _date_ensured, _week_visibility_ensured
        bot = Bot(token=config.BOT_TOKEN)
        try:
            now = datetime.now(ZoneInfo(config.TZ))
            today = now.strftime("%Y-%m-%d")
            if _date_ensured != today:
                try:
                    await asyncio.to_thread(sheets.tracker_ensure_dates_through, ddb.now_ts())
                    _date_ensured = today
                except Exception:  # noqa: BLE001
                    logging.getLogger("reminder").exception(
                        "Не смог автоматически добавить дату HostAI")
            week_key = now.strftime("%G-W%V")
            if now.weekday() == 0 and _week_visibility_ensured != week_key:
                try:
                    await asyncio.to_thread(sheets.tracker_hide_old_weeks, ddb.now_ts())
                    _week_visibility_ensured = week_key
                except Exception:  # noqa: BLE001
                    logging.getLogger("reminder").exception(
                        "Не смог скрыть старые недели HostAI")
            await scan_due_reminders(bot)
            await scan_cadence(bot)
            await scan_pushes(bot)
            await scan_df_reminders(bot)
            await scan_daily_tasks(bot)
        finally:
            await bot.session.close()
    asyncio.run(run())
    return {"ok": True}
