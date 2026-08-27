"""
Запись командных тасков в общий Google Sheet.
Единственный модуль, который пишет в Sheets. Логика поиска строки/колонки —
чистые функции (locate_columns / locate_row / a1), тестируются без сети.

Раскладка листа (см. шапку):
  A: метка категории (DEEP FOCUS / Short Tasks / Maintenance / merge duplicate / observability)
  B: дата (DD.MM) — только на строке DEEP FOCUS (первая строка блока дня)
  далее пары колонок:  <Имя> , <Имя>-status , <Имя> , <Имя>-status , ...

SA тот же, что у календаря, но со scope Sheets. Лист должен быть расшарен
на SA-email как Editor, а в GCP-проекте включён Google Sheets API.
"""
import logging
import os
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import config

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
log = logging.getLogger("sheets")

_service = None
_tab_title_cache = None


class SheetError(Exception):
    """Понятная человеку ошибка записи (показываем в Telegram)."""


# ---------- чистая логика (тестируется без сети) ----------
def col_a1(col_idx: int) -> str:
    """0-based индекс колонки -> буквы A1 (0->A, 26->AA)."""
    letters = ""
    c = col_idx
    while True:
        letters = chr(ord("A") + c % 26) + letters
        c = c // 26 - 1
        if c < 0:
            break
    return letters


def a1(col_idx: int, row_idx: int) -> str:
    """0-based (col,row) -> 'B4'."""
    return f"{col_a1(col_idx)}{row_idx + 1}"


def _cell(grid, ri, ci) -> str:
    if 0 <= ri < len(grid) and ci < len(grid[ri]):
        return (grid[ri][ci] or "").strip()
    return ""


def locate_columns(grid):
    """
    Шапка = первая строка, где есть пары «X» и «X-status». Люди детектятся ДИНАМИЧЕСКИ
    из самой шапки (не из хардкода) — /adduser и /removeuser работают без правки кода.
    -> (header_row_idx, {name: value_col}, {name: status_col}); (None,{},{}) если не нашли.
    """
    for ri, row in enumerate(grid):
        cells = {}
        for ci, raw in enumerate(row):
            c = (raw or "").strip()
            if c:
                cells[c] = ci
        val, st = {}, {}
        for name, ci in cells.items():
            if name.lower().endswith("-status"):
                continue
            stat_key = next((k for k in cells if k.lower() == f"{name.lower()}-status"), None)
            if stat_key is not None:
                val[name] = ci
                st[name] = cells[stat_key]
        if val:                                  # нашли хотя бы одну пара -> это шапка
            order = sorted(val, key=lambda n: val[n])   # слева направо
            return ri, {n: val[n] for n in order}, {n: st[n] for n in order}
    return None, {}, {}


def locate_row(grid, date_str: str, category_label: str, header_idx: int):
    """
    Строка нужной категории внутри блока даты. date_str = 'DD.MM'.
    Блок = от строки, где колонка B == date_str, до следующей непустой B. -> row_idx | None.
    """
    block_start = None
    for ri in range(header_idx + 1, len(grid)):
        if _cell(grid, ri, 1) == date_str:
            block_start = ri
            break
    if block_start is None:
        return None
    block_end = len(grid)
    for ri in range(block_start + 1, len(grid)):
        if _cell(grid, ri, 1):  # следующая дата -> конец блока
            block_end = ri
            break
    for ri in range(block_start, block_end):
        if _cell(grid, ri, 0).lower() == category_label.lower():
            return ri
    return None


def date_str_for(now_ts: int) -> str:
    return datetime.fromtimestamp(now_ts, ZoneInfo(config.TZ)).strftime("%d.%m")


# ---------- Tracker-HostAI (новая раскладка: дата × слот engnr A/B/C) ----------
_WD_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def tracker_date_label(now_ts: int) -> str:
    """'пн 10.08' — как в шапке столбца Date нового трекера."""
    d = datetime.fromtimestamp(now_ts, ZoneInfo(config.TZ))
    return f"{_WD_RU[d.weekday()]} {d.strftime('%d.%m')}"


def tracker_date_label_for(value: date) -> str:
    """Вид даты в трекере, совпадающий с существующей вкладкой HostAI."""
    return f"{_WD_RU[value.weekday()]} {value.strftime('%d.%m')}"


def tracker_locate(grid):
    """Шапка трекера: ячейки 'engnr A/B/C' + '<l>-status'. Слот пишется позиционно.
    -> (header_idx, {slot: value_col}, {slot: status_col}); (None,{},{}) если не нашли."""
    for ri, row in enumerate(grid):
        low = {(c or "").strip().lower(): ci for ci, c in enumerate(row)}
        val, st = {}, {}
        for slot in config.TRACKER_SLOTS:
            vc = low.get(f"engnr {slot.lower()}")
            if vc is None:
                continue
            sc = low.get(f"{slot.lower()}-status", vc + 1)  # статус справа от задачи
            val[slot] = vc
            st[slot] = sc
        if val:
            return ri, val, st
    return None, {}, {}


def _tracker_row(grid, header_idx, dd_mm):
    """Строка сегодняшней даты: колонка Date (0) содержит 'DD.MM'. -> row_idx | None."""
    for ri in range(header_idx + 1, len(grid)):
        cell = _cell(grid, ri, 0)
        if cell and dd_mm in cell:      # 'пн 10.08' содержит '10.08'
            return ri
    return None


def _tracker_rows(grid, header_idx, dd_mm):
    """Все строки даты. Дата может повторяться, когда у инженера больше одной задачи."""
    return [ri for ri in range(header_idx + 1, len(grid))
            if dd_mm in _cell(grid, ri, 0)]


def _date_from_dd_mm(dd_mm: str, now_ts: int) -> date:
    """Разрешает DD.MM в ближайшую к текущей календарную дату (корректно на границе года)."""
    m = re.fullmatch(r"\s*(\d{1,2})\.(\d{1,2})\s*", dd_mm or "")
    if not m:
        raise SheetError(f"неверная дата «{dd_mm}»")
    day, month = int(m.group(1)), int(m.group(2))
    now = datetime.fromtimestamp(now_ts, ZoneInfo(config.TZ)).date()
    candidates = []
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            pass
    if not candidates:
        raise SheetError(f"неверная дата «{dd_mm}»")
    return min(candidates, key=lambda value: abs((value - now).days))


def _date_from_label(label: str, now_ts: int):
    m = re.search(r"(?<!\d)(\d{1,2}\.\d{1,2})(?!\d)", label or "")
    if not m:
        return None
    try:
        return _date_from_dd_mm(m.group(1), now_ts)
    except SheetError:
        return None


def _write_tracker_date(svc, title: str, row_idx: int, value: date) -> None:
    """Пишет короткую метку вида «пн 24.08» в текстовую Date-колонку."""
    svc.spreadsheets().values().update(
        spreadsheetId=config.SHEET_ID,
        range=f"'{title}'!{a1(0, row_idx)}",
        valueInputOption="RAW",
        body={"values": [[tracker_date_label_for(value)]]},
    ).execute()


def tracker_ensure_tab(svc):
    """Создаёт вкладку TRACKER_TAB с шапкой, если её нет. Идемпотентно."""
    meta = svc.spreadsheets().get(spreadsheetId=config.SHEET_ID).execute()
    if any(sh["properties"]["title"] == config.TRACKER_TAB for sh in meta["sheets"]):
        return
    svc.spreadsheets().batchUpdate(
        spreadsheetId=config.SHEET_ID,
        body={"requests": [{"addSheet": {"properties": {"title": config.TRACKER_TAB}}}]},
    ).execute()
    header = ["Date"]
    for slot in config.TRACKER_SLOTS:
        header += [f"engnr {slot}", f"{slot.lower()}-status"]
    svc.spreadsheets().values().update(
        spreadsheetId=config.SHEET_ID, range=f"'{config.TRACKER_TAB}'!A1",
        valueInputOption="RAW", body={"values": [header]},
    ).execute()
    global _tab_title_cache
    _tab_title_cache = None
    # свежесозданную вкладку сразу оформляем (цвета/чипы/зебра). Не критично к успеху.
    try:
        format_tracker(svc, config.TRACKER_TAB)
    except Exception:  # noqa: BLE001
        log.exception("tracker_ensure_tab: оформление не удалось (вкладка создана)")


def tracker_write(slot: str, text: str, now_ts: int, date_str: str = None) -> dict:
    """Добавляет задачу в общий дневной список инженера в HostAI.

    Все задачи одного слота за дату лежат строками в одной ячейке, а соседний
    dropdown хранит один общий статус всего списка. Новая задача возвращает общий
    статус в todo. -> {row, status_col, line, title, date, label}.
    """
    svc = _build_service()
    tracker_ensure_tab(svc)
    title = current_tab_title()
    tracker_ensure_dates_through(now_ts, svc=svc, title=title)
    grid = _read_grid(svc, title)
    header_idx, valcols, statuscols = tracker_locate(grid)
    if header_idx is None or slot not in valcols:
        raise SheetError(f"слот «engnr {slot}» не найден в трекере (шапка не распознана)")

    dd_mm = date_str or date_str_for(now_ts)
    target_date = _date_from_dd_mm(dd_mm, now_ts)
    col = valcols[slot]
    status_col = statuscols[slot]
    row_idx = next(iter(_tracker_rows(grid, header_idx, dd_mm)), None)
    created_row = row_idx is None
    if row_idx is None:
        row_idx = len(grid)
        _write_tracker_date(svc, title, row_idx, target_date)

    existing = _cell(grid, row_idx, col)
    tasks = existing.split("\n") if existing else []
    new_value = "\n".join(tasks + [text])

    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=config.SHEET_ID,
        body={"valueInputOption": "USER_ENTERED", "data": [
            {"range": f"'{title}'!{a1(col, row_idx)}", "values": [[new_value]]},
            {"range": f"'{title}'!{a1(status_col, row_idx)}", "values": [["todo"]]},
        ]},
    ).execute()
    _set_status_dropdown(svc, title, row_idx, status_col)
    if created_row:
        # updateTable может применить формат DATE колонки к только что расширенному диапазону.
        _write_tracker_date(svc, title, row_idx, target_date)
    return {"row": row_idx, "status_col": status_col, "line": 0,
            "title": title, "date": dd_mm, "label": f"engnr {slot}"}


def tracker_read_today(now_ts: int) -> dict:
    """Задачи трекера за сегодня: {slot: [(label, text, status)]}. Формат как read_today_tasks."""
    svc = _build_service()
    title = current_tab_title()
    grid = _read_grid(svc, title)
    header_idx, valcols, statuscols = tracker_locate(grid)
    out = {s: [] for s in valcols}
    if header_idx is None:
        return out
    row_indexes = _tracker_rows(grid, header_idx, date_str_for(now_ts))
    if not row_indexes:
        return out
    for row_idx in row_indexes:
        for slot, vc in valcols.items():
            val = _cell(grid, row_idx, vc)
            if not val.strip():
                continue
            status = _common_status(_cell(grid, row_idx, statuscols[slot]))
            for tline in val.split("\n"):
                if not tline.strip():
                    continue
                out[slot].append((f"engnr {slot}", tline.strip(), status))
    return out


# ---------- оформление трекера (цвета/чипы/зебра) ----------
# Значения статус-чипов совпадают с кнопками бота под /new.
TRACKER_STATUS_VALUES = ["todo", "later", "done", "skipped"]


def _common_status(raw: str) -> str:
    """Схлопывает легаси-многострочный статус в один безопасный общий статус."""
    values = []
    for value in (raw or "").split("\n"):
        value = value.strip().lower()
        if value:
            values.append("skipped" if value == "skip" else value)
    if not values:
        return ""
    if len(set(values)) == 1:
        return values[0] if values[0] in TRACKER_STATUS_VALUES else "todo"
    if "todo" in values:
        return "todo"
    if "later" in values:
        return "later"
    return "todo"

# палитра под скрин: тёмно-зелёная шапка, белый текст, красные понедельники, лёгкая зебра
_C_HEADER_BG = {"red": 0.235, "green": 0.443, "blue": 0.337}   # ~#3a7156
_C_WHITE = {"red": 1, "green": 1, "blue": 1}
_C_RED = {"red": 0.80, "green": 0.00, "blue": 0.00}
_C_BAND = {"red": 0.945, "green": 0.965, "blue": 0.953}         # ~#f1f6f3
# цвета статусов (фон + текст): done=зелёный, later=янтарь, skipped=красный
_C_ST_GRN = {"red": 0.85, "green": 0.94, "blue": 0.83}
_C_ST_GRN_T = {"red": 0.11, "green": 0.37, "blue": 0.13}
_C_ST_AMB = {"red": 1.00, "green": 0.95, "blue": 0.80}
_C_ST_AMB_T = {"red": 0.60, "green": 0.40, "blue": 0.00}
_C_ST_RED = {"red": 0.98, "green": 0.85, "blue": 0.85}
_C_ST_RED_T = {"red": 0.70, "green": 0.10, "blue": 0.10}


def _tab_meta(svc, title, sheet_id=None):
    """(sheetId, rowCount, [conditional_format_indexes], [banded_range_ids]) для вкладки title."""
    sid = sheet_id or config.SHEET_ID
    meta = svc.spreadsheets().get(
        spreadsheetId=sid,
        fields="sheets(properties(sheetId,title,gridProperties),conditionalFormats,bandedRanges)",
    ).execute()
    for sh in meta.get("sheets", []):
        if sh["properties"]["title"] == title:
            gp = sh["properties"].get("gridProperties", {})
            n_cf = len(sh.get("conditionalFormats", []) or [])
            band_ids = [b["bandedRangeId"] for b in (sh.get("bandedRanges", []) or [])]
            return (sh["properties"]["sheetId"], gp.get("rowCount", 1000), n_cf, band_ids)
    raise SheetError(f"вкладка «{title}» не найдена")


def format_tracker(svc, title=None, sheet_id=None) -> dict:
    """Оформляет вкладку трекера как на макете. Идемпотентно: удаляет свои прежние
    условные форматы/зебру и накладывает заново. -> сводка что применено."""
    sid = sheet_id or config.SHEET_ID
    title = title or current_tab_title()
    grid = _read_grid_of(svc, sid, title)
    header_idx, valcols, statuscols = tracker_locate(grid)
    if header_idx is None:
        raise SheetError("шапка трекера не распознана (нет 'engnr A/B/C')")

    sheet_gid, row_count, n_cf, band_ids = _tab_meta(svc, title, sid)
    used_cols = max(list(valcols.values()) + list(statuscols.values())) + 1
    end_row = max(row_count, 1000)

    reqs = []
    # 0) чистим прежнее оформление, чтобы повторный прогон не плодил дубли
    for bid in band_ids:
        reqs.append({"deleteBanding": {"bandedRangeId": bid}})
    for i in range(n_cf - 1, -1, -1):
        reqs.append({"deleteConditionalFormatRule": {"sheetId": sheet_gid, "index": i}})

    # 1) закрепить шапку и колонку Date
    reqs.append({"updateSheetProperties": {
        "properties": {"sheetId": sheet_gid,
                       "gridProperties": {"frozenRowCount": header_idx + 1, "frozenColumnCount": 1}},
        "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount"}})

    # 2) зелёная шапка, белый жирный текст
    reqs.append({"repeatCell": {
        "range": {"sheetId": sheet_gid, "startRowIndex": header_idx, "endRowIndex": header_idx + 1,
                  "startColumnIndex": 0, "endColumnIndex": used_cols},
        "cell": {"userEnteredFormat": {
            "backgroundColor": _C_HEADER_BG,
            "textFormat": {"foregroundColor": _C_WHITE, "bold": True},
            "verticalAlignment": "MIDDLE", "horizontalAlignment": "LEFT", "wrapStrategy": "WRAP"}},
        "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment,"
                  "horizontalAlignment,wrapStrategy)"}})

    # 3) зебра по строкам данных
    reqs.append({"addBanding": {"bandedRange": {
        "range": {"sheetId": sheet_gid, "startRowIndex": header_idx + 1, "endRowIndex": end_row,
                  "startColumnIndex": 0, "endColumnIndex": used_cols},
        "rowProperties": {"firstBandColor": _C_WHITE, "secondBandColor": _C_BAND}}}})

    # Списки задач в одной ячейке должны быть видны целиком.
    for vc in sorted(valcols.values()):
        reqs.append({"repeatCell": {
            "range": {"sheetId": sheet_gid, "startRowIndex": header_idx + 1,
                      "endRowIndex": end_row, "startColumnIndex": vc,
                      "endColumnIndex": vc + 1},
            "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP",
                                              "verticalAlignment": "TOP"}},
            "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}})

    # 4) красные понедельники в колонке Date (текст начинается с «пн»)
    reqs.append({"addConditionalFormatRule": {"index": 0, "rule": {
        "ranges": [{"sheetId": sheet_gid, "startRowIndex": header_idx + 1, "endRowIndex": end_row,
                    "startColumnIndex": 0, "endColumnIndex": 1}],
        "booleanRule": {
            "condition": {"type": "TEXT_STARTS_WITH", "values": [{"userEnteredValue": "пн"}]},
            "format": {"textFormat": {"foregroundColor": _C_RED, "bold": True}}}}}})

    # 5) цвет статусов условным форматированием.
    status_ranges = [{"sheetId": sheet_gid, "startRowIndex": header_idx + 1, "endRowIndex": end_row,
                      "startColumnIndex": sc, "endColumnIndex": sc + 1}
                     for sc in sorted(statuscols.values())]
    for i, (needle, bg, fg) in enumerate([
            ("skipped", _C_ST_RED, _C_ST_RED_T),
            ("later", _C_ST_AMB, _C_ST_AMB_T),
            ("done", _C_ST_GRN, _C_ST_GRN_T)], start=1):
        reqs.append({"addConditionalFormatRule": {"index": i, "rule": {
            "ranges": status_ranges,
            "booleanRule": {
                "condition": {"type": "TEXT_CONTAINS", "values": [{"userEnteredValue": needle}]},
                "format": {"backgroundColor": bg,
                           "textFormat": {"foregroundColor": fg, "bold": True}}}}}})

    # 6) настоящий dropdown в каждой статус-ячейке
    for sc in sorted(statuscols.values()):
        reqs.append({"setDataValidation": {
            "range": {"sheetId": sheet_gid, "startRowIndex": header_idx + 1, "endRowIndex": end_row,
                      "startColumnIndex": sc, "endColumnIndex": sc + 1},
            "rule": _status_validation_rule()}})

    # 7) ширины колонок: Date узкая, engnr широкие, статусы средние
    def _width(start, end, px):
        return {"updateDimensionProperties": {
            "range": {"sheetId": sheet_gid, "dimension": "COLUMNS", "startIndex": start, "endIndex": end},
            "properties": {"pixelSize": px}, "fields": "pixelSize"}}
    reqs.append(_width(0, 1, 110))
    for c in valcols.values():
        reqs.append(_width(c, c + 1, 320))
    for c in statuscols.values():
        reqs.append(_width(c, c + 1, 96))

    svc.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": reqs}).execute()
    log.info("format_tracker: оформлена вкладка «%s» (%s колонок, %s статус-колонок)",
             title, used_cols, len(statuscols))
    return {"title": title, "cols": used_cols, "status_cols": len(statuscols),
            "cleared_cf": n_cf, "cleared_bands": len(band_ids)}


def _read_grid_of(svc, sheet_id, title):
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{title}'!A1:Z2000").execute()
    return resp.get("values", [])


def _status_condition() -> dict:
    return {"type": "ONE_OF_LIST", "values": [
        {"userEnteredValue": value} for value in TRACKER_STATUS_VALUES
    ]}


def _status_validation_rule() -> dict:
    return {
        "condition": _status_condition(),
        "strict": True,
        "showCustomUi": True,
    }


def _native_tracker_table(svc, title: str):
    """(sheet properties, table) для нативной таблицы HostAI; table=None для plain grid."""
    meta = svc.spreadsheets().get(
        spreadsheetId=config.SHEET_ID,
        fields="sheets(properties(sheetId,title,gridProperties),tables)",
    ).execute()
    for sh in meta.get("sheets", []):
        if sh["properties"]["title"] == title:
            tables = sh.get("tables", []) or []
            return sh["properties"], (tables[0] if tables else None)
    raise SheetError(f"вкладка «{title}» не найдена")


def _ensure_status_structure(svc, title: str, status_cols, min_end_row: int) -> None:
    """Расширяет нативную таблицу и её dropdown-колонки либо ставит grid validation."""
    props, table = _native_tracker_table(svc, title)
    if table:
        table_range = dict(table["range"])
        table_range["endRowIndex"] = max(table_range.get("endRowIndex", 0), min_end_row)
        start_col = table_range.get("startColumnIndex", 0)
        relative_statuses = {col - start_col for col in status_cols}
        columns = []
        for pos, raw in enumerate(table.get("columnProperties", []) or []):
            col = dict(raw)
            relative_idx = int(col.get("columnIndex", pos))
            if relative_idx in relative_statuses:
                col["columnType"] = "DROPDOWN"
                col["dataValidationRule"] = {"condition": _status_condition()}
            columns.append(col)
        svc.spreadsheets().batchUpdate(
            spreadsheetId=config.SHEET_ID,
            body={"requests": [{"updateTable": {
                "table": {"tableId": table["tableId"], "range": table_range,
                          "columnProperties": columns},
                "fields": "range,columnProperties",
            }}]},
        ).execute()
        return

    row_count = props.get("gridProperties", {}).get("rowCount", 1000)
    requests = [{"setDataValidation": {
        "range": {"sheetId": props["sheetId"], "startRowIndex": 1,
                  "endRowIndex": row_count, "startColumnIndex": col,
                  "endColumnIndex": col + 1},
        "rule": _status_validation_rule(),
    }} for col in sorted(status_cols)]
    if requests:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=config.SHEET_ID, body={"requests": requests}).execute()


def _set_status_dropdown(svc, title: str, row_idx: int, status_col: int) -> None:
    _ensure_status_structure(svc, title, [status_col], row_idx + 1)


def tracker_ensure_dates_through(now_ts: int, svc=None, title: str = None) -> dict:
    """Дописать календарные дни от последней даты до недели вперёд.

    Вызывается из плановой Lambda и перед записью задачи, поэтому HostAI не зависит
    от ручного продления дат.
    """
    svc = svc or _build_service()
    tracker_ensure_tab(svc)
    title = title or current_tab_title()
    grid = _read_grid(svc, title)
    header_idx, _, statuscols = tracker_locate(grid)
    if header_idx is None:
        raise SheetError("шапка трекера не распознана (нет 'engnr A/B/C')")

    today = datetime.fromtimestamp(now_ts, ZoneInfo(config.TZ)).date()
    horizon = today + timedelta(days=7)
    present = set()
    dated = []
    for ri in range(header_idx + 1, len(grid)):
        parsed = _date_from_label(_cell(grid, ri, 0), now_ts)
        if parsed:
            present.add(parsed)
            if parsed <= today:
                dated.append(parsed)

    start = (max(dated) + timedelta(days=1)) if dated else today
    added = []
    added_rows = []
    cursor = start
    while cursor <= horizon:
        if cursor not in present:
            row_idx = len(grid)
            _write_tracker_date(svc, title, row_idx, cursor)
            grid.append([tracker_date_label_for(cursor)])
            added.append(cursor.strftime("%d.%m"))
            added_rows.append((row_idx, cursor))
        cursor += timedelta(days=1)

    # Самовосстановление диапазона нативной таблицы и её dropdown-колонок.
    _ensure_status_structure(svc, title, statuscols.values(), len(grid))
    for row_idx, value in added_rows:
        _write_tracker_date(svc, title, row_idx, value)
    return {"title": title, "added": added}


def _week_visibility_ranges(grid, header_idx: int, now_ts: int):
    """Смежные диапазоны строк: (start, end, hidden), где end не включён."""
    today = datetime.fromtimestamp(now_ts, ZoneInfo(config.TZ)).date()
    week_start = today - timedelta(days=today.weekday())
    rows = []
    for row_idx in range(header_idx + 1, len(grid)):
        value = _date_from_label(_cell(grid, row_idx, 0), now_ts)
        if value:
            rows.append((row_idx, value < week_start))
    ranges = []
    for row_idx, hidden in rows:
        if ranges and ranges[-1][1] == row_idx and ranges[-1][2] == hidden:
            ranges[-1] = (ranges[-1][0], row_idx + 1, hidden)
        else:
            ranges.append((row_idx, row_idx + 1, hidden))
    return ranges


def tracker_hide_old_weeks(now_ts: int, svc=None, title: str = None) -> dict:
    """Скрывает завершённые недели и раскрывает текущую/будущие строки HostAI."""
    svc = svc or _build_service()
    tracker_ensure_tab(svc)
    title = title or current_tab_title()
    grid = _read_grid(svc, title)
    header_idx, _, _ = tracker_locate(grid)
    if header_idx is None:
        raise SheetError("шапка трекера не распознана (нет 'engnr A/B/C')")
    props, _ = _native_tracker_table(svc, title)
    ranges = _week_visibility_ranges(grid, header_idx, now_ts)
    requests = [{"updateDimensionProperties": {
        "range": {"sheetId": props["sheetId"], "dimension": "ROWS",
                  "startIndex": start, "endIndex": end},
        "properties": {"hiddenByUser": hidden},
        "fields": "hiddenByUser",
    }} for start, end, hidden in ranges]
    if requests:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=config.SHEET_ID, body={"requests": requests}).execute()
    return {
        "title": title,
        "hidden_rows": sum(end - start for start, end, hidden in ranges if hidden),
        "visible_rows": sum(end - start for start, end, hidden in ranges if not hidden),
    }


# ---------- сетевая часть ----------
def is_configured() -> bool:
    if not config.SHEET_ID:
        return False
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
    _service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return _service


def current_tab_title() -> str:
    global _tab_title_cache
    if config.SHEET_TAB:
        return config.SHEET_TAB
    if _tab_title_cache:
        return _tab_title_cache
    svc = _build_service()
    meta = svc.spreadsheets().get(spreadsheetId=config.SHEET_ID).execute()
    _tab_title_cache = meta["sheets"][0]["properties"]["title"]
    return _tab_title_cache


def _read_grid(svc, title):
    rng = f"'{title}'!A1:Z2000"
    resp = svc.spreadsheets().values().get(spreadsheetId=config.SHEET_ID, range=rng).execute()
    return resp.get("values", [])


def _append_date_block(svc, title, grid, date_str):
    """Добавляет блок дня В КОНЕЦ листа. values.append с якорем A1 ненадёжен
    (на пустой верхней строке вставляет блок наверх, выше шапки) - поэтому пишем
    явным update в первую свободную строку после текущих данных."""
    start = len(grid) + 1  # 1-based: следующая пустая строка
    rows = []
    for label in config.SHEET_CATEGORIES.values():
        rows.append([label, date_str] if label == "DEEP FOCUS" else [label, ""])
    end = start + len(rows) - 1
    svc.spreadsheets().values().update(
        spreadsheetId=config.SHEET_ID, range=f"'{title}'!A{start}:B{end}",
        valueInputOption="RAW",  # дату пишем как текст, без авто-парсинга в date/number
        body={"values": rows},
    ).execute()
    return len(grid)  # 0-based индекс первой дописанной строки (строка DEEP FOCUS)


def write_entry(person: str, cat_abbr: str, text: str, now_ts: int, date_str: str = None) -> dict:
    """
    Дописывает задачу в ячейку [строка категории за дату] x [колонка человека].
    date_str='DD.MM' задаёт день явно; None -> сегодня. Повторный вызов за день —
    новой строкой в ту же ячейку. -> {row, status_col, line, title, date, label}.
    """
    label = config.SHEET_CATEGORIES[cat_abbr]
    svc = _build_service()
    title = current_tab_title()
    grid = _read_grid(svc, title)

    header_idx, valcols, statuscols = locate_columns(grid)
    if header_idx is None or person not in valcols:
        raise SheetError(f"колонка «{person}» не найдена в шите (шапка не распознана)")

    date_str = date_str or date_str_for(now_ts)
    row_idx = locate_row(grid, date_str, label, header_idx)
    if row_idx is None:
        # Блока дня нет -> дописываем в конец и вычисляем строку НАПРЯМУЮ из позиции
        # блока. Повторное чтение после append может не увидеть новые строки сразу
        # (read-after-write лаг), из-за чего первая команда на новую дату падала.
        block_start = _append_date_block(svc, title, grid, date_str)
        labels = list(config.SHEET_CATEGORIES.values())
        row_idx = block_start + labels.index(label)  # header не двигался, valcols валидны

    col = valcols[person]
    existing = _cell(grid, row_idx, col)
    existing_lines = existing.split("\n") if existing.strip() else []
    line_idx = len(existing_lines)                  # индекс строки новой задачи в ячейке
    new_val = "\n".join(existing_lines + [text])
    svc.spreadsheets().values().update(
        spreadsheetId=config.SHEET_ID, range=f"'{title}'!{a1(col, row_idx)}",
        valueInputOption="USER_ENTERED", body={"values": [[new_val]]},
    ).execute()
    return {"row": row_idx, "status_col": statuscols[person], "line": line_idx,
            "title": title, "date": date_str, "label": label}


def read_today_tasks(now_ts: int) -> dict:
    """Задачи за сегодня по каждому человеку: {person: [(категория, текст, статус)]}.
    Статусы построчно выровнены с задачами."""
    svc = _build_service()
    title = current_tab_title()
    grid = _read_grid(svc, title)
    header_idx, valcols, statuscols = locate_columns(grid)
    out = {p: [] for p in valcols}
    if header_idx is None:
        return out
    date_str = date_str_for(now_ts)
    block_start = None
    for ri in range(header_idx + 1, len(grid)):
        if _cell(grid, ri, 1) == date_str:
            block_start = ri
            break
    if block_start is None:
        return out
    block_end = len(grid)
    for ri in range(block_start + 1, len(grid)):
        if _cell(grid, ri, 1):
            block_end = ri
            break
    for ri in range(block_start, block_end):
        label = _cell(grid, ri, 0)
        if not label:
            continue
        for person, vc in valcols.items():
            val = _cell(grid, ri, vc)
            if not val.strip():
                continue
            slines = _cell(grid, ri, statuscols[person]).split("\n")
            for i, tline in enumerate(val.split("\n")):
                if not tline.strip():
                    continue
                st = slines[i].strip() if i < len(slines) else ""
                out[person].append((label, tline.strip(), st))
    return out


def tracker_today_tasks(slot: str, now_ts: int, title: str = None) -> list:
    """Задачи слота на сегодня С КООРДИНАТАМИ (для кнопок меню):
    [{line, text, status, row, status_col}]. Пусто, если задач/строки нет."""
    svc = _build_service()
    title = title or current_tab_title()
    grid = _read_grid(svc, title)
    header_idx, valcols, statuscols = tracker_locate(grid)
    if header_idx is None or slot not in valcols:
        return []
    row_indexes = _tracker_rows(grid, header_idx, date_str_for(now_ts))
    if not row_indexes:
        return []
    vc, sc = valcols[slot], statuscols[slot]
    out = []
    for row_idx in row_indexes:
        val = _cell(grid, row_idx, vc)
        status = _common_status(_cell(grid, row_idx, sc))
        for i, tline in enumerate(val.split("\n")):
            if not tline.strip():
                continue
            out.append({"line": i, "text": tline.strip(),
                        "status": status,
                        "row": row_idx, "status_col": sc})
    return out


def tracker_edit_line(row_idx: int, value_col: int, line: int, new_text: str,
                      title: str = None) -> dict:
    """Меняет ТЕКСТ конкретной задачи (строка line в ячейке текста), не трогая соседние."""
    svc = _build_service()
    title = title or current_tab_title()
    cell = f"'{title}'!{a1(value_col, row_idx)}"
    cur = svc.spreadsheets().values().get(spreadsheetId=config.SHEET_ID, range=cell).execute().get("values")
    lines = cur[0][0].split("\n") if (cur and cur[0] and cur[0][0]) else []
    while len(lines) <= line:
        lines.append("")
    lines[line] = new_text
    svc.spreadsheets().values().update(
        spreadsheetId=config.SHEET_ID, range=cell,
        valueInputOption="USER_ENTERED", body={"values": [["\n".join(lines)]]},
    ).execute()
    return {"title": title}


def tracker_delete_line(row_idx: int, value_col: int, status_col: int, line: int,
                        title: str = None) -> dict:
    """Удаляет одну задачу из дневного списка, сохраняя его общий статус.

    Если удалена последняя задача, очищается и общий статус.
    """
    svc = _build_service()
    title = title or current_tab_title()
    value_cell = f"'{title}'!{a1(value_col, row_idx)}"
    cur = svc.spreadsheets().values().get(
        spreadsheetId=config.SHEET_ID, range=value_cell).execute().get("values")
    lines = cur[0][0].split("\n") if (cur and cur[0] and cur[0][0]) else []
    if line < len(lines):
        lines.pop(line)
        remaining = "\n".join(value for value in lines if value.strip())
        updates = [{"range": value_cell, "values": [[remaining]]}]
        if not remaining:
            updates.append({"range": f"'{title}'!{a1(status_col, row_idx)}", "values": [[""]]})
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=config.SHEET_ID,
            body={"valueInputOption": "USER_ENTERED", "data": updates},
        ).execute()
    return {"title": title}


def write_status(title: str, row_idx: int, status_col: int, status: str, line: int = 0) -> None:
    """Пишет единый статус дневного списка задач; line оставлен для callback-совместимости."""
    svc = _build_service()
    scell = f"'{title}'!{a1(status_col, row_idx)}"
    svc.spreadsheets().values().update(
        spreadsheetId=config.SHEET_ID, range=scell,
        valueInputOption="USER_ENTERED", body={"values": [[status]]},
    ).execute()
    _set_status_dropdown(svc, title, row_idx, status_col)


# ---------- управление людьми (колонками) ----------
def _tab_props(svc):
    """(sheetId, title) для нужной вкладки (SHEET_TAB или первой)."""
    meta = svc.spreadsheets().get(spreadsheetId=config.SHEET_ID).execute()
    tabs = meta["sheets"]
    if config.SHEET_TAB:
        for sh in tabs:
            if sh["properties"]["title"] == config.SHEET_TAB:
                return sh["properties"]["sheetId"], config.SHEET_TAB
    p = tabs[0]["properties"]
    return p["sheetId"], p["title"]


def get_people() -> list:
    """Список людей из шапки листа (динамически)."""
    svc = _build_service()
    _, val, _ = locate_columns(_read_grid(svc, current_tab_title()))
    return list(val.keys())


def add_person(name: str) -> bool:
    """Добавляет пару колонок «Имя» / «Имя-status» справа. False = уже есть."""
    name = name.strip()
    svc = _build_service()
    title = current_tab_title()
    header_idx, val, st = locate_columns(_read_grid(svc, title))
    if header_idx is None:
        raise SheetError("шапка листа не распознана")
    if any(p.lower() == name.lower() for p in val):
        return False
    last_col = max(list(val.values()) + list(st.values())) if val else 1
    vcol, scol = last_col + 1, last_col + 2
    svc.spreadsheets().values().update(
        spreadsheetId=config.SHEET_ID, range=f"'{title}'!{a1(vcol, header_idx)}:{a1(scol, header_idx)}",
        valueInputOption="RAW", body={"values": [[name, f"{name}-status"]]},
    ).execute()
    return True


def remove_person(name: str) -> bool:
    """Удаляет колонки человека (значение + статус) со сдвигом остальных влево.
    False = человек не найден."""
    svc = _build_service()
    sid, title = _tab_props(svc)
    _, val, st = locate_columns(_read_grid(svc, title))
    key = next((p for p in val if p.lower() == name.strip().lower()), None)
    if key is None:
        return False
    # удаляем справа налево, чтобы индексы не съезжали
    cols = sorted({val[key], st[key]}, reverse=True)
    reqs = [{"deleteDimension": {"range": {
        "sheetId": sid, "dimension": "COLUMNS", "startIndex": c, "endIndex": c + 1}}}
        for c in cols]
    svc.spreadsheets().batchUpdate(spreadsheetId=config.SHEET_ID, body={"requests": reqs}).execute()
    return True
