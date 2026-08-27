"""
Реакции на ключевые слова: увидел слово -> прислал гифку.

Два режима подбора гифки:
  1) динамически через Tenor API (бот сам ищет свежую гифку по запросу) — если задан TENOR_API_KEY;
  2) статически из KEYWORD_GIFS (заранее заданные file_id/URL) — как оверрайд/фолбэк.

Матчинг по границе слова + кулдаун — чтобы не спамить. Сетевые вызовы кэшируются.
"""
import random
import re
import time

from . import config

try:
    import aiohttp
except ImportError:  # aiohttp приходит с aiogram, но на всякий случай
    aiohttp = None

# ключевое слово -> поисковый запрос в Tenor (бот подберёт гифку сам)
KEYWORD_QUERIES: dict[str, str] = {
    "гг": "gg win",
    "деплой": "deploy",
    "пятница": "friday vibes",
    "поехали": "lets go",
    "прод": "it works production",
}

# ключевое слово -> конкретные гифки (file_id/URL). Имеет приоритет над Tenor.
KEYWORD_GIFS: dict[str, list[str]] = {}

COOLDOWN_SECONDS = 60
_last_fired: dict[tuple[int, str], int] = {}

_CACHE_TTL = 3600
_cache: dict[str, tuple[int, list[str]]] = {}   # query -> (ts, [urls])


def match_keyword(text: str) -> str | None:
    """Первое ключевое слово, найденное по границе слова, иначе None."""
    low = text.lower()
    for kw in list(KEYWORD_GIFS) + list(KEYWORD_QUERIES):
        if re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", low):
            return kw
    return None


def on_cooldown(chat_id: int, kw: str, now: int | None = None) -> bool:
    now = now if now is not None else int(time.time())
    key = (chat_id, kw)
    if now - _last_fired.get(key, 0) < COOLDOWN_SECONDS:
        return True
    _last_fired[key] = now
    return False


async def get_gif(kw: str) -> str | None:
    """Статический оверрайд, иначе динамический поиск в Tenor по запросу для этого слова."""
    if KEYWORD_GIFS.get(kw):
        return random.choice(KEYWORD_GIFS[kw])
    return await _fetch_tenor(KEYWORD_QUERIES.get(kw, kw))


async def _fetch_tenor(query: str) -> str | None:
    if not config.TENOR_API_KEY or aiohttp is None:
        return None
    now = int(time.time())
    cached = _cache.get(query)
    if cached and now - cached[0] < _CACHE_TTL:
        urls = cached[1]
    else:
        urls = await _tenor_search(query)
        if urls:
            _cache[query] = (now, urls)
    return random.choice(urls) if urls else None


async def _tenor_search(query: str) -> list[str]:
    params = {
        "q": query, "key": config.TENOR_API_KEY, "client_key": "telegram_os_mvp",
        "limit": "20", "media_filter": "gif", "contentfilter": "medium",
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://tenor.googleapis.com/v2/search",
                params=params, timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status != 200:
                    return []
                data = await r.json()
    except Exception:  # noqa: BLE001
        return []
    urls = []
    for res in data.get("results", []):
        fmts = res.get("media_formats", {})
        g = fmts.get("gif") or fmts.get("tinygif")
        if g and g.get("url"):
            urls.append(g["url"])
    return urls
