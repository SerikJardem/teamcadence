"""Webhook Lambda: API Gateway -> aiogram Dispatcher. Тенант резолвится в хендлерах из chat_id."""
import asyncio
import base64
import json
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import Update

from . import awssecrets, config
from .handlers import router

logging.getLogger().setLevel(logging.INFO)
log = logging.getLogger("webhook")

awssecrets.load_from_ssm()          # холодный старт: секреты из SSM
_dp = Dispatcher()                  # диспатчер без сессии — можно переиспользовать
_dp.include_router(router)


async def _process(update: dict) -> None:
    msg = update.get("message") or update.get("edited_message") or {}
    cbq = update.get("callback_query") or {}
    log.info("UPD keys=%s chat=%s thread=%s text=%r cb=%r",
             list(update.keys()),
             (msg.get("chat") or {}).get("id"),
             msg.get("message_thread_id"),
             msg.get("text"), cbq.get("data"))
    # Bot держит aiohttp-сессию, привязанную к loop -> создаём на инвок и закрываем
    bot = Bot(token=config.BOT_TOKEN)
    try:
        upd = Update.model_validate(update, context={"bot": bot})
        await _dp.feed_update(bot, upd)
    finally:
        await bot.session.close()


def handler(event, context):
    body = event.get("body")
    if not body:
        return {"statusCode": 200, "body": "ok"}
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    try:
        asyncio.run(_process(json.loads(body)))
    except Exception:  # noqa: BLE001
        log.exception("Ошибка обработки апдейта")
    # Telegram всегда получает 200, иначе будет ретраить
    return {"statusCode": 200, "body": "ok"}
