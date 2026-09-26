from types import SimpleNamespace

import pytest

from bot import config, event_sales, handlers, keyboards


class FakeMessage:
    def __init__(self):
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


class FakeCallback:
    def __init__(self):
        self.answers = []

    async def answer(self, text="", **kwargs):
        self.answers.append((text, kwargs))


def test_sale_kb_is_two_column_grid_with_event_items():
    kb = keyboards.sale_kb()
    flat = [btn.text for row in kb.inline_keyboard for btn in row]
    assert flat == config.EVENT_SALE_ITEMS
    assert all(len(row) <= 2 for row in kb.inline_keyboard)
    assert len(kb.inline_keyboard) == 3  # 6 items → 3 rows × 2


def test_next_sale_col_and_total_match_reference_layout():
    grid = [
        [],
        [],
        ["", "Су-вид", "1", "1"],
        ["", "Гриль", "1"],
        ["", "Говядина", "1"],
        ["", "Фри", "1", "1"],
        ["", "Улун", "1"],
        ["", "Яблоко", "1"],
    ]
    assert event_sales._find_item_row(grid, "Фри") == 5
    assert event_sales._sale_total(grid, 5) == 2
    assert event_sales._next_sale_col(grid, 5) == 4  # next empty after two 1s
    assert event_sales._next_sale_col(grid, 3) == 3  # Гриль has one 1


@pytest.mark.asyncio
async def test_kassa_posts_prompt_and_buttons(monkeypatch):
    monkeypatch.setattr(config, "EVENT_SHEET_ID", "sheet-id")
    monkeypatch.setattr(config, "EVENT_SHEET_URL", "https://example/sheet")
    monkeypatch.setattr(
        config, "EVENT_SALE_ITEMS",
        ["Су-вид", "Гриль", "Говядина", "Фри", "Улун", "Яблоко"],
    )
    msg = FakeMessage()
    await handlers.cmd_kassa(msg)
    text, kwargs = msg.answers[-1]
    assert "Отметьте продажу." in text
    assert kwargs["reply_markup"].inline_keyboard[0][0].text == "Су-вид"
    assert kwargs["reply_markup"].inline_keyboard[0][1].text == "Гриль"


@pytest.mark.asyncio
async def test_sale_callback_writes_item(monkeypatch):
    monkeypatch.setattr(
        config, "EVENT_SALE_ITEMS",
        ["Су-вид", "Гриль", "Говядина", "Фри", "Улун", "Яблоко"],
    )
    recorded = []

    def fake_record(item):
        recorded.append(item)
        return {"item": item, "total": 3, "a1": "D3"}

    monkeypatch.setattr(handlers.event_sales, "record_sale", fake_record)
    cb = FakeCallback()
    await handlers.on_sale(cb, SimpleNamespace(idx=3))
    assert recorded == ["Фри"]
    assert cb.answers[-1][0] == "✅ Фри · всего 3"
