"""Sync Lambda: EventBridge (cron ~2-5 мин) -> sync_calendar циклом по тенантам."""
import asyncio
import logging

from aiogram import Bot

from . import awssecrets, config
from .scheduler import sync_calendar

logging.getLogger().setLevel(logging.INFO)
awssecrets.load_from_ssm()


def handler(event, context):
    async def run():
        bot = Bot(token=config.BOT_TOKEN)
        try:
            await sync_calendar(bot)
        finally:
            await bot.session.close()
    asyncio.run(run())
    return {"ok": True}
