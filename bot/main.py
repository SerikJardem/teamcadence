"""Точка входа локального MVP: бот (long-polling) + планировщик в одном процессе."""
import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher

from . import config
from .handlers import router
from .scheduler import setup_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("main")


async def main() -> None:
    if not config.BOT_TOKEN or "PUT_YOUR_TOKEN" in config.BOT_TOKEN:
        log.error("BOT_TOKEN не задан. Скопируй .env.example в .env и вставь токен от @BotFather.")
        sys.exit(1)

    log.info("DynamoDB таблица: %s (регион: %s)", config.DDB_TABLE, config.AWS_REGION or "default")

    bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    scheduler = setup_scheduler(bot)
    scheduler.start()
    log.info("Планировщик запущен: скан каждые %sс", config.SCAN_INTERVAL_SECONDS)

    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
