"""
Google Calendar sync — локальный аналог Sync Lambda.
Единственный модуль, который ходит в Google. Всё, что связано с идентичностью
(«кто есть кто»), собрано в parse_events / resolve_assignees — это чистые функции
без сети, чтобы их можно было тестировать.
"""
import datetime as dt
import logging
import os
import re
from zoneinfo import ZoneInfo

from . import config

_URL_RE = re.compile(r"https?://\S+")

log = logging.getLogger("gcal")

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
_service = None  # кэш build() — discovery дорогой


# ---------- чистая логика (тестируется без сети) ----------
def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def _iso_ts(node: dict) -> int | None:
    iso = node.get("dateTime")
    if not iso:
        return None
    d = dt.datetime.fromisoformat(iso)
    if d.tzinfo is None:
        # защита: наивную дату трактуем как таймзону события (или тенанта),
        # иначе .timestamp() возьмёт локальную зону раннера -> сдвиг времени
        try:
            d = d.replace(tzinfo=ZoneInfo(node.get("timeZone") or config.TZ))
        except Exception:  # noqa: BLE001
            d = d.replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp())


def resolve_participants(ev: dict, users_by_email: dict[str, int]) -> tuple[list[int], list[str]]:
    """
    Возвращает (known_ids, unknown_emails) — сколько людей из нашей базы касается событие.
    Учитываем attendees (кроме declined и комнат) + организатора. Дубли схлопываем.
    По числу known решаем: 1–2 -> в личку каждому, 3+ -> общий колл в топик.
    """
    known: set[int] = set()
    unknown: list[str] = []
    for a in ev.get("attendees", []) or []:
        if a.get("responseStatus") == "declined" or a.get("resource"):
            continue
        em = normalize_email(a.get("email"))
        if not em:
            continue
        uid = users_by_email.get(em)
        if uid:
            known.add(uid)
        else:
            unknown.append(em)

    org_id = users_by_email.get(normalize_email(ev.get("organizer", {}).get("email")))
    if org_id:
        known.add(org_id)
    return sorted(known), unknown


def extract_join_link(ev: dict) -> str | None:
    """Ссылка на созвон: Google Meet (hangoutLink / conferenceData) или URL из location/description."""
    if ev.get("hangoutLink"):
        return ev["hangoutLink"]
    for ep in (ev.get("conferenceData", {}) or {}).get("entryPoints", []) or []:
        if ep.get("entryPointType") == "video" and ep.get("uri", "").startswith("http"):
            return ep["uri"]
    m = _URL_RE.search(ev.get("location", "") or "")
    if m:
        return m.group(0)
    # в описании берём только ссылки известных платформ, чтобы не хватать случайные url
    for m in _URL_RE.finditer(ev.get("description", "") or ""):
        if re.search(r"(zoom|meet\.google|teams\.microsoft|whereby|webex)", m.group(0), re.I):
            return m.group(0)
    return None


def parse_events(raw_events, users_by_email, now_ts: int, lead_seconds: int):
    """
    Сырые события Google -> одна запись на событие:
    {event_id, title, fire_at_ts, deadline_ts (start), end_ts, known_ids}.
    Плюс множество unknown email (для лога/уведомления фаундера).
    """
    out = []
    unknown_all: set[str] = set()

    for ev in raw_events:
        if ev.get("status") == "cancelled":
            continue
        start_ts = _iso_ts(ev.get("start", {}))
        if start_ts is None or start_ts <= now_ts:  # all-day или уже началось/прошло
            continue
        end_ts = _iso_ts(ev.get("end", {})) or (start_ts + 3600)

        fire_at = max(start_ts - lead_seconds, now_ts)
        known, unknown = resolve_participants(ev, users_by_email)
        unknown_all.update(unknown)

        out.append({
            "event_id": ev["id"],
            "title": ev.get("summary") or "(без названия)",
            "fire_at_ts": fire_at,
            "deadline_ts": start_ts,
            "end_ts": end_ts,
            "known_ids": known,
            "join_url": extract_join_link(ev),
        })

    return out, unknown_all


# ---------- сетевая часть ----------
def is_configured() -> bool:
    # SA-ключ общий для всех тенантов; calendar_id теперь у каждого тенанта свой.
    if config.GOOGLE_SA_JSON:
        return True
    return bool(
        config.GOOGLE_SERVICE_ACCOUNT_FILE
        and os.path.exists(config.GOOGLE_SERVICE_ACCOUNT_FILE)
    )


def _build_service():
    global _service
    if _service is not None:
        return _service
    import json as _json

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    if config.GOOGLE_SA_JSON:
        creds = service_account.Credentials.from_service_account_info(
            _json.loads(config.GOOGLE_SA_JSON), scopes=SCOPES)
    else:
        creds = service_account.Credentials.from_service_account_file(
            config.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    _service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return _service


def fetch_upcoming(calendar_id: str, horizon_hours: int) -> list[dict]:
    """Блокирующий вызов — из планировщика зови через asyncio.to_thread."""
    service = _build_service()
    now = dt.datetime.now(dt.timezone.utc)
    tmax = now + dt.timedelta(hours=horizon_hours)
    resp = (
        service.events()
        .list(
            calendarId=calendar_id,
            timeMin=now.isoformat(),
            timeMax=tmax.isoformat(),
            singleEvents=True,   # разворачиваем повторяющиеся в инстансы (у каждого свой id)
            orderBy="startTime",
            maxResults=50,
        )
        .execute()
    )
    return resp.get("items", [])
