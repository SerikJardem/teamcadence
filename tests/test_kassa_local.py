from pathlib import Path

from kassa_local import store


def test_record_sale_appends_ones_like_sheet(tmp_path: Path):
    csv_path = tmp_path / "sales.csv"
    a = store.record_sale(csv_path, "Фри")
    b = store.record_sale(csv_path, "Фри")
    c = store.record_sale(csv_path, "Су-вид")

    assert a["total"] == 1
    assert b["total"] == 2
    assert c["total"] == 1

    grid = store.load_grid(csv_path)
    fri_row = store.find_item_row(grid, "Фри")
    assert fri_row is not None
    assert grid[fri_row][2:] == ["1", "1"]

    totals = {row["item"]: row["total"] for row in store.totals(grid)}
    assert totals["Фри"] == 2
    assert totals["Су-вид"] == 1
    assert totals["Гриль"] == 0


def test_load_preserves_existing_marks(tmp_path: Path):
    csv_path = tmp_path / "sales.csv"
    csv_path.write_text(
        ",,\n"
        ",,\n"
        ",Су-вид,1,1\n"
        ",Гриль,1\n"
        ",Говядина,1\n"
        ",Фри,1,1\n"
        ",Улун,1\n"
        ",Яблоко,1\n",
        encoding="utf-8",
    )
    grid = store.load_grid(csv_path)
    assert store.sale_total(grid, store.find_item_row(grid, "Су-вид")) == 2
    assert store.next_sale_col(grid, store.find_item_row(grid, "Гриль")) == 3
