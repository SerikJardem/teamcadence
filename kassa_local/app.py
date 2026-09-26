"""Local gastroweek kassa — browser UI, CSV storage, no Telegram."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import store

CSV_PATH = Path(os.getenv("KASSA_CSV", store.default_csv_path()))
ITEMS = store.items_from_env()

app = FastAPI(title="Gastroweek kassa", docs_url=None, redoc_url=None)
static_dir = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/", response_class=HTMLResponse)
async def index(_: Request) -> HTMLResponse:
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/api/state")
async def api_state():
    grid = store.load_grid(CSV_PATH, ITEMS)
    return {
        "items": store.totals(grid, ITEMS),
        "csv": str(CSV_PATH),
        "grand_total": sum(row["total"] for row in store.totals(grid, ITEMS)),
    }


@app.post("/api/sale/{item}")
async def api_sale(item: str):
    if item not in ITEMS:
        raise HTTPException(status_code=404, detail="unknown item")
    try:
        result = store.record_sale(CSV_PATH, item, ITEMS)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    grid = store.load_grid(CSV_PATH, ITEMS)
    return {
        **result,
        "items": store.totals(grid, ITEMS),
        "grand_total": sum(row["total"] for row in store.totals(grid, ITEMS)),
    }


@app.get("/api/csv")
async def download_csv():
    from fastapi.responses import FileResponse

    if not CSV_PATH.exists():
        store.save_grid(CSV_PATH, store._ensure_grid(ITEMS))
    return FileResponse(
        CSV_PATH,
        media_type="text/csv",
        filename="gastroweek-sales.csv",
    )


@app.post("/api/reset")
async def api_reset():
    """Wipe sale marks; keep item catalog rows."""
    grid = store._ensure_grid(ITEMS)
    store.save_grid(CSV_PATH, grid)
    return JSONResponse({"ok": True, "items": store.totals(grid, ITEMS), "grand_total": 0})
