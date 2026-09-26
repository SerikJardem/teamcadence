"""Local event kassa: same sale logic as Telegram /kassa, persisted in CSV."""
from __future__ import annotations

import csv
import os
from pathlib import Path

DEFAULT_ITEMS = ["Су-вид", "Гриль", "Говядина", "Фри", "Улун", "Яблоко"]

# Match Google Sheet layout: A empty, B = item, C+ = each sale as "1"
_NAME_COL = 1
_SALES_START_COL = 2
_HEADER_PAD_ROWS = 2


def default_csv_path() -> Path:
    root = Path(__file__).resolve().parent
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    return data / "sales.csv"


def _blank_row(width: int = 2) -> list[str]:
    return [""] * width


def _ensure_grid(items: list[str]) -> list[list[str]]:
    grid: list[list[str]] = [_blank_row() for _ in range(_HEADER_PAD_ROWS)]
    for item in items:
        row = _blank_row()
        while len(row) <= _NAME_COL:
            row.append("")
        row[_NAME_COL] = item
        grid.append(row)
    return grid


def load_grid(path: Path, items: list[str] | None = None) -> list[list[str]]:
    items = list(items or DEFAULT_ITEMS)
    if not path.exists() or path.stat().st_size == 0:
        return _ensure_grid(items)

    with path.open(newline="", encoding="utf-8") as f:
        grid = [list(row) for row in csv.reader(f)]

    # Drop completely empty trailing rows for easier edits, then ensure catalog.
    while grid and all(not (c or "").strip() for c in grid[-1]):
        grid.pop()

    known = {_cell(grid, ri, _NAME_COL).lower(): ri for ri in range(len(grid))}
    for item in items:
        if item.strip().lower() not in known:
            row = _blank_row()
            while len(row) <= _NAME_COL:
                row.append("")
            row[_NAME_COL] = item
            grid.append(row)
            known[item.strip().lower()] = len(grid) - 1

    if len(grid) < _HEADER_PAD_ROWS:
        grid = [_blank_row() for _ in range(_HEADER_PAD_ROWS)] + grid
    return grid


def save_grid(path: Path, grid: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(grid)


def _cell(grid: list[list[str]], ri: int, ci: int) -> str:
    if 0 <= ri < len(grid) and ci < len(grid[ri]):
        return (grid[ri][ci] or "").strip()
    return ""


def find_item_row(grid: list[list[str]], item: str) -> int | None:
    needle = item.strip().lower()
    for ri in range(len(grid)):
        if _cell(grid, ri, _NAME_COL).lower() == needle:
            return ri
    return None


def sale_total(grid: list[list[str]], ri: int) -> int:
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


def next_sale_col(grid: list[list[str]], ri: int) -> int:
    row = grid[ri] if 0 <= ri < len(grid) else []
    ci = _SALES_START_COL
    while ci < len(row) and (row[ci] or "").strip():
        ci += 1
    return ci


def totals(grid: list[list[str]], items: list[str] | None = None) -> list[dict]:
    items = list(items or DEFAULT_ITEMS)
    out = []
    for item in items:
        ri = find_item_row(grid, item)
        out.append({
            "item": item,
            "total": sale_total(grid, ri) if ri is not None else 0,
        })
    return out


def record_sale(path: Path, item: str, items: list[str] | None = None) -> dict:
    items = list(items or DEFAULT_ITEMS)
    if item not in items:
        raise ValueError(f"unknown item: {item}")
    grid = load_grid(path, items)
    ri = find_item_row(grid, item)
    if ri is None:
        raise RuntimeError(f"item row missing: {item}")
    ci = next_sale_col(grid, ri)
    while len(grid[ri]) <= ci:
        grid[ri].append("")
    grid[ri][ci] = "1"
    save_grid(path, grid)
    return {"item": item, "total": sale_total(grid, ri), "col": ci}


def items_from_env() -> list[str]:
    raw = os.getenv("EVENT_SALE_ITEMS", "").strip()
    if not raw:
        return list(DEFAULT_ITEMS)
    return [s.strip() for s in raw.split(",") if s.strip()]
