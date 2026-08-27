"""Подтягивание секретов из SSM Parameter Store при холодном старте Lambda.
Локально не вызывается — там значения приходят из .env."""
import logging
import os

from . import config

log = logging.getLogger("secrets")
SSM_PREFIX = os.getenv("SSM_PREFIX", "/prod/telegram-bot-team")


def load_from_ssm() -> None:
    if config.BOT_TOKEN and config.GOOGLE_SA_JSON:
        return  # уже заданы через окружение

    import boto3
    ssm = boto3.client("ssm", region_name=config.AWS_REGION or None)

    def get(name: str) -> str:
        try:
            return ssm.get_parameter(Name=f"{SSM_PREFIX}/{name}",
                                     WithDecryption=True)["Parameter"]["Value"]
        except Exception as e:  # noqa: BLE001
            log.warning("SSM %s/%s недоступен: %s", SSM_PREFIX, name, e)
            return ""

    if not config.BOT_TOKEN:
        config.BOT_TOKEN = get("bot-token").strip()
    if not config.GOOGLE_SA_JSON:
        config.GOOGLE_SA_JSON = get("google-sa-json").strip()
