"""HTTP entrypoint for TeamCadence services on Google Cloud Run."""
from __future__ import annotations

import asyncio
import hmac
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request

from . import config, ddb, sheets
from .handlers import router
from .scheduler import (
    scan_cadence,
    scan_daily_tasks,
    scan_df_reminders,
    scan_due_reminders,
    scan_pushes,
    sync_calendar,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cloudrun")

app = FastAPI(title="TeamCadence", docs_url=None, redoc_url=None)
dispatcher = Dispatcher()
dispatcher.include_router(router)

_date_ensured: str | None = None
_week_visibility_ensured: str | None = None


def _mode_allows(*allowed: str) -> bool:
    return config.SERVICE_MODE in {*allowed, "all"}


def _require_token() -> str:
    if not config.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not configured")
    return config.BOT_TOKEN


async def process_update(payload: dict) -> None:
    bot = Bot(token=_require_token())
    try:
        update = Update.model_validate(payload, context={"bot": bot})
        await dispatcher.feed_update(bot, update)
    finally:
        await bot.session.close()


async def run_reminders() -> None:
    global _date_ensured, _week_visibility_ensured

    bot = Bot(token=_require_token())
    try:
        now = datetime.now(ZoneInfo(config.TZ))
        today = now.strftime("%Y-%m-%d")
        if _date_ensured != today:
            try:
                await asyncio.to_thread(sheets.tracker_ensure_dates_through, ddb.now_ts())
                _date_ensured = today
            except Exception:  # noqa: BLE001
                log.exception("Could not extend Tracker-HostAI dates")

        week_key = now.strftime("%G-W%V")
        if now.weekday() == 0 and _week_visibility_ensured != week_key:
            try:
                await asyncio.to_thread(sheets.tracker_hide_old_weeks, ddb.now_ts())
                _week_visibility_ensured = week_key
            except Exception:  # noqa: BLE001
                log.exception("Could not hide old Tracker-HostAI weeks")

        await scan_due_reminders(bot)
        await scan_cadence(bot)
        await scan_pushes(bot)
        await scan_df_reminders(bot)
        await scan_daily_tasks(bot)
    finally:
        await bot.session.close()


async def run_sync() -> None:
    bot = Bot(token=_require_token())
    try:
        await sync_calendar(bot)
    finally:
        await bot.session.close()


@app.get("/health")
async def healthz():
    return {
        "ok": True,
        "service_mode": config.SERVICE_MODE,
        "storage": config.STORAGE_BACKEND,
    }


@app.post("/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    if not _mode_allows("webhook"):
        raise HTTPException(status_code=404, detail="Webhook is disabled for this service")
    expected = config.TELEGRAM_WEBHOOK_SECRET
    if not expected or not x_telegram_bot_api_secret_token or not hmac.compare_digest(
        expected, x_telegram_bot_api_secret_token
    ):
        raise HTTPException(status_code=403, detail="Invalid Telegram webhook secret")
    payload = await request.json()
    try:
        await process_update(payload)
    except Exception:  # noqa: BLE001
        # Telegram must receive 200 or it retries the same update.
        log.exception("Telegram update failed")
    return {"ok": True}


@app.post("/run")
async def scheduled_run():
    if _mode_allows("reminder"):
        await run_reminders()
        return {"ok": True, "job": "reminder"}
    if _mode_allows("sync"):
        await run_sync()
        return {"ok": True, "job": "sync"}
    raise HTTPException(status_code=404, detail="Scheduled run is disabled for this service")
