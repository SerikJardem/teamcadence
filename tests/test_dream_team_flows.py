from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from bot import config, handlers, scheduler


GID = -1003971694622


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=len(self.messages))


@pytest.mark.asyncio
async def test_new_task_confirmation_links_to_tracker(monkeypatch):
    monkeypatch.setattr(handlers.ddb, "now_ts", lambda: 1_700_000_000)
    monkeypatch.setattr(
        handlers.sheets,
        "tracker_write",
        lambda *args, **kwargs: {
            "row": 4,
            "status_col": 6,
            "line": 0,
            "title": "HostAI",
            "date": "27.08",
            "label": "engnr C",
        },
    )
    monkeypatch.setattr(config, "TRACKER_URL", "https://tracker.example")

    text, keyboard = await handlers._create_sheet_task(
        GID, "C", None, "Собрать релиз", "27.08", None, GID, 23, 388434409
    )

    assert "Собрать релиз" in text
    assert keyboard.inline_keyboard[0][0].text == "📋 Открыть трекер"
    assert keyboard.inline_keyboard[0][0].url == "https://tracker.example"
    assert keyboard.inline_keyboard[0][0].callback_data is None


@pytest.mark.asyncio
async def test_1030_missing_tasks_sends_configured_meme_not_text(monkeypatch):
    now_hhmm = datetime.now(ZoneInfo(config.TZ)).strftime("%H:%M")
    monkeypatch.setattr(config, "PUSH_SCHEDULE", {"push_missing": now_hhmm})
    monkeypatch.setattr(scheduler.ddb, "list_tenants", lambda: [GID])
    monkeypatch.setattr(scheduler.ddb, "dest_for_kind", lambda gid, kind: (GID, 23))
    monkeypatch.setattr(scheduler.ddb, "get_setting", lambda *args: "")
    monkeypatch.setattr(scheduler.ddb, "set_setting", lambda *args: None)

    async def missing(_gid):
        return True

    monkeypatch.setattr(scheduler, "_someone_without_tasks", missing)
    meme = ("photo", "telegram-file-id")
    monkeypatch.setattr(
        scheduler.media,
        "get_media",
        lambda gid, event: meme if event == "push_missing" else None,
    )
    sent_media = []

    async def send_media(bot, chat_id, thread_id, media, **kwargs):
        sent_media.append((chat_id, thread_id, media, kwargs))

    monkeypatch.setattr(scheduler.media, "send_media", send_media)
    bot = FakeBot()

    await scheduler.scan_pushes(bot)

    assert sent_media == [(GID, 23, meme, {"caption": None, "reply_markup": None, "parse_mode": None})]
    assert bot.messages == []


@pytest.mark.asyncio
async def test_standup_prompts_only_creators_with_task_status_buttons(monkeypatch):
    monkeypatch.setattr(scheduler.ddb, "dest_for_kind", lambda gid, kind: (GID, 23))
    monkeypatch.setattr(
        scheduler.ddb,
        "list_settings",
        lambda gid, prefix: {
            "iam:1098100008": "A",
            "iam:1670559165": "B",
            "iam:388434409": "C",
        },
    )
    monkeypatch.setattr(scheduler.ddb, "mark_reminder", lambda *args: None)

    def today_tasks(slot, _now):
        if slot == "C":
            return [
                {"line": 0, "text": "Собрать релиз", "status": "todo", "row": 4, "status_col": 6},
                {"line": 1, "text": "Проверить логи", "status": "todo", "row": 4, "status_col": 6},
            ]
        return []

    monkeypatch.setattr(scheduler.sheets, "tracker_today_tasks", today_tasks)
    bot = FakeBot()
    rem = {"gid": GID, "rid": "standup-1", "task_deadline": 1_700_000_000}

    await scheduler._send_standup(bot, rem, 1_700_000_000)

    assert len(bot.messages) == 1
    chat_id, text, kwargs = bot.messages[0]
    assert chat_id == GID
    assert "Собрать релиз" in text and "Проверить логи" in text
    assert "StandUP через" not in text
    assert "388434409" in text
    buttons = kwargs["reply_markup"].inline_keyboard[0]
    assert [button.text for button in buttons] == ["✅ done", "🕓 later", "❌ skipped"]
