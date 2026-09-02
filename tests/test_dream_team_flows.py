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


class FakeMessage:
    def __init__(self, *, uid=388434409, photo=None, animation=None, sticker=None):
        self.chat = SimpleNamespace(type="supergroup", id=GID)
        self.from_user = SimpleNamespace(id=uid)
        self.photo = photo
        self.animation = animation
        self.sticker = sticker
        self.reply_to_message = None
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


@pytest.mark.asyncio
async def test_setmedia_waits_for_and_saves_the_next_photo(monkeypatch):
    wizard = {}
    saved = []

    monkeypatch.setattr(
        handlers.ddb,
        "wiz_set",
        lambda gid, uid, **fields: wizard.update({"gid": gid, "uid": uid, **fields}),
    )
    monkeypatch.setattr(handlers.ddb, "get_wiz", lambda gid, uid: dict(wizard))
    monkeypatch.setattr(handlers.ddb, "del_wiz", lambda gid, uid: wizard.clear())
    monkeypatch.setattr(
        handlers.media,
        "set_media",
        lambda gid, event, kind, ref: saved.append((gid, event, kind, ref)),
    )

    command_message = FakeMessage()
    await handlers.cmd_setmedia(
        command_message,
        SimpleNamespace(args="push_morning"),
    )

    assert wizard["step"] == "media_event"
    assert wizard["media_target"] == "push_morning"
    assert "следующее фото" in command_message.answers[-1][0].lower()
    assert await handlers._pending_media_active(command_message) is True

    photo_message = FakeMessage(photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="large")])
    await handlers.on_pending_media(photo_message)

    assert saved == [(GID, "push_morning", "photo", "large")]
    assert wizard == {}
    assert "сохранено" in photo_message.answers[-1][0].lower()
    assert await handlers._pending_media_active(photo_message) is False


@pytest.mark.asyncio
async def test_setcallmedia_waits_for_and_saves_the_next_animation(monkeypatch):
    wizard = {}
    saved = []

    monkeypatch.setattr(
        handlers.ddb,
        "wiz_set",
        lambda gid, uid, **fields: wizard.update({"gid": gid, "uid": uid, **fields}),
    )
    monkeypatch.setattr(handlers.ddb, "get_wiz", lambda gid, uid: dict(wizard))
    monkeypatch.setattr(handlers.ddb, "del_wiz", lambda gid, uid: wizard.clear())
    monkeypatch.setattr(
        handlers.media,
        "set_call_media",
        lambda gid, title, kind, ref: saved.append((gid, title, kind, ref)),
    )

    command_message = FakeMessage()
    await handlers.cmd_setcallmedia(
        command_message,
        SimpleNamespace(args="Sprint planning"),
    )

    assert wizard["step"] == "media_call"
    assert wizard["media_target"] == "Sprint planning"
    assert "следующее фото" in command_message.answers[-1][0].lower()

    animation_message = FakeMessage(animation=SimpleNamespace(file_id="gif-file-id"))
    await handlers.on_pending_media(animation_message)

    assert saved == [(GID, "Sprint planning", "animation", "gif-file-id")]
    assert wizard == {}
    assert "сохранено" in animation_message.answers[-1][0].lower()


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
async def test_1000_task_push_sends_one_meme_with_week_caption(monkeypatch):
    now = datetime.now(ZoneInfo(config.TZ))
    now_hhmm = now.strftime("%H:%M")
    week_emoji = "".join(f"{digit}\ufe0f\u20e3" for digit in str(now.isocalendar().week))
    expected_caption = f"W {week_emoji} - напиши задачу:"

    monkeypatch.setattr(config, "PUSH_SCHEDULE", {"push_create": now_hhmm})
    monkeypatch.setattr(scheduler.ddb, "list_tenants", lambda: [GID])
    monkeypatch.setattr(scheduler.ddb, "dest_for_kind", lambda gid, kind: (GID, 23))
    monkeypatch.setattr(scheduler.ddb, "get_setting", lambda *args: "")
    monkeypatch.setattr(scheduler.ddb, "set_setting", lambda *args: None)

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

    assert sent_media == [(
        GID, 23, meme,
        {"caption": expected_caption, "reply_markup": None, "parse_mode": None},
    )]
    assert bot.messages == []


def test_default_push_schedule_is_single_10am():
    assert config.PUSH_SCHEDULE == {"push_create": "10:00"}


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
    standup_at = int(
        datetime(2026, 9, 1, 17, 30, tzinfo=ZoneInfo(config.TZ)).timestamp()
    )
    rem = {"gid": GID, "rid": "standup-1", "task_deadline": standup_at}

    await scheduler._send_standup(bot, rem, standup_at - 30 * 60)

    assert len(bot.messages) == 1
    chat_id, text, kwargs = bot.messages[0]
    assert chat_id == GID
    assert text.startswith("<b>W</b> 3️⃣6️⃣\n")
    assert "Собрать релиз" in text and "Проверить логи" in text
    assert "StandUP через" not in text
    assert "388434409" in text
    buttons = kwargs["reply_markup"].inline_keyboard[0]
    assert [button.text for button in buttons] == ["✅ done", "🕓 later", "❌ skipped"]
