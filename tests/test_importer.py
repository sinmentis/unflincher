import openpyxl
import pytest

from diary.db import get_connection, init_schema
from diary.importer import MissingColumnsError, import_excel


def _make_workbook(path, header, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "日记"
    ws.append(header)
    for row in rows:
        ws.append(row)
    wb.save(path)


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.db"))
    init_schema(c)
    yield c
    c.close()


def test_import_valid_export(tmp_path, conn):
    xlsx_path = tmp_path / "export.xlsx"
    _make_workbook(
        xlsx_path,
        ["标题", "链接", "创建时间", "修改时间", "内容"],
        [
            [
                "2026", "https://www.douban.com/note/1/",
                "2026-01-08 06:46:57", "2026-01-08 06:46:58",
                '<p data-page="0">不知不觉已经2026年了</p><p></p>',
            ],
            [
                "旧的一篇", "https://www.douban.com/note/2/",
                "2014-10-01 20:57:17", "2014-10-01 20:57:17",
                "<p>很久以前写的</p>",
            ],
        ],
    )

    count = import_excel(str(xlsx_path), conn)

    assert count == 2
    rows = conn.execute("SELECT * FROM diary_entry ORDER BY entry_date").fetchall()
    assert len(rows) == 2
    assert rows[0]["title"] == "旧的一篇"
    assert rows[0]["source"] == "import"
    assert rows[0]["douban_url"] == "https://www.douban.com/note/2/"
    assert "很久以前写的" in rows[0]["content_text"]
    # empty <p></p> collapsed away, data-page stripped, real paragraph kept
    assert "<p></p>" not in rows[1]["content_html"]
    assert "data-page" not in rows[1]["content_html"]
    assert "不知不觉已经2026年了" in rows[1]["content_text"]


def test_import_missing_required_column_fails_loudly(tmp_path, conn):
    xlsx_path = tmp_path / "bad_export.xlsx"
    _make_workbook(
        xlsx_path,
        ["标题", "链接", "内容"],  # missing 创建时间/修改时间
        [["t", "https://x", "<p>x</p>"]],
    )

    with pytest.raises(MissingColumnsError) as exc_info:
        import_excel(str(xlsx_path), conn)

    assert set(exc_info.value.missing) == {"创建时间", "修改时间"}
    # nothing partially imported
    assert conn.execute("SELECT COUNT(*) AS n FROM diary_entry").fetchone()["n"] == 0
