"""Касса мероприятия: кнопки товаров → «1» в Google Sheet для подсчёта продаж.

Раскладка (как в рабочем листе):
  колонка B — название товара;
  колонка C и дальше — каждая продажа = ячейка «1».
Пустая колонка A сохраняется как в исходной таблице.
"""
from __future__ import annotations

import logging

from . import config

log = logging.getLogger("event_sales")

# 0-based: B = имя, C+ = продажи
_NAME_COL = 1
_SALES_START_COL = 2


def _col_a1(col_idx: int) -> str:
    letters = ""
    c = col_idx
    while True:
        letters = chr(ord("A") + c % 26) + letters
        c = c // 26 - 1
        if c < 0:
            break
    return letters


def _a1(col_idx: int, row_idx: int) -> str:
    return f"{_col_a1(col_idx)}{row_idx + 1}"


def _sheet_id() -> str:
    sid = (config.EVENT_SHEET_ID or "").strip()
    if not sid:
        raise ValueError("EVENT_SHEET_ID is not configured")
    return sid


def _tab_title(svc) -> str:
    tab = (config.EVENT_SHEET_TAB or "").strip()
    if tab:
        return tab
    meta = svc.spreadsheets().get(spreadsheetId=_sheet_id()).execute()
    return meta["sheets"][0]["properties"]["title"]


def _read_grid(svc, title: str) -> list[list[str]]:
    rng = f"'{title}'!A1:ZZ500"
    resp = svc.spreadsheets().values().get(
        spreadsheetId=_sheet_id(), range=rng).execute()
    return resp.get("values", [])


def _cell(grid: list[list[str]], ri: int, ci: int) -> str:
    if 0 <= ri < len(grid) and ci < len(grid[ri]):
        return (grid[ri][ci] or "").strip()
    return ""


def _find_item_row(grid: list[list[str]], item: str) -> int | None:
    needle = item.strip().lower()
    for ri, row in enumerate(grid):
        if _cell(grid, ri, _NAME_COL).lower() == needle:
            return ri
    return None


def _sale_total(grid: list[list[str]], ri: int) -> int:
    """Сколько продаж в строке: сумма целых чисел в ячейках C+ (обычно все «1»)."""
    total = 0
    row = grid[ri] if 0 <= ri < len(grid) else []
    for raw in row[_SALES_START_COL:]:
        text = (raw or "").strip()
        if not text:
            continue
        try:
            total += int(float(text.replace(",", ".")))
        except ValueError:
            continue
    return total


def _next_sale_col(grid: list[list[str]], ri: int) -> int:
    row = grid[ri] if 0 <= ri < len(grid) else []
    ci = _SALES_START_COL
    while ci < len(row) and (row[ci] or "").strip():
        ci += 1
    return ci


def ensure_catalog(svc=None) -> str:
    """Гарантирует строки товаров в колонке B. Возвращает title вкладки."""
    from . import sheets as sheets_mod

    svc = svc or sheets_mod._build_service()
    title = _tab_title(svc)
    grid = _read_grid(svc, title)
    missing = [item for item in config.EVENT_SALE_ITEMS
               if _find_item_row(grid, item) is None]
    if not missing:
        return title

    start_row = len(grid)  # 0-based index of first new row
    # если лист пустой — оставим две пустые строки сверху, как в исходнике
    if start_row == 0:
        start_row = 2
    values = []
    for item in missing:
        # A пустая, B = имя
        values.append(["", item])
    rng = f"'{title}'!A{start_row + 1}"
    svc.spreadsheets().values().update(
        spreadsheetId=_sheet_id(),
        range=rng,
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()
    log.info("event_sales: добавил товары %s на «%s»", missing, title)
    return title


def record_sale(item: str) -> dict:
    """Пишет «1» в следующую свободную ячейку строки товара.
    -> {item, total, a1}"""
    from . import sheets as sheets_mod

    item = (item or "").strip()
    if item not in config.EVENT_SALE_ITEMS:
        raise ValueError(f"unknown item: {item}")

    svc = sheets_mod._build_service()
    title = ensure_catalog(svc)
    grid = _read_grid(svc, title)
    ri = _find_item_row(grid, item)
    if ri is None:
        raise RuntimeError(f"item row not found after ensure: {item}")

    ci = _next_sale_col(grid, ri)
    cell = _a1(ci, ri)
    svc.spreadsheets().values().update(
        spreadsheetId=_sheet_id(),
        range=f"'{title}'!{cell}",
        valueInputOption="USER_ENTERED",
        body={"values": [["1"]]},
    ).execute()

    # локально обновим сетку для total без повторного чтения
    while len(grid) <= ri:
        grid.append([])
    while len(grid[ri]) <= ci:
        grid[ri].append("")
    grid[ri][ci] = "1"
    return {"item": item, "total": _sale_total(grid, ri), "a1": cell}
