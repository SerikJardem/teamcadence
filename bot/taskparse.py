"""
Парсер карточки таска. Чистые функции без сети/Telegram — легко тестировать.

Формат (/task в топике board):
    /task
    @aruzhan
    15.07 18:00
    бриф: сверстать лендинг под HoReCa
    формат: figma + ссылка
    пинги: -1д, -1ч, 0        (необязательно)

Заказчик = автор сообщения (подставляется в хендлере).
"""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

# 15.07 18:00  |  15.07.2026 18:00
_DEADLINE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\s+(\d{1,2}):(\d{2})\b")
_MENTION_RE = re.compile(r"@([A-Za-z0-9_]{3,})")
_PING_RE = re.compile(r"^-?\d+\s*[дчмdhm]$|^0$", re.IGNORECASE)
_UNIT = {"д": 86400, "d": 86400, "ч": 3600, "h": 3600, "м": 60, "m": 60}


def parse_deadline(text: str, tz: str, now_ts: int) -> int | None:
    m = _DEADLINE_RE.search(text)
    if not m:
        return None
    day, month, year, hh, mm = m.groups()
    zone = ZoneInfo(tz)
    year = int(year) if year else datetime.fromtimestamp(now_ts, zone).year
    try:
        dt = datetime(year, int(month), int(day), int(hh), int(mm), tzinfo=zone)
    except ValueError:
        return None
    ts = int(dt.timestamp())
    # если год не указан и дата уже прошла — считаем, что это следующий год
    if not m.group(3) and ts < now_ts:
        dt = dt.replace(year=year + 1)
        ts = int(dt.timestamp())
    return ts


_DF_REL = re.compile(r"(?:^|\s)!(\d+)\s*([чмдhmd])(?=\s|$)", re.IGNORECASE)
_DF_ABS = re.compile(r"(?:^|\s)(?:в\s*)?(\d{1,2}):(\d{2})(?=\s|$)")
_DF_DATE = re.compile(r"(?:^|\s)(\d{1,2})\.(\d{1,2})\s*$")   # дата DD.MM только в конце текста
_DF_UNIT = {"ч": 3600, "h": 3600, "м": 60, "m": 60, "д": 86400, "d": 86400}


def date_ts(date_str: str, hh: int, mm: int, tz: str, now_ts: int):
    """ts для 'DD.MM' в hh:mm (текущий год, локальная зона). None если дата невалидна."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        d, m = (int(x) for x in date_str.split("."))
        now = datetime.fromtimestamp(now_ts, ZoneInfo(tz))
        return int(datetime(now.year, m, d, hh, mm, tzinfo=ZoneInfo(tz)).timestamp())
    except (ValueError, TypeError):
        return None


def wiz_deadline(date_str: str, time_kind: str, tz: str, now_ts: int):
    """Дедлайн для мастера. time_kind: 'none' | 'Nh' | 'HH:MM'."""
    if not time_kind or time_kind == "none":
        return None
    if time_kind.endswith("h"):
        return now_ts + int(time_kind[:-1]) * 3600
    try:
        hh, mm = (int(x) for x in time_kind.split(":"))
    except ValueError:
        return None
    return date_ts(date_str, hh, mm, tz, now_ts)


def parse_df_when(text: str, tz: str, now_ts: int):
    """Разбирает дату и время из текста /df.
    Дата 'DD.MM' (в конце) -> в какой блок листа; время '!2ч' | 'в 18:00' -> напоминание.
    Возвращает (date_str | None, deadline_ts | None, чистый_текст)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    # 1) время (в любом месте) — вырезаем ПЕРВЫМ, чтобы дата оказалась в конце
    rel_off, abs_hm = None, None
    m = _DF_REL.search(text)
    if m:
        rel_off = int(m.group(1)) * _DF_UNIT.get(m.group(2).lower(), 60)
        text = (text[:m.start()] + " " + text[m.end():]).strip()
    else:
        m = _DF_ABS.search(text)
        if m:
            abs_hm = (int(m.group(1)), int(m.group(2)))
            text = (text[:m.start()] + " " + text[m.end():]).strip()

    # 2) дата DD.MM в конце
    date_str = None
    m = _DF_DATE.search(text)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        if 1 <= d <= 31 and 1 <= mo <= 12:                 # валидируем, чтобы «2.5» не ловить
            date_str = f"{d:02d}.{mo:02d}"
            text = text[:m.start()].strip()

    # 3) дедлайн
    deadline_ts = None
    if rel_off is not None:
        deadline_ts = now_ts + rel_off
    elif abs_hm is not None:
        base = date_str or datetime.fromtimestamp(now_ts, ZoneInfo(tz)).strftime("%d.%m")
        ts = date_ts(base, abs_hm[0], abs_hm[1], tz, now_ts)
        if ts and ts <= now_ts and not date_str:           # прошло сегодня, дата не указана -> завтра
            ts += 86400
        deadline_ts = ts
    return date_str, deadline_ts, text


def parse_pings(spec: str) -> list[int]:
    """'-1д, -1ч, 0' -> [-86400, -3600, 0] (смещения в секундах относительно дедлайна)."""
    out: list[int] = []
    for tok in spec.replace(";", ",").split(","):
        t = tok.strip().lower()
        if not t or not _PING_RE.match(t):
            continue
        if t == "0":
            out.append(0)
            continue
        num = int(re.match(r"-?\d+", t).group())
        unit = t[-1]
        out.append(num * _UNIT.get(unit, 60))
    # уникализируем и сортируем от раннего к дедлайну
    return sorted(set(out))


def _labeled(lines: list[str], *labels: str) -> str | None:
    for ln in lines:
        low = ln.lower().strip()
        for lab in labels:
            if low.startswith(lab):
                return ln.split(":", 1)[1].strip() if ":" in ln else ""
    return None


def parse_task(text: str, tz: str, now_ts: int, default_pings: list[int]) -> dict:
    """
    Возвращает {ok, error, assignee_username, deadline_ts, brief, result_format, pings, title}.
    text_mention (юзер без username) резолвится в хендлере через entities — здесь только @username.
    """
    # убираем ведущую команду
    body = re.sub(r"^/task(@\w+)?\b", "", text.strip(), flags=re.IGNORECASE).strip()
    lines = [ln for ln in body.splitlines() if ln.strip()]

    mention = _MENTION_RE.search(body)
    assignee_username = mention.group(1) if mention else None

    deadline_ts = parse_deadline(body, tz, now_ts)

    brief = _labeled(lines, "бриф", "brief", "задача")
    result_format = _labeled(lines, "формат", "format", "результат")
    pings_raw = _labeled(lines, "пинги", "pings", "напоминания")
    pings = parse_pings(pings_raw) if pings_raw else list(default_pings)

    # если бриф без метки — берём первую осмысленную строку, вычистив из неё @mention и дедлайн
    if not brief:
        for ln in lines:
            s = ln.strip()
            if ":" in s and s.split(":", 1)[0].lower() in (
                "формат", "format", "пинги", "pings", "результат", "напоминания"):
                continue
            cleaned = _DEADLINE_RE.sub("", _MENTION_RE.sub("", s)).strip(" -–—:")
            if cleaned:
                brief = cleaned
                break

    errors = []
    if not assignee_username:
        errors.append("не указан исполнитель (@username)")
    if not deadline_ts:
        errors.append("не понял дедлайн (нужно ДД.ММ ЧЧ:ММ, напр. 15.07 18:00)")
    if not brief:
        errors.append("пустой бриф")

    title = (brief or "").strip()
    if len(title) > 60:
        title = title[:57].rstrip() + "…"

    return {
        "ok": not errors,
        "error": "; ".join(errors),
        "assignee_username": assignee_username,
        "deadline_ts": deadline_ts,
        "brief": brief,
        "result_format": result_format,
        "pings": pings,
        "title": title,
    }
