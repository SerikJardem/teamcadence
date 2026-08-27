"""
Реестр медиа по событиям бота (масштабируемо).

Одно фото/гиф на событие (call / reminder / done / later / skip / expired),
хранится per-тенант в settings под ключом media:<event> в виде "kind|ref",
где kind ∈ {photo, animation}, ref = Telegram file_id или URL.

Использование:
    m = media.get_media(gid, "done")
    if m:
        await media.send_media(bot, chat_id, thread_id, m, caption=..., reply_markup=...)

Добавить новое событие = добавить ключ в config.MEDIA_EVENTS и позвать get_media
в нужном месте. Больше ничего трогать не надо.
"""
import os

from . import config, ddb


def _kind_for_url(ref: str) -> str:
    return "animation" if ref.lower().split("?")[0].endswith(".gif") else "photo"


def pack(kind: str, ref: str) -> str:
    return f"{kind}|{ref}"


def get_media(gid: int, event: str):
    """Возвращает (kind, ref) или None. Порядок: настройка тенанта -> env -> легаси."""
    v = ddb.get_setting(gid, f"media:{event}")
    if not v:
        v = os.getenv(f"MEDIA_{event.upper()}", "").strip()
    if not v and event == "call":  # обратная совместимость со старым call_photo
        old = ddb.get_setting(gid, "call_photo") or config.CALL_PHOTO or ""
        if old:
            v = pack(_kind_for_url(old), old)
    if not v:
        return None
    if "|" in v:
        kind, ref = v.split("|", 1)
        return kind, ref
    return _kind_for_url(v), v


def _norm(s: str) -> str:
    """Нормализация для матчинга: только буквы/цифры, нижний регистр.
    'Sync-Up' и 'SyncUP' -> 'syncup'; 'Sprint planning' -> 'sprintplanning'."""
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def get_call_media(gid: int, title: str):
    """Медиа созвона по НАЗВАНИЮ события (из календаря). Ищем ключевое слово,
    заданное через /setcallmedia (settings callmedia:<kw>), которое входит в title.
    Матчинг устойчив к дефисам/пробелам/регистру (sync-up == SyncUP).
    Если совпадений нет — общий фолбэк media:call. -> (kind, ref) | None."""
    title_n = _norm(title)
    best = None
    for key, val in ddb.list_settings(gid, "callmedia:").items():
        if not val:
            continue
        kw = _norm(key.split("callmedia:", 1)[1])
        if kw and kw in title_n and (best is None or len(kw) > best[0]):
            best = (len(kw), val)   # самое длинное (специфичное) совпадение
    if best:
        v = best[1]
        if "|" in v:
            kind, ref = v.split("|", 1)
            return kind, ref
        return _kind_for_url(v), v
    return get_media(gid, "call")


def set_call_media(gid: int, keyword: str, kind: str, ref: str) -> None:
    ddb.set_setting(gid, f"callmedia:{keyword.strip().lower()}", pack(kind, ref))


def clear_call_media(gid: int, keyword: str) -> None:
    ddb.set_setting(gid, f"callmedia:{keyword.strip().lower()}", "")


def list_call_media(gid: int) -> dict:
    """{keyword: (kind, ref)} для заданных фото созвонов."""
    out = {}
    for key, val in ddb.list_settings(gid, "callmedia:").items():
        if not val:
            continue
        kw = key.split("callmedia:", 1)[1]
        out[kw] = tuple(val.split("|", 1)) if "|" in val else (_kind_for_url(val), val)
    return out


def set_media(gid: int, event: str, kind: str, ref: str) -> None:
    ddb.set_setting(gid, f"media:{event}", pack(kind, ref))


def clear_media(gid: int, event: str) -> None:
    ddb.set_setting(gid, f"media:{event}", "")


async def send_media(bot, chat_id, thread_id, media, caption=None, reply_markup=None,
                     parse_mode=None):
    """Отправляет фото / гиф / стикер. Возвращает Message с подписью (для трекинга).
    Стикер не поддерживает caption: если есть текст/кнопка — шлём их отдельным
    сообщением после стикера и возвращаем именно его."""
    kind, ref = media
    if kind == "sticker":
        await bot.send_sticker(chat_id=chat_id, message_thread_id=thread_id, sticker=ref)
        if caption or reply_markup:
            return await bot.send_message(
                chat_id=chat_id, message_thread_id=thread_id,
                text=caption or " ", reply_markup=reply_markup, parse_mode=parse_mode)
        return None
    if kind == "animation":
        return await bot.send_animation(
            chat_id=chat_id, message_thread_id=thread_id, animation=ref,
            caption=caption, reply_markup=reply_markup, parse_mode=parse_mode)
    return await bot.send_photo(
        chat_id=chat_id, message_thread_id=thread_id, photo=ref,
        caption=caption, reply_markup=reply_markup, parse_mode=parse_mode)
