"""
DynamoDB data layer (порт P1) — мультитенантный single-table.
Схема: PK=TENANT#<gid>, SK=META|TOPIC#|USER#|TASK#|REMINDER#|EVENT#; плюс PK=REGISTRY.
GSI1 (overloaded, время): DUE#pending|CLEANUP|EXPIRE + <ts>.  GSI2: EMAIL#<gid> + email.

Имена функций повторяют bot/db.py, но добавлен первым аргументом tenant gid.
В DDB нет JOIN — поля таска (title/deadline/join_url/end) денормализуются на напоминание.
"""
import time
import uuid
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import boto3
from boto3.dynamodb.conditions import Attr, Key

from . import config

TABLE = config.DDB_TABLE

# ---- схема (single source of truth: тесты и реальное создание) ----
TABLE_SCHEMA = dict(
    TableName=TABLE,
    BillingMode="PAY_PER_REQUEST",
    AttributeDefinitions=[
        {"AttributeName": "PK", "AttributeType": "S"},
        {"AttributeName": "SK", "AttributeType": "S"},
        {"AttributeName": "GSI1PK", "AttributeType": "S"},
        {"AttributeName": "GSI1SK", "AttributeType": "N"},
        {"AttributeName": "GSI2PK", "AttributeType": "S"},
        {"AttributeName": "GSI2SK", "AttributeType": "S"},
    ],
    KeySchema=[
        {"AttributeName": "PK", "KeyType": "HASH"},
        {"AttributeName": "SK", "KeyType": "RANGE"},
    ],
    GlobalSecondaryIndexes=[
        {
            "IndexName": "GSI1",
            "KeySchema": [
                {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        },
        {
            "IndexName": "GSI2",
            "KeySchema": [
                {"AttributeName": "GSI2PK", "KeyType": "HASH"},
                {"AttributeName": "GSI2SK", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        },
    ],
)

_ddb = None


def _resource():
    global _ddb
    if _ddb is None:
        kw = {}
        if config.AWS_REGION:
            kw["region_name"] = config.AWS_REGION
        if config.DDB_ENDPOINT:
            kw["endpoint_url"] = config.DDB_ENDPOINT
        _ddb = boto3.resource("dynamodb", **kw)
    return _ddb


def _t():
    return _resource().Table(TABLE)


def create_table():
    """Создать таблицу (для тестов на moto и первичного провижининга)."""
    return _resource().create_table(**TABLE_SCHEMA)


# ---------- утилиты ----------
def now_ts() -> int:
    return int(time.time())


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _tk(gid: int) -> str:
    return f"TENANT#{gid}"


def _clean(item):
    """Decimal -> int, чтобы наверх уходили обычные числа."""
    if item is None:
        return None
    out = {}
    for k, v in item.items():
        out[k] = int(v) if isinstance(v, Decimal) else v
    return out


def _clean_list(items):
    return [_clean(i) for i in items]


# ---------- тенанты ----------
def register_tenant(gid: int, name: str = "", calendar_id: str = "", tz: str = None) -> None:
    # upsert: не затираем уже заданные calendar_id/name/tz пустыми значениями (напр. при повторном /setup)
    ex = get_tenant(gid) or {}
    t = _t()
    t.put_item(Item={
        "PK": _tk(gid), "SK": "META", "gid": gid,
        "name": name or ex.get("name", ""),
        "calendar_id": calendar_id or ex.get("calendar_id", ""),
        "tz": tz or ex.get("tz") or config.TZ,
        "active": ex.get("active", True),
        "created_at": ex.get("created_at", now_ts()),
    })
    t.put_item(Item={"PK": "REGISTRY", "SK": f"TENANT#{gid}", "gid": gid})


def get_tenant(gid: int):
    return _clean(_t().get_item(Key={"PK": _tk(gid), "SK": "META"}).get("Item"))


def set_tenant_calendar(gid: int, calendar_id: str) -> None:
    _t().update_item(
        Key={"PK": _tk(gid), "SK": "META"},
        UpdateExpression="SET calendar_id=:c",
        ExpressionAttributeValues={":c": calendar_id},
    )


def list_tenants() -> list[int]:
    r = _t().query(KeyConditionExpression=Key("PK").eq("REGISTRY"))
    return [int(i["gid"]) for i in r.get("Items", [])]


# ---------- топики ----------
def set_topic(gid: int, kind: str, chat_id: int, thread_id: int | None) -> None:
    _t().put_item(Item={
        "PK": _tk(gid), "SK": f"TOPIC#{kind}",
        "chat_id": chat_id, "thread_id": thread_id or 0,
    })


def get_topic(gid: int, kind: str):
    r = _t().get_item(Key={"PK": _tk(gid), "SK": f"TOPIC#{kind}"}).get("Item")
    if not r:
        return None
    th = int(r.get("thread_id") or 0)
    return int(r["chat_id"]), (th or None)


def dest_for_kind(gid: int, reminder_kind: str):
    """work -> топик work, call -> топик calls; иначе None (=ЛС)."""
    topic_kind = "calls" if reminder_kind == "call" else "work"
    return get_topic(gid, topic_kind)


# ---------- пользователи ----------
def upsert_user(gid: int, uid: int, username: str | None,
                role: str | None = None, email: str | None = None) -> None:
    ex = get_user(gid, uid) or {}
    item = {"PK": _tk(gid), "SK": f"USER#{uid}", "uid": uid, "username": username}
    item["role"] = role or ex.get("role") or config.DEFAULT_ROLE
    em = email if email is not None else ex.get("email")
    if em:
        item["email"] = em.strip().lower()
        item["GSI2PK"] = f"EMAIL#{gid}"
        item["GSI2SK"] = em.strip().lower()
    item["aura"] = ex.get("aura", 0)
    item["created_at"] = ex.get("created_at", now_ts())
    _t().put_item(Item=item)
    # обратный индекс членства: по uid найти все тенанты (нужно для лички)
    _t().put_item(Item={"PK": f"UMEM#{uid}", "SK": f"TENANT#{gid}", "gid": gid, "uid": uid})


def tenants_of_user(uid: int) -> list[int]:
    """Все тенанты (группы), в которых состоит пользователь — для резолва в ЛС."""
    r = _t().query(KeyConditionExpression=Key("PK").eq(f"UMEM#{uid}"))
    return [int(i["gid"]) for i in r.get("Items", [])]


def get_user(gid: int, uid: int):
    return _clean(_t().get_item(Key={"PK": _tk(gid), "SK": f"USER#{uid}"}).get("Item"))


def set_user_email(gid: int, uid: int, email: str) -> None:
    u = get_user(gid, uid) or {}
    upsert_user(gid, uid, u.get("username"), role=u.get("role"), email=email)


def get_user_by_email(gid: int, email: str):
    r = _t().query(
        IndexName="GSI2",
        KeyConditionExpression=Key("GSI2PK").eq(f"EMAIL#{gid}")
        & Key("GSI2SK").eq(email.strip().lower()),
    )
    items = r.get("Items", [])
    return _clean(items[0]) if items else None


def get_user_by_username(gid: int, username: str):
    uname = username.lstrip("@").strip().lower()
    for u in all_users(gid):
        if (u.get("username") or "").lower() == uname:
            return u
    return None


def users_email_map(gid: int) -> dict[str, int]:
    return {u["email"]: int(u["uid"]) for u in all_users(gid) if u.get("email")}


def all_users(gid: int):
    r = _t().query(KeyConditionExpression=Key("PK").eq(_tk(gid)) & Key("SK").begins_with("USER#"))
    return _clean_list(r.get("Items", []))


# ---------- настройки (key-value на тенанта) ----------
def set_setting(gid: int, key: str, value: str) -> None:
    _t().put_item(Item={"PK": _tk(gid), "SK": f"SETTING#{key}", "value": value})


def get_setting(gid: int, key: str):
    r = _t().get_item(Key={"PK": _tk(gid), "SK": f"SETTING#{key}"}).get("Item")
    return r.get("value") if r else None


def list_settings(gid: int, prefix: str = "") -> dict:
    """Все настройки тенанта с данным префиксом. Ключи возвращаются без 'SETTING#'."""
    r = _t().query(
        KeyConditionExpression=Key("PK").eq(_tk(gid)) & Key("SK").begins_with(f"SETTING#{prefix}")
    )
    out = {}
    for i in r.get("Items", []):
        out[i["SK"].split("SETTING#", 1)[1]] = i.get("value")
    return out


# ---------- мастер задачи (/new): состояние диалога ----------
def wiz_set(gid: int, uid: int, **fields) -> None:
    names, vals, sets = {}, {}, []
    for i, (k, v) in enumerate(fields.items()):
        names[f"#f{i}"] = k
        vals[f":v{i}"] = v
        sets.append(f"#f{i}=:v{i}")
    names["#ttl"] = "ttl"
    vals[":ttl"] = now_ts() + 3600
    sets.append("#ttl=:ttl")
    _t().update_item(
        Key={"PK": _tk(gid), "SK": f"WIZ#{uid}"},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names, ExpressionAttributeValues=vals,
    )


def get_wiz(gid: int, uid: int):
    return _clean(_t().get_item(Key={"PK": _tk(gid), "SK": f"WIZ#{uid}"}).get("Item"))


def uid_by_iam_name(gid: int, name: str):
    """uid участника по его sheet-имени (обратный поиск по iam:<uid>=имя)."""
    if not name:
        return None
    for key, val in list_settings(gid, "iam:").items():
        if (val or "").strip().lower() == name.strip().lower():
            try:
                return int(key.split("iam:", 1)[1])
            except (ValueError, IndexError):
                pass
    return None


def del_wiz(gid: int, uid: int) -> None:
    _t().delete_item(Key={"PK": _tk(gid), "SK": f"WIZ#{uid}"})


# ---------- задачи ----------
def create_task(gid: int, title: str, assignee_id: int, deadline_ts: int) -> str:
    tid = _new_id()
    _t().put_item(Item={
        "PK": _tk(gid), "SK": f"TASK#{tid}", "tid": tid, "title": title,
        "assignee_id": assignee_id, "deadline_ts": deadline_ts,
        "status": "open", "created_at": now_ts(),
    })
    return tid


def create_task_full(gid: int, title: str, assignee_id: int, requester_id: int,
                     deadline_ts: int, brief: str, result_format: str) -> str:
    tid = _new_id()
    _t().put_item(Item={
        "PK": _tk(gid), "SK": f"TASK#{tid}", "tid": tid, "title": title,
        "assignee_id": assignee_id, "requester_id": requester_id,
        "deadline_ts": deadline_ts, "status": "open",
        "brief": brief, "result_format": result_format, "created_at": now_ts(),
    })
    return tid


def get_task(gid: int, tid: str):
    return _clean(_t().get_item(Key={"PK": _tk(gid), "SK": f"TASK#{tid}"}).get("Item"))


def set_task_status(gid: int, tid: str, status: str) -> None:
    _t().update_item(
        Key={"PK": _tk(gid), "SK": f"TASK#{tid}"},
        UpdateExpression="SET #s=:s", ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": status},
    )


def set_task_card(gid: int, tid: str, chat_id: int, message_id: int) -> None:
    _t().update_item(
        Key={"PK": _tk(gid), "SK": f"TASK#{tid}"},
        UpdateExpression="SET card_chat_id=:c, card_message_id=:m",
        ExpressionAttributeValues={":c": chat_id, ":m": message_id},
    )


def upsert_gcal_task(gid: int, event_id: str, title: str, assignee_id: int,
                     deadline_ts: int, end_ts: int | None = None, join_url: str | None = None):
    """Детерминированный ключ = идемпотентность без GSI. Возвращает (tid, created)."""
    tid = f"GCAL#{event_id}#{assignee_id}"
    key = {"PK": _tk(gid), "SK": f"TASK#{tid}"}
    ex = _t().get_item(Key=key).get("Item")
    item = {
        **key, "tid": tid, "title": title, "assignee_id": assignee_id,
        "deadline_ts": deadline_ts, "end_ts": end_ts, "join_url": join_url,
        "status": ex.get("status") if ex else "open",
        "gcal_event_id": event_id, "created_at": ex.get("created_at") if ex else now_ts(),
    }
    _t().put_item(Item=item)
    return tid, (ex is None)


# ---------- напоминания ----------
def _put_reminder(gid, rid, task_id, assignee, fire_at, kind, status):
    task = get_task(gid, task_id) or {}
    _t().put_item(Item={
        "PK": _tk(gid), "SK": f"REMINDER#{rid}", "rid": rid, "gid": gid,
        "task_id": task_id, "assignee_id": assignee, "fire_at_ts": fire_at,
        "kind": kind, "status": status, "created_at": now_ts(),
        # денормализация полей таска (нет JOIN в DDB)
        "task_title": task.get("title", ""),
        "task_deadline": task.get("deadline_ts"),
        "task_end_ts": task.get("end_ts"),
        "task_join_url": task.get("join_url"),
        # индекс времени: пока pending — в очереди на отправку
        "GSI1PK": "DUE#pending", "GSI1SK": fire_at,
    })


def create_reminder(gid, task_id, assignee_id, fire_at_ts, kind="work") -> str:
    rid = _new_id()
    _put_reminder(gid, rid, task_id, assignee_id, fire_at_ts, kind, "pending")
    return rid


def ensure_reminder(gid, task_id, assignee_id, fire_at_ts, kind="work", slot="") -> None:
    # детерминированный rid: одно напоминание на (задача, слот). slot="" -> прежний rid
    # (совместимо), slot="after" -> отдельное напоминание «после колла».
    rid = "det-" + task_id.replace("#", "_") + (f"_{slot}" if slot else "")
    key = {"PK": _tk(gid), "SK": f"REMINDER#{rid}"}
    ex = _t().get_item(Key=key).get("Item")
    if ex is None:
        _put_reminder(gid, rid, task_id, assignee_id, fire_at_ts, kind, "pending")
    elif ex.get("status") == "pending":
        _t().update_item(
            Key=key, UpdateExpression="SET fire_at_ts=:f, GSI1SK=:f",
            ExpressionAttributeValues={":f": fire_at_ts},
        )


def get_reminder(gid: int, rid: str):
    return _clean(_t().get_item(Key={"PK": _tk(gid), "SK": f"REMINDER#{rid}"}).get("Item"))


def due_reminders(now: int):
    """Глобально по всем тенантам: GSI1 DUE#pending, fire_at <= now."""
    r = _t().query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq("DUE#pending") & Key("GSI1SK").lte(now),
    )
    return _clean_list(r.get("Items", []))


def mark_reminder(gid: int, rid: str, status: str, sent_at: int | None = None) -> None:
    """Терминальный статус: убираем из всех временных индексов (REMOVE GSI1)."""
    _t().update_item(
        Key={"PK": _tk(gid), "SK": f"REMINDER#{rid}"},
        UpdateExpression="SET #s=:s REMOVE GSI1PK, GSI1SK",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": status},
    )


def mark_reminder_sent(gid: int, rid: str, sent_at: int, chat_id: int, message_id: int) -> None:
    """Отправлено: work -> в индекс EXPIRE(sent_at); call/call_dm -> в CLEANUP(end_ts)."""
    item = get_reminder(gid, rid) or {}
    if item.get("kind") in ("call", "call_dm"):
        gpk, gsk = "CLEANUP", (item.get("task_end_ts") or sent_at)
    else:
        gpk, gsk = "EXPIRE", sent_at
    _t().update_item(
        Key={"PK": _tk(gid), "SK": f"REMINDER#{rid}"},
        UpdateExpression=("SET #s=:s, sent_at=:t, sent_chat_id=:c, sent_message_id=:m, "
                          "GSI1PK=:gp, GSI1SK=:gs"),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": "sent", ":t": sent_at, ":c": chat_id, ":m": message_id,
            ":gp": gpk, ":gs": gsk,
        },
    )


def reschedule_reminder(gid: int, rid: str, fire_at_ts: int) -> None:
    _t().update_item(
        Key={"PK": _tk(gid), "SK": f"REMINDER#{rid}"},
        UpdateExpression=("SET #s=:s, fire_at_ts=:f, GSI1PK=:gp, GSI1SK=:f "
                          "REMOVE sent_at"),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "pending", ":f": fire_at_ts, ":gp": "DUE#pending"},
    )


def stale_call_messages(now: int):
    r = _t().query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq("CLEANUP") & Key("GSI1SK").lte(now),
    )
    return _clean_list(r.get("Items", []))


def stale_sent_reminders(older_than_ts: int):
    r = _t().query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq("EXPIRE") & Key("GSI1SK").lte(older_than_ts),
    )
    return _clean_list(r.get("Items", []))


# ---------- df-напоминания (задачи из Sheet-трекера /df) ----------
def create_df_reminder(gid, uid, chat_id, thread_id, text, sheet_title, row, col, line,
                       fire_at, kind) -> str:
    rid = _new_id()
    _t().put_item(Item={
        "PK": _tk(gid), "SK": f"DREM#{rid}", "rid": rid, "gid": gid,
        "uid": uid, "chat_id": chat_id, "thread_id": thread_id or 0,
        "text": text, "sheet_title": sheet_title, "row": row, "col": col, "line": line,
        "kind": kind, "status": "open", "fire_at_ts": fire_at, "created_at": now_ts(),
        "GSI1PK": "DUE#drem", "GSI1SK": fire_at,
    })
    return rid


def due_df_reminders(now: int):
    r = _t().query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq("DUE#drem") & Key("GSI1SK").lte(now),
    )
    return _clean_list(r.get("Items", []))


def reschedule_df_reminder(gid, rid, fire_at) -> None:
    _t().update_item(
        Key={"PK": _tk(gid), "SK": f"DREM#{rid}"},
        UpdateExpression="SET fire_at_ts=:f, GSI1SK=:f",
        ExpressionAttributeValues={":f": fire_at},
    )


def df_set(gid, rid, **fields) -> None:
    """Обновить произвольные поля df-напоминания (stage/penalized и т.п.)."""
    names, vals, sets = {}, {}, []
    for i, (k, v) in enumerate(fields.items()):
        names[f"#f{i}"] = k
        vals[f":v{i}"] = v
        sets.append(f"#f{i}=:v{i}")
    _t().update_item(
        Key={"PK": _tk(gid), "SK": f"DREM#{rid}"},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names, ExpressionAttributeValues=vals,
    )


def stop_df_reminder(gid, rid) -> None:
    """Снять из очереди (терминально): убираем GSI1, но запись оставляем как closed."""
    _t().update_item(
        Key={"PK": _tk(gid), "SK": f"DREM#{rid}"},
        UpdateExpression="SET #s=:c REMOVE GSI1PK, GSI1SK",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":c": "closed"},
    )


def _open_drems_for_cell(gid, row, col, line):
    r = _t().query(KeyConditionExpression=Key("PK").eq(_tk(gid)) & Key("SK").begins_with("DREM#"))
    out = []
    for it in r.get("Items", []):
        if (it.get("status") == "open" and int(it.get("row", -1)) == int(row)
                and int(it.get("col", -1)) == int(col) and int(it.get("line", 0)) == int(line)):
            out.append(it)
    return out


def close_df_reminders(gid, row, col, line) -> int:
    """Закрыть открытые df-напоминания КОНКРЕТНОЙ строки задачи (done/skipped)."""
    n = 0
    for it in _open_drems_for_cell(gid, row, col, line):
        stop_df_reminder(gid, it["rid"]); n += 1
    return n


def snooze_df_reminders(gid, row, col, line, fire_at) -> int:
    """Отложить df-напоминания строки задачи (later) + сбросить stage/penalized,
    чтобы после снуза цикл дедлайна начался заново."""
    n = 0
    for it in _open_drems_for_cell(gid, row, col, line):
        reschedule_df_reminder(gid, it["rid"], fire_at)
        df_set(gid, it["rid"], stage="", penalized=0)
        n += 1
    return n


# ---------- ledger + aura ----------
def add_event(gid: int, uid: int, kind: str, delta_aura: int, task_id: str | None = None) -> None:
    ts = now_ts()
    _t().put_item(Item={
        "PK": _tk(gid), "SK": f"EVENT#{ts}#{uid}", "uid": uid, "kind": kind,
        "delta_aura": delta_aura, "task_id": task_id, "created_at": ts,
    })
    # атомарный аккумулятор ауры на пользователе
    _t().update_item(
        Key={"PK": _tk(gid), "SK": f"USER#{uid}"},
        UpdateExpression="ADD aura :d", ExpressionAttributeValues={":d": delta_aura},
    )


def user_aura(gid: int, uid: int) -> int:
    u = get_user(gid, uid)
    return int(u["aura"]) if u and "aura" in u else 0


def leaderboard(gid: int, limit: int = 10):
    users = [u for u in all_users(gid) if u.get("aura")]
    users.sort(key=lambda u: u.get("aura", 0), reverse=True)
    return users[:limit]


def founder_digest(gid: int):
    """Сводка за сегодня по исполнителям: done/total напоминаний тенанта."""
    z = ZoneInfo(config.TZ)
    start = int(datetime.now(z).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    r = _t().query(
        KeyConditionExpression=Key("PK").eq(_tk(gid)) & Key("SK").begins_with("REMINDER#"),
        FilterExpression=Attr("created_at").gte(start),
    )
    names = {u["uid"]: (u.get("username") or str(u["uid"])) for u in all_users(gid)}
    agg: dict[int, list[int]] = {}
    for it in r.get("Items", []):
        uid = int(it["assignee_id"])
        total, done = agg.get(uid, [0, 0])
        total += 1
        done += 1 if it.get("status") == "done" else 0
        agg[uid] = [total, done]
    return [{"name": names.get(uid, str(uid)), "total": t, "done": d}
            for uid, (t, d) in sorted(agg.items(), key=lambda kv: names.get(kv[0], ""))]
