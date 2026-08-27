"""Firestore storage with the same public API as the legacy DynamoDB module.

The DynamoDB PK/SK model is preserved so production state can be copied without
a lossy transformation. Each partition becomes a document and its items live
in an ``items`` subcollection. The TeamCadence dataset is intentionally small,
so scheduler scans do not require composite Firestore indexes.
"""
from __future__ import annotations

import base64
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from .. import config

_client = None


def now_ts() -> int:
    return int(time.time())


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _tk(gid: int) -> str:
    return f"TENANT#{gid}"


def _doc_id(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _db():
    global _client
    if _client is None:
        from google.cloud import firestore

        _client = firestore.Client(project=config.GCP_PROJECT, database=config.FIRESTORE_DATABASE)
    return _client


def _partition(pk: str):
    return _db().collection(config.FIRESTORE_COLLECTION).document(_doc_id(pk))


def _ref(pk: str, sk: str):
    return _partition(pk).collection("items").document(_doc_id(sk))


def _put(item: dict) -> None:
    payload = dict(item)
    _partition(payload["PK"]).set({"pk": payload["PK"]}, merge=True)
    _ref(payload["PK"], payload["SK"]).set(payload)


def import_item(item: dict) -> None:
    """Import one raw DynamoDB item after Decimal conversion."""
    _put(item)


def _get(pk: str, sk: str):
    snapshot = _ref(pk, sk).get()
    return snapshot.to_dict() if snapshot.exists else None


def _delete(pk: str, sk: str) -> None:
    _ref(pk, sk).delete()


def _items(pk: str, prefix: str = "") -> list[dict]:
    out = []
    for snapshot in _partition(pk).collection("items").stream():
        item = snapshot.to_dict() or {}
        if not prefix or str(item.get("SK", "")).startswith(prefix):
            out.append(item)
    out.sort(key=lambda item: str(item.get("SK", "")))
    return out


def _update(pk: str, sk: str, fields: dict | None = None, remove=()) -> dict:
    from google.cloud import firestore

    ref = _ref(pk, sk)
    _partition(pk).set({"pk": pk}, merge=True)
    payload = {"PK": pk, "SK": sk, **(fields or {})}
    if remove:
        payload.update({key: firestore.DELETE_FIELD for key in remove})
    ref.set(payload, merge=True)
    return _get(pk, sk) or payload


def create_table():
    """Compatibility no-op; the database is provisioned by the deploy script."""
    return _db()


# ---------- tenants ----------
def register_tenant(gid: int, name: str = "", calendar_id: str = "", tz: str = None) -> None:
    existing = get_tenant(gid) or {}
    _put({
        "PK": _tk(gid), "SK": "META", "gid": gid,
        "name": name or existing.get("name", ""),
        "calendar_id": calendar_id or existing.get("calendar_id", ""),
        "tz": tz or existing.get("tz") or config.TZ,
        "active": existing.get("active", True),
        "created_at": existing.get("created_at", now_ts()),
    })
    _put({"PK": "REGISTRY", "SK": f"TENANT#{gid}", "gid": gid})


def get_tenant(gid: int):
    return _get(_tk(gid), "META")


def set_tenant_calendar(gid: int, calendar_id: str) -> None:
    _update(_tk(gid), "META", {"calendar_id": calendar_id})


def list_tenants() -> list[int]:
    return [int(item["gid"]) for item in _items("REGISTRY", "TENANT#")]


# ---------- topics ----------
def set_topic(gid: int, kind: str, chat_id: int, thread_id: int | None) -> None:
    _put({"PK": _tk(gid), "SK": f"TOPIC#{kind}", "chat_id": chat_id,
          "thread_id": thread_id or 0})


def get_topic(gid: int, kind: str):
    item = _get(_tk(gid), f"TOPIC#{kind}")
    if not item:
        return None
    thread_id = int(item.get("thread_id") or 0)
    return int(item["chat_id"]), (thread_id or None)


def dest_for_kind(gid: int, reminder_kind: str):
    return get_topic(gid, "calls" if reminder_kind == "call" else "work")


# ---------- users ----------
def upsert_user(gid: int, uid: int, username: str | None,
                role: str | None = None, email: str | None = None) -> None:
    existing = get_user(gid, uid) or {}
    item = {
        "PK": _tk(gid), "SK": f"USER#{uid}", "uid": uid, "username": username,
        "role": role or existing.get("role") or config.DEFAULT_ROLE,
        "aura": existing.get("aura", 0),
        "created_at": existing.get("created_at", now_ts()),
    }
    normalized_email = email if email is not None else existing.get("email")
    if normalized_email:
        item["email"] = normalized_email.strip().lower()
    _put(item)
    _put({"PK": f"UMEM#{uid}", "SK": f"TENANT#{gid}", "gid": gid, "uid": uid})


def tenants_of_user(uid: int) -> list[int]:
    return [int(item["gid"]) for item in _items(f"UMEM#{uid}", "TENANT#")]


def get_user(gid: int, uid: int):
    return _get(_tk(gid), f"USER#{uid}")


def set_user_email(gid: int, uid: int, email: str) -> None:
    user = get_user(gid, uid) or {}
    upsert_user(gid, uid, user.get("username"), role=user.get("role"), email=email)


def get_user_by_email(gid: int, email: str):
    normalized = email.strip().lower()
    return next((user for user in all_users(gid)
                 if (user.get("email") or "").lower() == normalized), None)


def get_user_by_username(gid: int, username: str):
    normalized = username.lstrip("@").strip().lower()
    return next((user for user in all_users(gid)
                 if (user.get("username") or "").lower() == normalized), None)


def users_email_map(gid: int) -> dict[str, int]:
    return {user["email"]: int(user["uid"]) for user in all_users(gid) if user.get("email")}


def all_users(gid: int):
    return _items(_tk(gid), "USER#")


# ---------- settings and wizard ----------
def set_setting(gid: int, key: str, value: str) -> None:
    _put({"PK": _tk(gid), "SK": f"SETTING#{key}", "value": value})


def get_setting(gid: int, key: str):
    item = _get(_tk(gid), f"SETTING#{key}")
    return item.get("value") if item else None


def list_settings(gid: int, prefix: str = "") -> dict:
    out = {}
    for item in _items(_tk(gid), f"SETTING#{prefix}"):
        out[item["SK"].split("SETTING#", 1)[1]] = item.get("value")
    return out


def wiz_set(gid: int, uid: int, **fields) -> None:
    fields["ttl"] = now_ts() + 3600
    _update(_tk(gid), f"WIZ#{uid}", fields)


def get_wiz(gid: int, uid: int):
    return _get(_tk(gid), f"WIZ#{uid}")


def uid_by_iam_name(gid: int, name: str):
    if not name:
        return None
    for key, value in list_settings(gid, "iam:").items():
        if (value or "").strip().lower() == name.strip().lower():
            try:
                return int(key.split("iam:", 1)[1])
            except (ValueError, IndexError):
                pass
    return None


def del_wiz(gid: int, uid: int) -> None:
    _delete(_tk(gid), f"WIZ#{uid}")


# ---------- tasks ----------
def create_task(gid: int, title: str, assignee_id: int, deadline_ts: int) -> str:
    tid = _new_id()
    _put({"PK": _tk(gid), "SK": f"TASK#{tid}", "tid": tid, "title": title,
          "assignee_id": assignee_id, "deadline_ts": deadline_ts,
          "status": "open", "created_at": now_ts()})
    return tid


def create_task_full(gid: int, title: str, assignee_id: int, requester_id: int,
                     deadline_ts: int, brief: str, result_format: str) -> str:
    tid = _new_id()
    _put({"PK": _tk(gid), "SK": f"TASK#{tid}", "tid": tid, "title": title,
          "assignee_id": assignee_id, "requester_id": requester_id,
          "deadline_ts": deadline_ts, "status": "open", "brief": brief,
          "result_format": result_format, "created_at": now_ts()})
    return tid


def get_task(gid: int, tid: str):
    return _get(_tk(gid), f"TASK#{tid}")


def set_task_status(gid: int, tid: str, status: str) -> None:
    _update(_tk(gid), f"TASK#{tid}", {"status": status})


def set_task_card(gid: int, tid: str, chat_id: int, message_id: int) -> None:
    _update(_tk(gid), f"TASK#{tid}", {"card_chat_id": chat_id, "card_message_id": message_id})


def upsert_gcal_task(gid: int, event_id: str, title: str, assignee_id: int,
                     deadline_ts: int, end_ts: int | None = None, join_url: str | None = None):
    tid = f"GCAL#{event_id}#{assignee_id}"
    existing = get_task(gid, tid)
    _put({"PK": _tk(gid), "SK": f"TASK#{tid}", "tid": tid, "title": title,
          "assignee_id": assignee_id, "deadline_ts": deadline_ts, "end_ts": end_ts,
          "join_url": join_url, "status": existing.get("status") if existing else "open",
          "gcal_event_id": event_id,
          "created_at": existing.get("created_at") if existing else now_ts()})
    return tid, (existing is None)


# ---------- reminders ----------
def _put_reminder(gid, rid, task_id, assignee, fire_at, kind, status):
    task = get_task(gid, task_id) or {}
    _put({"PK": _tk(gid), "SK": f"REMINDER#{rid}", "rid": rid, "gid": gid,
          "task_id": task_id, "assignee_id": assignee, "fire_at_ts": fire_at,
          "kind": kind, "status": status, "created_at": now_ts(),
          "task_title": task.get("title", ""), "task_deadline": task.get("deadline_ts"),
          "task_end_ts": task.get("end_ts"), "task_join_url": task.get("join_url")})


def create_reminder(gid, task_id, assignee_id, fire_at_ts, kind="work") -> str:
    rid = _new_id()
    _put_reminder(gid, rid, task_id, assignee_id, fire_at_ts, kind, "pending")
    return rid


def ensure_reminder(gid, task_id, assignee_id, fire_at_ts, kind="work", slot="") -> None:
    rid = "det-" + task_id.replace("#", "_") + (f"_{slot}" if slot else "")
    item = get_reminder(gid, rid)
    if item is None:
        _put_reminder(gid, rid, task_id, assignee_id, fire_at_ts, kind, "pending")
    elif item.get("status") == "pending":
        _update(_tk(gid), f"REMINDER#{rid}", {"fire_at_ts": fire_at_ts})


def get_reminder(gid: int, rid: str):
    return _get(_tk(gid), f"REMINDER#{rid}")


def due_reminders(now: int):
    out = []
    for gid in list_tenants():
        out.extend(item for item in _items(_tk(gid), "REMINDER#")
                   if item.get("status") == "pending" and int(item.get("fire_at_ts") or 0) <= now)
    return out


def mark_reminder(gid: int, rid: str, status: str, sent_at: int | None = None) -> None:
    fields = {"status": status}
    if sent_at is not None:
        fields["sent_at"] = sent_at
    _update(_tk(gid), f"REMINDER#{rid}", fields, remove=("GSI1PK", "GSI1SK"))


def mark_reminder_sent(gid: int, rid: str, sent_at: int, chat_id: int, message_id: int) -> None:
    _update(_tk(gid), f"REMINDER#{rid}", {
        "status": "sent", "sent_at": sent_at,
        "sent_chat_id": chat_id, "sent_message_id": message_id,
    }, remove=("GSI1PK", "GSI1SK"))


def reschedule_reminder(gid: int, rid: str, fire_at_ts: int) -> None:
    _update(_tk(gid), f"REMINDER#{rid}", {"status": "pending", "fire_at_ts": fire_at_ts},
            remove=("sent_at", "GSI1PK", "GSI1SK"))


def stale_call_messages(now: int):
    out = []
    for gid in list_tenants():
        out.extend(item for item in _items(_tk(gid), "REMINDER#")
                   if item.get("status") == "sent" and item.get("kind") in ("call", "call_dm")
                   and int(item.get("task_end_ts") or item.get("sent_at") or 0) <= now)
    return out


def stale_sent_reminders(older_than_ts: int):
    out = []
    for gid in list_tenants():
        out.extend(item for item in _items(_tk(gid), "REMINDER#")
                   if item.get("status") == "sent" and item.get("kind") not in ("call", "call_dm")
                   and int(item.get("sent_at") or 0) <= older_than_ts)
    return out


# ---------- Sheet reminder records ----------
def create_df_reminder(gid, uid, chat_id, thread_id, text, sheet_title, row, col, line,
                       fire_at, kind) -> str:
    rid = _new_id()
    _put({"PK": _tk(gid), "SK": f"DREM#{rid}", "rid": rid, "gid": gid,
          "uid": uid, "chat_id": chat_id, "thread_id": thread_id or 0, "text": text,
          "sheet_title": sheet_title, "row": row, "col": col, "line": line,
          "kind": kind, "status": "open", "fire_at_ts": fire_at, "created_at": now_ts()})
    return rid


def due_df_reminders(now: int):
    out = []
    for gid in list_tenants():
        out.extend(item for item in _items(_tk(gid), "DREM#")
                   if item.get("status") == "open" and int(item.get("fire_at_ts") or 0) <= now)
    return out


def reschedule_df_reminder(gid, rid, fire_at) -> None:
    _update(_tk(gid), f"DREM#{rid}", {"fire_at_ts": fire_at})


def df_set(gid, rid, **fields) -> None:
    _update(_tk(gid), f"DREM#{rid}", fields)


def stop_df_reminder(gid, rid) -> None:
    _update(_tk(gid), f"DREM#{rid}", {"status": "closed"}, remove=("GSI1PK", "GSI1SK"))


def _open_drems_for_cell(gid, row, col, line):
    return [item for item in _items(_tk(gid), "DREM#")
            if item.get("status") == "open" and int(item.get("row", -1)) == int(row)
            and int(item.get("col", -1)) == int(col) and int(item.get("line", 0)) == int(line)]


def close_df_reminders(gid, row, col, line) -> int:
    items = _open_drems_for_cell(gid, row, col, line)
    for item in items:
        stop_df_reminder(gid, item["rid"])
    return len(items)


def snooze_df_reminders(gid, row, col, line, fire_at) -> int:
    items = _open_drems_for_cell(gid, row, col, line)
    for item in items:
        reschedule_df_reminder(gid, item["rid"], fire_at)
        df_set(gid, item["rid"], stage="", penalized=0)
    return len(items)


# ---------- ledger and aura ----------
def add_event(gid: int, uid: int, kind: str, delta_aura: int, task_id: str | None = None) -> None:
    from google.cloud import firestore

    ts = now_ts()
    _put({"PK": _tk(gid), "SK": f"EVENT#{ts}#{uid}", "uid": uid, "kind": kind,
          "delta_aura": delta_aura, "task_id": task_id, "created_at": ts})
    _ref(_tk(gid), f"USER#{uid}").set({
        "PK": _tk(gid), "SK": f"USER#{uid}", "uid": uid,
        "aura": firestore.Increment(int(delta_aura)),
    }, merge=True)


def user_aura(gid: int, uid: int) -> int:
    user = get_user(gid, uid)
    return int(user.get("aura") or 0) if user else 0


def leaderboard(gid: int, limit: int = 10):
    users = [user for user in all_users(gid) if user.get("aura")]
    users.sort(key=lambda user: user.get("aura", 0), reverse=True)
    return users[:limit]


def founder_digest(gid: int):
    zone = ZoneInfo(config.TZ)
    start = int(datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    reminders = [item for item in _items(_tk(gid), "REMINDER#")
                 if int(item.get("created_at") or 0) >= start]
    names = {int(user["uid"]): (user.get("username") or str(user["uid"]))
             for user in all_users(gid)}
    aggregate: dict[int, list[int]] = {}
    for item in reminders:
        uid = int(item["assignee_id"])
        total, done = aggregate.get(uid, [0, 0])
        aggregate[uid] = [total + 1, done + (1 if item.get("status") == "done" else 0)]
    return [{"name": names.get(uid, str(uid)), "total": total, "done": done}
            for uid, (total, done) in sorted(aggregate.items(), key=lambda pair: names.get(pair[0], ""))]
